"""
A minimal SMTP server (RFC 5321), written directly on TCP sockets.

SMTP is a *push* protocol: the client opens a connection and pushes a message
at the server. The conversation is a strict state machine -- each command is
only legal in certain states, and the server replies to every one of them with
a three-digit code:

    2xx  the command succeeded
    3xx  the server wants more input (only 354, "go ahead and send the body")
    4xx  temporary failure, try again later
    5xx  permanent failure, do not retry

A complete transaction:

    S: 220 localhost Simple SMTP Service Ready
    C: EHLO client.local
    S: 250-localhost Hello client.local
    S: 250 HELP
    C: MAIL FROM:<aryan@localhost>          <- envelope sender
    S: 250 2.1.0 Sender OK
    C: RCPT TO:<prof@localhost>             <- envelope recipient (repeatable)
    S: 250 2.1.5 Recipient OK
    C: DATA
    S: 354 End data with <CRLF>.<CRLF>
    C: ...headers, blank line, body...
    C: .
    S: 250 2.0.0 OK: queued as 1756...
    C: QUIT
    S: 221 2.0.0 localhost closing connection

The addresses in MAIL FROM and RCPT TO are the *envelope*. They are what the
server actually routes on, and they are completely separate from the From:
and To: headers inside the message -- the server never even reads those.

Run directly:   python3 smtp_server.py [--port 1025] [--require-auth]
"""

import argparse
import base64
import re
import socket
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from common import wire
from common.mailstore import MailStore, default_store
from common.mime import rfc5322_date
from common.trace import Tracer
from common.users import UserDirectory, mailbox_of

# Conversation states. The server refuses commands issued out of order with a
# 503 reply, which is what makes this a state machine rather than a switch.
STATE_INIT = "INIT"          # connected, no HELO/EHLO yet
STATE_GREETED = "GREETED"    # identified, ready for a transaction
STATE_MAIL = "MAIL"          # MAIL FROM accepted, awaiting RCPT
STATE_RCPT = "RCPT"          # at least one RCPT accepted, DATA allowed

# MAIL FROM:<a@b> / RCPT TO:<a@b>, tolerating spaces and trailing parameters
# such as 'SIZE=1234' that ESMTP clients append.
_MAIL_RE = re.compile(r"^MAIL\s+FROM:\s*<?([^>\s]*)>?(.*)$", re.IGNORECASE)
_RCPT_RE = re.compile(r"^RCPT\s+TO:\s*<?([^>\s]*)>?(.*)$", re.IGNORECASE)


class SMTPSession:
    """Handles one client connection from 220 greeting to 221 goodbye."""

    def __init__(self, conn, addr, server, session_id):
        self.conn = conn
        self.addr = addr
        self.server = server
        self.tracer = Tracer(f"smtp#{session_id}",
                             to_console=server.verbose,
                             to_file=server.log_to_file)
        self.reader = wire.LineReader(conn, timeout=config.SOCKET_TIMEOUT)

        self.state = STATE_INIT
        self.client_name = ""
        self.authenticated_as = None
        self.sender = None
        self.recipients = []

    # -- I/O helpers -------------------------------------------------------- #

    def reply(self, text):
        """Send one response line and record it in the trace."""
        self.tracer.server(text)
        wire.send_line(self.conn, text)

    def reply_multi(self, lines):
        """Send a multi-line reply.

        Continuation lines use 'code-text' and only the final line uses
        'code text'. That hyphen is how the client knows more is coming.
        """
        code = lines[0][:3]
        for line in lines[:-1]:
            text = f"{code}-{line[4:] if line[:3] == code else line}"
            self.tracer.server(text)
            wire.send_line(self.conn, text)
        last = lines[-1]
        text = last if last[:3] == code else f"{code} {last}"
        self.tracer.server(text)
        wire.send_line(self.conn, text)

    # -- main loop ---------------------------------------------------------- #

    def run(self):
        self.tracer.info(f"connection from {self.addr[0]}:{self.addr[1]}")
        self.reply(f"220 {config.SERVER_DOMAIN} Simple SMTP Service Ready "
                   f"(CN Project)")
        try:
            while True:
                try:
                    line = self.reader.read_line()
                except wire.LineTooLong:
                    self.reply("500 5.5.6 Line too long")
                    continue
                self.tracer.client(line)
                if not self.dispatch(line):
                    break
        except wire.ConnectionClosed as exc:
            self.tracer.info(f"session ended: {exc}")
        except Exception as exc:                        # noqa: BLE001
            self.tracer.error(f"unexpected error: {exc}")
        finally:
            try:
                self.conn.close()
            except OSError:
                pass
            self.tracer.info("connection closed")

    def dispatch(self, line):
        """Route one command. Returns False when the session should end."""
        if not line.strip():
            self.reply("500 5.5.2 Error: bad syntax")
            return True

        verb = line.split()[0].upper()
        handlers = {
            "HELO": self.cmd_helo,
            "EHLO": self.cmd_ehlo,
            "AUTH": self.cmd_auth,
            "MAIL": self.cmd_mail,
            "RCPT": self.cmd_rcpt,
            "DATA": self.cmd_data,
            "RSET": self.cmd_rset,
            "NOOP": self.cmd_noop,
            "VRFY": self.cmd_vrfy,
            "HELP": self.cmd_help,
            "QUIT": self.cmd_quit,
        }
        handler = handlers.get(verb)
        if handler is None:
            self.reply(f'500 5.5.1 Command "{verb}" not recognised')
            return True
        return handler(line)

    # -- identification ----------------------------------------------------- #

    def cmd_helo(self, line):
        parts = line.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            self.reply("501 5.5.4 Syntax: HELO hostname")
            return True
        self.client_name = parts[1].strip()
        self.reset_transaction()
        self.state = STATE_GREETED
        self.reply(f"250 {config.SERVER_DOMAIN} Hello {self.client_name} "
                   f"[{self.addr[0]}]")
        return True

    def cmd_ehlo(self, line):
        """The extended greeting: the reply advertises what this server can do."""
        parts = line.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            self.reply("501 5.5.4 Syntax: EHLO hostname")
            return True
        self.client_name = parts[1].strip()
        self.reset_transaction()
        self.state = STATE_GREETED
        self.reply_multi([
            f"250 {config.SERVER_DOMAIN} Hello {self.client_name} [{self.addr[0]}]",
            f"250 SIZE {config.MAX_MESSAGE_BYTES}",
            "250 8BITMIME",
            "250 AUTH LOGIN PLAIN",
            "250 HELP",
        ])
        return True

    # -- authentication ----------------------------------------------------- #

    def _read_b64_response(self):
        """Read one line of the AUTH challenge/response exchange."""
        line = self.reader.read_line()
        self.tracer.client(line)
        if line.strip() == "*":
            return None                      # client cancelled the exchange
        try:
            return base64.b64decode(line.strip(), validate=True).decode(
                "utf-8", errors="replace")
        except (ValueError, TypeError):
            return None

    def cmd_auth(self, line):
        """SMTP AUTH, LOGIN and PLAIN mechanisms (RFC 4954).

        LOGIN is a two-step challenge: the server sends base64('Username:'),
        the client answers with the base64 username, and the same again for
        the password. PLAIN sends '\\0user\\0pass' base64-encoded in one go.
        Neither is encryption -- both are readable in a packet capture.
        """
        if self.authenticated_as:
            self.reply("503 5.5.1 Already authenticated")
            return True
        if self.state == STATE_INIT:
            self.reply("503 5.5.1 Send HELO/EHLO first")
            return True

        parts = line.split()
        if len(parts) < 2:
            self.reply("501 5.5.4 Syntax: AUTH mechanism")
            return True
        mechanism = parts[1].upper()

        if mechanism == "LOGIN":
            if len(parts) >= 3:                       # initial response form
                try:
                    username = base64.b64decode(parts[2]).decode("utf-8")
                except (ValueError, TypeError):
                    self.reply("501 5.5.2 Cannot decode initial response")
                    return True
            else:
                self.reply("334 VXNlcm5hbWU6")        # base64 of "Username:"
                username = self._read_b64_response()
                if username is None:
                    self.reply("501 5.5.2 Authentication cancelled")
                    return True
            self.reply("334 UGFzc3dvcmQ6")            # base64 of "Password:"
            password = self._read_b64_response()
            if password is None:
                self.reply("501 5.5.2 Authentication cancelled")
                return True

        elif mechanism == "PLAIN":
            if len(parts) >= 3:
                blob = parts[2]
            else:
                self.reply("334 ")
                line2 = self.reader.read_line()
                self.tracer.client(line2)
                blob = line2.strip()
            try:
                decoded = base64.b64decode(blob).decode("utf-8")
            except (ValueError, TypeError):
                self.reply("501 5.5.2 Cannot decode AUTH PLAIN blob")
                return True
            fields = decoded.split("\0")
            if len(fields) < 3:
                self.reply("501 5.5.2 Malformed AUTH PLAIN blob")
                return True
            username, password = fields[1], fields[2]

        else:
            self.reply(f"504 5.5.4 Unrecognised authentication type "
                       f"{mechanism}")
            return True

        if self.server.users.authenticate(username, password):
            self.authenticated_as = mailbox_of(username)
            self.tracer.info(f"authenticated as {self.authenticated_as}")
            self.reply("235 2.7.0 Authentication successful")
        else:
            self.tracer.info(f"authentication FAILED for {username!r}")
            self.reply("535 5.7.8 Authentication credentials invalid")
        return True

    # -- the mail transaction ----------------------------------------------- #

    def reset_transaction(self):
        self.sender = None
        self.recipients = []
        if self.state in (STATE_MAIL, STATE_RCPT):
            self.state = STATE_GREETED

    def cmd_mail(self, line):
        if self.state == STATE_INIT:
            self.reply("503 5.5.1 Send HELO/EHLO first")
            return True
        if self.state in (STATE_MAIL, STATE_RCPT):
            self.reply("503 5.5.1 Nested MAIL command")
            return True
        if self.server.require_auth and not self.authenticated_as:
            self.reply("530 5.7.0 Authentication required")
            return True

        match = _MAIL_RE.match(line.strip())
        if not match:
            self.reply("501 5.5.4 Syntax: MAIL FROM:<address>")
            return True
        address = match.group(1).strip()

        # ESMTP clients may append SIZE=n; refuse oversized mail up front
        # rather than after transferring it.
        size_match = re.search(r"SIZE=(\d+)", match.group(2), re.IGNORECASE)
        if size_match and int(size_match.group(1)) > config.MAX_MESSAGE_BYTES:
            self.reply(f"552 5.3.4 Message size exceeds "
                       f"{config.MAX_MESSAGE_BYTES} bytes")
            return True

        self.sender = address
        self.recipients = []
        self.state = STATE_MAIL
        self.reply(f"250 2.1.0 Sender <{address}> OK")
        return True

    def cmd_rcpt(self, line):
        # The classic ordering error: RCPT before MAIL. 503 means "you sent a
        # valid command at an invalid point in the conversation".
        if self.state not in (STATE_MAIL, STATE_RCPT):
            self.reply("503 5.5.1 Need MAIL command before RCPT")
            return True
        if len(self.recipients) >= config.MAX_RECIPIENTS:
            self.reply("452 4.5.3 Too many recipients")
            return True

        match = _RCPT_RE.match(line.strip())
        if not match:
            self.reply("501 5.5.4 Syntax: RCPT TO:<address>")
            return True
        address = match.group(1).strip()
        mailbox = mailbox_of(address)

        # This server is the final destination for everything, so an unknown
        # local mailbox is a permanent failure. A relaying MTA would instead
        # look the domain up in DNS and forward it.
        if not self.server.users.exists(mailbox):
            self.tracer.info(f"rejecting unknown mailbox {mailbox!r}")
            self.reply(f"550 5.1.1 <{address}>: no such user here")
            return True

        self.recipients.append((address, mailbox))
        self.state = STATE_RCPT
        self.reply(f"250 2.1.5 Recipient <{address}> OK")
        return True

    def cmd_data(self, line):
        if self.state != STATE_RCPT:
            self.reply("503 5.5.1 Need RCPT command before DATA")
            return True

        self.reply("354 End data with <CRLF>.<CRLF>")
        try:
            body = self.reader.read_dot_terminated()
        except wire.LineTooLong:
            self.reply("500 5.5.6 Line too long in message data")
            self.reset_transaction()
            return True
        self.tracer.data(body, "DATA received")

        if len(body) > config.MAX_MESSAGE_BYTES:
            self.reply(f"552 5.3.4 Message exceeds "
                       f"{config.MAX_MESSAGE_BYTES} bytes")
            self.reset_transaction()
            return True

        queue_id = None
        delivered = []
        for address, mailbox in self.recipients:
            stamped = self._add_received_header(body, address)
            try:
                queue_id = self.server.store.deliver(mailbox, stamped)
                delivered.append(mailbox)
            except OSError as exc:
                self.tracer.error(f"delivery to {mailbox} failed: {exc}")
                self.reply("451 4.3.0 Local delivery error")
                self.reset_transaction()
                return True

        self.tracer.info(f"delivered to {', '.join(delivered)}")
        self.reply(f"250 2.0.0 OK: queued as {self.server.store.uidl(queue_id)}")
        self.reset_transaction()
        return True

    def _add_received_header(self, body, recipient):
        """Prepend the Received: trace header every hop is required to add.

        Reading these headers bottom-up on a real message reconstructs the
        exact path it took between servers.
        """
        header = (
            f"Received: from {self.client_name or 'unknown'} "
            f"({self.addr[0]})\r\n"
            f"\tby {config.SERVER_DOMAIN} (CN-Project SMTP) with "
            f"{'ESMTP' if self.client_name else 'SMTP'};\r\n"
            f"\tfor <{recipient}>; {rfc5322_date()}\r\n"
        )
        return header.encode("utf-8") + body

    # -- housekeeping commands ---------------------------------------------- #

    def cmd_rset(self, line):
        self.reset_transaction()
        self.reply("250 2.0.0 Reset state")
        return True

    def cmd_noop(self, line):
        self.reply("250 2.0.0 OK")
        return True

    def cmd_vrfy(self, line):
        parts = line.split(None, 1)
        if len(parts) < 2:
            self.reply("501 5.5.4 Syntax: VRFY address")
            return True
        mailbox = mailbox_of(parts[1])
        if self.server.users.exists(mailbox):
            self.reply(f"250 2.1.5 <{self.server.users.address(mailbox)}>")
        else:
            # Real servers usually answer 252 to everything, because a truthful
            # VRFY hands spammers a free list of valid addresses.
            self.reply(f"550 5.1.1 <{parts[1].strip()}>: no such user here")
        return True

    def cmd_help(self, line):
        self.reply_multi([
            "214 Supported commands:",
            "214 HELO EHLO AUTH MAIL RCPT DATA RSET NOOP VRFY HELP QUIT",
            "214 End of HELP",
        ])
        return True

    def cmd_quit(self, line):
        self.reply(f"221 2.0.0 {config.SERVER_DOMAIN} closing connection")
        return False


class SMTPServer:
    """Accepts TCP connections and runs one SMTPSession per client thread."""

    def __init__(self, host=config.HOST, port=config.SMTP_PORT, store=None,
                 users=None, require_auth=False, verbose=True,
                 log_to_file=True):
        self.host = host
        self.port = port
        self.store = store or default_store()
        self.users = users or UserDirectory()
        self.require_auth = require_auth
        self.verbose = verbose
        self.log_to_file = log_to_file

        self.tracer = Tracer("smtp", to_console=verbose, to_file=log_to_file)
        self._sock = None
        self._thread = None
        self._running = False
        self._session_counter = 0
        self._counter_lock = threading.Lock()

    def _next_session_id(self):
        with self._counter_lock:
            self._session_counter += 1
            return self._session_counter

    def start(self):
        """Bind, listen, and serve in a background thread. Returns the port."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Without SO_REUSEADDR a restart within the TIME_WAIT window fails
        # with "Address already in use" even though nothing is listening.
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]     # resolves port 0
        self._running = True
        self.tracer.info(f"SMTP server listening on {self.host}:{self.port}"
                         + ("  [auth required]" if self.require_auth else ""))
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self.port

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break                       # socket closed by stop()
            conn.settimeout(config.SOCKET_TIMEOUT)
            session = SMTPSession(conn, addr, self, self._next_session_id())
            threading.Thread(target=session.run, daemon=True).start()

    def stop(self):
        self._running = False
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        self.tracer.info("SMTP server stopped")

    def serve_forever(self):
        self.start()
        try:
            while self._running:
                self._thread.join(timeout=0.5)
        except KeyboardInterrupt:
            print()
            self.stop()


def main():
    parser = argparse.ArgumentParser(description="Simple SMTP server (RFC 5321)")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.SMTP_PORT)
    parser.add_argument("--require-auth", action="store_true",
                        help="reject MAIL FROM until the client authenticates")
    parser.add_argument("--quiet", action="store_true",
                        help="do not print the protocol trace")
    args = parser.parse_args()

    store = MailStore(config.STORE_DIR)
    users = UserDirectory()
    server = SMTPServer(args.host, args.port, store=store, users=users,
                        require_auth=args.require_auth, verbose=not args.quiet)
    print(f"Mailboxes: {', '.join(users.usernames())}")
    print(f"Mail store: {store.root}")
    print("Press Ctrl+C to stop.\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
