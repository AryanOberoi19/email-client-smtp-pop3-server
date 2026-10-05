"""
Gmail-style mail client (CustomTkinter front-end).

    pip install customtkinter
    python3 client/gui_modern.py

customtkinter is the only third-party package in the project, and only this
file needs it. If it is not installed, running this file prints a hint and
opens the standard-library Tkinter client (client/gui.py) instead.

The window mirrors a real webmail client:

    left    sidebar  -- brand, Compose, folder list, and the Server Status
                        card where both servers can be started in-process, so
                        the whole system runs from one window during a demo
    centre  message list -- one card per message in the maildrop
    right   Compose / Inbox tabs
    bottom  the live protocol trace, showing exactly the same C:/S: lines the
            terminal client prints

Only the *look* differs from the Tkinter version in client/gui.py. Every
network call still runs on a worker thread (Tk is not thread-safe), results
are handed back through a queue.Queue, and a repeating root.after() callback
on the main thread does the actual widget updates. None of the SMTP/POP3
logic, the client classes, or the servers were touched.
"""

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import customtkinter as ctk
except ImportError:
    if __name__ != "__main__":
        raise
    print("customtkinter is not installed (pip install customtkinter); "
          "opening the standard Tkinter client instead.", file=sys.stderr)
    from client import gui
    gui.main()
    sys.exit(0)

import config
from client.mail_client import MailClientError, POP3Client, SMTPClient
from common import mime, trace
from common.mailstore import MailStore
from common.trace import Tracer
from common.users import UserDirectory
from pop3_server import POP3Server
from smtp_server import SMTPServer

# ---------------------------------------------------------------------------
#  Palette -- a light, webmail-style theme.
# ---------------------------------------------------------------------------
ACCENT = "#2f6fed"          # primary blue
ACCENT_HOVER = "#255ad0"
BG = "#f5f7fa"              # window background
SIDEBAR_BG = "#ffffff"
CARD_BG = "#ffffff"
CARD_HOVER = "#eef3fc"
CARD_SELECT = "#e3ecfd"
BORDER = "#e3e7ee"
TEXT = "#1b2430"
MUTED = "#6b7385"
GREEN = "#1a9d54"
RED = "#d9463b"
DANGER_HOVER = "#c23a30"
TRACE_BG = "#0f1720"
TRACE_FG = "#d8dee9"

# Colours for the trace pane, matching the terminal's C:/S: colouring.
TRACE_COLOURS = {
    "C": "#4aa8d8",     # client -> server
    "S": "#5bd88a",     # server -> client
    "*": "#e2b341",     # informational
    "!": "#ef6b6b",     # error
}

AVATAR_COLOURS = ["#2f6fed", "#8b5cf6", "#0ea5a4", "#e0574f",
                  "#d97706", "#db2777", "#16a34a"]


def _all_children(widget):
    for child in widget.winfo_children():
        yield child
        yield from _all_children(child)


# ===========================================================================
#  Widget wrappers so the unchanged handlers can keep calling
#  .config(text=..., foreground=...) / .config(text="Stop SMTP") as before.
# ===========================================================================
class _StatusLabel(ctk.CTkLabel):
    """A CTkLabel whose .config(foreground=...) maps to text_color, and which
    recolours an optional companion status dot."""

    dot = None

    def config(self, text=None, foreground=None, **kw):      # noqa: A003
        opts = dict(kw)
        if text is not None:
            opts["text"] = text
        if foreground is not None:
            colour = foreground if foreground else MUTED
            opts["text_color"] = colour
            if self.dot is not None:
                self.dot.configure(text_color=colour)
        self.configure(**opts)


class _ToggleButton(ctk.CTkButton):
    """A CTkButton whose .config(text=...) also flips its fill colour between
    the start (blue) and stop (red) states, matching the mockup."""

    def __init__(self, *args, start_text="Start", stop_text="Stop", **kw):
        self._start_text = start_text
        self._stop_text = stop_text
        super().__init__(*args, **kw)

    def config(self, text=None, **kw):                        # noqa: A003
        opts = dict(kw)
        if text is not None:
            opts["text"] = text
            if text == self._stop_text:
                opts.update(fg_color=RED, hover_color=DANGER_HOVER)
            else:
                opts.update(fg_color=ACCENT, hover_color=ACCENT_HOVER)
        self.configure(**opts)


# ===========================================================================
#  Small adapter: a Gmail-style message list that exposes just the slice of
#  the ttk.Treeview API the inbox handlers use (delete, get_children, insert,
#  selection, bind).  The handlers therefore stay unchanged -- they still call
#  self.tree.insert(...), self.tree.selection(), etc.
# ===========================================================================
class EmailListView(ctk.CTkScrollableFrame):
    def __init__(self, master, on_open=None, **kw):
        super().__init__(master, fg_color="transparent", **kw)
        self._on_open = on_open
        self._rows = {}          # iid -> row frame
        self._haystack = {}      # iid -> lowercase "#n sender subject" for search
        self._terms = []         # current search terms; empty = show everything
        self._selected = None
        self._empty = ctk.CTkLabel(self, text="", text_color=MUTED,
                                   font=ctk.CTkFont(size=12))

    # -- Treeview-compatible surface ------------------------------------- #

    def get_children(self):
        return list(self._rows.keys())

    def delete(self, *iids):
        if not iids:
            iids = list(self._rows.keys())
        for iid in iids:
            row = self._rows.pop(str(iid), None)
            self._haystack.pop(str(iid), None)
            if row is not None:
                row.destroy()
        if self._selected not in self._rows:
            self._selected = None
        self._update_empty_note()

    def selection(self):
        return (self._selected,) if self._selected else ()

    def bind(self, *_args, **_kw):
        # Double-click is handled per-card; nothing global to bind.
        return None

    def insert(self, _parent, _index, iid=None, values=(), **_kw):
        number, sender, subject, size = values
        iid = str(iid if iid is not None else number)
        self._add_card(iid, number, sender, subject, size)

    # -- search ---------------------------------------------------------- #

    def filter(self, query):
        """Show only cards whose number, sender or subject contain every
        whitespace-separated term of `query` (case-insensitive). Returns
        (shown, total)."""
        self._terms = query.lower().split()
        for card in self._rows.values():
            card.pack_forget()
        for iid, card in self._rows.items():       # re-pack in list order
            if self._matches(iid):
                card.pack(fill="x", padx=2, pady=4)
        if self._selected is not None and not self._matches(self._selected):
            self._select(None)                      # never act on a hidden card
        self._update_empty_note()
        return self.shown_count(), len(self._rows)

    def shown_count(self):
        return sum(1 for iid in self._rows if self._matches(iid))

    def _matches(self, iid):
        haystack = self._haystack.get(iid, "")
        return all(term in haystack for term in self._terms)

    def _update_empty_note(self):
        if self._rows and self.shown_count() == 0:
            self._empty.configure(
                text=f"No messages match “{' '.join(self._terms)}”")
            self._empty.pack(pady=24)
        else:
            self._empty.pack_forget()

    # -- rendering ------------------------------------------------------- #

    def _add_card(self, iid, number, sender, subject, size):
        starred = str(subject).endswith("[+]")     # attachment marker -> star
        subject = str(subject).replace(" [+]", "").replace("[+]", "").strip()

        card = ctk.CTkFrame(self, fg_color=CARD_BG, corner_radius=10,
                            border_width=1, border_color=BORDER)
        card.grid_columnconfigure(1, weight=1)

        colour = AVATAR_COLOURS[hash(str(sender)) % len(AVATAR_COLOURS)]
        letter = (str(sender).strip()[:1] or "?").upper()
        avatar = ctk.CTkLabel(card, text=letter, width=40, height=40,
                              corner_radius=20, fg_color=colour,
                              text_color="#ffffff",
                              font=ctk.CTkFont(size=15, weight="bold"))
        avatar.grid(row=0, column=0, rowspan=3, padx=(12, 10), pady=12)

        top = ctk.CTkFrame(card, fg_color="transparent")
        top.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=(10, 0))
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text=str(sender), anchor="w", text_color=TEXT,
                     font=ctk.CTkFont(size=13, weight="bold")
                     ).grid(row=0, column=0, sticky="w")
        star = "\u2605" if starred else "\u2606"
        ctk.CTkLabel(top, text=f"#{number}   {star}", anchor="e",
                     text_color=(ACCENT if starred else MUTED),
                     font=ctk.CTkFont(size=11)).grid(row=0, column=1, sticky="e")

        ctk.CTkLabel(card, text=subject or "(no subject)", anchor="w",
                     text_color=TEXT, font=ctk.CTkFont(size=12)
                     ).grid(row=1, column=1, sticky="ew", padx=(0, 12),
                            pady=(0, 2))
        ctk.CTkLabel(card, text=f"{size} bytes", anchor="w", text_color=MUTED,
                     font=ctk.CTkFont(size=11)
                     ).grid(row=2, column=1, sticky="ew", padx=(0, 12),
                            pady=(0, 10))

        for w in [card] + list(_all_children(card)):
            w.bind("<Button-1>", lambda _e, i=iid: self._select(i))
            w.bind("<Double-Button-1>", lambda _e, i=iid: self._open(i))

        self._rows[iid] = card
        self._haystack[iid] = f"#{number} {sender} {subject}".lower()
        if self._matches(iid):                      # respect an active search
            card.pack(fill="x", padx=2, pady=4)
        self._update_empty_note()

    def _select(self, iid):
        self._selected = iid
        for other, card in self._rows.items():
            card.configure(fg_color=CARD_SELECT if other == iid else CARD_BG,
                           border_color=ACCENT if other == iid else BORDER)

    def _open(self, iid):
        self._select(iid)
        if self._on_open is not None:
            self._on_open()


# ===========================================================================
#  Main window
# ===========================================================================
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

        root.title("CN Mail Client  \u2014  SMTP & POP3 (Localhost)")
        root.geometry("1180x780")
        root.minsize(1040, 680)
        root.configure(fg_color=BG)

        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(1, weight=1)

        self._build_sidebar()
        self._build_topbar()
        self._build_main_area()
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

    def _build_sidebar(self):
        bar = ctk.CTkFrame(self.root, width=250, corner_radius=0,
                           fg_color=SIDEBAR_BG)
        bar.grid(row=0, column=0, rowspan=3, sticky="nsw")
        bar.grid_propagate(False)

        brand = ctk.CTkFrame(bar, fg_color="transparent")
        brand.pack(fill="x", padx=18, pady=(18, 6))
        ctk.CTkLabel(brand, text="\u2709", font=ctk.CTkFont(size=26),
                     text_color=ACCENT).pack(side="left")
        title = ctk.CTkFrame(brand, fg_color="transparent")
        title.pack(side="left", padx=8)
        ctk.CTkLabel(title, text="CN Mail Client", anchor="w", text_color=TEXT,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(title, text="SMTP & POP3 (Localhost)", anchor="w",
                     text_color=MUTED,
                     font=ctk.CTkFont(size=10)).pack(anchor="w")

        ctk.CTkButton(bar, text="\u270E   Compose", height=44,
                      corner_radius=10, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER,
                      font=ctk.CTkFont(size=14, weight="bold"),
                      command=self._focus_compose
                      ).pack(fill="x", padx=18, pady=(10, 14))

        folders = [("\U0001F4E5", "Inbox", self._refresh_inbox)]
        for icon, name, cmd in folders:
            ctk.CTkButton(
                bar, text=f"   {icon}   {name}", anchor="w", height=38,
                corner_radius=8, fg_color="transparent", text_color=TEXT,
                hover_color=CARD_HOVER, font=ctk.CTkFont(size=13),
                command=(cmd or (lambda: None))).pack(fill="x", padx=12, pady=1)

        ctk.CTkLabel(bar, text="Server Status", anchor="w", text_color=MUTED,
                     font=ctk.CTkFont(size=12, weight="bold")
                     ).pack(fill="x", padx=20, pady=(22, 4))

        self.smtp_status = self._server_block(
            bar, "SMTP Server", self.smtp_port, "Start SMTP", "Stop SMTP",
            self._toggle_smtp)
        self.smtp_button = self._last_toggle_button

        self.pop_status = self._server_block(
            bar, "POP3 Server", self.pop_port, "Start POP3", "Stop POP3",
            self._toggle_pop)
        self.pop_button = self._last_toggle_button

    def _server_block(self, parent, name, port, start_text, stop_text, cmd):
        block = ctk.CTkFrame(parent, fg_color="transparent")
        block.pack(fill="x", padx=16, pady=(6, 4))

        head = ctk.CTkFrame(block, fg_color="transparent")
        head.pack(fill="x")
        dot = ctk.CTkLabel(head, text="\u25CF", text_color=MUTED,
                           font=ctk.CTkFont(size=13))
        dot.pack(side="left")
        ctk.CTkLabel(head, text=f"  {name}", anchor="w", text_color=TEXT,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")

        status = _StatusLabel(block, text=f"stopped ({port})",
                              anchor="w", text_color=MUTED,
                              font=ctk.CTkFont(size=11))
        status.pack(fill="x", padx=(20, 0))
        status.dot = dot        # so config(foreground=) can recolour the dot

        buttons = ctk.CTkFrame(block, fg_color="transparent")
        buttons.pack(fill="x", padx=(20, 0), pady=(4, 0))
        toggle = _ToggleButton(buttons, text=start_text, width=90, height=28,
                               corner_radius=7, fg_color=ACCENT,
                               hover_color=ACCENT_HOVER,
                               start_text=start_text, stop_text=stop_text,
                               font=ctk.CTkFont(size=11), command=cmd)
        toggle.pack(side="left")
        self._last_toggle_button = toggle
        return status

    def _build_topbar(self):
        bar = ctk.CTkFrame(self.root, height=64, corner_radius=0,
                           fg_color=SIDEBAR_BG)
        bar.grid(row=0, column=1, sticky="new")
        bar.grid_columnconfigure(0, weight=1)
        bar.grid_propagate(False)

        self.search_entry = ctk.CTkEntry(
            bar, placeholder_text="\U0001F50D   Search by sender, subject or #number "
                                  "(Esc to clear)",
            height=38, corner_radius=10, border_color=BORDER, fg_color=BG)
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(20, 12),
                               pady=13)
        self.search_entry.bind("<KeyRelease>", lambda _e: self._on_search())
        self.search_entry.bind("<Escape>", lambda _e: self._clear_search())

        account = ctk.CTkFrame(bar, fg_color="transparent")
        account.grid(row=0, column=1, sticky="e", padx=(0, 16))

        ctk.CTkLabel(account, text="User", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 4))
        self.user_entry = ctk.CTkComboBox(account, width=110, height=32,
                                          values=self.users.usernames(),
                                          border_color=BORDER, fg_color=BG,
                                          button_color=ACCENT,
                                          button_hover_color=ACCENT_HOVER)
        self.user_entry.pack(side="left", padx=(0, 6))
        if self.users.usernames():
            self.user_entry.set(self.users.usernames()[0])

        self.password_entry = ctk.CTkEntry(account, width=110, height=32,
                                           placeholder_text="Password",
                                           show="\u2022",
                                           border_color=BORDER, fg_color=BG)
        self.password_entry.pack(side="left", padx=(0, 6))
        self.password_entry.bind("<Return>", lambda _e: self._login())

        self.login_button = ctk.CTkButton(account, text="Log in", width=70,
                                          height=32, corner_radius=8,
                                          fg_color=ACCENT,
                                          hover_color=ACCENT_HOVER,
                                          font=ctk.CTkFont(size=12),
                                          command=self._login)
        self.login_button.pack(side="left", padx=(0, 8))

        self.account_label = _StatusLabel(account, text="not logged in",
                                          text_color=MUTED,
                                          font=ctk.CTkFont(size=12))
        self.account_label.pack(side="left")

    def _build_main_area(self):
        area = ctk.CTkFrame(self.root, fg_color="transparent")
        area.grid(row=1, column=1, sticky="nsew", padx=14, pady=(8, 4))
        area.grid_columnconfigure(0, weight=4, uniform="cols")
        area.grid_columnconfigure(1, weight=5, uniform="cols")
        area.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(area, fg_color=CARD_BG, corner_radius=12,
                            border_width=1, border_color=BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.grid_rowconfigure(1, weight=1)
        left.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(left, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        ctk.CTkLabel(head, text="Inbox", text_color=TEXT,
                     font=ctk.CTkFont(size=15, weight="bold")).pack(side="left")
        ctk.CTkButton(head, text="Refresh", width=76, height=28,
                      corner_radius=7, fg_color=BG, text_color=ACCENT,
                      hover_color=CARD_HOVER, border_width=1,
                      border_color=BORDER, font=ctk.CTkFont(size=11),
                      command=self._refresh_inbox).pack(side="right")

        self.tree = EmailListView(left, on_open=self._read_selected)
        self.tree.grid(row=1, column=0, sticky="nsew", padx=8, pady=(4, 10))

        self.tabs = ctk.CTkTabview(
            area, fg_color=CARD_BG, corner_radius=12, border_width=1,
            border_color=BORDER, segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER)
        self.tabs.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.tabs.add("Compose")
        self.tabs.add("Inbox")
        self._build_compose_tab(self.tabs.tab("Compose"))
        self._build_inbox_tab(self.tabs.tab("Inbox"))

    def _build_compose_tab(self, tab):
        tab.grid_columnconfigure(1, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(tab, text="To:", text_color=MUTED,
                     font=ctk.CTkFont(size=12)).grid(row=0, column=0,
                                                     sticky="w", pady=6)
        self.to_entry = ctk.CTkEntry(tab, height=34, border_color=BORDER,
                                     fg_color=BG,
                                     placeholder_text="bob@local, alice@local")
        self.to_entry.grid(row=0, column=1, sticky="ew", pady=6, padx=(8, 0))

        ctk.CTkLabel(tab, text="Subject:", text_color=MUTED,
                     font=ctk.CTkFont(size=12)).grid(row=1, column=0,
                                                     sticky="w", pady=6)
        self.subject_entry = ctk.CTkEntry(tab, height=34, border_color=BORDER,
                                          fg_color=BG)
        self.subject_entry.grid(row=1, column=1, sticky="ew", pady=6,
                                padx=(8, 0))

        self.body_text = ctk.CTkTextbox(tab, border_width=1,
                                        border_color=BORDER, fg_color=BG,
                                        text_color=TEXT, corner_radius=8,
                                        font=ctk.CTkFont(size=13))
        self.body_text.grid(row=2, column=0, columnspan=2, sticky="nsew",
                            pady=(8, 6))

        attach = ctk.CTkFrame(tab, fg_color="transparent")
        attach.grid(row=3, column=0, columnspan=2, sticky="ew", pady=2)
        ctk.CTkButton(attach, text="\U0001F4CE Attach", width=90, height=30,
                      corner_radius=7, fg_color=BG, text_color=ACCENT,
                      hover_color=CARD_HOVER, border_width=1,
                      border_color=BORDER, font=ctk.CTkFont(size=11),
                      command=self._attach_file).pack(side="left")
        ctk.CTkButton(attach, text="Clear", width=60, height=30,
                      corner_radius=7, fg_color=BG, text_color=MUTED,
                      hover_color=CARD_HOVER, border_width=1,
                      border_color=BORDER, font=ctk.CTkFont(size=11),
                      command=self._clear_attachments).pack(side="left",
                                                            padx=6)
        self.attach_label = _StatusLabel(attach, text="no attachments",
                                         text_color=MUTED,
                                         font=ctk.CTkFont(size=11))
        self.attach_label.pack(side="left", padx=10)

        actions = ctk.CTkFrame(tab, fg_color="transparent")
        actions.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        self.send_button = ctk.CTkButton(actions, text="\u2708  Send (SMTP)",
                                         height=38, corner_radius=8,
                                         fg_color=ACCENT,
                                         hover_color=ACCENT_HOVER,
                                         font=ctk.CTkFont(size=13,
                                                          weight="bold"),
                                         command=self._send)
        self.send_button.pack(side="left")
        ctk.CTkButton(actions, text="Clear", height=38, width=80,
                      corner_radius=8, fg_color=BG, text_color=MUTED,
                      hover_color=CARD_HOVER, border_width=1,
                      border_color=BORDER, command=self._clear_compose
                      ).pack(side="left", padx=8)

    def _build_inbox_tab(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        buttons = ctk.CTkFrame(tab, fg_color="transparent")
        buttons.grid(row=0, column=0, sticky="ew", pady=(2, 8))
        for text, width, cmd in (("Refresh", 72, self._refresh_inbox),
                                 ("Read", 60, self._read_selected),
                                 ("Delete", 66, self._delete_selected),
                                 ("Save files", 84, self._save_attachments)):
            ctk.CTkButton(buttons, text=text, width=width, height=30,
                          corner_radius=7, fg_color=BG, text_color=ACCENT,
                          hover_color=CARD_HOVER, border_width=1,
                          border_color=BORDER, font=ctk.CTkFont(size=11),
                          command=cmd).pack(side="left", padx=(0, 6))

        # Header fields (like Compose view)
        headers = ctk.CTkFrame(tab, fg_color="transparent")
        headers.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        headers.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(headers, text="From:", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).grid(row=0, column=0,
                                                     sticky="w", padx=(0, 8))
        self.msg_from = ctk.CTkLabel(headers, text="", text_color=TEXT,
                                     font=ctk.CTkFont(size=11))
        self.msg_from.grid(row=0, column=1, sticky="w")

        ctk.CTkLabel(headers, text="To:", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).grid(row=1, column=0,
                                                     sticky="w", padx=(0, 8), pady=(6, 0))
        self.msg_to = ctk.CTkLabel(headers, text="", text_color=TEXT,
                                   font=ctk.CTkFont(size=11))
        self.msg_to.grid(row=1, column=1, sticky="w", pady=(6, 0))

        ctk.CTkLabel(headers, text="Subject:", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).grid(row=2, column=0,
                                                     sticky="w", padx=(0, 8), pady=(6, 0))
        self.msg_subject = ctk.CTkLabel(headers, text="", text_color=TEXT,
                                        font=ctk.CTkFont(size=11, weight="bold"))
        self.msg_subject.grid(row=2, column=1, sticky="w", pady=(6, 0))

        ctk.CTkLabel(headers, text="Date:", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).grid(row=3, column=0,
                                                     sticky="w", padx=(0, 8), pady=(6, 0))
        self.msg_date = ctk.CTkLabel(headers, text="", text_color=TEXT,
                                     font=ctk.CTkFont(size=10))
        self.msg_date.grid(row=3, column=1, sticky="w", pady=(6, 0))

        ctk.CTkLabel(headers, text="Files:", text_color=MUTED,
                     font=ctk.CTkFont(size=11)).grid(row=4, column=0,
                                                     sticky="w", padx=(0, 8), pady=(6, 0))
        self.msg_files = ctk.CTkLabel(headers, text="", text_color=ACCENT,
                                      font=ctk.CTkFont(size=10))
        self.msg_files.grid(row=4, column=1, sticky="w", pady=(6, 0))

        # Message body
        self.message_view = ctk.CTkTextbox(tab, border_width=1,
                                           border_color=BORDER, fg_color=BG,
                                           text_color=TEXT, corner_radius=8,
                                           font=ctk.CTkFont(size=13),
                                           wrap="word")
        self.message_view.grid(row=2, column=0, sticky="nsew", pady=(4, 0))
        self.message_view.configure(state="disabled")

    def _build_trace_pane(self):
        frame = ctk.CTkFrame(self.root, fg_color=CARD_BG, corner_radius=12,
                             border_width=1, border_color=BORDER, height=210)
        frame.grid(row=2, column=1, sticky="nsew", padx=14, pady=(4, 12))
        frame.grid_propagate(False)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        controls = ctk.CTkFrame(frame, fg_color="transparent")
        controls.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        controls.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(controls, text="\u2265_  Protocol Trace", text_color=TEXT,
                     font=ctk.CTkFont(size=13, weight="bold")
                     ).grid(row=0, column=0, sticky="w")

        right = ctk.CTkFrame(controls, fg_color="transparent")
        right.grid(row=0, column=2, sticky="e")
        self.status_label = _StatusLabel(right, text="", text_color=MUTED,
                                         font=ctk.CTkFont(size=11))
        self.status_label.pack(side="left", padx=(0, 12))
        self.trace_enabled = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(right, text="Show trace", variable=self.trace_enabled,
                        checkbox_width=18, checkbox_height=18,
                        fg_color=ACCENT, hover_color=ACCENT_HOVER,
                        font=ctk.CTkFont(size=11)).pack(side="left", padx=6)
        ctk.CTkButton(right, text="Clear", width=58, height=26, corner_radius=7,
                      fg_color=BG, text_color=MUTED, hover_color=CARD_HOVER,
                      border_width=1, border_color=BORDER,
                      font=ctk.CTkFont(size=11), command=self._clear_trace
                      ).pack(side="left", padx=(6, 0))

        self.trace_view = ctk.CTkTextbox(frame, fg_color=TRACE_BG,
                                         text_color=TRACE_FG, corner_radius=8,
                                         wrap="none",
                                         font=ctk.CTkFont(family="Courier New",
                                                          size=12))
        self.trace_view.grid(row=1, column=0, sticky="nsew", padx=12,
                             pady=(0, 12))
        for kind, colour in TRACE_COLOURS.items():
            self.trace_view.tag_config(kind, foreground=colour)
        self.trace_view.configure(state="disabled")

    # ------------------------------------------------------------------ #
    #  UI-only helpers (new; do not touch protocol logic)
    # ------------------------------------------------------------------ #

    def _focus_compose(self):
        self.tabs.set("Compose")
        self.to_entry.focus_set()

    def _clear_compose(self):
        self.to_entry.delete(0, "end")
        self.subject_entry.delete(0, "end")
        self.body_text.delete("1.0", "end")
        self._clear_attachments()

    def _on_search(self):
        """Filter the already-fetched message list locally; no POP3 traffic."""
        query = self.search_entry.get().strip()
        shown, total = self.tree.filter(query)
        if query:
            self._set_status(f"{shown} of {total} message(s) match “{query}”")
        elif total:
            self._set_status(f"{total} message(s) in the maildrop")

    def _clear_search(self):
        self.search_entry.delete(0, "end")
        self._on_search()

    # ------------------------------------------------------------------ #
    #  Thread-safe plumbing  (unchanged from the original)
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
    #  Server controls  (unchanged logic)
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
            self.smtp_status.config(text=f"Running on port {self.smtp_port}",
                                    foreground=GREEN)
        else:
            self.smtp_server.stop()
            self.smtp_server = None
            self.smtp_button.config(text="Start SMTP")
            self.smtp_status.config(text=f"stopped ({self.smtp_port})",
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
            self.pop_status.config(text=f"Running on port {self.pop_port}",
                                   foreground=GREEN)
        else:
            self.pop_server.stop()
            self.pop_server = None
            self.pop_button.config(text="Start POP3")
            self.pop_status.config(text=f"stopped ({self.pop_port})",
                                   foreground="")

    # ------------------------------------------------------------------ #
    #  Account  (unchanged logic)
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
                text=f"{username}  \u2022  Logged in", foreground=GREEN)
            self._set_status(f"Logged in as {username} "
                             f"({count} msg, {octets} octets)")
            self._refresh_inbox()

        self._set_status("Logging in over POP3...")
        self._run_async(work, done, self._login_failed)

    def _login_failed(self, exc):
        self.username = self.password = None
        self.account_label.config(text="login failed", foreground=RED)
        message = str(exc)
        if isinstance(exc, ConnectionRefusedError):
            message = (f"Connection refused on port {self.pop_port}.\n\n"
                       f"Start the POP3 server first, either with the button "
                       f"in the sidebar or with 'python3 pop3_server.py'.")
        messagebox.showerror("Login failed", message)
        self._set_status("Login failed")

    def _require_login(self):
        if self.username is None:
            messagebox.showwarning("Not logged in",
                                   "Log in before sending or reading mail.")
            return False
        return True

    # ------------------------------------------------------------------ #
    #  Compose  (unchanged logic)
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
            self.attach_label.config(text="no attachments", foreground=MUTED)
        else:
            names = ", ".join(p.name for p in self.attachments)
            total = sum(p.stat().st_size for p in self.attachments)
            self.attach_label.config(text=f"{names}  ({total} bytes)",
                                     foreground=ACCENT)

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
    #  Inbox  (unchanged logic; self.tree is now the card list adapter)
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
            status = f"{len(rows)} message(s) in the maildrop"
            if self.search_entry.get().strip():
                status += f", {self.tree.shown_count()} match the search"
            self._set_status(status)

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
            # Update header fields
            self.msg_from.configure(text=message.sender)
            self.msg_to.configure(text=message.recipients)
            self.msg_subject.configure(text=message.subject)
            self.msg_date.configure(text=message.date)
            if message.attachments:
                files_text = ", ".join(f"{a.filename} ({a.size} B)" for a in message.attachments)
            else:
                files_text = "(no attachments)"
            self.msg_files.configure(text=files_text)

            # Update body
            self.message_view.configure(state="normal")
            self.message_view.delete("1.0", "end")
            self.message_view.insert("1.0", message.body.replace("\r\n", "\n"))
            self.message_view.configure(state="disabled")
            self.tabs.set("Inbox")
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
            for label in (self.msg_from, self.msg_to, self.msg_subject,
                          self.msg_date, self.msg_files):
                label.configure(text="")
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
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    MailClientGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
