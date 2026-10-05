"""
A minimal POP3 server (RFC 1939), written directly on TCP sockets.

Where SMTP pushes mail towards a mailbox, POP3 *pulls* it back out. It is the
simpler of the two protocols: no numeric codes, just two possible statuses.

    +OK   the command succeeded
    -ERR  it did not

A session moves through exactly three states, and this ordering is the whole
protocol:

    AUTHORIZATION  the client identifies itself (USER/PASS, or APOP) and the
                   server takes an exclusive lock on the maildrop.
    TRANSACTION    the client inspects and marks messages: STAT, LIST, RETR,
                   TOP, UIDL, DELE, RSET.
    UPDATE         entered by QUIT. Only now are messages marked with DELE
                   actually removed, and then the connection closes.

That last part is the detail worth noticing. DELE does not delete anything --
it sets a flag. RSET clears every flag. If the connection drops before QUIT,
nothing is deleted at all. The design is deliberate: it means a client that
crashes half way through collecting mail loses nothing.

Message numbers are assigned once, when the session opens, and never shift.
Deleting message 1 does not renumber message 2; message 1 simply becomes
inaccessible for the rest of the session.

Run directly:   python3 pop3_server.py [--port 1110]
"""

import argparse
import hashlib
import os
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from common import wire
from common.mailstore import MailboxLocked, MailStore, default_store
from common.trace import Tracer
from common.users import UserDirectory, mailbox_of

STATE_AUTHORIZATION = "AUTHORIZATION"
STATE_TRANSACTION = "TRANSACTION"
STATE_UPDATE = "UPDATE"


class POP3Session:
    """Handles one client connection through the three protocol states."""

    def __init__(self, conn, addr, server, session_id):
        self.conn = conn
        self.addr = addr
        self.server = server
        self.session_id = session_id
        self.tracer = Tracer(f"pop3#{session_id}",
                             to_console=server.verbose,
                             to_file=server.log_to_file)
        self.reader = wire.LineReader(conn, timeout=config.SOCKET_TIMEOUT)

        self.state = STATE_AUTHORIZATION
        self.username = None            # offered by USER, not yet verified
        self.mailbox = None             # set once authenticated
        self.messages = []              # [(number, filename, size)] fixed for the session
        self.deleted = set()            # message numbers marked by DELE
        self.holds_lock = False

        # The APOP banner. It must be unique per session, which is why it
        # mixes the process id with the current time.
        self.banner = f"<{os.getpid()}.{int(time.time() * 1000)}@{config.SERVER_DOMAIN}>"

    # -- I/O helpers -------------------------------------------------------- #

    def ok(self, text=""):
        line = f"+OK {text}".rstrip()
        self.tracer.server(line)
        wire.send_line(self.conn, line)

    def err(self, text=""):
        line = f"-ERR {text}".rstrip()
        self.tracer.server(line)
        wire.send_line(self.conn, line)

    def send_block(self, data, label="data"):
        """Send a multi-line response: dot-stuffed body then a lone '.'."""
        self.tracer.data(data if isinstance(data, bytes) else data.encode(),
                         label)
        wire.send_dot_terminated(self.conn, data)
        self.tracer.server(".")

    # -- main loop ---------------------------------------------------------- #

    def run(self):
        self.tracer.info(f"connection from {self.addr[0]}:{self.addr[1]}")
        self.ok(f"POP3 server ready (CN Project) {self.banner}")
        try:
            while True:
                try:
                    line = self.reader.read_line()
                except wire.LineTooLong:
                    self.err("line too long")
                    continue
                self.tracer.client(line)
                if not self.dispatch(line):
                    break
        except wire.ConnectionClosed as exc:
            # Dropping the connection without QUIT skips the UPDATE state, so
            # anything marked with DELE survives. That is by design.
            self.tracer.info(f"session ended without QUIT: {exc}")
            if self.deleted:
                self.tracer.info(f"{len(self.deleted)} message(s) stay marked "
                                 f"but undeleted (no UPDATE state reached)")
        except Exception as exc:                        # noqa: BLE001
            self.tracer.error(f"unexpected error: {exc}")
        finally:
            self.release()
            try:
                self.conn.close()
            except OSError:
                pass
            self.tracer.info("connection closed")

    def release(self):
        if self.holds_lock and self.mailbox:
            self.server.store.release_lock(self.mailbox, self.session_id)
            self.holds_lock = False

    def dispatch(self, line):
        parts = line.strip().split()
        if not parts:
            self.err("empty command")
            return True
        verb = parts[0].upper()
        args = parts[1:]

        handlers = {
            "USER": (self.cmd_user, STATE_AUTHORIZATION),
            "PASS": (self.cmd_pass, STATE_AUTHORIZATION),
            "APOP": (self.cmd_apop, STATE_AUTHORIZATION),
            "CAPA": (self.cmd_capa, None),
            "STAT": (self.cmd_stat, STATE_TRANSACTION),
            "LIST": (self.cmd_list, STATE_TRANSACTION),
            "UIDL": (self.cmd_uidl, STATE_TRANSACTION),
            "RETR": (self.cmd_retr, STATE_TRANSACTION),
            "TOP": (self.cmd_top, STATE_TRANSACTION),
            "DELE": (self.cmd_dele, STATE_TRANSACTION),
            "RSET": (self.cmd_rset, STATE_TRANSACTION),
            "NOOP": (self.cmd_noop, STATE_TRANSACTION),
            "QUIT": (self.cmd_quit, None),
        }
        entry = handlers.get(verb)
        if entry is None:
            self.err(f'unknown command "{verb}"')
            return True

        handler, required_state = entry
        if required_state is not None and self.state != required_state:
            self.err(f"command {verb} not valid in {self.state} state")
            return True
        return handler(args)

    # -- AUTHORIZATION state ------------------------------------------------ #

    def cmd_user(self, args):
        if not args:
            self.err("syntax: USER username")
            return True
        # Answer +OK even for an unknown name. Saying "no such user" here would
        # let anyone enumerate valid mailboxes one guess at a time.
        self.username = mailbox_of(args[0])
        self.ok(f"user {self.username} accepted, send PASS")
        return True

    def cmd_pass(self, args):
        if self.username is None:
            self.err("send USER first")
            return True
        password = " ".join(args)       # passwords may contain spaces
        if not self.server.users.authenticate(self.username, password):
            self.tracer.info(f"authentication FAILED for {self.username!r}")
            self.username = None
            self.err("invalid username or password")
            return True
        return self._enter_transaction()

    def cmd_apop(self, args):
        """APOP: prove knowledge of the secret without sending it.

        The client sends MD5(banner + secret). The server computes the same
        digest and compares. The password never crosses the wire -- but the
        server must store it in plaintext to do this, which is why APOP is
        obsolete and TLS replaced it.
        """
        if len(args) < 2:
            self.err("syntax: APOP username digest")
            return True
        self.username = mailbox_of(args[0])
        secret = self.server.users.apop_secret(self.username)
        if secret is None:
            self.username = None
            self.err("permission denied")
            return True
        expected = hashlib.md5(
            (self.banner + secret).encode("utf-8")).hexdigest()
        if args[1].lower() != expected:
            self.tracer.info(f"APOP digest mismatch for {self.username!r}")
            self.username = None
            self.err("permission denied")
            return True
        return self._enter_transaction()

    def _enter_transaction(self):
        """Lock the maildrop and take the snapshot of message numbers."""
        try:
            self.server.store.acquire_lock(self.username, self.session_id)
        except MailboxLocked:
            self.tracer.info(f"maildrop {self.username!r} is locked by "
                             f"another session")
            self.err("maildrop already locked by another session")
            self.username = None
            return True

        self.holds_lock = True
        self.mailbox = self.username
        self.messages = self.server.store.list_messages(self.mailbox)
        self.deleted = set()
        self.state = STATE_TRANSACTION

        total = sum(size for _, _, size in self.messages)
        self.tracer.info(f"maildrop locked for {self.mailbox}")
        self.ok(f"maildrop has {len(self.messages)} message(s) "
                f"({total} octets)")
        return True

    def cmd_capa(self, args):
        capabilities = ["TOP", "UIDL", "USER", "RESP-CODES",
                        "IMPLEMENTATION CN-Project-POP3"]
        self.ok("capability list follows")
        self.send_block("\r\n".join(capabilities) + "\r\n", "CAPA list")
        return True

    # -- TRANSACTION state -------------------------------------------------- #

    def _live_messages(self):
        """Messages not marked for deletion, in session numbering."""
        return [m for m in self.messages if m[0] not in self.deleted]

    def _lookup(self, number_text):
        """Resolve a message number argument, or return None after replying."""
        try:
            number = int(number_text)
        except (TypeError, ValueError):
            self.err(f"invalid message number {number_text!r}")
            return None
        if number in self.deleted:
            self.err(f"message {number} already deleted")
            return None
        for entry in self.messages:
            if entry[0] == number:
                return entry
        self.err(f"no such message, only {len(self.messages)} in maildrop")
        return None

    def cmd_stat(self, args):
        live = self._live_messages()
        total = sum(size for _, _, size in live)
        # STAT's reply is deliberately just two numbers so simple clients can
        # parse it without any string handling.
        self.ok(f"{len(live)} {total}")
        return True

    def cmd_list(self, args):
        if args:
            entry = self._lookup(args[0])
            if entry is None:
                return True
            self.ok(f"{entry[0]} {entry[2]}")
            return True

        live = self._live_messages()
        total = sum(size for _, _, size in live)
        self.ok(f"{len(live)} message(s) ({total} octets)")
        listing = "".join(f"{num} {size}\r\n" for num, _, size in live)
        self.send_block(listing, "scan listing")
        return True

    def cmd_uidl(self, args):
        store = self.server.store
        if args:
            entry = self._lookup(args[0])
            if entry is None:
                return True
            self.ok(f"{entry[0]} {store.uidl(entry[1])}")
            return True

        self.ok("unique-id listing follows")
        listing = "".join(f"{num} {store.uidl(name)}\r\n"
                          for num, name, _ in self._live_messages())
        self.send_block(listing, "unique-id listing")
        return True

    def cmd_retr(self, args):
        if not args:
            self.err("syntax: RETR n")
            return True
        entry = self._lookup(args[0])
        if entry is None:
            return True
        number, filename, size = entry
        try:
            raw = self.server.store.read_message(self.mailbox, filename)
        except OSError as exc:
            self.err(f"cannot read message {number}: {exc}")
            return True
        self.ok(f"{len(raw)} octets")
        self.send_block(raw, f"message {number}")
        return True

    def cmd_top(self, args):
        """TOP n m: the headers plus the first m lines of the body.

        This is how a client shows a subject list without downloading every
        message in full.
        """
        if len(args) < 2:
            self.err("syntax: TOP n lines")
            return True
        entry = self._lookup(args[0])
        if entry is None:
            return True
        try:
            body_lines = int(args[1])
            if body_lines < 0:
                raise ValueError
        except ValueError:
            self.err(f"invalid line count {args[1]!r}")
            return True

        try:
            raw = self.server.store.read_message(self.mailbox, entry[1])
        except OSError as exc:
            self.err(f"cannot read message: {exc}")
            return True

        normalized = wire.normalize_crlf(raw)
        if b"\r\n\r\n" in normalized:
            headers, body = normalized.split(b"\r\n\r\n", 1)
        else:
            headers, body = normalized, b""
        selected = body.split(b"\r\n")[:body_lines]
        extract = headers + b"\r\n\r\n" + b"\r\n".join(selected)

        self.ok(f"top of message {entry[0]} follows")
        self.send_block(extract, f"TOP {entry[0]} {body_lines}")
        return True

    def cmd_dele(self, args):
        if not args:
            self.err("syntax: DELE n")
            return True
        entry = self._lookup(args[0])
        if entry is None:
            return True
        # Marked only. The file is not touched until the UPDATE state.
        self.deleted.add(entry[0])
        self.tracer.info(f"message {entry[0]} marked for deletion "
                         f"(removed at QUIT)")
        self.ok(f"message {entry[0]} marked deleted")
        return True

    def cmd_rset(self, args):
        count = len(self.deleted)
        self.deleted = set()
        self.tracer.info(f"unmarked {count} message(s)")
        live = self._live_messages()
        total = sum(size for _, _, size in live)
        self.ok(f"maildrop has {len(live)} message(s) ({total} octets)")
        return True

    def cmd_noop(self, args):
        self.ok()
        return True

    # -- UPDATE state ------------------------------------------------------- #

    def cmd_quit(self, args):
        if self.state != STATE_TRANSACTION:
            self.ok(f"{config.SERVER_DOMAIN} POP3 server signing off")
            return False

        # This is the UPDATE state: the only place messages are really removed.
        self.state = STATE_UPDATE
        removed = 0
        failed = 0
        for number, filename, _ in self.messages:
            if number in self.deleted:
                if self.server.store.delete_message(self.mailbox, filename):
                    removed += 1
                else:
                    failed += 1
        self.release()
        self.tracer.info(f"UPDATE state: {removed} message(s) deleted")
        if failed:
            self.err(f"some messages could not be removed ({failed} failed)")
        else:
            self.ok(f"{config.SERVER_DOMAIN} POP3 server signing off "
                    f"({removed} message(s) deleted)")
        return False


class POP3Server:
    """Accepts TCP connections and runs one POP3Session per client thread."""

    def __init__(self, host=config.HOST, port=config.POP3_PORT, store=None,
                 users=None, verbose=True, log_to_file=True):
        self.host = host
        self.port = port
        self.store = store or default_store()
        self.users = users or UserDirectory()
        self.verbose = verbose
        self.log_to_file = log_to_file

        self.tracer = Tracer("pop3", to_console=verbose, to_file=log_to_file)
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
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._running = True
        self.tracer.info(f"POP3 server listening on {self.host}:{self.port}")
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self.port

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break
            conn.settimeout(config.SOCKET_TIMEOUT)
            session = POP3Session(conn, addr, self, self._next_session_id())
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
        self.tracer.info("POP3 server stopped")

    def serve_forever(self):
        self.start()
        try:
            while self._running:
                self._thread.join(timeout=0.5)
        except KeyboardInterrupt:
            print()
            self.stop()


def main():
    parser = argparse.ArgumentParser(description="Simple POP3 server (RFC 1939)")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.POP3_PORT)
    parser.add_argument("--quiet", action="store_true",
                        help="do not print the protocol trace")
    args = parser.parse_args()

    store = MailStore(config.STORE_DIR)
    users = UserDirectory()
    server = POP3Server(args.host, args.port, store=store, users=users,
                        verbose=not args.quiet)
    print(f"Mailboxes: {', '.join(users.usernames())}")
    print(f"Mail store: {store.root}")
    print("Press Ctrl+C to stop.\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
