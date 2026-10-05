# Email Client Using SMTP and POP3

**Computer Networks project**

A complete mail system running entirely on `127.0.0.1`: an SMTP server, a POP3
server and a mail client, all written directly on TCP sockets with no external
dependencies and without using Python's `smtplib`, `poplib` or `email`
modules. Every protocol line is printed as it goes past.

---

## What it does

```text
        ┌──────────────────────────────────────┐
        │   Client   (CLI  ·  Tkinter GUI)     │
        └────────┬────────────────────┬────────┘
       SMTPClient│                    │POP3Client
                 ▼                    ▼
    127.0.0.1:1025                 127.0.0.1:1110
      smtp_server.py               pop3_server.py
      RFC 5321                     RFC 1939
                 │                    │
                 └────►  store/  ◄────┘
                      <user>/*.eml
```

The SMTP server writes a message file into the recipient's mailbox; the POP3
server later reads that same file back out. Nothing else connects them — which
is how a real mail system works, where the transfer agent spools to disk and a
separate access server hands messages to the user.

| Protocol | Direction | Implemented |
| --- | --- | --- |
| SMTP | push, client → server | `HELO` `EHLO` `AUTH LOGIN/PLAIN` `MAIL FROM` `RCPT TO` `DATA` `RSET` `NOOP` `VRFY` `HELP` `QUIT` |
| POP3 | pull, server → client | `USER` `PASS` `APOP` `CAPA` `STAT` `LIST` `UIDL` `RETR` `TOP` `DELE` `RSET` `NOOP` `QUIT` |

Also: multiple mailboxes with salted SHA-256 password verification, MIME `multipart/mixed`
attachments built and parsed by hand, correct dot-stuffing, maildrop locking,
and the POP3 three-state model where `DELE` only marks and `QUIT` commits.

---

## Requirements

Python 3.8+ and nothing else. `tkinter` ships with the standard installer and
is only needed for the GUI.

Optional: the modern-looking GUI (`client/gui_modern.py`) needs
`pip install customtkinter`. It is the only third-party package anywhere in the
project, and nothing else imports it. If it is missing, `gui_modern.py` opens
the standard Tkinter GUI instead.

```bash
python3 --version
```

## Quick start

**The fastest way to see everything work** — starts both servers in-process
and walks the whole protocol with the trace printed:

```bash
python3 run_demo.py
```

**The normal way**, three terminals:

```bash
# terminal 1
python3 smtp_server.py

# terminal 2
python3 pop3_server.py

# terminal 3
python3 client/cli.py          # or:  python3 client/gui.py
                               # or:  python3 client/gui_modern.py
```

**Run the tests:**

```bash
python3 -m unittest discover tests -v      # 48 tests
```

## Default accounts

Created automatically in `users.json` on first run.

| Mailbox | Password | Address |
| --- | --- | --- |
| `aryan` | `aryan123` | `aryan@localhost` |
| `prof` | `prof123` | `prof@localhost` |
| `friend` | `friend123` | `friend@localhost` |

## Ports

| | This project | Real world |
| --- | --- | --- |
| SMTP | 1025 | 25 (587 for submission, 465 implicit TLS) |
| POP3 | 1110 | 110 (995 implicit TLS) |

Ports below 1024 need root to bind, so the project uses unprivileged ones. The
protocol logic is identical — only the number changes. Override with
`--port`.

---

## Talking to the servers by hand

The best proof that these are real protocol implementations is that a generic
tool can drive them. Type the commands yourself:

```text
$ nc 127.0.0.1 1025
220 localhost Simple SMTP Service Ready (CN Project)
HELO test.local
250 localhost Hello test.local [127.0.0.1]
MAIL FROM:<aryan@localhost>
250 2.1.0 Sender <aryan@localhost> OK
RCPT TO:<prof@localhost>
250 2.1.5 Recipient <prof@localhost> OK
DATA
354 End data with <CRLF>.<CRLF>
Subject: Typed by hand

Hello sir.
.
250 2.0.0 OK: queued as 1787955154-8621-000001
QUIT
221 2.0.0 localhost closing connection
```

Then collect it:

```text
$ nc 127.0.0.1 1110
+OK POP3 server ready (CN Project) <8633.1787955193783@localhost>
USER prof
+OK user prof accepted, send PASS
PASS prof123
+OK maildrop has 1 message(s) (217 octets)
STAT
+OK 1 217
RETR 1
+OK 217 octets
...the message...
.
QUIT
+OK localhost POP3 server signing off (0 message(s) deleted)
```

Try getting it wrong on purpose — `RCPT TO:` before `MAIL FROM:` returns
`503`, an unknown mailbox returns `550`, and a command that does not exist
returns `500`. Those three replies are the state machine talking.

---

## Project layout

```text
Project/
├── config.py                 ports, paths, limits
├── smtp_server.py            SMTP server + state machine    (RFC 5321)
├── pop3_server.py            POP3 server + state machine    (RFC 1939)
├── run_demo.py               scripted end-to-end demo
├── users.json                accounts (created on first run)
├── common/
│   ├── wire.py               socket line I/O, CRLF, dot-stuffing
│   ├── trace.py              the C:/S: protocol tracer
│   ├── mailstore.py          mailboxes on disk, maildrop locking
│   ├── mime.py               message build/parse, attachments
│   └── users.py              accounts, password hashing
├── client/
│   ├── mail_client.py        SMTPClient and POP3Client
│   ├── cli.py                menu-driven terminal client
│   ├── gui.py                Tkinter client (standard library only)
│   └── gui_modern.py         webmail-style client (needs customtkinter)
├── tests/test_project.py     48 unit / integration / negative tests
├── docs/
│   ├── report/Project Report_CN.ipynb   the executed project report
│   └── network/wireshark.md  capturing and reading the packets
├── store/                    the mailboxes
└── logs/                     protocol traces, one file per component
```

## Command-line options

```bash
python3 smtp_server.py  [--host H] [--port N] [--require-auth] [--quiet]
python3 pop3_server.py  [--host H] [--port N] [--quiet]
python3 client/cli.py   [--host H] [--smtp-port N] [--pop-port N]
                        [--user U] [--password P] [--no-trace]
python3 run_demo.py     [--keep]     # --keep writes to store/ not a temp dir
```

`--require-auth` makes the SMTP server refuse `MAIL FROM` until the client has
authenticated — the difference between an open relay and a submission server.

---

## Things the code demonstrates deliberately

**The envelope is not the headers.** `MAIL FROM` / `RCPT TO` are what the
server routes on; the `From:` and `To:` lines inside `DATA` are just text the
server never reads. They can disagree completely. That gap is the mechanism
behind email spoofing.

**`DELE` does not delete.** POP3 only marks the message. Deletion happens in
the UPDATE state, entered at `QUIT`. Drop the connection instead and nothing
is lost — deliberate, so a client that crashes mid-collection loses no mail.
`run_demo.py` steps 8 and 9 show both outcomes.

**Dot-stuffing.** A lone `.` on a line ends the data transfer, so a message
body containing such a line must escape it. Without this, a message would
silently truncate. `tests/test_project.py` covers exactly that case.

**CRLF and only CRLF.** Python's `splitlines()` also breaks on a bare `\n`,
`\r`, `\v` and `\f`, which corrupts message bodies. `common/wire.py` scans for
the two-byte sequence explicitly.

**Passwords cross the wire in the clear.** `AUTH LOGIN` base64-encodes them,
which is an encoding, not encryption. `docs/network/wireshark.md` shows how to
read one straight out of a packet capture. TLS is the real answer; `APOP`
(implemented in the POP3 server) was the historical alternative — and it
carries its own trade-off, since verifying an APOP digest forces the server to
keep the password in plaintext on disk. That is exactly why it was abandoned.

The trace logger masks credentials before writing to `logs/`, so a submitted
project does not ship a working password.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Address already in use` | A server is already running. `lsof -i :1025` then `kill <pid>`, or use `--port`. |
| `Connection refused` in the client | The servers are not running. Start them first. |
| Client hangs after a command | A line was sent without CRLF. `nc` handles this; a hand-rolled `telnet` on some systems does not. |
| `no such user here` (550) | The recipient mailbox does not exist. Valid names are in `users.json`. |
| `maildrop already locked` | Another POP3 session has the mailbox open. Close it — this is RFC 1939 behaviour, not a bug. |
| GUI will not start | `python3 -c "import tkinter"` — install the Tk-enabled Python build if this fails. |

## Limitations

Single host, no relaying, no DNS MX lookup, no TLS, no queue-and-retry for
temporary failures, and no IMAP. Each is noted in the report's *Future Scope*
section along with what it would take to add.

## References

* RFC 5321 — Simple Mail Transfer Protocol
* RFC 1939 — Post Office Protocol version 3
* RFC 5322 — Internet Message Format
* RFC 2045/2046 — MIME
* RFC 4954 — SMTP Service Extension for Authentication
* Tanenbaum, *Computer Networks*, 5th ed., §7.2 — Electronic Mail
* Forouzan, *Data Communications and Networking*, 5th ed., ch. 26
