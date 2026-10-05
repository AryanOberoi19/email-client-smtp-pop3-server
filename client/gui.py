"""
Tkinter mail client.

    python3 client/gui.py

The window has three parts:

    top     server controls -- both servers can be started inside this process,
            so the whole system runs from one window during a demo
    middle  Compose and Inbox tabs
    bottom  the live protocol trace, showing exactly the same C:/S: lines the
            terminal client prints

Threading note: every network call runs on a worker thread so the window never
freezes, but Tk is not thread-safe -- a widget must only be touched from the
thread that created it. Worker threads therefore never call a widget directly.
They put results on a queue.Queue, and a repeating root.after() callback on
the main thread drains it and does the actual updating.
"""

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from client.mail_client import MailClientError, POP3Client, SMTPClient
from common import mime, trace
from common.mailstore import MailStore
from common.trace import Tracer
from common.users import UserDirectory
from pop3_server import POP3Server
from smtp_server import SMTPServer

# Colours for the trace pane, matching the terminal's C:/S: colouring.
TRACE_COLOURS = {
    "C": "#0a7ea4",     # client -> server
    "S": "#1a7f37",     # server -> client
    "*": "#9a6700",     # informational
    "!": "#cf222e",     # error
}


class MailClientGUI:

    def __init__(self, root, host=config.HOST, smtp_port=config.SMTP_PORT,
                 pop_port=config.POP3_PORT):
        self.root = root
        self.host = host
        self.smtp_port = smtp_port
        self.pop_port = pop_port

        self.username = None
        self.password = None
        self.attachments = []
        self.inbox = []              # [(number, size, parsed headers)]
        self.current_message = None

        self.smtp_server = None
        self.pop_server = None
        self.store = MailStore(config.STORE_DIR)
        self.users = UserDirectory()

        self.tracer = Tracer("gui-client", to_console=False, to_file=True)
        self.events = queue.Queue()

        root.title("CN Project - Email Client over SMTP and POP3  |  I041")
        root.geometry("1080x760")
        root.minsize(900, 620)

        self._build_server_bar()
        self._build_account_bar()
        self._build_tabs()
        self._build_trace_pane()

        # Mirror every tracer in the process -- client and both servers -- into
        # the trace pane.
        trace.subscribe_all(self._on_trace)
        self.root.after(80, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._set_status("Start the servers, then log in.")

    # ------------------------------------------------------------------ #
    #  Layout
    # ------------------------------------------------------------------ #

    def _build_server_bar(self):
        frame = ttk.LabelFrame(self.root, text="Servers", padding=8)
        frame.pack(fill="x", padx=10, pady=(10, 4))

        self.smtp_button = ttk.Button(frame, text="Start SMTP",
                                      command=self._toggle_smtp)
        self.smtp_button.grid(row=0, column=0, padx=(0, 6))
        self.smtp_status = ttk.Label(frame, text=f"SMTP stopped ({self.smtp_port})")
        self.smtp_status.grid(row=0, column=1, padx=(0, 24))

        self.pop_button = ttk.Button(frame, text="Start POP3",
                                     command=self._toggle_pop)
        self.pop_button.grid(row=0, column=2, padx=(0, 6))
        self.pop_status = ttk.Label(frame, text=f"POP3 stopped ({self.pop_port})")
        self.pop_status.grid(row=0, column=3, padx=(0, 24))

        ttk.Label(frame, text="(or run smtp_server.py / pop3_server.py in a "
                              "terminal instead)",
                  foreground="#666").grid(row=0, column=4, sticky="w")

    def _build_account_bar(self):
        frame = ttk.LabelFrame(self.root, text="Account", padding=8)
        frame.pack(fill="x", padx=10, pady=4)

        ttk.Label(frame, text="User:").grid(row=0, column=0)
        self.user_entry = ttk.Combobox(frame, width=16,
                                       values=self.users.usernames())
        self.user_entry.grid(row=0, column=1, padx=(4, 12))
        if self.users.usernames():
            self.user_entry.set(self.users.usernames()[0])

        ttk.Label(frame, text="Password:").grid(row=0, column=2)
        self.password_entry = ttk.Entry(frame, width=16, show="\u2022")
        self.password_entry.grid(row=0, column=3, padx=(4, 12))
        self.password_entry.bind("<Return>", lambda _e: self._login())

        self.login_button = ttk.Button(frame, text="Log in",
                                       command=self._login)
        self.login_button.grid(row=0, column=4, padx=(0, 12))

        self.account_label = ttk.Label(frame, text="not logged in",
                                       foreground="#666")
        self.account_label.grid(row=0, column=5, sticky="w")

    def _build_tabs(self):
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=10, pady=4)
        self._build_compose_tab()
        self._build_inbox_tab()

    def _build_compose_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="Compose")

        ttk.Label(tab, text="To:").grid(row=0, column=0, sticky="w")
        self.to_entry = ttk.Entry(tab)
        self.to_entry.grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Label(tab, text="comma separated; bare names get @localhost",
                  foreground="#666").grid(row=0, column=2, padx=8, sticky="w")

        ttk.Label(tab, text="Subject:").grid(row=1, column=0, sticky="w")
        self.subject_entry = ttk.Entry(tab)
        self.subject_entry.grid(row=1, column=1, sticky="ew", pady=3)

        ttk.Label(tab, text="Message:").grid(row=2, column=0, sticky="nw",
                                             pady=(6, 0))
        self.body_text = tk.Text(tab, height=12, wrap="word",
                                 font=("Helvetica", 12))
        self.body_text.grid(row=2, column=1, columnspan=2, sticky="nsew",
                            pady=6)

        attach_frame = ttk.Frame(tab)
        attach_frame.grid(row=3, column=1, columnspan=2, sticky="ew")
        ttk.Button(attach_frame, text="Attach file...",
                   command=self._attach_file).pack(side="left")
        ttk.Button(attach_frame, text="Clear attachments",
                   command=self._clear_attachments).pack(side="left", padx=6)
        self.attach_label = ttk.Label(attach_frame, text="no attachments",
                                      foreground="#666")
        self.attach_label.pack(side="left", padx=10)

        self.send_button = ttk.Button(tab, text="Send  (SMTP)",
                                      command=self._send)
        self.send_button.grid(row=4, column=1, sticky="w", pady=(10, 0))

        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(2, weight=1)

    def _build_inbox_tab(self):
        tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(tab, text="Inbox")

        buttons = ttk.Frame(tab)
        buttons.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(buttons, text="Refresh  (LIST)",
                   command=self._refresh_inbox).pack(side="left")
        ttk.Button(buttons, text="Read  (RETR)",
                   command=self._read_selected).pack(side="left", padx=6)
        ttk.Button(buttons, text="Delete  (DELE + QUIT)",
                   command=self._delete_selected).pack(side="left")
        ttk.Button(buttons, text="Save attachments",
                   command=self._save_attachments).pack(side="left", padx=6)

        columns = ("number", "from", "subject", "size")
        self.tree = ttk.Treeview(tab, columns=columns, show="headings",
                                 height=8, selectmode="browse")
        for column, heading, width in (("number", "#", 44),
                                       ("from", "From", 190),
                                       ("subject", "Subject", 380),
                                       ("size", "Size", 90)):
            self.tree.heading(column, text=heading)
            self.tree.column(column, width=width,
                             anchor="e" if column in ("number", "size") else "w")
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _e: self._read_selected())

        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)

        self.message_view = ScrolledText(tab, height=12, wrap="word",
                                         state="disabled",
                                         font=("Helvetica", 12))
        self.message_view.grid(row=2, column=0, columnspan=2, sticky="nsew",
                               pady=(8, 0))

        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        tab.rowconfigure(2, weight=2)

    def _build_trace_pane(self):
        frame = ttk.LabelFrame(self.root, text="Protocol trace", padding=6)
        frame.pack(fill="both", expand=True, padx=10, pady=(4, 6))

        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(0, 4))
        self.trace_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Show trace",
                        variable=self.trace_enabled).pack(side="left")
        ttk.Button(controls, text="Clear",
                   command=self._clear_trace).pack(side="left", padx=8)
        self.status_label = ttk.Label(controls, text="", foreground="#444")
        self.status_label.pack(side="right")

        self.trace_view = ScrolledText(frame, height=12, wrap="none",
                                       state="disabled",
                                       font=("Menlo", 11), background="#111820",
                                       foreground="#d8dee9")
        self.trace_view.pack(fill="both", expand=True)
        for kind, colour in TRACE_COLOURS.items():
            self.trace_view.tag_configure(kind, foreground=colour)

    # ------------------------------------------------------------------ #
    #  Thread-safe plumbing
    # ------------------------------------------------------------------ #

    def _on_trace(self, name, kind, text):
        """Called from arbitrary threads: only touch the queue here."""
        self.events.put(("trace", (name, kind, text)))

    def _drain_events(self):
        """Runs on the main thread; the only place widgets are updated."""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "trace":
                    self._append_trace(*payload)
                elif kind == "status":
                    self.status_label.config(text=payload)
                elif kind == "callback":
                    payload()
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def _run_async(self, work, on_success=None, on_error=None):
        """Run `work` off the main thread, deliver the result back onto it."""
        def runner():
            try:
                result = work()
            except Exception as exc:                    # noqa: BLE001
                if on_error is not None:
                    self.events.put(("callback", lambda e=exc: on_error(e)))
                else:
                    self.events.put(("callback",
                                     lambda e=exc: self._show_error(e)))
                return
            if on_success is not None:
                self.events.put(("callback", lambda r=result: on_success(r)))

        threading.Thread(target=runner, daemon=True).start()

    def _set_status(self, text):
        self.events.put(("status", text))

    def _show_error(self, exc):
        self._set_status(str(exc))
        messagebox.showerror("Error", str(exc))

    def _append_trace(self, name, kind, text):
        if not self.trace_enabled.get():
            return
        self.trace_view.configure(state="normal")
        self.trace_view.insert("end", f"[{name}] {kind}: {text}\n", kind)
        self.trace_view.see("end")
        self.trace_view.configure(state="disabled")

    def _clear_trace(self):
        self.trace_view.configure(state="normal")
        self.trace_view.delete("1.0", "end")
        self.trace_view.configure(state="disabled")

    # ------------------------------------------------------------------ #
    #  Server controls
    # ------------------------------------------------------------------ #

    def _toggle_smtp(self):
        if self.smtp_server is None:
            try:
                server = SMTPServer(self.host, self.smtp_port, store=self.store,
                                    users=self.users, verbose=False,
                                    log_to_file=True)
                self.smtp_port = server.start()
            except OSError as exc:
                messagebox.showerror(
                    "Cannot start SMTP",
                    f"{exc}\n\nPort {self.smtp_port} may already be in use by "
                    f"a server started in a terminal.")
                return
            self.smtp_server = server
            self.smtp_button.config(text="Stop SMTP")
            self.smtp_status.config(text=f"SMTP running on {self.smtp_port}",
                                    foreground="#1a7f37")
        else:
            self.smtp_server.stop()
            self.smtp_server = None
            self.smtp_button.config(text="Start SMTP")
            self.smtp_status.config(text=f"SMTP stopped ({self.smtp_port})",
                                    foreground="")

    def _toggle_pop(self):
        if self.pop_server is None:
            try:
                server = POP3Server(self.host, self.pop_port, store=self.store,
                                    users=self.users, verbose=False,
                                    log_to_file=True)
                self.pop_port = server.start()
            except OSError as exc:
                messagebox.showerror(
                    "Cannot start POP3",
                    f"{exc}\n\nPort {self.pop_port} may already be in use by "
                    f"a server started in a terminal.")
                return
            self.pop_server = server
            self.pop_button.config(text="Stop POP3")
            self.pop_status.config(text=f"POP3 running on {self.pop_port}",
                                   foreground="#1a7f37")
        else:
            self.pop_server.stop()
            self.pop_server = None
            self.pop_button.config(text="Start POP3")
            self.pop_status.config(text=f"POP3 stopped ({self.pop_port})",
                                   foreground="")

    # ------------------------------------------------------------------ #
    #  Account
    # ------------------------------------------------------------------ #

    def _login(self):
        username = self.user_entry.get().strip()
        password = self.password_entry.get()
        if not username or not password:
            messagebox.showwarning("Log in", "Enter a username and password.")
            return

        def work():
            client = POP3Client(self.host, self.pop_port, self.tracer)
            client.connect()
            client.login(username, password)
            result = client.stat()
            client.quit()
            return result

        def done(result):
            count, octets = result
            self.username, self.password = username, password
            self.account_label.config(
                text=f"{username}@{config.SERVER_DOMAIN} - {count} message(s), "
                     f"{octets} octets", foreground="#1a7f37")
            self._set_status(f"Logged in as {username}")
            self._refresh_inbox()

        self._set_status("Logging in over POP3...")
        self._run_async(work, done, self._login_failed)

    def _login_failed(self, exc):
        self.username = self.password = None
        self.account_label.config(text="not logged in", foreground="#cf222e")
        message = str(exc)
        if isinstance(exc, ConnectionRefusedError):
            message = (f"Connection refused on port {self.pop_port}.\n\n"
                       f"Start the POP3 server first, either with the button "
                       f"above or with 'python3 pop3_server.py'.")
        messagebox.showerror("Login failed", message)
        self._set_status("Login failed")

    def _require_login(self):
        if self.username is None:
            messagebox.showwarning("Not logged in",
                                   "Log in before sending or reading mail.")
            return False
        return True

    # ------------------------------------------------------------------ #
    #  Compose
    # ------------------------------------------------------------------ #

    def _attach_file(self):
        paths = filedialog.askopenfilenames(title="Attach file(s)")
        for path in paths:
            self.attachments.append(Path(path))
        self._update_attach_label()

    def _clear_attachments(self):
        self.attachments = []
        self._update_attach_label()

    def _update_attach_label(self):
        if not self.attachments:
            self.attach_label.config(text="no attachments", foreground="#666")
        else:
            names = ", ".join(p.name for p in self.attachments)
            total = sum(p.stat().st_size for p in self.attachments)
            self.attach_label.config(text=f"{names}  ({total} bytes)",
                                     foreground="#0a7ea4")

    def _send(self):
        if not self._require_login():
            return
        raw_to = self.to_entry.get().strip()
        recipients = [r.strip() for r in raw_to.split(",") if r.strip()]
        if not recipients:
            messagebox.showwarning("Send", "Enter at least one recipient.")
            return
        recipients = [r if "@" in r else f"{r}@{config.SERVER_DOMAIN}"
                      for r in recipients]

        subject = self.subject_entry.get().strip()
        body = self.body_text.get("1.0", "end-1c")
        attachments = list(self.attachments)
        sender = f"{self.username}@{config.SERVER_DOMAIN}"
        username, password = self.username, self.password

        def work():
            client = SMTPClient(self.host, self.smtp_port, self.tracer)
            client.connect()
            client.ehlo()
            client.login(username, password)
            queue_id = client.send_mail(sender, recipients, subject, body,
                                        attachments)
            client.quit()
            return queue_id

        def done(queue_id):
            self._set_status(f"Sent to {', '.join(recipients)} [{queue_id}]")
            messagebox.showinfo("Sent",
                                f"Delivered to {', '.join(recipients)}.")
            self.to_entry.delete(0, "end")
            self.subject_entry.delete(0, "end")
            self.body_text.delete("1.0", "end")
            self._clear_attachments()

        self._set_status("Sending over SMTP...")
        self._run_async(work, done)

    # ------------------------------------------------------------------ #
    #  Inbox
    # ------------------------------------------------------------------ #

    def _refresh_inbox(self):
        if not self._require_login():
            return
        username, password = self.username, self.password

        def work():
            client = POP3Client(self.host, self.pop_port, self.tracer)
            client.connect()
            client.login(username, password)
            rows = []
            for number, size in client.list():
                # TOP n 0 = headers only, so the list view does not download
                # every message in full just to show a subject line.
                headers = mime.parse_message(client.top(number, 0))
                rows.append((number, size, headers))
            client.quit()
            return rows

        def done(rows):
            self.inbox = rows
            self.tree.delete(*self.tree.get_children())
            for number, size, headers in rows:
                marker = " [+]" if headers.header("content-type", "") \
                    .startswith("multipart") else ""
                self.tree.insert("", "end", iid=str(number), values=(
                    number, headers.sender, headers.subject + marker, size))
            self._set_status(f"{len(rows)} message(s) in the maildrop")

        self._set_status("Fetching the message list over POP3...")
        self._run_async(work, done)

    def _selected_number(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Select a message",
                                "Pick a message from the list first.")
            return None
        return int(selection[0])

    def _fetch(self, number, on_success):
        username, password = self.username, self.password

        def work():
            client = POP3Client(self.host, self.pop_port, self.tracer)
            client.connect()
            client.login(username, password)
            raw = client.retr(number)
            client.quit()
            return mime.parse_message(raw)

        self._run_async(work, on_success)

    def _read_selected(self):
        if not self._require_login():
            return
        number = self._selected_number()
        if number is None:
            return

        def done(message):
            self.current_message = message
            lines = [
                f"From    : {message.sender}",
                f"To      : {message.recipients}",
                f"Subject : {message.subject}",
                f"Date    : {message.date}",
            ]
            if message.attachments:
                lines.append("Files   : " + ", ".join(
                    f"{a.filename} ({a.size} B)" for a in message.attachments))
            lines.append("-" * 78)
            lines.append(message.body.replace("\r\n", "\n"))

            self.message_view.configure(state="normal")
            self.message_view.delete("1.0", "end")
            self.message_view.insert("1.0", "\n".join(lines))
            self.message_view.configure(state="disabled")
            self._set_status(f"Retrieved message {number}")

        self._set_status(f"RETR {number}...")
        self._fetch(number, done)

    def _delete_selected(self):
        if not self._require_login():
            return
        number = self._selected_number()
        if number is None:
            return
        if not messagebox.askyesno(
                "Delete message",
                f"Mark message {number} deleted and QUIT?\n\n"
                f"DELE only marks it; the message is actually removed when "
                f"the session enters the UPDATE state at QUIT."):
            return

        username, password = self.username, self.password

        def work():
            client = POP3Client(self.host, self.pop_port, self.tracer)
            client.connect()
            client.login(username, password)
            client.dele(number)
            return client.quit()            # QUIT is what commits it

        def done(_status):
            self._set_status(f"Message {number} deleted (UPDATE state)")
            self.message_view.configure(state="normal")
            self.message_view.delete("1.0", "end")
            self.message_view.configure(state="disabled")
            self._refresh_inbox()

        self._run_async(work, done)

    def _save_attachments(self):
        if not self._require_login():
            return
        number = self._selected_number()
        if number is None:
            return
        outdir = filedialog.askdirectory(title="Save attachments into")
        if not outdir:
            return

        def done(message):
            if not message.attachments:
                messagebox.showinfo("No attachments",
                                    f"Message {number} has no attachments.")
                return
            written = mime.save_attachments(message, outdir)
            messagebox.showinfo(
                "Saved",
                "\n".join(str(p) for p in written))
            self._set_status(f"Saved {len(written)} attachment(s)")

        self._set_status(f"RETR {number} for attachments...")
        self._fetch(number, done)

    # ------------------------------------------------------------------ #

    def _on_close(self):
        trace.unsubscribe_all(self._on_trace)
        if self.smtp_server is not None:
            self.smtp_server.stop()
        if self.pop_server is not None:
            self.pop_server.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("aqua")       # native look on macOS
    except tk.TclError:
        pass
    MailClientGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
