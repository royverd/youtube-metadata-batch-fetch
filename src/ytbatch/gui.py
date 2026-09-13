"""
gui.py

Desktop front end for the whole workflow, in three tabs sharing one log pane:

  Fetch   grab_watchlist (playlist -> IDs) then batch_fetch (IDs ->
          data/metadata.json + data/metadata.csv, transcripts included)
  Screen  runs Claude Code over the next N unscreened videos against the
          youtube-video-screener skill, and renders the result to HTML
  Review  records what you actually did with each video and what you thought
          of it, into data/progress.json

Nothing here reimplements the pipeline or the screening prompt - it collects
the settings the CLI would have prompted for, then calls the same functions.
The skill is edited in place and re-read by Claude every run, so this file
never holds a copy of it.

tkinter on purpose: it ships with Python, so the GUI adds no dependency to a
project whose whole install is currently "yt-dlp and youtube-transcript-api".

The work runs on a background thread with stdout piped into the log pane, so
the window stays responsive and Stop lands immediately - including mid-delay,
since the transcript pass waits on the stop event rather than sleeping.

Run: ytb-gui
Deps: same as batch_fetch (yt-dlp on PATH, youtube-transcript-api), plus the
claude CLI for the Screen tab. No Python extras - tkinter ships with Python.
"""

import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from . import (batch_fetch, config, grab_watchlist, paths, progress,
               render_screening, screen, settings, theme)

POLL_MS = 60          # log drain interval; fast enough to look live, cheap enough to ignore
MAX_LOG_LINES = 5000  # trim from the top beyond this - a long run otherwise grows the widget forever

# transcript_pass and fetch both emit "[n/total]" as their first token; parsing
# it is what drives the progress bar without threading a callback through the
# pipeline just for the GUI's benefit.
PROGRESS_RE = re.compile(r"\[(\d+)/(\d+)\]")

MODES = [
    ("0", "Proxy pool", "Origin IP first, then proxies.txt plus an auto-refreshed free pool."),
    ("1", "Webshare", "Origin IP first, then your Webshare account (needs the env vars set)."),
    ("2", "Direct only", "This machine's IP only, spaced out. No proxy fallback."),
]


# Thresholds descend so the first hit is the largest applicable unit. One
# decimal only below 100 - "19.3M" is informative, "193.4M" is noise.
_UNITS = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))


def _short_count(n):
    """19300001 -> 19.3M. Views are for comparing at a glance, and a raw
    eight-digit number in a narrow column is read digit by digit or not at
    all."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n < 0:
        return ""
    for size, suffix in _UNITS:
        # 0.9995 rather than 1.0: a value just under a unit rounds up into it
        # at display precision, and 999,999 shown as "1000K" is a number
        # nobody writes. Claim it for the larger unit instead.
        if n >= size * 0.9995:
            scaled = n / size
            return f"{scaled:.0f}{suffix}" if scaled >= 100 else f"{scaled:.1f}{suffix}"
    return str(n)


def _pretty_date(raw):
    """yt-dlp gives YYYYMMDD. Rendered with dashes so it reads as a date and
    still sorts correctly as a plain string."""
    raw = str(raw or "")
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 and raw.isdigit() else ""


def _expand_count(short):
    try:
        for size, suffix in _UNITS:
            if short.endswith(suffix):
                return int(float(short[:-1]) * size)
        return int(short)
    except (TypeError, ValueError, AttributeError):
        return 0


def _sort_key(col, row, desc=False):
    """One key function for every column.

    Direction is left entirely to sort(reverse=), never baked in by negating
    the value - doing both cancels out, which is how Rating and Views ended up
    sorting worst-first the moment they were given a descending default.

    Missing values always land at the bottom whichever way the column is
    sorted, so the rank flag is inverted under reverse rather than riding
    along with it."""
    def rank(flag):
        return -flag if desc else flag

    value = row.get(col, "")
    if col == "num":
        # Archived rows sink below every numbered one instead of sorting as
        # text among them.
        return (rank(0), value, "") if isinstance(value, int) else (rank(1), 0, "")
    if col == "views":
        # Sort on the number, not on the abbreviation: as text "9.9K" beats
        # "19.3M" and the column becomes actively misleading.
        return (rank(0), _expand_count(value), "") if value else (rank(1), 0, "")
    if col == "rating":
        if progress.is_rating(value):
            return (rank(0), value, "")
        # The skip sentinel is not a score; it sits below every real one.
        return (rank(1), 0, "") if value == progress.UNRATED else (rank(2), 0, "")
    text = str(value)
    return (rank(1), 0, "") if not text else (rank(0), 0, text.casefold())


def _atomic_write(path, text):
    """Same temp-and-replace as progress.save. Used for the skill and the
    rendered page: both are things an interrupted write would leave truncated
    with no copy anywhere else."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".ytb-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class QueueWriter:
    """stdout replacement for the worker thread. Text goes to a queue and the
    Tk thread drains it - widgets must only ever be touched from that thread."""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        self.q.put(s)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


class App:
    def __init__(self, root):
        self.root = root
        self.log_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self._run_active = False      # a worker is running and hasn't been finalized yet
        self._overwrite_line = False  # set by a \r, consumed by the next write
        # Every button that must go dead for the duration of a run registers
        # here. The alternative - naming each one in _start and _finished -
        # is how the two-tab version was written, and adding a third action
        # meant editing both.
        self._action_btns = []
        self.cfg = config.load()
        self.ui = settings.load()
        self.ui_font, self.mono_font = theme.apply(root, self.ui["theme"])
        # Review tab state, restored so a session picks up the table exactly
        # as it was left.
        self._sort_col = self.ui.get("review_sort", "num")
        self._sort_desc = bool(self.ui.get("review_desc", False))
        self._saved_widths = self.ui.get("review_widths") or {}

        root.title("ytbatch")
        if self.ui.get("geometry"):
            root.geometry(self.ui["geometry"])
        else:
            # A fixed size is either cramped on a laptop or a postage stamp on
            # a 4K panel. Take most of the screen, capped so it stays a window.
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            w, h = min(int(sw * 0.80), 2300), min(int(sh * 0.85), 1550)
            root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
        root.minsize(900, 640)
        if self.ui.get("fullscreen"):
            root.attributes("-fullscreen", True)
        root.bind("<F11>", lambda _e: self.toggle_fullscreen())
        root.bind("<Escape>", lambda _e: self.set_fullscreen(False))

        self._build_widgets()
        self.refresh_watchlist_count()
        self.refresh_screen_counts()
        self.refresh_review()
        self._banner()
        # Deferred: sashpos is meaningless until the panes have been mapped and
        # given a height, and setting it now would silently clamp to zero.
        if self.ui.get("sash"):
            self.root.after(120, lambda: self._restore_sash(self.ui["sash"]))
        self.root.after(POLL_MS, self._drain_log)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- layout ----------

    def _build_widgets(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # A paned split rather than a fixed stack: the Review table and the
        # log compete for the same vertical space, and which one you want
        # larger changes by the minute.
        self.split = ttk.PanedWindow(outer, orient="vertical")
        self.split.pack(fill="both", expand=True)

        top = ttk.Frame(self.split)
        self.split.add(top, weight=3)

        nb = ttk.Notebook(top)
        nb.pack(fill="both", expand=True)
        nb.add(self._build_fetch_tab(nb), text="Fetch")
        nb.add(self._build_screen_tab(nb), text="Screen")
        nb.add(self._build_review_tab(nb), text="Review")
        nb.add(self._build_settings_tab(nb), text="Settings")

        # Log, progress and status sit below the notebook rather than inside a
        # tab: a run started on one tab stays visible while you work on another.
        logframe = ttk.LabelFrame(self.split, text="Log", padding=6)
        self.split.add(logframe, weight=2)
        self.log = tk.Text(logframe, wrap="none", height=10,
                           background=theme.P["log_bg"],
                           foreground=theme.P["log_fg"],
                           insertbackground=theme.P["log_fg"],
                           font=(self.mono_font, 10), state="disabled",
                           relief="flat", borderwidth=0, padx=10, pady=8)
        yscroll = ttk.Scrollbar(logframe, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=yscroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

        status = ttk.Frame(outer)
        status.pack(fill="x", side="bottom", pady=(8, 0))
        self.status_var = tk.StringVar(value="Idle.")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")
        self.stop_btn = ttk.Button(status, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="right", padx=(6, 0))
        ttk.Button(status, text="Clear Log", command=self.on_clear).pack(side="right")
        self.progress = ttk.Progressbar(status, length=200, mode="determinate")
        self.progress.pack(side="right", padx=(0, 10))

    def _action(self, parent, text, command, **kw):
        """A button that has to be dead while a worker runs."""
        btn = ttk.Button(parent, text=text, command=command, **kw)
        self._action_btns.append(btn)
        return btn

    def _build_fetch_tab(self, parent):
        tab = ttk.Frame(parent, padding=10)

        step1 = ttk.LabelFrame(tab, text="1. Watchlist", padding=10)
        step1.pack(fill="x")
        step1.columnconfigure(1, weight=1)

        ttk.Label(step1, text="Browser:").grid(row=0, column=0, sticky="w", pady=2)
        self.browser_var = tk.StringVar(value="System default")
        browser_names = ["System default"] + [label for label, _ in grab_watchlist.BROWSERS]
        self.browser_box = ttk.Combobox(step1, textvariable=self.browser_var,
                                        values=browser_names, state="readonly", width=18)
        self.browser_box.grid(row=0, column=1, sticky="w", padx=(6, 0), pady=2)

        ttk.Label(step1, text="Playlist:").grid(row=1, column=0, sticky="w", pady=2)
        self.playlist_var = tk.StringVar(value=grab_watchlist.DEFAULT_PLAYLIST)
        ttk.Entry(step1, textvariable=self.playlist_var).grid(
            row=1, column=1, sticky="ew", padx=(6, 6), pady=2)
        self.grab_btn = self._action(step1, "Fetch Watchlist", self.on_fetch_watchlist)
        self.grab_btn.grid(row=1, column=2, sticky="e", pady=2)

        self.watchlist_lbl = ttk.Label(step1, text="", style="Muted.TLabel")
        self.watchlist_lbl.grid(row=2, column=1, sticky="w", padx=(6, 0), pady=(4, 0))

        step2 = ttk.LabelFrame(tab, text="2. Transcript routing", padding=10)
        step2.pack(fill="x", pady=(10, 0))
        self.mode_var = tk.StringVar(value="0")
        for row, (value, name, desc) in enumerate(MODES):
            ttk.Radiobutton(step2, text=name, value=value,
                            variable=self.mode_var).grid(row=row, column=0, sticky="w")
            ttk.Label(step2, text=desc, style="Muted.TLabel").grid(
                row=row, column=1, sticky="w", padx=(10, 0))

        actions = ttk.Frame(tab)
        actions.pack(fill="x", pady=(10, 0))
        self.run_btn = self._action(actions, "Run Fetch", self.on_run)
        self.run_btn.pack(side="left")
        ttk.Button(actions, text="Open Output Folder",
                   command=self.on_open_folder).pack(side="right")
        return tab

    def _build_screen_tab(self, parent):
        tab = ttk.Frame(parent, padding=10)

        batch = ttk.LabelFrame(tab, text="Screen with Claude Code", padding=10)
        batch.pack(fill="x")
        ttk.Label(batch, text="Screen next:").pack(side="left")
        self.batch_var = tk.StringVar(value="10")
        ttk.Spinbox(batch, from_=1, to=100, width=5,
                    textvariable=self.batch_var).pack(side="left", padx=(6, 6))
        self._action(batch, "Run", self.on_screen).pack(side="left")
        self._action(batch, "Open Claude terminal",
                     self.on_claude_terminal).pack(side="left", padx=(16, 0))

        out = ttk.LabelFrame(tab, text="Skill and output", padding=10)
        out.pack(fill="x", pady=(10, 0))
        self._action(out, "Edit skill", self.on_edit_skill).pack(side="left")
        self._action(out, "Render + open", self.on_render).pack(side="left", padx=(6, 0))
        self._action(out, "Backfill from titles",
                     self.on_backfill).pack(side="left", padx=(6, 0))

        self.screen_lbl = ttk.Label(tab, text="", style="Muted.TLabel")
        self.screen_lbl.pack(anchor="w", pady=(10, 0))
        return tab

    # Column id -> (heading, default width, min width, alignment). Everything
    # is centred except the title: titles vary wildly in length, and centred
    # ragged-both text gives the eye no left edge to scan down.
    REVIEW_COLS = (
        ("num", "#", 55, 44, "center"),
        ("date", "Date", 100, 80, "center"),
        ("title", "Title", 380, 120, "w"),
        ("channel", "Channel", 160, 80, "center"),
        ("views", "Views", 82, 62, "center"),
        ("ai", "AI", 72, 52, "center"),
        ("status", "You", 128, 70, "center"),
        ("rating", "Rating", 76, 58, "center"),
    )

    def _build_review_tab(self, parent):
        tab = ttk.Frame(parent, padding=12)

        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 8))
        self.hide_decided = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Hide decided", variable=self.hide_decided,
                        command=self.refresh_review).pack(side="left")
        ttk.Label(bar, text="Click a heading to sort; click again to reverse.",
                  style="Muted.TLabel").pack(side="left", padx=(14, 0))
        ttk.Button(bar, text="Refresh", command=self.refresh_review).pack(side="right")

        # The scrollbar gets its own grid column rather than being placed over
        # the tree: overlaying it covers the last column's separator, and a
        # header drag that lands on the scrollbar does nothing at all.
        holder = ttk.Frame(tab)
        holder.pack(side="top", fill="both", expand=True)
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)

        cols = tuple(c[0] for c in self.REVIEW_COLS)
        # extended, not browse: ctrl-click adds, shift-click takes a run, and
        # Save then applies one decision to the whole selection.
        self.tree = ttk.Treeview(holder, columns=cols, show="headings", height=12,
                                 selectmode="extended")
        for col, label, width, minwidth, anchor in self.REVIEW_COLS:
            # Heading anchor is separate from the column's: setting only the
            # column leaves every header hugging the left while its cells sit
            # centred underneath.
            self.tree.heading(col, text=label, anchor=anchor,
                              command=lambda c=col: self.on_sort_column(c))
            # Every column stretches: with only one stretching, dragging any
            # other separator was immediately undone by the re-layout.
            self.tree.column(col, width=self._saved_widths.get(col, width),
                             minwidth=minwidth, stretch=True, anchor=anchor)
        for slug, colour in theme.verdicts().items():
            self.tree.tag_configure(f"ai-{slug}", foreground=colour)
        tscroll = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tscroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self.on_select_video)
        self.tree.bind("<Double-1>", lambda _e: self.on_open_video())
        # Ctrl-A has no default binding on a Treeview, and the stock Text
        # binding would fire first if this were left to the toplevel.
        for seq in ("<Control-a>", "<Control-A>"):
            self.tree.bind(seq, self.on_select_all)

        entry = ttk.Frame(tab)
        entry.pack(fill="x", pady=(12, 0))
        self.status_choice = tk.StringVar(value="")
        self.status_choice.trace_add("write", self._on_status_change)
        for value, label in (("watched_full", "Watched in full"),
                             ("scrubbed", "Scrubbed"),
                             ("read_summary", "Read the summary"),
                             ("skipped", "Skipped")):
            ttk.Radiobutton(entry, text=label, value=value,
                            variable=self.status_choice).pack(side="left", padx=(0, 14))
        self.sel_lbl = ttk.Label(entry, text="", style="Muted.TLabel")
        self.sel_lbl.pack(side="right")
        ttk.Label(entry, text="Rating:").pack(side="left", padx=(10, 5))
        self.rating_var = tk.StringVar(value="")
        # Steps of 0.5 for the arrows, but the field is typeable - the scale
        # goes to one decimal, and 7.3 should not need nine clicks.
        ttk.Spinbox(entry, from_=1, to=10, increment=0.5, format="%.1f", width=5,
                    textvariable=self.rating_var).pack(side="left")
        ttk.Button(entry, text="Save", command=self.on_save_decision,
                   style="Accent.TButton").pack(side="left", padx=(14, 0))
        ttk.Button(entry, text="Open video",
                   command=self.on_open_video).pack(side="left", padx=(6, 0))

        self.review_lbl = ttk.Label(tab, text="", style="Muted.TLabel")
        self.review_lbl.pack(anchor="w", pady=(10, 0))
        return tab

    def _build_settings_tab(self, parent):
        tab = ttk.Frame(parent, padding=10)
        tab.columnconfigure(1, weight=1)

        # Empty in config means "detect at runtime", so the entries show what
        # detection found and only persist a value once it is actually edited.
        self.setting_vars = {}
        rows = (
            ("_home", "Corpus folder", str(paths.home())),
            ("skill_path", "Screener SKILL.md", screen.find_skill()),
            ("claude_bin", "claude binary", screen.find_claude()),
            ("screening_md", "Screening markdown", screen.screening_path(self.cfg)),
        )
        for row, (key, label, detected) in enumerate(rows):
            ttk.Label(tab, text=label + ":").grid(row=row, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=self.cfg.get(key) or detected)
            self.setting_vars[key] = var
            ttk.Entry(tab, textvariable=var).grid(row=row, column=1, sticky="ew",
                                                  padx=(8, 0), pady=3)

        row = len(rows)
        ttk.Label(tab, text="Browser:").grid(row=row, column=0, sticky="w", pady=3)
        found = screen.find_browsers()
        self.browser_pref = tk.StringVar(value=self.cfg.get("browser") or (found[0] if found else ""))
        ttk.Combobox(tab, textvariable=self.browser_pref, values=found or [""],
                     state="readonly" if found else "normal").grid(
            row=row, column=1, sticky="w", padx=(8, 0), pady=3)

        row += 1
        ttk.Separator(tab, orient="horizontal").grid(
            row=row, column=0, columnspan=2, sticky="ew", pady=(14, 10))

        row += 1
        ttk.Label(tab, text="Appearance:").grid(row=row, column=0, sticky="w", pady=3)
        appearance = ttk.Frame(tab)
        appearance.grid(row=row, column=1, sticky="w", padx=(8, 0), pady=3)
        self.theme_var = tk.StringVar(value=self.ui["theme"])
        theme_box = ttk.Combobox(appearance, textvariable=self.theme_var,
                                 values=list(settings.THEMES), state="readonly", width=10)
        theme_box.pack(side="left")
        # Applied on pick rather than on Save: a theme you have to commit to
        # before seeing is a theme you choose twice.
        theme_box.bind("<<ComboboxSelected>>", lambda _e: self.apply_theme(self.theme_var.get()))
        self.fullscreen_var = tk.BooleanVar(value=bool(self.ui.get("fullscreen")))
        ttk.Checkbutton(appearance, text="Fullscreen (F11)",
                        variable=self.fullscreen_var,
                        command=lambda: self.set_fullscreen(self.fullscreen_var.get())
                        ).pack(side="left", padx=(16, 0))

        row += 1
        ttk.Button(tab, text="Save settings", command=self.on_save_settings).grid(
            row=row, column=1, sticky="w", padx=(8, 0), pady=(12, 0))
        ttk.Label(tab, text=f"Blank fields are re-detected on each start. "
                            f"Window, theme and table layout live in {settings.PATH}.",
                  style="Muted.TLabel").grid(row=row + 1, column=1, sticky="w",
                                             padx=(8, 0), pady=(6, 0))
        return tab

    def _restore_sash(self, pos):
        try:
            if 60 < pos < self.split.winfo_height() - 60:
                self.split.sashpos(0, pos)
        except tk.TclError:
            pass

    def _banner(self):
        """Which corpus is loaded, said out loud at startup. Fetching into the
        wrong data/ looks exactly like fetching into the right one until the
        transcripts come back empty, so the anchor is never left implicit."""
        home = paths.home()
        self.log_line("ytbatch - fetch, screen and review a YouTube watchlist")
        self.log_line(f"corpus   {home}"
                      + ("  (from YTBATCH_HOME)" if os.environ.get(paths.ENV_HOME)
                         else "  (from settings)" if paths.read_marker()
                         else "  (working directory)"))
        try:
            corpus = screen.load_corpus()
            queued = screen.queued_count(corpus)
            self.log_line(f"metadata {len(corpus)} videos"
                          + (f" ({queued} in the watchlist, "
                             f"{len(corpus) - queued} archived)"
                             if len(corpus) != queued else ""))
        except (OSError, ValueError) as e:
            self.log_line(f"metadata not loaded: {e}")

    # ---------- log plumbing ----------

    def _drain_log(self):
        """Pull whatever the worker printed and render it. Batched per tick so
        a burst of output is one widget update rather than hundreds.

        This also owns end-of-run cleanup. Tk isn't thread-safe and even
        root.after() has to come from the main thread, so the worker never
        touches a widget - it just exits, and this loop notices. Liveness is
        sampled *before* draining: a thread already dead by then has
        necessarily finished queueing its output, so nothing gets finalized
        before its last lines are rendered."""
        finished = self._run_active and (self.worker is None or not self.worker.is_alive())
        chunks = []
        try:
            while True:
                chunks.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if chunks:
            self._append("".join(chunks))
        if finished:
            self._run_active = False
            self._finished()
        self.root.after(POLL_MS, self._drain_log)

    def _append(self, text):
        at_bottom = self.log.yview()[1] > 0.999  # only autoscroll if already following
        self.log.configure(state="normal")
        # The pipeline's live counters are "\r...", i.e. rewrite the current
        # line. A Text widget has no cursor-return, so emulate it: a \r means
        # the next visible chunk replaces the last line instead of appending.
        for part in re.split(r"(\r\n|\r|\n)", text):
            if not part:
                continue
            if part == "\r":
                self._overwrite_line = True
            elif part in ("\n", "\r\n"):
                self.log.insert("end", "\n")
                self._overwrite_line = False
            else:
                if self._overwrite_line:
                    self.log.delete("end-1c linestart", "end-1c")
                    self._overwrite_line = False
                self.log.insert("end", part)
                self._update_progress(part)
        # Trim oldest lines so a multi-thousand-video run can't grow unbounded.
        excess = int(self.log.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.configure(state="disabled")
        if at_bottom:
            self.log.see("end")

    def _update_progress(self, line):
        m = PROGRESS_RE.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if total:
                self.progress.configure(maximum=total, value=done)

    def log_line(self, text):
        """Post a GUI-side message through the same path as worker output, so
        ordering stays consistent instead of racing the queue."""
        self.log_queue.put(text + "\n")

    # ---------- actions ----------

    def refresh_watchlist_count(self):
        if os.path.isfile(batch_fetch.INPUT):
            n = len(batch_fetch.read_ids(batch_fetch.INPUT))
            self.watchlist_lbl.configure(text=f"watchlist.txt: {n} IDs")
        else:
            self.watchlist_lbl.configure(text="watchlist.txt: not found - fetch it first")

    def _selected_browser_token(self):
        """None means 'ask the OS', matching the CLI's 0-for-default option."""
        choice = self.browser_var.get()
        if choice == "System default":
            return grab_watchlist.detect_default_browser()
        for label, token in grab_watchlist.BROWSERS:
            if label == choice:
                return token
        return None

    def on_fetch_watchlist(self):
        if self._busy():
            return
        token = self._selected_browser_token()
        if not token:
            messagebox.showerror("Browser", "Couldn't detect the default browser. Pick one explicitly.")
            return
        playlist = self.playlist_var.get().strip().strip('"').strip("'")
        if not playlist:
            playlist = grab_watchlist.DEFAULT_PLAYLIST

        def work():
            print(f"Fetching watchlist from {token}...")
            ids = grab_watchlist.grab(token, playlist)
            if not ids:
                print("No IDs returned. Are you logged in on that browser, and is it fully closed?")
                return
            with open(batch_fetch.INPUT, "w", encoding="utf-8") as f:
                f.write("\n".join(ids) + "\n")
            print(f"Wrote {len(ids)} unique IDs to {batch_fetch.INPUT}")

        self._start(work, "Fetching watchlist...")

    def on_run(self):
        if self._busy():
            return
        if not os.path.isfile(batch_fetch.INPUT):
            messagebox.showerror("No watchlist", "watchlist.txt not found. Fetch the watchlist first.")
            return
        mode = self.mode_var.get()

        def work():
            batch_fetch.run_pipeline(mode, self.stop_event)

        self._start(work, "Fetching metadata and transcripts...")

    def on_stop(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.status_var.set("Stopping - finishing the current request...")
            self.stop_btn.configure(state="disabled")

    def on_clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.progress.configure(value=0)

    def on_open_folder(self):
        # Explorer only on Windows; the fallbacks keep this usable if the
        # project is ever run from macOS/Linux.
        folder = str(paths.data_dir())
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            messagebox.showerror("Open folder", f"Couldn't open {folder}\n{e}")

    # ---------- screening ----------

    def refresh_screen_counts(self):
        try:
            total = len(screen.load_corpus(queued_only=True))
        except (OSError, ValueError):
            total = None
        s = progress.summary(total)
        left = "?" if s["left"] is None else s["left"]
        self.screen_lbl.configure(
            text=f"{s['screened']} screened, {left} left  -  "
                 f"watch {s['verdict_watch']} / read {s['verdict_read']} / skip {s['verdict_skip']}"
                 f"  -  {s['decided']} decided, {s['rated']} rated")

    def on_screen(self):
        if self._busy():
            return
        try:
            n = int(self.batch_var.get())
        except ValueError:
            messagebox.showerror("Batch size", "Enter a whole number.")
            return
        cfg = self.cfg

        def work():
            screen.run(n, cfg, self.stop_event)

        self._start(work, f"Screening {n} videos with Claude Code...")

    def on_claude_terminal(self):
        """Claude runs in a real terminal emulator rather than in here: tkinter
        has no ANSI or alt-screen handling, and on Wayland there is no XEmbed
        to borrow a real one with."""
        name, args = screen.find_terminal()
        if not name:
            messagebox.showerror("No terminal", "No supported terminal emulator found.\n"
                                                "Tried: " + ", ".join(t for t, _ in screen.TERMINALS))
            return
        binary = screen.claude_bin(self.cfg)
        if not binary:
            messagebox.showerror("claude", "claude not found. Set its path in Settings.")
            return
        cwd = str(paths.home())
        cmd = [name] + [a.format(cwd=cwd) for a in args] + [binary]
        try:
            subprocess.Popen(cmd, cwd=cwd, start_new_session=True)
        except OSError as e:
            messagebox.showerror("Terminal", f"Couldn't launch {name}\n{e}")

    def on_edit_skill(self):
        """Plain text, no validation. The skill is a prompt, not a config file -
        anything that tried to parse it would go stale the moment it changed."""
        path = screen.skill_path(self.cfg)
        if not path or not os.path.isfile(path):
            messagebox.showerror("Skill", "Screener skill not found. Set its path in Settings.")
            return
        win = tk.Toplevel(self.root)
        win.title(path)
        win.geometry("900x700")
        win.configure(background=theme.P["bg"])
        text = tk.Text(win, wrap="word", undo=True, font=(self.mono_font, 11),
                       background=theme.P["card"], foreground=theme.P["fg"],
                       insertbackground=theme.P["fg"], relief="flat",
                       borderwidth=0, padx=14, pady=12)
        scroll = ttk.Scrollbar(win, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        bar = ttk.Frame(win, padding=6)
        bar.pack(side="bottom", fill="x")
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        with open(path, encoding="utf-8") as f:
            text.insert("1.0", f.read())
        text.edit_modified(False)

        def save():
            body = text.get("1.0", "end-1c")
            if not body.strip():
                messagebox.showwarning("Skill", "Refusing to save an empty skill.", parent=win)
                return
            try:
                _atomic_write(path, body)
            except OSError as e:
                messagebox.showerror("Skill", f"Couldn't write {path}\n{e}", parent=win)
                return
            text.edit_modified(False)
            self.log_line(f"Saved {path}")

        ttk.Button(bar, text="Save", command=save).pack(side="right")
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right", padx=(0, 6))

    def on_render(self):
        if self._busy():
            return
        md = screen.screening_path(self.cfg)
        if not os.path.isfile(md):
            messagebox.showerror("Render", f"Nothing to render: {md} does not exist.")
            return
        browser = self.cfg.get("browser") or (screen.find_browsers() or [None])[0]

        def work():
            page, total, tally = render_screening.build(
                open(md, encoding="utf-8").read())
            out = os.path.splitext(md)[0] + ".html"
            _atomic_write(out, page)
            print(f"\n{out}  {total} videos  "
                  f"watch {tally['watch']} / read {tally['read']} / skip {tally['skip']}")
            url = "file://" + os.path.abspath(out)
            try:
                (webbrowser.get(browser) if browser else webbrowser).open(url)
            except webbrowser.Error:
                # A configured browser that isn't registered is not worth
                # failing the render over - the page is already on disk.
                webbrowser.open(url)

        self._start(work, "Rendering...")

    def on_backfill(self):
        """For the write-ups made before blocks carried an id comment. Matches
        on exact title, so anything renamed or dropped is reported, not guessed."""
        if self._busy():
            return
        cfg = self.cfg

        def work():
            screen.backfill_ids(cfg=cfg)

        self._start(work, "Backfilling screening history...")

    # ---------- review ----------

    def refresh_review(self):
        try:
            corpus = screen.load_corpus()
        except (OSError, ValueError) as e:
            self.review_lbl.configure(
                text=f"No corpus at {paths.home()} - {e}. "
                     f"Set the corpus folder in Settings.")
            self.tree.delete(*self.tree.get_children())
            return
        data = progress.load()

        rows = []
        for pos, rec in corpus:
            entry = data.get(rec["id"])
            if not entry:
                continue
            rating = entry.get("rating")
            # "Decided" is anything carrying a rating, sentinel included -
            # which is the whole reason a skip stores "-" rather than nothing.
            if self.hide_decided.get() and rating not in (None, ""):
                continue
            rows.append((rec["id"], {
                # Archived videos carry no queue position; the dash says
                # "kept, but no longer in your watchlist".
                "num": pos if pos != "" else progress.UNRATED,
                "date": _pretty_date(rec.get("upload_date")),
                "title": rec.get("title", ""),
                "channel": rec.get("channel", ""),
                "views": _short_count(rec.get("view_count")),
                "ai": entry.get("ai_verdict", ""),
                "status": (entry.get("status") or "").replace("_", " "),
                "rating": "" if rating is None else rating,
            }))

        rows.sort(key=lambda r: _sort_key(self._sort_col, r[1], self._sort_desc),
                  reverse=self._sort_desc)

        self.tree.delete(*self.tree.get_children())
        for vid, row in rows:
            verdict = row["ai"]
            self.tree.insert("", "end", iid=vid,
                             values=tuple(row[c[0]] for c in self.REVIEW_COLS),
                             tags=(f"ai-{verdict}",) if verdict else ())
        self._mark_sort_heading()

        s = progress.summary(len(corpus))
        mean = "-" if s["mean_rating"] is None else s["mean_rating"]
        shown = f"{len(rows)} shown  -  " if len(rows) != s["screened"] else ""
        self.review_lbl.configure(
            text=f"{shown}{s['decided']} decided of {s['screened']} screened  -  "
                 f"watched {s['watched_full']} / scrubbed {s['scrubbed']} / "
                 f"read {s['read_summary']} / skipped {s['skipped']}  -  "
                 f"mean rating {mean} over {s['rated']}")

    def on_sort_column(self, col):
        """Second click on the same heading reverses; a new column starts
        ascending, except the ones people read newest-first."""
        if col == self._sort_col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = col in ("date", "rating", "views")
        self.refresh_review()

    def _mark_sort_heading(self):
        arrow = " \u25be" if self._sort_desc else " \u25b4"
        for col, label, _w, _m, anchor in self.REVIEW_COLS:
            self.tree.heading(col, anchor=anchor,
                              text=label + (arrow if col == self._sort_col else ""))

    def _selected_videos(self):
        return list(self.tree.selection())

    def _selected_video(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def on_select_all(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"  # stop the class binding from re-running the select

    def on_select_video(self, _event=None):
        """With one row selected the fields show that video's saved decision.
        With several they are left alone: prefilling from whichever row happens
        to be first would make Save look like it was confirming what is already
        there, when it is about to overwrite all of them."""
        sel = self._selected_videos()
        self.sel_lbl.configure(text=f"{len(sel)} selected" if len(sel) > 1 else "")
        if len(sel) != 1:
            return
        entry = progress.get(sel[0])
        rating = entry.get("rating")
        self.status_choice.set(entry.get("status") or "")
        # An unrated video opens at the midpoint rather than empty; a rated one
        # shows what it already has, so Save never quietly changes a score.
        self.rating_var.set(str(rating) if rating not in (None, "")
                            else self.DEFAULT_RATING)

    # Midpoint start: the arrows are then at most five steps from either end,
    # instead of nine from the top when the box opens empty.
    DEFAULT_RATING = "5"

    BULK_CONFIRM = 5  # above this, a mis-click is expensive enough to ask about

    def _on_status_change(self, *_args):
        """Skipping and scoring are mutually exclusive - a skipped video was
        never watched. Without this the midpoint default would quietly record
        a 5 for every skip."""
        if self.status_choice.get() == "skipped":
            self.rating_var.set(progress.UNRATED)
        elif self.rating_var.get() == progress.UNRATED:
            self.rating_var.set(self.DEFAULT_RATING)

    def on_save_decision(self):
        vids = self._selected_videos()
        if not vids:
            messagebox.showinfo("Review", "Select a video first.")
            return
        status = self.status_choice.get()
        if not status:
            messagebox.showinfo("Review", "Pick what you did with it.")
            return
        raw = self.rating_var.get().strip()
        if len(vids) > self.BULK_CONFIRM and not messagebox.askokcancel(
                "Review", f"Apply \"{status.replace('_', ' ')}\""
                          + (f" and rating {raw}" if raw else "")
                          + f" to {len(vids)} videos?"):
            return
        saved, failed = 0, None
        for vid in vids:
            try:
                progress.set_decision(vid, status, raw or None)
            except ValueError as e:
                failed = e  # a bad rating fails identically for every row
                break
            saved += 1
        if failed is not None:
            messagebox.showerror("Review", str(failed))
        if not saved:
            return
        self.log_line(f"{saved} video(s): {status}" + (f", rated {raw}" if raw else ""))
        self.refresh_review()
        # Rows can vanish under Hide decided, so only reselect what survived.
        still = [v for v in vids if self.tree.exists(v)]
        if still:
            self.tree.selection_set(still)
            self.tree.see(still[0])

    def on_open_video(self):
        vids = self._selected_videos()
        if not vids:
            return
        if len(vids) > 1:
            if not messagebox.askokcancel("Review", f"Open {len(vids)} videos in tabs?"):
                return
            for vid in vids:
                self._open_url(f"https://www.youtube.com/watch?v={vid}")
                # Browsers silently drop tabs spawned in a tight loop.
                self.root.after(400)
            return
        vid = vids[0]
        self._open_url(f"https://www.youtube.com/watch?v={vid}")

    def _open_url(self, url):
        browser = self.cfg.get("browser") or (screen.find_browsers() or [None])[0]
        try:
            (webbrowser.get(browser) if browser else webbrowser).open(url)
        except webbrowser.Error:
            # A configured browser that isn't registered shouldn't mean no
            # browser at all - fall back to whatever the OS prefers.
            webbrowser.open(url)

    # ---------- appearance ----------

    def apply_theme(self, mode):
        """ttk restyles live, so only the raw Text widgets need repainting by
        hand - they are the two things ttk.Style cannot reach."""
        self.ui["theme"] = mode
        theme.apply(self.root, mode)
        self.log.configure(background=theme.P["log_bg"], foreground=theme.P["log_fg"],
                           insertbackground=theme.P["log_fg"])
        for slug, colour in theme.verdicts().items():
            self.tree.tag_configure(f"ai-{slug}", foreground=colour)
        self.refresh_review()

    def set_fullscreen(self, on):
        self.root.attributes("-fullscreen", bool(on))
        self.ui["fullscreen"] = bool(on)
        if hasattr(self, "fullscreen_var"):
            self.fullscreen_var.set(bool(on))

    def toggle_fullscreen(self):
        self.set_fullscreen(not self.root.attributes("-fullscreen"))

    # ---------- settings ----------

    def on_save_settings(self):
        # The corpus folder can't live in config.json: config.json is found
        # *through* it. It goes to a marker file outside the corpus instead,
        # and only takes hold on restart because every module resolves its
        # paths at import.
        new_home = self.setting_vars["_home"].get().strip().strip('"').strip("'")
        if new_home and new_home != str(paths.home()):
            if not os.path.isdir(new_home):
                messagebox.showerror("Settings", f"Not a directory: {new_home}")
                return
            try:
                paths.write_marker(new_home)
            except OSError as e:
                messagebox.showerror("Settings", f"Couldn't save the corpus folder\n{e}")
                return
            messagebox.showinfo("Settings", "Corpus folder saved. Restart to use it.")

        for key, var in self.setting_vars.items():
            if key.startswith("_"):
                continue
            self.cfg[key] = var.get().strip().strip('"').strip("'")
        self.cfg["browser"] = self.browser_pref.get().strip()
        try:
            config.save(self.cfg)
        except OSError as e:
            messagebox.showerror("Settings", f"Couldn't write config.json\n{e}")
            return
        self.log_line("Settings saved.")
        self.refresh_screen_counts()

    # ---------- worker lifecycle ----------

    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _start(self, work, status):
        self.stop_event.clear()
        self.progress.configure(value=0)
        self.status_var.set(status)
        for btn in self._action_btns:
            btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        def runner():
            # Redirect only for this thread's duration; the worker is the sole
            # writer while it runs, so a process-wide swap is safe here.
            real_stdout = sys.stdout
            sys.stdout = QueueWriter(self.log_queue)
            try:
                work()
            except FileNotFoundError as e:
                # Only the fetch stages shell out; anything else reaching here
                # is a missing data file, and blaming yt-dlp for that sent the
                # last reader looking in entirely the wrong place.
                name = getattr(e, "filename", "") or ""
                if "yt-dlp" in str(name) or "yt-dlp" in str(e):
                    print("yt-dlp not found on PATH. Install it first: pip install yt-dlp")
                else:
                    print(f"\nMissing file: {name or e}")
            except Exception as e:
                # A crashed worker must never leave the UI stuck on "running",
                # so every failure is reported rather than dying silently.
                print(f"\nFailed: {type(e).__name__}: {e}")
            finally:
                sys.stdout = real_stdout
                # No Tk call here on purpose - _drain_log notices this thread
                # exiting and finalizes on the main thread.

        self.worker = threading.Thread(target=runner, daemon=True)
        self._run_active = True
        self.worker.start()

    def _finished(self):
        for btn in self._action_btns:
            btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopped." if self.stop_event.is_set() else "Done.")
        self.refresh_watchlist_count()
        self.refresh_screen_counts()
        self.refresh_review()

    def _save_ui(self):
        try:
            full = bool(self.root.attributes("-fullscreen"))
            self.ui.update({
                "theme": self.ui.get("theme", "light"),
                "fullscreen": full,
                # A fullscreen window reports the screen as its geometry, which
                # would strand the next non-fullscreen session at that size.
                "geometry": self.ui.get("geometry", "") if full else self.root.geometry(),
                "sash": int(self.split.sashpos(0)),
                "review_sort": self._sort_col,
                "review_desc": self._sort_desc,
                "review_widths": {c[0]: int(self.tree.column(c[0], "width"))
                                  for c in self.REVIEW_COLS},
            })
            settings.save(self.ui)
        except (OSError, tk.TclError):
            # Losing window state is not worth blocking the close on.
            pass

    def _on_close(self):
        if self._busy():
            if not messagebox.askokcancel("Quit", "A fetch is still running. Stop it and quit?"):
                return
            self.stop_event.set()
        self._save_ui()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
