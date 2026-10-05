"""
Central configuration for the SMTP / POP3 mail system.

Everything runs on the loopback interface (127.0.0.1) so no real mail ever
leaves the machine.

A note on port numbers
----------------------
The IANA well-known ports for these protocols are 25 (SMTP) and 110 (POP3).
Ports below 1024 are privileged on Unix-like systems and need root to bind,
so this project uses 1025 and 1110 instead. The protocol logic is identical --
only the port number changes.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- Network -----------------------------------------------------------------
HOST = "127.0.0.1"          # loopback only; never exposed to the LAN
SMTP_PORT = 1025            # real SMTP is port 25
POP3_PORT = 1110            # real POP3 is port 110
SERVER_DOMAIN = "localhost"

SOCKET_TIMEOUT = 300        # seconds a server waits on an idle client
CONNECT_TIMEOUT = 10        # seconds a client waits to connect

# --- Protocol limits ---------------------------------------------------------
MAX_LINE = 1000             # RFC 5321 section 4.5.3.1: 1000 octets incl. CRLF
MAX_MESSAGE_BYTES = 10 * 1024 * 1024
MAX_RECIPIENTS = 100

# --- Storage -----------------------------------------------------------------
STORE_DIR = BASE_DIR / "store"      # store/<user>/<id>.eml
LOG_DIR = BASE_DIR / "logs"         # logs/<name>.log
USERS_FILE = BASE_DIR / "users.json"

# Accounts created on first run. Passwords are stored salted+hashed in
# users.json; these plaintext values exist only to seed the demo.
DEFAULT_ACCOUNTS = {
    "aryan": "aryan123",
    "prof": "prof123",
    "friend": "friend123",
}
