# Capturing the SMTP and POP3 traffic

The protocol trace printed by the servers shows what the *program* believes it
sent. A packet capture shows what actually crossed the interface. Putting the
two side by side is the point of this exercise — and it is where the security
argument becomes concrete rather than theoretical.

Everything here runs on the loopback interface, so no traffic leaves the
machine and no permission from anyone else is needed.

---

## 1. Which interface

Loopback has a different name on each platform:

| Platform | Interface |
|---|---|
| macOS | `lo0` |
| Linux | `lo` |
| Windows | Npcap's "Adapter for loopback traffic" |

This project was developed on macOS, so the commands below use `lo0`.

## 2. Start the capture

In one terminal:

```bash
sudo tcpdump -i lo0 -s 0 -w cn_smtp_pop.pcap 'tcp port 1025 or tcp port 1110'
```

* `-i lo0` — capture on loopback
* `-s 0` — capture whole packets, not just the first 96 bytes (without this
  the message body is truncated and Follow TCP Stream shows nothing useful)
* `-w file.pcap` — write for Wireshark instead of printing
* the filter keeps only our two ports, so the file stays small

To watch it live in the terminal instead of saving it, drop `-w` and add `-A`
to print the payload as ASCII:

```bash
sudo tcpdump -i lo0 -A 'tcp port 1025 or tcp port 1110'
```

## 3. Generate traffic

In a second terminal, from the project directory:

```bash
python3 run_demo.py --keep
```

Or run the servers and the CLI client by hand if you want to control exactly
what gets captured.

Then stop `tcpdump` with Ctrl+C.

## 4. Open it in Wireshark

```bash
open cn_smtp_pop.pcap        # macOS
wireshark cn_smtp_pop.pcap   # Linux
```

Display filter:

```
tcp.port == 1025 || tcp.port == 1110
```

### The gotcha worth knowing

Wireshark's SMTP and POP dissectors are registered against the *well-known*
ports, 25 and 110. Because this project runs on 1025 and 1110 to avoid needing
root, Wireshark shows the packets as plain TCP with no protocol decoding.

The fix: right-click any packet → **Decode As…** → set the TCP port field to
**SMTP** for 1025 and **POP** for 1110. The Info column then shows
`C: MAIL FROM:<aryan@localhost>` and `S: 250 2.1.0 Sender OK` directly.

This is worth mentioning in a viva: it demonstrates that "port 25 is SMTP" is
a convention recorded in a registry, not a property of the protocol. The
protocol is identical on any port; only the software's default guess changes.

## 5. What to look at

**Follow the conversation.** Right-click any packet in the session →
**Follow → TCP Stream**. The entire SMTP dialogue appears as readable text,
client lines in one colour and server lines in the other. This one window is
usually the best screenshot for a report.

Five things worth capturing:

1. **The three-way handshake.** Filter `tcp.flags.syn == 1`. The SYN,
   SYN-ACK, ACK before any mail data connects this project back to the TCP
   material in Unit 5 — SMTP and POP3 do nothing until TCP has a connection.

2. **The 220 greeting.** The server speaks first. Unusually for a
   client-server protocol, the client must wait and read before it may send
   anything.

3. **The DATA / 354 exchange.** Watch the server answer `354`, then the
   message flow as ordinary TCP segments, then the lone `.` line, then `250`.
   A large attachment is visibly split across several segments — the
   application sees one message, the network sees a byte stream cut into MSS-
   sized pieces.

4. **The password.** Find the `AUTH LOGIN` exchange. The two base64 blobs
   after each `334` are the username and password. Decode either one:

   ```bash
   echo 'YXJ5YW4=' | base64 --decode        # -> aryan
   ```

   Base64 is an encoding, not encryption. Anyone on the path reads the
   password. The same is true of POP3's `PASS` command, which does not even
   bother with base64. This is exactly why real deployments wrap both
   protocols in TLS — implicit TLS on ports 465/995, or `STARTTLS` upgrading
   the plaintext port in place.

5. **APOP, for contrast.** The `apop()` path in the client sends
   `APOP prof <md5 digest>`. Capture that and confirm the password itself
   never appears anywhere in the stream — only a digest of the server's
   one-time banner combined with the shared secret.

## 6. Useful display filters

| Goal | Filter |
|---|---|
| Everything in this project | `tcp.port == 1025 \|\| tcp.port == 1110` |
| SMTP commands only (after Decode As) | `smtp.req` |
| SMTP responses only | `smtp.rsp` |
| Just the connection setup | `tcp.flags.syn == 1` |
| Packets containing a string | `frame contains "MAIL FROM"` |
| Find the password exchange | `frame contains "AUTH LOGIN"` |
| Connection teardown | `tcp.flags.fin == 1` |

## 7. If the capture comes back empty

* **No packets at all** — check the interface name (`ifconfig -l` on macOS)
  and that you captured on loopback rather than `en0`. Traffic to 127.0.0.1
  never touches the physical interface.
* **Packets but no payload** — the `-s 0` flag was missing, so only headers
  were saved.
* **Nothing decoded as SMTP/POP** — apply Decode As, per section 4.
* **`tcpdump: permission denied`** — capturing needs `sudo`. Running the
  *servers* does not; only the capture does.
