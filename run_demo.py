"""
End-to-end demonstration: one command that exercises the whole system.

    python3 run_demo.py

It starts both servers in-process, then walks through every part of the
protocol with the full trace printed, so the terminal output can be pasted
straight into the report:

    1. SMTP  - EHLO capability negotiation
    2. SMTP  - AUTH LOGIN
    3. SMTP  - a plain message, aryan -> prof
    4. SMTP  - a message with an attachment
    5. SMTP  - one message to two recipients at once
    6. SMTP  - what the server says to a bad command sequence
    7. POP3  - STAT / LIST / UIDL / TOP / RETR
    8. POP3  - DELE without QUIT: nothing is deleted
    9. POP3  - DELE with QUIT: the UPDATE state commits it

Use --keep to leave the mail in the default store instead of running against
a throwaway directory.
"""

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config
from client.mail_client import POP3Client, POP3Error, SMTPClient, SMTPError
from common import mime, wire
from common.mailstore import MailStore
from common.trace import Tracer
from common.users import UserDirectory
from pop3_server import POP3Server
from smtp_server import SMTPServer

BAR = "=" * 74


def step(number, title):
    print(f"\n{BAR}\n  STEP {number}: {title}\n{BAR}")


def note(text):
    print(f"\n  >> {text}")


def main():
    parser = argparse.ArgumentParser(description="End-to-end SMTP/POP3 demo")
    parser.add_argument("--keep", action="store_true",
                        help="use the real store/ directory instead of a "
                             "temporary one")
    args = parser.parse_args()

    if args.keep:
        workdir = config.BASE_DIR
        store = MailStore(config.STORE_DIR)
        users = UserDirectory()
        cleanup = None
    else:
        workdir = Path(tempfile.mkdtemp(prefix="cn-demo-"))
        store = MailStore(workdir / "store")
        users = UserDirectory(workdir / "users.json")
        cleanup = workdir

    print(BAR)
    print("  COMPUTER NETWORKS PROJECT - EMAIL CLIENT USING SMTP AND POP3")
    print("  Aryan Oberoi (I041)  |  B.Tech AI  |  Division I, Batch B1")
    print(BAR)
    print(f"  Mail store : {store.root}")
    print(f"  Mailboxes  : {', '.join(users.usernames())}")

    smtp_server = SMTPServer("127.0.0.1", 0, store=store, users=users,
                             verbose=True, log_to_file=True)
    pop_server = POP3Server("127.0.0.1", 0, store=store, users=users,
                            verbose=True, log_to_file=True)
    smtp_port = smtp_server.start()
    pop_port = pop_server.start()
    print(f"  SMTP       : 127.0.0.1:{smtp_port}")
    print(f"  POP3       : 127.0.0.1:{pop_port}")

    tracer = Tracer("demo-client", to_console=True, to_file=True)

    try:
        # ---------------------------------------------------------------- #
        step(1, "SMTP - connect and negotiate capabilities with EHLO")
        smtp = SMTPClient("127.0.0.1", smtp_port, tracer)
        smtp.connect()
        smtp.ehlo("demo.client.local")
        note(f"server advertised: {', '.join(smtp.capabilities)}")

        # ---------------------------------------------------------------- #
        step(2, "SMTP - authenticate with AUTH LOGIN")
        note("username and password are base64-encoded, which is an encoding, "
             "not encryption -- both are readable in a packet capture")
        smtp.login("aryan", "aryan123")

        # ---------------------------------------------------------------- #
        step(3, "SMTP - send a plain text message (aryan -> prof)")
        smtp.send_mail(
            "aryan@localhost", ["prof@localhost"],
            "Lab 6 submission",
            "Good evening sir,\n\n"
            "Please find my Computer Networks lab submission below.\n\n"
            "Regards,\nAryan Oberoi (I041)")

        # ---------------------------------------------------------------- #
        step(4, "SMTP - send a message with an attachment (multipart/mixed)")
        attachment = workdir / "readings.csv"
        attachment.write_text("trial,rtt_ms\n1,0.41\n2,0.38\n3,0.44\n")
        note(f"attaching {attachment.name}; the file is base64-encoded into a "
             f"MIME part so it survives a text-only transport")
        smtp.send_mail(
            "aryan@localhost", ["prof@localhost"],
            "Loopback RTT readings",
            "Sir, the measurements are attached.",
            attachments=[attachment])

        # ---------------------------------------------------------------- #
        step(5, "SMTP - one message, two recipients")
        note("a single DATA transfer, but RCPT TO is issued once per "
             "recipient and the server writes one copy into each mailbox")
        smtp.send_mail(
            "aryan@localhost", ["prof@localhost", "friend@localhost"],
            "Group update",
            "Sharing this with both of you.")
        smtp.quit()

        # ---------------------------------------------------------------- #
        step(6, "SMTP - how the server answers an out-of-order command")
        note("RCPT before MAIL is a valid command at an invalid point in the "
             "conversation, so the reply is 503, not 500")
        smtp = SMTPClient("127.0.0.1", smtp_port, tracer)
        smtp.connect()
        smtp.helo("demo.client.local")
        try:
            smtp._send("RCPT TO:<prof@localhost>")
            smtp._expect(250)
        except SMTPError as exc:
            note(f"rejected as expected -> {exc}")
        note("and an unknown mailbox is a permanent 550 failure")
        try:
            smtp._send("MAIL FROM:<aryan@localhost>")
            smtp._expect(250)
            smtp._send("RCPT TO:<nosuchuser@localhost>")
            smtp._expect(250)
        except SMTPError as exc:
            note(f"rejected as expected -> {exc}")
        smtp.quit()

        # ---------------------------------------------------------------- #
        step(7, "POP3 - inspect the maildrop (STAT, LIST, UIDL, TOP, RETR)")
        pop = POP3Client("127.0.0.1", pop_port, tracer)
        pop.connect()
        note("the greeting carries the APOP timestamp banner")
        pop.login("prof", "prof123")

        count, octets = pop.stat()
        note(f"STAT -> {count} message(s), {octets} octets")

        note("LIST gives sizes without downloading anything")
        for number, size in pop.list():
            print(f"       message {number}: {size} octets")

        note("UIDL gives ids that stay the same across sessions, which is how "
             "a client implements 'leave mail on server'")
        for number, uid in pop.uidl():
            print(f"       message {number}: {uid}")

        note("TOP 1 3 fetches headers plus 3 body lines -- enough to render a "
             "message list without downloading every message in full")
        pop.top(1, 3)

        note("RETR 1 downloads the message in full")
        first = mime.parse_message(pop.retr(1))
        print(f"       From    : {first.sender}")
        print(f"       Subject : {first.subject}")
        print(f"       Received: {first.header('received')[:60]}...")

        note("message 2 carries the attachment")
        second = mime.parse_message(pop.retr(2))
        for item in second.attachments:
            print(f"       attachment: {item.filename} "
                  f"({item.size} bytes, {item.content_type})")

        # ---------------------------------------------------------------- #
        step(8, "POP3 - DELE without QUIT deletes nothing")
        note("DELE only marks a message. Dropping the connection skips the "
             "UPDATE state, so the maildrop is untouched.")
        pop.dele(1)
        print(f"       STAT now reports {pop.stat()[0]} message(s) "
              f"(the marked one is hidden)")
        pop.close()                                  # deliberate: no QUIT
        on_disk = len(store.list_messages("prof"))
        note(f"after closing without QUIT, {on_disk} message(s) are still on "
             f"disk -- nothing was lost")

        # ---------------------------------------------------------------- #
        step(9, "POP3 - DELE followed by QUIT enters UPDATE and commits")
        pop = POP3Client("127.0.0.1", pop_port, tracer)
        pop.connect()
        pop.login("prof", "prof123")
        note("RSET would undo a mark; here we mark message 1 and QUIT")
        pop.dele(1)
        pop.quit()
        remaining = len(store.list_messages("prof"))
        note(f"after QUIT the maildrop holds {remaining} message(s) "
             f"-- the UPDATE state removed one")

        # ---------------------------------------------------------------- #
        print(f"\n{BAR}\n  DEMONSTRATION COMPLETE\n{BAR}")
        print(f"  Mailbox contents now:")
        for user in users.usernames():
            count, size = store.message_count(user)
            print(f"    {user:<8} {count} message(s), {size} octets")
        print(f"\n  Protocol traces were also written to {config.LOG_DIR}")
        if cleanup is not None:
            print(f"  (this run used a temporary store; "
                  f"use --keep to write to {config.STORE_DIR})")

    finally:
        smtp_server.stop()
        pop_server.stop()
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)


if __name__ == "__main__":
    main()
