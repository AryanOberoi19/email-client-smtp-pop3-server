"""
Protocol tracing.

The whole point of writing SMTP and POP3 by hand is being able to see the
conversation. Every line sent or received passes through a Tracer, which
renders it in the classic transcript notation used in the RFCs and in
textbooks:

    C:  a line the client sent
    S:  a line the server sent

The same Tracer feeds three sinks at once -- the console, a log file under
logs/, and any number of subscriber callbacks (the Tkinter GUI registers one
so its trace pane mirrors the terminal exactly).

Credentials are masked before anything is written. SMTP AUTH LOGIN and POP3
USER/PASS carry the password in the clear (base64 is an encoding, not
encryption), so an unmasked log file would ship a working password inside the
submitted project. The trace still shows that the exchange happened.
"""

import sys
import threading
from datetime import datetime

try:
    from config import LOG_DIR
except ImportError:  # pragma: no cover
    from pathlib import Path
    LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# ANSI colours; disabled automatically when output is redirected to a file.
_COLOR = {
    "C": "\033[36m",     # cyan  - client
    "S": "\033[32m",     # green - server
    "*": "\033[33m",     # yellow- informational
    "!": "\033[31m",     # red   - error
}
_RESET = "\033[0m"

# Subscribers that want to see *every* tracer's output (e.g. the GUI pane).
_global_subscribers = []
_global_lock = threading.Lock()


def subscribe_all(callback):
    """Register a callback receiving (tracer_name, kind, text) for all tracers."""
    with _global_lock:
        _global_subscribers.append(callback)
    return callback


def unsubscribe_all(callback):
    with _global_lock:
        if callback in _global_subscribers:
            _global_subscribers.remove(callback)


def _looks_like_secret(previous, line):
    """Decide whether `line` carries a credential that must not be logged."""
    upper = line.strip().upper()
    if upper.startswith("PASS ") or upper.startswith("APOP "):
        return True
    if upper.startswith("AUTH LOGIN ") or upper.startswith("AUTH PLAIN "):
        return True
    # After a '334' challenge the client replies with a bare base64 blob.
    if previous.startswith("334"):
        return True
    return False


class Tracer:
    """Renders and records one side of one protocol conversation."""

    def __init__(self, name, to_console=True, to_file=True, color=None):
        self.name = name
        self.enabled = True
        self.to_console = to_console
        self.to_file = to_file
        self.color = sys.stdout.isatty() if color is None else color
        self._subscribers = []
        self._lock = threading.Lock()
        self._last_line = ""
        self._path = None
        if to_file:
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                self._path = LOG_DIR / f"{name.split('#')[0]}.log"
            except OSError:
                self._path = None

    # -- subscriber management --------------------------------------------- #

    def subscribe(self, callback):
        self._subscribers.append(callback)
        return callback

    def unsubscribe(self, callback):
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    # -- the four things a caller records ----------------------------------- #

    def client(self, line):
        """A line that travelled client -> server."""
        self._emit("C", line)

    def server(self, line):
        """A line that travelled server -> client."""
        self._emit("S", line)

    def info(self, message):
        """A note about what the code is doing, not a protocol line."""
        self._emit("*", message)

    def error(self, message):
        self._emit("!", message)

    def data(self, raw, label="message body"):
        """Summarise a bulk transfer rather than dumping the whole thing."""
        size = len(raw) if raw is not None else 0
        lines = raw.count(b"\r\n") if isinstance(raw, bytes) else 0
        self._emit("*", f"<{label}: {size} octets, {lines} lines>")

    # -- rendering ---------------------------------------------------------- #

    def _emit(self, kind, text):
        if not self.enabled:
            return
        text = "" if text is None else str(text).rstrip("\r\n")

        if kind == "C" and _looks_like_secret(self._last_line, text):
            keyword = text.split(" ", 1)[0]
            text = f"{keyword} <credential hidden>" if " " in text else "<credential hidden>"
        if kind in ("C", "S"):
            self._last_line = text

        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        rendered = f"[{stamp}] [{self.name}] {kind}: {text}"

        with self._lock:
            if self.to_console:
                if self.color:
                    prefix = _COLOR.get(kind, "")
                    print(f"{prefix}{rendered}{_RESET}", flush=True)
                else:
                    print(rendered, flush=True)
            if self._path is not None:
                try:
                    with open(self._path, "a", encoding="utf-8") as fh:
                        fh.write(rendered + "\n")
                except OSError:
                    pass

        for callback in list(self._subscribers):
            try:
                callback(self.name, kind, text)
            except Exception:
                pass
        with _global_lock:
            watchers = list(_global_subscribers)
        for callback in watchers:
            try:
                callback(self.name, kind, text)
            except Exception:
                pass


def null_tracer(name="silent"):
    """A tracer that records nothing -- handy inside the test-suite."""
    tracer = Tracer(name, to_console=False, to_file=False, color=False)
    tracer.enabled = False
    return tracer
