"""
Test suite for the SMTP/POP3 mail system.

Run with:   python3 -m unittest discover tests -v

Three groups:
  * unit tests for the wire layer, MIME handling and the account store
  * integration tests that boot both servers on ephemeral ports and send a
    real message through them
  * negative tests that check the servers refuse bad input with the right
    reply codes, which is where most of the protocol logic actually lives
"""

import json
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from client.mail_client import POP3Client, POP3Error, SMTPClient, SMTPError
from common import mime, wire
from common.mailstore import MailboxLocked, MailStore
from common.trace import null_tracer
from common.users import UserDirectory, mailbox_of
from pop3_server import POP3Server
from smtp_server import SMTPServer


# --------------------------------------------------------------------------- #
#  Unit tests: the wire layer
# --------------------------------------------------------------------------- #

class FakeSocket:
    """A socket whose recv() hands back the stream in awkward little pieces.

    TCP gives no guarantee that one recv() returns one line, so the reader has
    to cope with a line arriving across several reads -- and with several
    lines arriving in one. Feeding it two bytes at a time proves it does.
    """

    def __init__(self, data, chunk_size=2):
        self.data = data
        self.pos = 0
        self.chunk_size = chunk_size

    def recv(self, _bufsize):
        chunk = self.data[self.pos:self.pos + self.chunk_size]
        self.pos += len(chunk)
        return chunk

    def settimeout(self, _timeout):
        pass


class TestWireLayer(unittest.TestCase):

    def test_reads_lines_split_across_recv_calls(self):
        stream = b"HELO one\r\nMAIL FROM:<a@b>\r\nQUIT\r\n"
        reader = wire.LineReader(FakeSocket(stream, chunk_size=3))
        self.assertEqual(reader.read_line(), "HELO one")
        self.assertEqual(reader.read_line(), "MAIL FROM:<a@b>")
        self.assertEqual(reader.read_line(), "QUIT")

    def test_reads_several_lines_from_one_recv(self):
        stream = b"STAT\r\nLIST\r\n"
        reader = wire.LineReader(FakeSocket(stream, chunk_size=4096))
        self.assertEqual(reader.read_line(), "STAT")
        self.assertEqual(reader.read_line(), "LIST")

    def test_bare_lf_is_not_a_line_ending(self):
        # str.splitlines() would wrongly break this into two lines. Only the
        # two-byte CRLF sequence terminates a protocol line.
        stream = b"Subject: a\nb\r\n"
        reader = wire.LineReader(FakeSocket(stream, chunk_size=1))
        self.assertEqual(reader.read_line(), "Subject: a\nb")

    def test_closed_connection_raises(self):
        reader = wire.LineReader(FakeSocket(b"PARTIAL", chunk_size=2))
        with self.assertRaises(wire.ConnectionClosed):
            reader.read_line()

    def test_overlong_line_raises(self):
        reader = wire.LineReader(FakeSocket(b"x" * 5000, chunk_size=512),
                                 max_line=1000)
        with self.assertRaises(wire.LineTooLong):
            reader.read_line()

    def test_dot_stuffing_round_trip(self):
        body = b"first line\r\n.\r\n..still not the end\r\nlast line\r\n"
        stuffed = wire.dot_stuff(body)
        self.assertIn(b"\r\n..\r\n", stuffed)          # the lone dot escaped
        self.assertEqual(wire.dot_unstuff(stuffed), body)

    def test_body_containing_a_lone_dot_survives_the_round_trip(self):
        # The failure this guards against: a message whose body contains a
        # line of just "." would otherwise truncate on delivery.
        body = b"Regards,\r\n.\r\nAryan\r\n"
        sock = FakeSocket(wire.dot_stuff(body) + b".\r\n", chunk_size=5)
        received = wire.LineReader(sock).read_dot_terminated()
        self.assertEqual(received, body)

    def test_normalize_crlf_handles_mixed_endings(self):
        self.assertEqual(wire.normalize_crlf("a\nb\r\nc\rd"),
                         b"a\r\nb\r\nc\r\nd")


# --------------------------------------------------------------------------- #
#  Unit tests: MIME
# --------------------------------------------------------------------------- #

class TestMime(unittest.TestCase):

    def test_plain_message_round_trip(self):
        raw = mime.build_message("aryan@localhost", ["prof@localhost"],
                                 "Lab 6", "Hello sir,\nAttached below.")
        parsed = mime.parse_message(raw)
        self.assertEqual(parsed.sender, "aryan@localhost")
        self.assertEqual(parsed.subject, "Lab 6")
        self.assertIn("Hello sir,", parsed.body)
        self.assertEqual(parsed.attachments, [])

    def test_headers_are_case_insensitive(self):
        parsed = mime.parse_message(b"SuBjEcT: Mixed\r\n\r\nbody\r\n")
        self.assertEqual(parsed.subject, "Mixed")

    def test_folded_header_is_unfolded(self):
        parsed = mime.parse_message(
            b"Subject: a very long\r\n  continued subject\r\n\r\nbody\r\n")
        self.assertEqual(parsed.subject, "a very long continued subject")

    def test_attachment_round_trip_preserves_binary_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = bytes(range(256)) * 8       # every byte value, twice over
            path = Path(tmp) / "report.bin"
            path.write_bytes(payload)

            raw = mime.build_message("aryan@localhost", ["prof@localhost"],
                                     "With attachment", "See attached.",
                                     attachments=[path])
            parsed = mime.parse_message(raw)

            self.assertIn("See attached.", parsed.body)
            self.assertEqual(len(parsed.attachments), 1)
            attachment = parsed.attachments[0]
            self.assertEqual(attachment.filename, "report.bin")
            self.assertEqual(attachment.data, payload)

    def test_multiple_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.txt"
            second = Path(tmp) / "b.txt"
            first.write_text("alpha")
            second.write_text("beta")
            raw = mime.build_message("a@localhost", ["b@localhost"], "Two",
                                     "body", attachments=[first, second])
            parsed = mime.parse_message(raw)
            self.assertEqual([a.filename for a in parsed.attachments],
                             ["a.txt", "b.txt"])
            self.assertEqual(parsed.attachments[1].data, b"beta")

    def test_save_attachments_cannot_escape_the_output_directory(self):
        message = mime.Message(attachments=[
            mime.Attachment("../../escaped.txt", "text/plain", b"nope")])
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp) / "out"
            written = mime.save_attachments(message, outdir)
            self.assertEqual(written[0].parent, outdir)
            self.assertEqual(written[0].name, "escaped.txt")

    def test_date_header_format(self):
        # e.g. 'Fri, 29 Aug 2026 03:40:00 +0530'
        date = mime.rfc5322_date()
        self.assertRegex(
            date,
            r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} "
            r"\d{2}:\d{2}:\d{2} [+-]\d{4}$")


# --------------------------------------------------------------------------- #
#  Unit tests: accounts and the mail store
# --------------------------------------------------------------------------- #

class TestUsersAndStore(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.users = UserDirectory(self.tmp / "users.json")
        self.store = MailStore(self.tmp / "store")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_authentication_uses_a_salted_hash(self):
        self.users.add_user("tester", "s3cret")
        record = json.loads((self.tmp / "users.json").read_text())["tester"]
        self.assertNotEqual(record["hash"], "s3cret")
        self.assertEqual(len(record["hash"]), 64)         # SHA-256 hex digest
        self.assertTrue(self.users.authenticate("tester", "s3cret"))
        self.assertFalse(self.users.authenticate("tester", "wrong"))

    def test_two_users_with_the_same_password_get_different_hashes(self):
        # This is what the per-user salt buys: identical passwords must not
        # produce identical digests, or the file leaks which accounts match.
        self.users.add_user("one", "same-password")
        self.users.add_user("two", "same-password")
        records = json.loads((self.tmp / "users.json").read_text())
        self.assertNotEqual(records["one"]["hash"], records["two"]["hash"])

    def test_mailbox_of_strips_domain_and_brackets(self):
        self.assertEqual(mailbox_of("<Aryan@Localhost>"), "aryan")
        self.assertEqual(mailbox_of("prof"), "prof")

    def test_deliver_then_list(self):
        self.store.deliver("prof", b"Subject: one\r\n\r\nbody\r\n")
        self.store.deliver("prof", b"Subject: two\r\n\r\nbody\r\n")
        messages = self.store.list_messages("prof")
        self.assertEqual([n for n, _, _ in messages], [1, 2])

    def test_maildrop_lock_is_exclusive(self):
        self.store.acquire_lock("prof", owner=1)
        with self.assertRaises(MailboxLocked):
            self.store.acquire_lock("prof", owner=2)
        self.store.release_lock("prof", owner=1)
        self.store.acquire_lock("prof", owner=2)      # now free


# --------------------------------------------------------------------------- #
#  Integration: both servers, a real client, a real message
# --------------------------------------------------------------------------- #

class ServerTestCase(unittest.TestCase):
    """Boots an SMTP and a POP3 server on ephemeral ports over a temp store."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = MailStore(self.tmp / "store")
        self.users = UserDirectory(self.tmp / "users.json")
        self.smtp_server = SMTPServer("127.0.0.1", 0, store=self.store,
                                      users=self.users, verbose=False,
                                      log_to_file=False)
        self.pop_server = POP3Server("127.0.0.1", 0, store=self.store,
                                     users=self.users, verbose=False,
                                     log_to_file=False)
        self.smtp_port = self.smtp_server.start()
        self.pop_port = self.pop_server.start()

    def tearDown(self):
        self.smtp_server.stop()
        self.pop_server.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------ #

    def smtp(self):
        client = SMTPClient("127.0.0.1", self.smtp_port, null_tracer())
        client.connect()
        client.ehlo("test.local")
        return client

    def pop(self, user="prof", password="prof123"):
        client = POP3Client("127.0.0.1", self.pop_port, null_tracer())
        client.connect()
        client.login(user, password)
        return client

    def raw(self, port):
        """A bare socket plus a reader, for poking at the protocol directly."""
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        reader = wire.LineReader(sock, timeout=5)
        reader.read_line()                       # consume the greeting
        return sock, reader

    def say(self, sock, reader, command):
        wire.send_line(sock, command)
        return reader.read_line()


class TestEndToEnd(ServerTestCase):

    def test_send_then_receive(self):
        client = self.smtp()
        client.login("aryan", "aryan123")
        client.send_mail("aryan@localhost", ["prof@localhost"],
                         "Integration test", "Body of the message.")
        client.quit()

        pop = self.pop()
        count, octets = pop.stat()
        self.assertEqual(count, 1)
        self.assertGreater(octets, 0)
        parsed = mime.parse_message(pop.retr(1))
        pop.quit()

        self.assertEqual(parsed.subject, "Integration test")
        self.assertIn("Body of the message.", parsed.body)
        # The SMTP server must have stamped its own trace header on delivery.
        self.assertIn("CN-Project SMTP", parsed.header("received"))

    def test_attachment_survives_the_full_round_trip(self):
        payload = bytes(range(256))
        path = self.tmp / "data.bin"
        path.write_bytes(payload)

        client = self.smtp()
        client.send_mail("aryan@localhost", ["prof@localhost"],
                         "With file", "See attached.", attachments=[path])
        client.quit()

        pop = self.pop()
        parsed = mime.parse_message(pop.retr(1))
        pop.quit()

        self.assertEqual(len(parsed.attachments), 1)
        self.assertEqual(parsed.attachments[0].data, payload)

    def test_body_with_a_lone_dot_line_is_not_truncated(self):
        client = self.smtp()
        client.send_mail("aryan@localhost", ["prof@localhost"], "Dots",
                         "before\n.\nafter")
        client.quit()

        pop = self.pop()
        parsed = mime.parse_message(pop.retr(1))
        pop.quit()
        self.assertIn("before", parsed.body)
        self.assertIn("after", parsed.body)

    def test_one_message_two_recipients_is_delivered_twice(self):
        client = self.smtp()
        client.send_mail("aryan@localhost",
                         ["prof@localhost", "friend@localhost"],
                         "Broadcast", "To both of you.")
        client.quit()

        for user, password in (("prof", "prof123"), ("friend", "friend123")):
            pop = self.pop(user, password)
            self.assertEqual(pop.stat()[0], 1)
            pop.quit()

    def test_unknown_recipient_is_skipped_but_valid_one_still_gets_mail(self):
        client = self.smtp()
        client.send_mail("aryan@localhost",
                         ["ghost@localhost", "prof@localhost"],
                         "Partial", "body")
        client.quit()

        pop = self.pop()
        self.assertEqual(pop.stat()[0], 1)
        pop.quit()


class TestPop3Semantics(ServerTestCase):

    def deliver(self, count=3, user="prof"):
        for i in range(1, count + 1):
            self.store.deliver(user, f"Subject: msg {i}\r\n\r\nbody {i}\r\n"
                               .encode())

    def test_dele_without_quit_leaves_the_message_on_disk(self):
        # Dropping the connection skips the UPDATE state, so nothing is
        # removed -- this is the behaviour RFC 1939 specifies.
        self.deliver(2)
        pop = self.pop()
        pop.dele(1)
        pop.close()                            # close without QUIT
        self.assertEqual(len(self.store.list_messages("prof")), 2)

    def test_dele_then_quit_removes_the_message(self):
        self.deliver(2)
        pop = self.pop()
        pop.dele(1)
        pop.quit()
        self.assertEqual(len(self.store.list_messages("prof")), 1)

    def test_rset_unmarks_deletions(self):
        self.deliver(2)
        pop = self.pop()
        pop.dele(1)
        self.assertEqual(pop.stat()[0], 1)     # hidden from STAT
        pop.rset()
        self.assertEqual(pop.stat()[0], 2)     # and back again
        pop.quit()
        self.assertEqual(len(self.store.list_messages("prof")), 2)

    def test_message_numbers_do_not_shift_after_dele(self):
        self.deliver(3)
        pop = self.pop()
        pop.dele(1)
        # Message 2 is still message 2; it does not slide down into slot 1.
        parsed = mime.parse_message(pop.retr(2))
        self.assertEqual(parsed.subject, "msg 2")
        with self.assertRaises(POP3Error):
            pop.retr(1)                        # deleted, so inaccessible
        pop.quit()

    def test_uidl_is_stable_across_sessions(self):
        self.deliver(2)
        pop = self.pop()
        first = pop.uidl()
        pop.quit()
        pop = self.pop()
        second = pop.uidl()
        pop.quit()
        self.assertEqual(first, second)

    def test_top_returns_headers_and_limited_body(self):
        self.store.deliver("prof",
                           b"Subject: long\r\n\r\nl1\r\nl2\r\nl3\r\nl4\r\n")
        pop = self.pop()
        extract = pop.top(1, 2).decode()
        pop.quit()
        self.assertIn("Subject: long", extract)
        self.assertIn("l2", extract)
        self.assertNotIn("l4", extract)

    def test_maildrop_is_locked_for_the_session(self):
        self.deliver(1)
        first = self.pop()
        second = POP3Client("127.0.0.1", self.pop_port, null_tracer())
        second.connect()
        with self.assertRaises(POP3Error):
            second.login("prof", "prof123")
        second.close()
        first.quit()

    def test_stat_on_an_empty_maildrop(self):
        pop = self.pop()
        self.assertEqual(pop.stat(), (0, 0))
        pop.quit()


# --------------------------------------------------------------------------- #
#  Negative tests: the servers must refuse bad input correctly
# --------------------------------------------------------------------------- #

class TestSmtpErrors(ServerTestCase):

    def test_rcpt_before_mail_is_503(self):
        sock, reader = self.raw(self.smtp_port)
        self.say(sock, reader, "HELO test")
        reply = self.say(sock, reader, "RCPT TO:<prof@localhost>")
        self.assertTrue(reply.startswith("503"), reply)
        sock.close()

    def test_mail_before_helo_is_503(self):
        sock, reader = self.raw(self.smtp_port)
        reply = self.say(sock, reader, "MAIL FROM:<aryan@localhost>")
        self.assertTrue(reply.startswith("503"), reply)
        sock.close()

    def test_unknown_command_is_500(self):
        sock, reader = self.raw(self.smtp_port)
        reply = self.say(sock, reader, "TELEPORT now")
        self.assertTrue(reply.startswith("500"), reply)
        sock.close()

    def test_malformed_mail_from_is_501(self):
        sock, reader = self.raw(self.smtp_port)
        self.say(sock, reader, "HELO test")
        reply = self.say(sock, reader, "MAIL aryan@localhost")
        self.assertTrue(reply.startswith("501"), reply)
        sock.close()

    def test_unknown_recipient_is_550(self):
        sock, reader = self.raw(self.smtp_port)
        self.say(sock, reader, "HELO test")
        self.say(sock, reader, "MAIL FROM:<aryan@localhost>")
        reply = self.say(sock, reader, "RCPT TO:<nobody@localhost>")
        self.assertTrue(reply.startswith("550"), reply)
        sock.close()

    def test_data_before_rcpt_is_503(self):
        sock, reader = self.raw(self.smtp_port)
        self.say(sock, reader, "HELO test")
        self.say(sock, reader, "MAIL FROM:<aryan@localhost>")
        reply = self.say(sock, reader, "DATA")
        self.assertTrue(reply.startswith("503"), reply)
        sock.close()

    def test_bad_credentials_are_535(self):
        client = SMTPClient("127.0.0.1", self.smtp_port, null_tracer())
        client.connect()
        client.ehlo("test.local")
        with self.assertRaises(SMTPError) as ctx:
            client.login("aryan", "wrong-password")
        self.assertEqual(ctx.exception.code, 535)
        client.close()

    def test_require_auth_rejects_unauthenticated_mail(self):
        server = SMTPServer("127.0.0.1", 0, store=self.store, users=self.users,
                            require_auth=True, verbose=False, log_to_file=False)
        port = server.start()
        try:
            sock, reader = self.raw(port)
            self.say(sock, reader, "HELO test")
            reply = self.say(sock, reader, "MAIL FROM:<aryan@localhost>")
            self.assertTrue(reply.startswith("530"), reply)
            sock.close()
        finally:
            server.stop()

    def test_rset_clears_a_transaction_in_progress(self):
        sock, reader = self.raw(self.smtp_port)
        self.say(sock, reader, "HELO test")
        self.say(sock, reader, "MAIL FROM:<aryan@localhost>")
        self.say(sock, reader, "RSET")
        reply = self.say(sock, reader, "RCPT TO:<prof@localhost>")
        self.assertTrue(reply.startswith("503"), reply)
        sock.close()


class TestPop3Errors(ServerTestCase):

    def test_bad_password_is_err(self):
        client = POP3Client("127.0.0.1", self.pop_port, null_tracer())
        client.connect()
        with self.assertRaises(POP3Error):
            client.login("prof", "wrong-password")
        client.close()

    def test_transaction_command_before_login_is_err(self):
        sock, reader = self.raw(self.pop_port)
        reply = self.say(sock, reader, "STAT")
        self.assertTrue(reply.startswith("-ERR"), reply)
        sock.close()

    def test_retr_out_of_range_is_err(self):
        pop = self.pop()
        with self.assertRaises(POP3Error):
            pop.retr(99)
        pop.quit()

    def test_unknown_command_is_err(self):
        sock, reader = self.raw(self.pop_port)
        reply = self.say(sock, reader, "FETCH 1")
        self.assertTrue(reply.startswith("-ERR"), reply)
        sock.close()

    def test_apop_logs_in_without_sending_the_password(self):
        self.store.deliver("prof", b"Subject: x\r\n\r\nbody\r\n")
        client = POP3Client("127.0.0.1", self.pop_port, null_tracer())
        client.connect()
        client.apop("prof", "prof123")
        self.assertEqual(client.stat()[0], 1)
        client.quit()

    def test_apop_with_a_wrong_secret_is_refused(self):
        client = POP3Client("127.0.0.1", self.pop_port, null_tracer())
        client.connect()
        with self.assertRaises(POP3Error):
            client.apop("prof", "not-the-secret")
        client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
