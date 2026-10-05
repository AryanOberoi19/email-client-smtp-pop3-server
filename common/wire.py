"""
The wire layer: the only place in the project that touches a raw socket.

Both SMTP (RFC 5321) and POP3 (RFC 1939) are line-oriented text protocols
spoken over TCP. Every command and every response ends with CRLF -- a
carriage return followed by a line feed -- and bulk data (a mail message
being sent, or a mail message being retrieved) is terminated by a line
containing a single period.

Two details trip people up, and both are handled here so the servers and the
client never have to think about them again:

1.  A line ends with CRLF and *only* CRLF. Python's str.splitlines() also
    splits on a bare \\n, \\r, \\v, \\f and a few Unicode separators, which
    would corrupt a message body. We scan for the two-byte sequence b"\\r\\n"
    explicitly.

2.  Because a lone "." on its own line means "end of data", a message body
    that genuinely contains such a line has to be escaped. The sender prefixes
    an extra "." to any line already starting with "." (dot-stuffing) and the
    receiver strips it back off. Get this wrong and a message that happens to
    contain a line of dots truncates on delivery.

TCP is a byte stream, not a message stream: one recv() may return half a line
or three lines at once. LineReader buffers so callers always see whole lines.
"""

import socket

CRLF = b"\r\n"
DOT_LINE = b"."

# Imported lazily so this module also works if config is not on the path.
try:
    from config import MAX_LINE, SOCKET_TIMEOUT
except ImportError:  # pragma: no cover - fallback for standalone use
    MAX_LINE = 1000
    SOCKET_TIMEOUT = 300


class ProtocolError(Exception):
    """Raised when the peer sends something that cannot be parsed."""


class ConnectionClosed(ProtocolError):
    """Raised when the peer closes the connection mid-conversation."""


class LineTooLong(ProtocolError):
    """Raised when a peer sends a line longer than the protocol allows."""


# --------------------------------------------------------------------------- #
#  Sending
# --------------------------------------------------------------------------- #

def send_line(sock, text):
    """Send one protocol line, appending the CRLF terminator.

    `text` is a str (a command or a response). Protocol keywords are ASCII,
    but headers and bodies may carry UTF-8, so we encode as UTF-8 throughout.
    """
    if isinstance(text, str):
        payload = text.encode("utf-8", errors="replace")
    else:
        payload = text
    sock.sendall(payload + CRLF)


def send_bytes(sock, data):
    """Send raw bytes exactly as given (used for message bodies)."""
    sock.sendall(data)


# --------------------------------------------------------------------------- #
#  Receiving
# --------------------------------------------------------------------------- #

class LineReader:
    """Buffers a socket so callers can read one CRLF-terminated line at a time.

    A single TCP segment carries an arbitrary slice of the stream, so we keep
    leftover bytes in `_buf` between calls and only hand back complete lines.
    """

    def __init__(self, sock, timeout=SOCKET_TIMEOUT, max_line=MAX_LINE):
        self.sock = sock
        self.max_line = max_line
        self._buf = bytearray()
        self._closed = False
        if timeout is not None:
            self.sock.settimeout(timeout)

    # -- low level ---------------------------------------------------------- #

    def _fill(self):
        """Pull one chunk from the socket into the buffer."""
        try:
            chunk = self.sock.recv(4096)
        except socket.timeout:
            raise ConnectionClosed("connection timed out waiting for data")
        except OSError as exc:
            raise ConnectionClosed(f"socket error: {exc}")
        if not chunk:
            self._closed = True
            raise ConnectionClosed("peer closed the connection")
        self._buf.extend(chunk)

    def read_line_bytes(self):
        """Return the next line as bytes, without its CRLF terminator."""
        while True:
            idx = self._buf.find(CRLF)
            if idx != -1:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 2]
                return line
            # No complete line yet. Guard against a peer that never sends CRLF.
            if len(self._buf) > self.max_line:
                raise LineTooLong(
                    f"line exceeds {self.max_line} octets with no CRLF"
                )
            self._fill()

    def read_line(self):
        """Return the next line as a str (commands and responses)."""
        return self.read_line_bytes().decode("utf-8", errors="replace")

    def read_dot_terminated(self):
        """Read a multi-line block ending in a lone '.' line.

        Returns the block as bytes with CRLF line endings and dot-stuffing
        already removed. This is the receiving half of both SMTP DATA and
        POP3 RETR / TOP / LIST.
        """
        lines = []
        while True:
            line = self.read_line_bytes()
            if line == DOT_LINE:
                break
            if line.startswith(DOT_LINE):
                line = line[1:]          # un-stuff: "..text" -> ".text"
            lines.append(line)
        if not lines:
            return b""
        return CRLF.join(lines) + CRLF


# --------------------------------------------------------------------------- #
#  Dot-stuffing helpers
# --------------------------------------------------------------------------- #

def normalize_crlf(data):
    """Convert any mix of line endings to the CRLF the protocols require."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    # Collapse CRLF and lone CR to LF first, then expand every LF to CRLF, so
    # a file with mixed endings cannot end up with doubled carriage returns.
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data.replace(b"\n", b"\r\n")


def dot_stuff(data):
    """Escape a message body for transmission.

    Any line starting with '.' gets a second '.' so it cannot be mistaken for
    the end-of-data marker. The receiver reverses this.
    """
    data = normalize_crlf(data)
    if not data:
        return b""
    lines = data.split(CRLF)
    stuffed = [b"." + ln if ln.startswith(DOT_LINE) else ln for ln in lines]
    return CRLF.join(stuffed)


def dot_unstuff(data):
    """Reverse dot_stuff() on a received body."""
    data = normalize_crlf(data)
    if not data:
        return b""
    lines = data.split(CRLF)
    plain = [ln[1:] if ln.startswith(DOT_LINE) else ln for ln in lines]
    return CRLF.join(plain)


def send_dot_terminated(sock, data):
    """Send a message body, dot-stuffed, followed by the terminating '.' line.

    The body is forced to end with CRLF first: without that, the final line of
    the message and the terminating dot would arrive on the same line.
    """
    body = dot_stuff(data)
    if body and not body.endswith(CRLF):
        body += CRLF
    send_bytes(sock, body + DOT_LINE + CRLF)
