"""
The mail store: one directory per mailbox, one file per message.

    store/
      aryan/    1756... -000001.eml
      prof/     1756... -000002.eml

This is the piece that joins the two protocols. The SMTP server *writes* a
file here when it accepts a message; the POP3 server *reads* that same file
when the recipient collects their mail. Nothing else connects them -- which
is exactly how a real mail system works, where an MTA spools to disk and a
separate access server hands messages to the user agent.

Two behaviours are modelled deliberately because they are what POP3 actually
specifies (RFC 1939):

*   The maildrop is locked for the duration of a session. A second client
    trying to open the same mailbox is refused rather than shown a mailbox
    that shifts underneath it.

*   Message numbers (1..N) are assigned when the session opens and stay fixed
    for that session, even after DELE. Deleting message 1 does not renumber
    message 2.
"""

import os
import threading
import time
from pathlib import Path

try:
    from config import STORE_DIR
except ImportError:  # pragma: no cover
    STORE_DIR = Path(__file__).resolve().parent.parent / "store"


class MailboxLocked(Exception):
    """Raised when a mailbox is already open in another POP3 session."""


class MailStore:
    """Filesystem-backed mailboxes."""

    def __init__(self, root=STORE_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._locks = {}                 # mailbox name -> owner token
        self._locks_guard = threading.Lock()

    # -- paths -------------------------------------------------------------- #

    def mailbox_path(self, user):
        path = self.root / user
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _next_id(self):
        with self._counter_lock:
            self._counter += 1
            return f"{int(time.time())}-{os.getpid()}-{self._counter:06d}"

    # -- delivery (used by the SMTP server) --------------------------------- #

    def deliver(self, user, raw_message):
        """Write one message into a mailbox and return its filename.

        The file is written under a temporary name and then renamed, so a
        POP3 session listing the directory can never see a half-written
        message.
        """
        if isinstance(raw_message, str):
            raw_message = raw_message.encode("utf-8")
        box = self.mailbox_path(user)
        name = self._next_id() + ".eml"
        tmp = box / (name + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(raw_message)
        final = box / name
        os.replace(tmp, final)
        return name

    # -- reading (used by the POP3 server and the client) ------------------- #

    def list_messages(self, user):
        """Return [(number, filename, size)] ordered oldest first.

        Filenames start with a zero-padded timestamp and counter, so sorting
        the names sorts by arrival time.
        """
        box = self.mailbox_path(user)
        names = sorted(p.name for p in box.iterdir()
                       if p.is_file() and p.suffix == ".eml")
        return [(i, name, (box / name).stat().st_size)
                for i, name in enumerate(names, start=1)]

    def read_message(self, user, filename):
        with open(self.mailbox_path(user) / filename, "rb") as fh:
            return fh.read()

    def delete_message(self, user, filename):
        try:
            (self.mailbox_path(user) / filename).unlink()
            return True
        except OSError:
            return False

    def message_count(self, user):
        messages = self.list_messages(user)
        return len(messages), sum(size for _, _, size in messages)

    def uidl(self, filename):
        """A unique-id for the UIDL command: the filename minus its suffix.

        RFC 1939 requires this to be stable across sessions and never reused,
        which the timestamp+pid+counter naming already guarantees.
        """
        return filename[:-4] if filename.endswith(".eml") else filename

    # -- maildrop locking --------------------------------------------------- #

    def acquire_lock(self, user, owner):
        """Claim exclusive access to a mailbox. Raises MailboxLocked."""
        with self._locks_guard:
            holder = self._locks.get(user)
            if holder is not None and holder != owner:
                raise MailboxLocked(f"maildrop for {user} is already locked")
            self._locks[user] = owner

    def release_lock(self, user, owner):
        with self._locks_guard:
            if self._locks.get(user) == owner:
                del self._locks[user]


_default_store = None


def default_store():
    """The shared MailStore used by both servers when none is passed in."""
    global _default_store
    if _default_store is None:
        _default_store = MailStore()
    return _default_store
