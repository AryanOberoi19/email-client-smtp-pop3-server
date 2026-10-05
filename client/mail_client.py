"""
The client half of both protocols, on raw sockets.

Python's standard library has smtplib and poplib, which would replace this
entire file. They are deliberately unused: driving the conversation by hand is
the other half of understanding it. Everything here is socket, send, recv.

Both classes take a Tracer, so the CLI and the GUI show the identical
transcript -- the same object that renders the server side renders the client
side too.

    smtp = SMTPClient()
    smtp.connect(); smtp.ehlo(); smtp.login("aryan", "aryan123")
    smtp.send_mail("aryan@localhost", ["prof@localhost"], "Subject", "Body")
    smtp.quit()

    pop = POP3Client()
    pop.connect(); pop.login("prof", "prof123")
    for number, size in pop.list():
        print(pop.retr(number))
    pop.quit()
"""

import base64
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from common import mime, wire
from common.trace import Tracer


class MailClientError(Exception):
    """Base class for a protocol-level failure reported by the server."""


class SMTPError(MailClientError):
    def __init__(self, code, message):
        super().__init__(f"SMTP {code}: {message}")
        self.code = code
        self.message = message


class POP3Error(MailClientError):
    pass


# --------------------------------------------------------------------------- #
#  SMTP
# --------------------------------------------------------------------------- #

class SMTPClient:
    """Sends mail by speaking RFC 5321 at a server."""

    def __init__(self, host=config.HOST, port=config.SMTP_PORT, tracer=None):
        self.host = host
        self.port = port
        self.tracer = tracer or Tracer("client-smtp")
        self.sock = None
        self.reader = None
        self.capabilities = []
        self.authenticated = False

    # -- plumbing ----------------------------------------------------------- #

    def _send(self, line):
        self.tracer.client(line)
        wire.send_line(self.sock, line)

    def _read_response(self):
        """Read a reply, joining continuation lines.

        A multi-line reply puts a hyphen after the code on every line but the
        last: '250-SIZE ...' then '250 HELP'. Returns (code, [text lines]).
        """
        lines = []
        code = 0
        while True:
            line = self.reader.read_line()
            self.tracer.server(line)
            if len(line) < 3 or not line[:3].isdigit():
                raise SMTPError(0, f"malformed reply: {line!r}")
            code = int(line[:3])
            lines.append(line[4:] if len(line) > 4 else "")
            if len(line) < 4 or line[3] != "-":
                break
        return code, lines

    def _expect(self, *ok_codes):
        code, lines = self._read_response()
        if code not in ok_codes:
            raise SMTPError(code, " ".join(lines))
        return code, lines

    # -- conversation ------------------------------------------------------- #

    def connect(self):
        self.tracer.info(f"connecting to SMTP {self.host}:{self.port}")
        self.sock = socket.create_connection((self.host, self.port),
                                             timeout=config.CONNECT_TIMEOUT)
        self.reader = wire.LineReader(self.sock, timeout=config.SOCKET_TIMEOUT)
        return self._expect(220)            # server speaks first

    def ehlo(self, hostname="cn-project-client"):
        self._send(f"EHLO {hostname}")
        code, lines = self._read_response()
        if code != 250:
            # An old server that does not know EHLO answers 500 or 502; the
            # correct fallback is the original HELO.
            self.tracer.info("EHLO refused, falling back to HELO")
            return self.helo(hostname)
        self.capabilities = [ln.strip() for ln in lines[1:]]
        return code, lines

    def helo(self, hostname="cn-project-client"):
        self._send(f"HELO {hostname}")
        return self._expect(250)

    def login(self, username, password):
        """AUTH LOGIN: base64 username, then base64 password."""
        self._send("AUTH LOGIN")
        self._expect(334)
        self._send(base64.b64encode(username.encode()).decode())
        self._expect(334)
        self._send(base64.b64encode(password.encode()).decode())
        self._expect(235)
        self.authenticated = True
        self.tracer.info(f"authenticated as {username}")
        return True

    def send_mail(self, sender, recipients, subject, body, attachments=None):
        """Run one full MAIL/RCPT/DATA transaction. Returns the queue id."""
        if isinstance(recipients, str):
            recipients = [recipients]

        self._send(f"MAIL FROM:<{sender}>")
        self._expect(250)

        accepted = []
        for recipient in recipients:
            self._send(f"RCPT TO:<{recipient}>")
            code, lines = self._read_response()
            if code in (250, 251):
                accepted.append(recipient)
            else:
                # One bad address does not have to abort the whole message.
                self.tracer.info(f"recipient {recipient} rejected: "
                                 f"{code} {' '.join(lines)}")
        if not accepted:
            self._send("RSET")
            self._read_response()
            raise SMTPError(550, "no valid recipients")

        message = mime.build_message(sender, recipients, subject, body,
                                     attachments)

        self._send("DATA")
        self._expect(354)
        # Everything from here until the lone '.' is opaque to SMTP.
        self.tracer.data(message, "sending message")
        wire.send_dot_terminated(self.sock, message)
        self.tracer.client(".")
        code, lines = self._expect(250)
        return " ".join(lines)

    def rset(self):
        self._send("RSET")
        return self._expect(250)

    def noop(self):
        self._send("NOOP")
        return self._expect(250)

    def quit(self):
        try:
            if self.sock is not None:
                self._send("QUIT")
                self._expect(221)
        except (MailClientError, wire.ProtocolError, OSError):
            pass
        finally:
            self.close()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
            self.reader = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.quit()
        return False


# --------------------------------------------------------------------------- #
#  POP3
# --------------------------------------------------------------------------- #

class POP3Client:
    """Retrieves mail by speaking RFC 1939 at a server."""

    def __init__(self, host=config.HOST, port=config.POP3_PORT, tracer=None):
        self.host = host
        self.port = port
        self.tracer = tracer or Tracer("client-pop3")
        self.sock = None
        self.reader = None
        self.banner = ""
        self.username = None

    # -- plumbing ----------------------------------------------------------- #

    def _send(self, line):
        self.tracer.client(line)
        wire.send_line(self.sock, line)

    def _read_status(self):
        """Read a status line. Returns the text after '+OK'; raises on '-ERR'."""
        line = self.reader.read_line()
        self.tracer.server(line)
        if line.startswith("+OK"):
            return line[3:].strip()
        if line.startswith("-ERR"):
            raise POP3Error(line[4:].strip())
        raise POP3Error(f"malformed status line: {line!r}")

    def _read_block(self):
        """Read a dot-terminated multi-line response (already un-stuffed)."""
        data = self.reader.read_dot_terminated()
        self.tracer.data(data, "multi-line response")
        self.tracer.server(".")
        return data

    # -- conversation ------------------------------------------------------- #

    def connect(self):
        self.tracer.info(f"connecting to POP3 {self.host}:{self.port}")
        self.sock = socket.create_connection((self.host, self.port),
                                             timeout=config.CONNECT_TIMEOUT)
        self.reader = wire.LineReader(self.sock, timeout=config.SOCKET_TIMEOUT)
        self.banner = self._read_status()
        return self.banner

    def login(self, username, password):
        """The USER/PASS exchange. Note the password goes out in the clear."""
        self._send(f"USER {username}")
        self._read_status()
        self._send(f"PASS {password}")
        status = self._read_status()
        self.username = username
        self.tracer.info(f"logged in as {username}")
        return status

    def apop(self, username, secret):
        """Log in with a digest instead of the password itself."""
        import hashlib
        start = self.banner.rfind("<")
        end = self.banner.rfind(">")
        if start == -1 or end == -1:
            raise POP3Error("server did not offer an APOP banner")
        challenge = self.banner[start:end + 1]
        digest = hashlib.md5((challenge + secret).encode()).hexdigest()
        self._send(f"APOP {username} {digest}")
        status = self._read_status()
        self.username = username
        return status

    def stat(self):
        """Returns (message count, total octets)."""
        self._send("STAT")
        status = self._read_status()
        parts = status.split()
        return int(parts[0]), int(parts[1])

    def list(self):
        """Returns [(number, size)] for every message in the maildrop."""
        self._send("LIST")
        self._read_status()
        block = self._read_block().decode("utf-8", errors="replace")
        entries = []
        for line in block.split("\r\n"):
            fields = line.split()
            if len(fields) >= 2 and fields[0].isdigit():
                entries.append((int(fields[0]), int(fields[1])))
        return entries

    def uidl(self):
        """Returns [(number, unique-id)] -- stable across sessions."""
        self._send("UIDL")
        self._read_status()
        block = self._read_block().decode("utf-8", errors="replace")
        entries = []
        for line in block.split("\r\n"):
            fields = line.split()
            if len(fields) >= 2 and fields[0].isdigit():
                entries.append((int(fields[0]), fields[1]))
        return entries

    def retr(self, number):
        """Download one message in full. Returns the raw bytes."""
        self._send(f"RETR {number}")
        self._read_status()
        return self._read_block()

    def top(self, number, lines=0):
        """Download headers plus `lines` body lines -- used to build a list view."""
        self._send(f"TOP {number} {lines}")
        self._read_status()
        return self._read_block()

    def dele(self, number):
        """Mark a message deleted. Nothing is removed until quit()."""
        self._send(f"DELE {number}")
        return self._read_status()

    def rset(self):
        """Unmark every message marked by dele()."""
        self._send("RSET")
        return self._read_status()

    def noop(self):
        self._send("NOOP")
        return self._read_status()

    def capa(self):
        self._send("CAPA")
        self._read_status()
        block = self._read_block().decode("utf-8", errors="replace")
        return [ln for ln in block.split("\r\n") if ln]

    def quit(self):
        """Enter the UPDATE state: this is what actually commits deletions."""
        try:
            if self.sock is not None:
                self._send("QUIT")
                return self._read_status()
        except (MailClientError, wire.ProtocolError, OSError):
            return None
        finally:
            self.close()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
            self.reader = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.quit()
        return False


# --------------------------------------------------------------------------- #
#  Convenience wrappers used by both front-ends
# --------------------------------------------------------------------------- #

def send_message(sender, recipients, subject, body, attachments=None,
                 username=None, password=None, host=config.HOST,
                 port=config.SMTP_PORT, tracer=None):
    """Open an SMTP session, send one message, close. Returns the queue id."""
    client = SMTPClient(host, port, tracer)
    client.connect()
    try:
        client.ehlo()
        if username and password:
            client.login(username, password)
        return client.send_mail(sender, recipients, subject, body, attachments)
    finally:
        client.quit()


def fetch_inbox(username, password, host=config.HOST, port=config.POP3_PORT,
                tracer=None, full=True):
    """Open a POP3 session and return a list of parsed messages.

    Each entry is (number, size, parsed Message). With full=False only the
    headers are fetched (via TOP), which is what a real client does to render
    a message list quickly.
    """
    client = POP3Client(host, port, tracer)
    client.connect()
    try:
        client.login(username, password)
        results = []
        for number, size in client.list():
            raw = client.retr(number) if full else client.top(number, 0)
            results.append((number, size, mime.parse_message(raw)))
        return results
    finally:
        client.quit()
