"""
Menu-driven terminal mail client.

Every protocol line is printed as it goes past, so the terminal output is a
complete transcript of the SMTP and POP3 conversations.

    python3 client/cli.py                 (expects both servers running)
    python3 client/cli.py --user aryan    (skip straight past the login prompt)

Start the servers first, in two other terminals:

    python3 smtp_server.py
    python3 pop3_server.py
"""

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from client.mail_client import (MailClientError, POP3Client, SMTPClient,
                                POP3Error)
from common import mime
from common.trace import Tracer

BAR = "=" * 72


def rule(title=""):
    if title:
        print(f"\n{BAR}\n  {title}\n{BAR}")
    else:
        print(BAR)


class MailClientCLI:

    def __init__(self, host=config.HOST, smtp_port=config.SMTP_PORT,
                 pop_port=config.POP3_PORT, trace=True):
        self.host = host
        self.smtp_port = smtp_port
        self.pop_port = pop_port
        self.username = None
        self.password = None
        self.tracer = Tracer("client", to_console=True, to_file=True)
        self.tracer.enabled = trace
        # Cached from the last "check inbox", so reading a message does not
        # need a second round trip just to learn what is there.
        self.inbox = []

    # -- session state ------------------------------------------------------ #

    @property
    def address(self):
        return f"{self.username}@{config.SERVER_DOMAIN}"

    def require_login(self):
        if self.username is None:
            print("\n  You need to log in first (option 1).")
            return False
        return True

    def open_pop(self):
        """Open an authenticated POP3 session, or None if it fails."""
        client = POP3Client(self.host, self.pop_port, self.tracer)
        try:
            client.connect()
            client.login(self.username, self.password)
            return client
        except (MailClientError, OSError) as exc:
            print(f"\n  POP3 error: {exc}")
            client.close()
            return None

    # -- menu actions ------------------------------------------------------- #

    def do_login(self):
        rule("LOGIN")
        username = input("  Username: ").strip()
        if not username:
            print("  Cancelled.")
            return
        password = getpass.getpass("  Password: ")

        # Verify against the POP3 server; a successful USER/PASS proves the
        # credentials are good before we go anywhere near SMTP.
        client = POP3Client(self.host, self.pop_port, self.tracer)
        try:
            client.connect()
            client.login(username, password)
            count, octets = client.stat()
            client.quit()
        except (MailClientError, OSError) as exc:
            print(f"\n  Login failed: {exc}")
            client.close()
            return

        self.username = username
        self.password = password
        print(f"\n  Logged in as {self.address} "
              f"({count} message(s), {octets} octets)")

    def do_send(self):
        if not self.require_login():
            return
        rule("COMPOSE")
        to = input(f"  To (comma separated) [{config.SERVER_DOMAIN} users]: ")
        recipients = [r.strip() for r in to.split(",") if r.strip()]
        if not recipients:
            print("  No recipients; cancelled.")
            return
        recipients = [r if "@" in r else f"{r}@{config.SERVER_DOMAIN}"
                      for r in recipients]

        subject = input("  Subject: ").strip()
        print("  Body (finish with a single '.' on its own line):")
        lines = []
        while True:
            line = input("  | ")
            if line.strip() == ".":
                break
            lines.append(line)
        body = "\n".join(lines)

        attachments = []
        paths = input("  Attach file(s), comma separated (blank for none): ")
        for raw in paths.split(","):
            raw = raw.strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            if path.is_file():
                attachments.append(path)
                print(f"    attached {path.name} ({path.stat().st_size} bytes)")
            else:
                print(f"    skipping {raw!r}: not a file")

        rule("SMTP SESSION")
        client = SMTPClient(self.host, self.smtp_port, self.tracer)
        try:
            client.connect()
            client.ehlo()
            client.login(self.username, self.password)
            queue_id = client.send_mail(self.address, recipients, subject,
                                        body, attachments)
            client.quit()
            print(f"\n  Sent to {', '.join(recipients)}  [{queue_id}]")
        except (MailClientError, OSError) as exc:
            print(f"\n  Send failed: {exc}")
            client.close()

    def do_inbox(self):
        if not self.require_login():
            return
        rule("POP3 SESSION - LIST")
        client = self.open_pop()
        if client is None:
            return
        try:
            listing = client.list()
            uids = dict(client.uidl())
            self.inbox = []
            for number, size in listing:
                # TOP n 0 fetches headers only -- enough for a list view
                # without downloading every message in full.
                headers = mime.parse_message(client.top(number, 0))
                self.inbox.append((number, size, headers, uids.get(number, "")))
        except MailClientError as exc:
            print(f"\n  POP3 error: {exc}")
            client.close()
            return
        client.quit()

        rule(f"INBOX - {self.address}")
        if not self.inbox:
            print("  (empty)")
            return
        print(f"  {'#':>3}  {'From':<26} {'Subject':<28} {'Size':>7}")
        print(f"  {'-' * 3}  {'-' * 26} {'-' * 28} {'-' * 7}")
        for number, size, headers, _ in self.inbox:
            print(f"  {number:>3}  {headers.sender[:26]:<26} "
                  f"{headers.subject[:28]:<28} {size:>7}")

    def do_read(self):
        if not self.require_login():
            return
        number = input("\n  Message number to read: ").strip()
        if not number.isdigit():
            print("  Not a number.")
            return

        rule("POP3 SESSION - RETR")
        client = self.open_pop()
        if client is None:
            return
        try:
            raw = client.retr(int(number))
        except MailClientError as exc:
            print(f"\n  {exc}")
            client.close()
            return
        client.quit()

        message = mime.parse_message(raw)
        rule(f"MESSAGE {number}")
        print(f"  From    : {message.sender}")
        print(f"  To      : {message.recipients}")
        print(f"  Subject : {message.subject}")
        print(f"  Date    : {message.date}")
        if message.attachments:
            names = ", ".join(f"{a.filename} ({a.size} B)"
                              for a in message.attachments)
            print(f"  Files   : {names}")
        print(f"  {'-' * 68}")
        for line in message.body.replace("\r\n", "\n").split("\n"):
            print(f"  {line}")
        print(f"  {'-' * 68}")
        self._last_message = message

    def do_delete(self):
        if not self.require_login():
            return
        number = input("\n  Message number to delete: ").strip()
        if not number.isdigit():
            print("  Not a number.")
            return

        rule("POP3 SESSION - DELE")
        client = self.open_pop()
        if client is None:
            return
        try:
            client.dele(int(number))
        except MailClientError as exc:
            print(f"\n  {exc}")
            client.close()
            return
        # DELE only marks. The QUIT below enters the UPDATE state, which is
        # where the message is actually removed from the maildrop.
        print("\n  Marked for deletion; sending QUIT to commit (UPDATE state)")
        client.quit()
        print(f"  Message {number} deleted.")

    def do_stats(self):
        if not self.require_login():
            return
        rule("POP3 SESSION - STAT")
        client = self.open_pop()
        if client is None:
            return
        try:
            count, octets = client.stat()
            capabilities = client.capa()
        except MailClientError as exc:
            print(f"\n  {exc}")
            client.close()
            return
        client.quit()
        print(f"\n  Mailbox   : {self.address}")
        print(f"  Messages  : {count}")
        print(f"  Total size: {octets} octets")
        print(f"  Server CAPA: {', '.join(capabilities)}")

    def do_save_attachments(self):
        if not self.require_login():
            return
        number = input("\n  Message number: ").strip()
        if not number.isdigit():
            print("  Not a number.")
            return
        outdir = input("  Save into directory [./attachments]: ").strip()
        outdir = Path(outdir or (config.BASE_DIR / "attachments")).expanduser()

        rule("POP3 SESSION - RETR (for attachments)")
        client = self.open_pop()
        if client is None:
            return
        try:
            raw = client.retr(int(number))
        except MailClientError as exc:
            print(f"\n  {exc}")
            client.close()
            return
        client.quit()

        message = mime.parse_message(raw)
        if not message.attachments:
            print("\n  That message has no attachments.")
            return
        for path in mime.save_attachments(message, outdir):
            print(f"  saved {path}")

    def do_toggle_trace(self):
        self.tracer.enabled = not self.tracer.enabled
        state = "ON" if self.tracer.enabled else "OFF"
        print(f"\n  Protocol trace is now {state}.")

    # -- main loop ---------------------------------------------------------- #

    MENU = """
  1. Login                    5. Delete message
  2. Send mail (SMTP)         6. Mailbox statistics
  3. Check inbox (POP3)       7. Save attachments
  4. Read message             8. Toggle protocol trace
                              9. Exit
"""

    def run(self):
        rule("CN PROJECT - MAIL CLIENT (localhost)")
        print(f"  SMTP {self.host}:{self.smtp_port}   "
              f"POP3 {self.host}:{self.pop_port}")

        actions = {
            "1": self.do_login,
            "2": self.do_send,
            "3": self.do_inbox,
            "4": self.do_read,
            "5": self.do_delete,
            "6": self.do_stats,
            "7": self.do_save_attachments,
            "8": self.do_toggle_trace,
        }

        while True:
            who = self.address if self.username else "not logged in"
            print(f"\n{BAR}\n  Mail client  [{who}]{self.MENU}{BAR}")
            try:
                choice = input("  Choice: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  Bye.")
                return
            if choice == "9":
                print("\n  Bye.")
                return
            action = actions.get(choice)
            if action is None:
                print("  Pick a number from the menu.")
                continue
            try:
                action()
            except (EOFError, KeyboardInterrupt):
                print("\n  Cancelled.")
            except ConnectionRefusedError:
                print("\n  Connection refused -- are the servers running?")
                print("  Start them with:  python3 smtp_server.py")
                print("                    python3 pop3_server.py")
            except MailClientError as exc:
                print(f"\n  Protocol error: {exc}")


def main():
    parser = argparse.ArgumentParser(description="SMTP/POP3 mail client (CLI)")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--smtp-port", type=int, default=config.SMTP_PORT)
    parser.add_argument("--pop-port", type=int, default=config.POP3_PORT)
    parser.add_argument("--user", help="log in as this user on startup")
    parser.add_argument("--password", help="password for --user (prompted if omitted)")
    parser.add_argument("--no-trace", action="store_true",
                        help="start with the protocol trace switched off")
    args = parser.parse_args()

    cli = MailClientCLI(args.host, args.smtp_port, args.pop_port,
                        trace=not args.no_trace)
    if args.user:
        password = args.password or getpass.getpass(f"Password for {args.user}: ")
        client = POP3Client(args.host, args.pop_port, cli.tracer)
        try:
            client.connect()
            client.login(args.user, password)
            client.quit()
            cli.username, cli.password = args.user, password
            print(f"Logged in as {cli.address}")
        except (POP3Error, OSError) as exc:
            print(f"Startup login failed: {exc}")
            client.close()
    cli.run()


if __name__ == "__main__":
    main()
