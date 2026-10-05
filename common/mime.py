"""
Message construction and parsing, written by hand.

Python ships an `email` package that would do all of this in three lines. It
is deliberately not used: the point of the project is to show what an email
message actually *is* on the wire.

A message has the shape defined by RFC 5322:

    From: aryan@localhost          <- headers, one per line
    To: prof@localhost
    Subject: Lab submission
    Date: Fri, 29 Aug 2026 03:40:00 +0530
                                   <- exactly one blank line
    Hello sir,                     <- body, everything after the blank line
    Attached is my lab file.

SMTP itself never looks inside this. To SMTP the whole block is opaque data
between DATA and the terminating '.' -- which is why the addresses in
MAIL FROM / RCPT TO (the "envelope") can differ entirely from the From:/To:
headers a user sees. That gap is the reason email spoofing works.

Attachments come from MIME (RFC 2045/2046), which layers structure on top:
Content-Type: multipart/mixed with a boundary string, each part separated by
'--boundary', and binary parts base64-encoded so they survive a text-only
transport.
"""

import base64
import mimetypes
import os
import random
import string
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from config import SERVER_DOMAIN
except ImportError:  # pragma: no cover
    SERVER_DOMAIN = "localhost"

CRLF = "\r\n"


# --------------------------------------------------------------------------- #
#  Building
# --------------------------------------------------------------------------- #

def rfc5322_date(when=None):
    """Format a timestamp the way a Date: header requires.

    e.g. 'Fri, 29 Aug 2026 03:40:00 +0530'. Built manually rather than with
    strftime('%a %b') so the weekday and month names are always the English
    abbreviations the standard mandates, regardless of system locale.
    """
    when = time.localtime() if when is None else when
    days = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    offset = -(time.altzone if when.tm_isdst else time.timezone)
    sign = "+" if offset >= 0 else "-"
    offset = abs(offset)
    return (f"{days[when.tm_wday]}, {when.tm_mday:02d} "
            f"{months[when.tm_mon - 1]} {when.tm_year} "
            f"{when.tm_hour:02d}:{when.tm_min:02d}:{when.tm_sec:02d} "
            f"{sign}{offset // 3600:02d}{(offset % 3600) // 60:02d}")


def make_message_id(domain=SERVER_DOMAIN):
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    return f"<{int(time.time() * 1000)}.{rand}@{domain}>"


def _make_boundary():
    rand = "".join(random.choices(string.ascii_letters + string.digits, k=24))
    return f"----=_CNProject_{rand}"


def _b64_wrapped(raw, width=76):
    """Base64-encode and wrap at 76 characters, as RFC 2045 requires."""
    encoded = base64.b64encode(raw).decode("ascii")
    return CRLF.join(encoded[i:i + width]
                     for i in range(0, len(encoded), width))


def build_message(sender, recipients, subject, body, attachments=None,
                  date=None, message_id=None):
    """Assemble a complete RFC 5322 message and return it as bytes.

    `recipients` is a list of addresses; `attachments` a list of file paths.
    With no attachments the result is a simple text/plain message; with them
    it becomes multipart/mixed.
    """
    attachments = attachments or []
    if isinstance(recipients, str):
        recipients = [recipients]

    headers = [
        f"From: {sender}",
        f"To: {', '.join(recipients)}",
        f"Subject: {subject}",
        f"Date: {date or rfc5322_date()}",
        f"Message-ID: {message_id or make_message_id()}",
        "MIME-Version: 1.0",
        "X-Mailer: CN-Project-MailClient/1.0 (I041)",
    ]

    if not attachments:
        headers.append('Content-Type: text/plain; charset="utf-8"')
        headers.append("Content-Transfer-Encoding: 8bit")
        message = CRLF.join(headers) + CRLF + CRLF + (body or "")
        return message.encode("utf-8")

    boundary = _make_boundary()
    headers.append(f'Content-Type: multipart/mixed; boundary="{boundary}"')

    parts = [
        # First part: the text the reader sees.
        CRLF.join([
            f"--{boundary}",
            'Content-Type: text/plain; charset="utf-8"',
            "Content-Transfer-Encoding: 8bit",
            "",
            body or "",
        ])
    ]

    for path in attachments:
        path = Path(path)
        with open(path, "rb") as fh:
            raw = fh.read()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts.append(CRLF.join([
            f"--{boundary}",
            f'Content-Type: {ctype}; name="{path.name}"',
            "Content-Transfer-Encoding: base64",
            f'Content-Disposition: attachment; filename="{path.name}"',
            "",
            _b64_wrapped(raw),
        ]))

    body_text = CRLF.join(parts) + CRLF + f"--{boundary}--" + CRLF
    return (CRLF.join(headers) + CRLF + CRLF + body_text).encode("utf-8")


# --------------------------------------------------------------------------- #
#  Parsing
# --------------------------------------------------------------------------- #

@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes

    @property
    def size(self):
        return len(self.data)


@dataclass
class Message:
    headers: dict = field(default_factory=dict)
    body: str = ""
    attachments: list = field(default_factory=list)
    raw: bytes = b""

    def header(self, name, default=""):
        return self.headers.get(name.lower(), default)

    @property
    def sender(self):
        return self.header("from", "(unknown)")

    @property
    def recipients(self):
        return self.header("to", "")

    @property
    def subject(self):
        return self.header("subject", "(no subject)")

    @property
    def date(self):
        return self.header("date", "")


def _split_headers(raw_text):
    """Split at the first blank line and unfold continuation lines.

    A header may be wrapped across lines by starting the continuation with a
    space or tab; those have to be joined back before parsing.
    """
    if CRLF + CRLF in raw_text:
        head, body = raw_text.split(CRLF + CRLF, 1)
    elif "\n\n" in raw_text:
        head, body = raw_text.split("\n\n", 1)
    else:
        head, body = raw_text, ""

    unfolded = []
    for line in head.replace(CRLF, "\n").split("\n"):
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += " " + line.strip()
        elif line.strip():
            unfolded.append(line)

    headers = {}
    for line in unfolded:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return headers, body


def _content_type_param(value, key):
    """Pull one parameter out of a Content-Type / Content-Disposition value."""
    for chunk in value.split(";")[1:]:
        if "=" in chunk:
            name, val = chunk.split("=", 1)
            if name.strip().lower() == key:
                return val.strip().strip('"').strip("'")
    return None


def parse_message(raw):
    """Parse a raw message into headers, readable body and attachments."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    text = raw.decode("utf-8", errors="replace")
    headers, body = _split_headers(text)

    message = Message(headers=headers, body=body, raw=raw)

    content_type = headers.get("content-type", "text/plain")
    if not content_type.lower().startswith("multipart/"):
        return message

    boundary = _content_type_param(content_type, "boundary")
    if not boundary:
        return message

    # Split on the boundary delimiter; the first chunk is the preamble and the
    # last is whatever follows the closing '--boundary--', both discarded.
    chunks = body.split(f"--{boundary}")
    text_body = ""
    for chunk in chunks[1:]:
        if chunk.startswith("--"):          # closing delimiter
            break
        chunk = chunk.lstrip("\r\n")
        part_headers, part_body = _split_headers(chunk)
        part_type = part_headers.get("content-type", "text/plain")
        disposition = part_headers.get("content-disposition", "")
        encoding = part_headers.get("content-transfer-encoding", "8bit").lower()

        filename = (_content_type_param(disposition, "filename")
                    or _content_type_param(part_type, "name"))

        if filename or "attachment" in disposition.lower():
            data = part_body.strip()
            if encoding == "base64":
                try:
                    data = base64.b64decode(data)
                except (ValueError, TypeError):
                    data = data.encode("utf-8", errors="replace")
            else:
                data = data.encode("utf-8", errors="replace")
            message.attachments.append(Attachment(
                filename=filename or "attachment.bin",
                content_type=part_type.split(";")[0].strip(),
                data=data,
            ))
        elif not text_body:
            text_body = part_body.rstrip("\r\n")

    message.body = text_body
    return message


def save_attachments(message, outdir):
    """Write a parsed message's attachments to disk; returns the paths."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    written = []
    for attachment in message.attachments:
        # Strip any directory components: a filename in a message is attacker
        # controlled and '../../etc/passwd' must not escape the output folder.
        safe = os.path.basename(attachment.filename) or "attachment.bin"
        target = outdir / safe
        counter = 1
        while target.exists():
            stem, suffix = os.path.splitext(safe)
            target = outdir / f"{stem}({counter}){suffix}"
            counter += 1
        with open(target, "wb") as fh:
            fh.write(attachment.data)
        written.append(target)
    return written
