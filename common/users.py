"""
Mailbox accounts.

Passwords are verified against a random salt plus SHA-256(salt + password), so
the normal USER/PASS and AUTH LOGIN paths never need the password itself.

One exception, and it is an instructive one: users.json also keeps a plaintext
`secret` field, used only by APOP. APOP verifies MD5(banner + secret), which
the server cannot compute from a hash -- it needs the original password. That
is APOP's fatal trade-off: it protects the password on the wire at the cost of
storing it readably on disk, which is why TLS replaced it. Delete the `secret`
field and everything except APOP keeps working.

Hashing protects the file at rest, but it does not protect the wire. Both
SMTP AUTH LOGIN and POP3 USER/PASS send the password across the connection in
the clear -- AUTH LOGIN merely base64-encodes it, and base64 is an encoding,
not encryption. Anyone capturing the traffic (see docs/network/wireshark.md)
reads the password straight off the packet. The real-world answer is to run
the session inside TLS: implicit TLS on port 465/995, or STARTTLS on the
plaintext port. APOP, implemented in the POP3 server, is the historical
alternative: it sends an MD5 digest of a server-supplied timestamp plus a
shared secret, so the secret itself never crosses the wire.
"""

import hashlib
import hmac
import json
import os
import threading

try:
    from config import USERS_FILE, DEFAULT_ACCOUNTS, SERVER_DOMAIN
except ImportError:  # pragma: no cover
    from pathlib import Path
    USERS_FILE = Path(__file__).resolve().parent.parent / "users.json"
    DEFAULT_ACCOUNTS = {"aryan": "aryan123"}
    SERVER_DOMAIN = "localhost"


def hash_password(password, salt=None):
    """Return (salt, hex digest) for a password."""
    if salt is None:
        salt = os.urandom(16).hex()
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return salt, digest


def mailbox_of(address):
    """Map an address to a local mailbox name.

    'aryan@localhost' -> 'aryan';  '<Prof@Localhost>' -> 'prof'
    Only the local part is used, because every mailbox lives on this host.
    """
    if address is None:
        return ""
    address = address.strip().strip("<>").strip()
    if "@" in address:
        address = address.split("@", 1)[0]
    return address.strip().lower()


class UserDirectory:
    """The account database, backed by a small JSON file."""

    def __init__(self, path=USERS_FILE, seed=True):
        self.path = path
        self._lock = threading.Lock()
        self._users = {}
        self.load()
        if seed and not self._users:
            for name, password in DEFAULT_ACCOUNTS.items():
                self.add_user(name, password)

    # -- persistence -------------------------------------------------------- #

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self._users = json.load(fh)
        except (OSError, ValueError):
            self._users = {}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except AttributeError:      # path given as a plain string
            pass
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self._users, fh, indent=2, sort_keys=True)

    # -- queries ------------------------------------------------------------ #

    def add_user(self, username, password):
        username = mailbox_of(username)
        salt, digest = hash_password(password)
        with self._lock:
            self._users[username] = {"salt": salt, "hash": digest,
                                     "secret": password}
            self._save()
        return username

    def exists(self, username):
        return mailbox_of(username) in self._users

    def authenticate(self, username, password):
        """Verify a password using a constant-time digest comparison."""
        record = self._users.get(mailbox_of(username))
        if record is None:
            return False
        _, digest = hash_password(password, record["salt"])
        return hmac.compare_digest(digest, record["hash"])

    def apop_secret(self, username):
        """The shared secret used by the POP3 APOP digest exchange.

        APOP has to compute MD5(banner + secret) server-side, which means the
        server needs the secret itself, not just a hash of it. That is exactly
        why APOP fell out of use -- it trades wire security for a plaintext
        password database. Kept here to demonstrate the mechanism.
        """
        record = self._users.get(mailbox_of(username))
        return None if record is None else record.get("secret")

    def usernames(self):
        return sorted(self._users)

    def address(self, username):
        return f"{mailbox_of(username)}@{SERVER_DOMAIN}"
