"""
gui.py

Desktop front end for the whole workflow: a sidebar with four pages and a log
drawer shared by all of them.

  Fetch     grab_watchlist (playlist -> IDs) then batch_fetch (IDs ->
            data/metadata.json + data/metadata.csv, transcripts included)
  Screen    screens unscreened videos with the configured AI against the
            youtube-video-screener skill, and renders the result to HTML
  Review    records what you actually did with each video and what you thought
            of it, into data/progress.json
  Settings  paths, the screening AI, appearance

Nothing here reimplements the pipeline or the screening prompt - it collects
the settings the CLI would have prompted for, then calls the same functions.

Built on CustomTkinter for flat, rounded widgets. The Review table is still a
ttk.Treeview: CustomTkinter has no table. Colours come from theme.py, and every
CTk widget is given a (light, dark) pair - so switching theme is live, but
editing a colour has to rebuild the widgets, since a pair is fixed when the
widget is made. All input state lives in tk variables created once, so a
rebuild keeps whatever was typed.

The work runs on a background thread with stdout piped into the log drawer,
so the window stays responsive and Stop lands immediately - including
mid-delay, since the transcript pass waits on the stop event.

Run: ytb-gui
Deps: customtkinter. Otherwise same as batch_fetch (yt-dlp on PATH,
youtube-transcript-api), plus an AI CLI or API key for screening.
"""

import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox

import customtkinter as ctk

from . import (backups, batch_fetch, colorpicker, config, grab_watchlist, guide, paths,
               progress, render_screening, screen, settings, theme)

def watchlist_digest():
    """sha256 of watchlist.txt's bytes, or None if unreadable. Run fetch and
    Fetch watchlist are separate buttons, and pressing only Run fetch reuses
    the last grab - that is how a whole evening's additions went missing.
    Comparing against the list the previous run used catches exactly that."""
    import hashlib
    try:
        with open(batch_fetch.INPUT, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


POLL_MS = 60         # log drain interval; fast enough to look live, cheap enough to ignore
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

PAGES = (
    ("fetch", "Fetch", "↓", "Pull the watchlist, then its metadata and transcripts."),
    ("screen", "Screen", "▶", "Have an AI screen the videos you haven't got to yet."),
    ("review", "Review", "☰", "Record what you did with each video and what you thought."),
    ("settings", "Settings", "⚙", "Paths, the screening AI, and how the app looks."),
)

STATUSES = (("watched_full", "Watched in full"), ("scrubbed", "Scrubbed"),
            ("read_summary", "Read the summary"), ("skipped", "Skipped"))


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


class Stepper(ctk.CTkFrame):
    """Number field with - and + either side. CustomTkinter has no spinbox;
    the field stays typeable, so 7.3 doesn't need a dozen clicks. A value that
    isn't a number (the "-" skip sentinel) steps from `start`."""

    def __init__(self, app, parent, var, lo, hi, step=1, fmt="{:.0f}", width=64, start=None):
        super().__init__(parent, fg_color="transparent")
        self.var, self.lo, self.hi, self.step, self.fmt = var, lo, hi, step, fmt
        self.start = lo if start is None else start
        app.button(self, "−", lambda: self.bump(-1), width=34).pack(side="left")
        app.entry(self, var, width=width, justify="center").pack(side="left", padx=4)
        app.button(self, "+", lambda: self.bump(1), width=34).pack(side="left")

    def bump(self, direction):
        try:
            value = float(self.var.get()) + direction * self.step
        except ValueError:
            value = self.start
        self.var.set(self.fmt.format(min(max(value, self.lo), self.hi)))


class FlowRow(ctk.CTkFrame):
    """Widgets left to right, wrapping onto a new line when the next one
    won't fit. pack(side="left") never wraps in Tk, so with a large font or a
    narrow window the end of a row was simply cut off - Refresh read "fre"
    and Save lost its first letter.

    Children are created with this frame as parent and handed to add() in
    order; placement is recomputed whenever the width changes."""

    def __init__(self, parent, gap=10, vgap=8, **kw):
        super().__init__(parent, fg_color="transparent", height=1, **kw)
        self.gap, self.vgap = gap, vgap
        self._items = []
        self._width = 0
        self.bind("<Configure>", self._on_configure)

    def add(self, widget, gap=None):
        self._items.append((widget, self.gap if gap is None else gap))
        self.after_idle(self._reflow)
        return widget

    def _on_configure(self, event):
        if event.width != self._width:
            self._width = event.width
            self._reflow()

    def _reflow(self):
        if not self.winfo_exists():
            return
        width = self._width or self.winfo_width()
        # Two passes: lines first, then placement, so each item can be centred
        # on its line - top-aligned, a label sat visibly higher than the
        # buttons beside it.
        lines, line, x = [], [], 0
        for widget, gap in self._items:
            w = widget.winfo_reqwidth()
            lead = gap if line else 0
            if line and width > 1 and x + lead + w > width:
                lines.append(line)
                line, x, lead = [], 0, 0
            line.append((widget, x + lead))
            x += lead + w
        if line:
            lines.append(line)
        y = 0
        for i, line in enumerate(lines):
            line_h = max(widget.winfo_reqheight() for widget, _ in line)
            for widget, left in line:
                widget.place(x=left, y=y + (line_h - widget.winfo_reqheight()) // 2)
            y += line_h + (self.vgap if i < len(lines) - 1 else 0)
        height = y
        if height and abs(self.winfo_reqheight() - height) > 1:
            self.configure(height=height)


class App:
    def __init__(self, root):
        self.root = root
        self.log_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self._run_active = False      # a worker is running and hasn't been finalized yet
        self._overwrite_line = False  # set by a \r, consumed by the next write
        self._agent_proc = None       # the screening terminal, while one is open
        self._agent_blocks = False    # whether that process lives as long as its window
        self._agent_gen = 0           # bumped per launch so an older watch stops itself
        self._progress_state = None   # (done, total) while a run reports progress, else None
        # Every button that must go dead for the duration of a run registers
        # here, rather than being named in both _start and _finished.
        self._action_btns = []
        self.shell = None
        self.cfg = config.load()
        self.ui = settings.load()
        if self.ui.get("theme") not in theme.PALETTES:
            self.ui["theme"] = "light"
        ctk.set_appearance_mode(self.ui["theme"])
        self._theme_apply()
        self._load_palettes()
        self._make_fonts()
        # Review state, restored so a session picks up the table as it was left.
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
        root.minsize(1000, 680)
        if self.ui.get("fullscreen"):
            root.attributes("-fullscreen", True)
        root.bind("<F11>", lambda _e: self.toggle_fullscreen())
        root.bind("<Escape>", lambda _e: self.set_fullscreen(False))

        self._make_vars()
        self._build_widgets()
        self.refresh_watchlist_count()
        self.refresh_screen_counts()
        self.refresh_review()
        self._banner()
        # Catches changes made while the app was closed - an AI CLI session
        # run by hand, or a file edited directly.
        made = backups.snapshot_all(self.cfg)
        if made:
            self.log_line(f"backups  {len(made)} new read-only copy(ies) in {backups.DIR}")
        self._refresh_backup_note()
        if not self.ui.get("guide_seen"):
            # Deferred so the main window is up behind it rather than after it.
            self.root.after(700, self.open_guide)
        self.root.after(POLL_MS, self._drain_log)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- palette and widget factories ----------

    def _load_palettes(self):
        colors = self.ui.get("colors")
        self.pal = {mode: theme.resolved(mode, colors) for mode in theme.PALETTES}

    def c(self, role):
        """(light, dark) pair for a colour role - CTk picks by appearance mode."""
        return self.pal["light"][role], self.pal["dark"][role]

    FONT_MIN, FONT_MAX = 9, 28

    # Offsets from the body size, so one number scales the whole hierarchy.
    FONT_STEPS = {"h1": (10, "bold"), "h2": (2, "bold"), "body": (0, "normal"),
                  "muted": (-1, "normal"), "small": (-2, "normal"), "nav": (1, "normal"),
                  "stat": (12, "bold"), "mono": (-1, "normal")}

    def font_size(self):
        try:
            return min(max(int(self.ui.get("font_size") or 14), self.FONT_MIN), self.FONT_MAX)
        except (TypeError, ValueError):
            return 14

    def _theme_apply(self):
        self.ui_font, self.mono_font = theme.apply(
            self.root, self.ui["theme"], self.ui.get("colors"), paint_root=False,
            font_family=self.ui.get("font_family") or "", font_size=self.font_size())

    def _make_fonts(self):
        base = self.font_size()
        self.fonts = {
            kind: ctk.CTkFont(family=self.mono_font if kind == "mono" else self.ui_font,
                              size=max(8, base + step), weight=weight)
            for kind, (step, weight) in self.FONT_STEPS.items()}

    def apply_fonts(self):
        """Live, family and size alike. CTkFont objects notify every widget
        using them, so changing them in place restyles the app without a
        rebuild; only the ttk table needs theme.apply."""
        family = self.font_family_var.get().strip()
        if family in ("", "Default"):
            family = ""
        elif family not in self._families:
            return  # half-typed name; wait for a real one
        try:
            size = int(float(self.font_size_var.get()))
        except ValueError:
            return  # emptied mid-edit; wait for a number
        clamped = min(max(size, self.FONT_MIN), self.FONT_MAX)
        if clamped != size:
            # Show what was actually applied; the re-trace this triggers
            # finds nothing changed and stops.
            self.font_size_var.set(str(clamped))
        size = clamped
        if (family, size) == (self.ui.get("font_family") or "", self.font_size()):
            return
        self.ui["font_family"], self.ui["font_size"] = family, size
        self._theme_apply()
        base = self.font_size()
        for kind, (step, weight) in self.FONT_STEPS.items():
            self.fonts[kind].configure(
                family=self.mono_font if kind == "mono" else self.ui_font,
                size=max(8, base + step), weight=weight)
        self._style_tree()
        self.sidebar.configure(width=self._sidebar_width())
        self._fit_drawer()  # the page's minimum height follows the font size
        self.save_ui()

    def _schedule_fonts(self, *_args):
        # Typing "16" passes through "1"; debounce so that isn't applied.
        if getattr(self, "_font_job", None):
            self.root.after_cancel(self._font_job)
        self._font_job = self.root.after(350, self.apply_fonts)

    def _sidebar_width(self):
        # Fixed width (pack_propagate off keeps it from jittering per page),
        # so it has to grow with the text or the subtitle clips past ~16px.
        return 220 + max(0, self.font_size() - 14) * 14


    def label(self, parent, text="", kind="body", **kw):
        quiet = kind in ("muted", "small")
        kw.setdefault("anchor", "w")
        kw.setdefault("justify", "left")
        kw.setdefault("text_color", self.c("muted" if quiet else "fg"))
        return ctk.CTkLabel(parent, text=text, font=self.fonts.get(kind, self.fonts["body"]), **kw)

    def button(self, parent, text, command, kind="secondary", action=False, **kw):
        """kind: primary (the one main action in a group), secondary, ghost."""
        if kind == "primary":
            colors = dict(fg_color=self.c("accent"), hover_color=self.c("accent_hi"),
                          text_color=self.c("accent_fg"))
        elif kind == "ghost":
            colors = dict(fg_color="transparent", hover_color=self.c("hover"),
                          text_color=self.c("fg"))
        else:
            colors = dict(fg_color=self.c("sel"), hover_color=self.c("line"),
                          text_color=self.c("fg"))
        kw.setdefault("height", 36)
        btn = ctk.CTkButton(parent, text=text, command=command, corner_radius=9,
                            font=self.fonts["body"], text_color_disabled=self.c("disabled"),
                            **colors, **kw)
        if action:
            self._action_btns.append(btn)
        return btn

    def entry(self, parent, var, **kw):
        kw.setdefault("height", 36)
        return ctk.CTkEntry(parent, textvariable=var, fg_color=self.c("bg"),
                            border_color=self.c("line"), text_color=self.c("fg"),
                            corner_radius=9, border_width=1, font=self.fonts["body"], **kw)

    def _dropdown_colors(self):
        return dict(dropdown_fg_color=self.c("card"), dropdown_hover_color=self.c("sel"),
                    dropdown_text_color=self.c("fg"), dropdown_font=self.fonts["body"],
                    text_color=self.c("fg"), text_color_disabled=self.c("disabled"),
                    font=self.fonts["body"], corner_radius=9, height=36)

    def option(self, parent, var, values, command=None, **kw):
        """Read-only choice."""
        return ctk.CTkOptionMenu(parent, variable=var, values=list(values) or [""],
                                 command=command, fg_color=self.c("sel"),
                                 button_color=self.c("sel"), button_hover_color=self.c("line"),
                                 **self._dropdown_colors(), **kw)

    def combo(self, parent, var, values, **kw):
        """Typeable, with suggestions."""
        return ctk.CTkComboBox(parent, variable=var, values=list(values),
                               fg_color=self.c("bg"), border_color=self.c("line"), border_width=1,
                               button_color=self.c("line"), button_hover_color=self.c("muted"),
                               **self._dropdown_colors(), **kw)

    def segmented(self, parent, values, command=None, **kw):
        return ctk.CTkSegmentedButton(parent, values=list(values), command=command,
                                      fg_color=self.c("sel"), selected_color=self.c("bg"),
                                      selected_hover_color=self.c("bg"),
                                      unselected_color=self.c("sel"),
                                      unselected_hover_color=self.c("line"),
                                      text_color=self.c("fg"), font=self.fonts["body"],
                                      corner_radius=9, height=36, **kw)

    def switch(self, parent, text, var, command=None):
        return ctk.CTkSwitch(parent, text=text, variable=var, command=command,
                             font=self.fonts["body"], text_color=self.c("fg"),
                             fg_color=self.c("line"), progress_color=self.c("accent"),
                             button_color=self.c("fg"), button_hover_color=self.c("muted"))

    def card(self, parent, title=None, subtitle=None, grid=None, action=None, **pack):
        """Rounded panel in parent; returns its padded body. Packed by
        default, gridded when `grid` holds grid options - Tk refuses both
        managers in one parent, so the caller has to say which. action is
        an optional (text, command) shown as a quiet button beside the title."""
        outer = ctk.CTkFrame(parent, fg_color=self.c("card"), corner_radius=14,
                             border_width=1, border_color=self.c("line"))
        if grid is not None:
            outer.grid(**grid)
        else:
            pack.setdefault("fill", "x")
            pack.setdefault("pady", (0, 14))
            outer.pack(**pack)
        if title:
            head = ctk.CTkFrame(outer, fg_color="transparent")
            head.pack(fill="x", padx=(20, 12), pady=(12, 0 if subtitle else 10))
            self.label(head, title, "h2").pack(side="left", pady=(4, 0))
            if action:
                self.button(head, action[0], action[1], kind="ghost").pack(side="right")
        if subtitle:
            self.label(outer, subtitle, "muted").pack(anchor="w", padx=20, pady=(2, 12))
        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(0 if title else 16, 18))
        return body

    # ---------- state ----------

    def _make_vars(self):
        """Every input's variable, made once. Widgets are rebuilt on a colour
        change; these are not, so nothing typed is lost to a repaint."""
        cfg = self.cfg
        self.status_var = tk.StringVar(value="Idle.")
        self.browser_var = tk.StringVar(value="System default")
        self.playlist_var = tk.StringVar(value=grab_watchlist.DEFAULT_PLAYLIST)
        self.mode_var = tk.StringVar(value="0")
        self.batch_var = tk.StringVar(value="10")
        self.hide_decided = tk.BooleanVar(value=False)
        self.status_choice = tk.StringVar(value="")
        self.status_choice.trace_add("write", self._on_status_change)
        self.rating_var = tk.StringVar(value="")

        # Empty in config means "detect at runtime", so the entries show what
        # detection found and only persist a value once it is actually edited.
        self.setting_vars = {key: tk.StringVar(value=cfg.get(key) or detected) for key, detected in (
            ("_home", str(paths.home())),
            ("skill_path", screen.find_skill()),
            ("claude_bin", screen.find_claude()),
            ("screening_md", screen.screening_path(cfg)),
        )}
        found = screen.find_browsers()
        self.browser_pref = tk.StringVar(value=cfg.get("browser") or (found[0] if found else ""))

        self.ai_mode = tk.StringVar(value="api" if cfg.get("screen_mode") == "api" else "subscription")
        self.ai_agent = tk.StringVar(value=screen.backend(dict(cfg, screen_mode="")))
        self.ai_bins = dict(cfg.get("screen_bins") or {})
        self.ai_bin = tk.StringVar()
        self.ai_agent_model = tk.StringVar(value=cfg.get("screen_agent_model", ""))
        self.ai_agent_effort = tk.StringVar(value=cfg.get("screen_agent_effort", ""))
        self.ai_provider = tk.StringVar(value=cfg.get("screen_provider") or cfg.get("provider"))
        self.ai_key = tk.StringVar()
        self.ai_api_model = tk.StringVar(value=cfg.get("screen_api_model", ""))
        self.ai_api_effort = tk.StringVar(value=cfg.get("screen_api_effort", ""))
        self.ai_batch = tk.StringVar(value=str(screen.batch_size(cfg)))
        self._ai_prev_agent = self.ai_agent.get()
        self._ai_prev_provider = self.ai_provider.get()

        self.fullscreen_var = tk.BooleanVar(value=bool(self.ui.get("fullscreen")))
        self._families = theme.families(self.root)
        self.font_family_var = tk.StringVar(value=self.ui.get("font_family") or "Default")
        self.font_size_var = tk.StringVar(value=str(self.font_size()))
        self.font_size_var.trace_add("write", self._schedule_fonts)
        self.backup_threshold_var = tk.StringVar(
            value=str(self.ui.get("backup_threshold") or backups.DEFAULT_THRESHOLD))
        self.backup_threshold_var.trace_add("write", self._on_backup_threshold)

    # ---------- layout ----------

    def _build_widgets(self):
        self._action_btns = []
        self.root.configure(fg_color=self.c("bg"))
        shell = self.shell = ctk.CTkFrame(self.root, fg_color=self.c("bg"), corner_radius=0)
        shell.pack(fill="both", expand=True)
        shell.grid_columnconfigure(1, weight=1)
        shell.grid_rowconfigure(0, weight=1)

        self._build_sidebar(shell).grid(row=0, column=0, sticky="ns")

        main = ctk.CTkFrame(shell, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=28, pady=(22, 16))
        # The status bar is packed first, at the bottom: pack hands out space
        # in order, so it is reserved before the page and drawer take theirs.
        # As the last grid row it was the first thing clipped when a big font
        # and a tall drawer outgrew the window - progress and Stop included.
        self._build_status_bar(main).pack(side="bottom", fill="x", pady=(12, 0))
        body = self.body = ctk.CTkFrame(main, fg_color="transparent")
        body.pack(side="top", fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)
        body.bind("<Configure>", self._fit_drawer)
        self._drawer_h = None

        header = self.header = ctk.CTkFrame(body, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        self.page_title = self.label(header, "", "h1")
        self.page_title.pack(anchor="w")
        self.page_sub = self.label(header, "", "muted")
        self.page_sub.pack(anchor="w")

        holder = ctk.CTkFrame(body, fg_color="transparent")
        holder.grid(row=1, column=0, sticky="nsew")
        holder.grid_columnconfigure(0, weight=1)
        holder.grid_rowconfigure(0, weight=1)
        self.pages = {
            "fetch": self._build_fetch_page(holder),
            "screen": self._build_screen_page(holder),
            "review": self._build_review_page(holder),
            "settings": self._build_settings_page(holder),
        }
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")
            page.grid_remove()

        # Drag handle for the drawer height; replaces the old paned sash.
        self.grip = ctk.CTkFrame(body, height=10, fg_color="transparent", cursor="sb_v_double_arrow")
        self.grip.grid(row=2, column=0, sticky="ew")
        self.grip.bind("<Button-1>", self._grip_start)
        self.grip.bind("<B1-Motion>", self._grip_drag)
        self.drawer = self._build_log_drawer(body)
        self.drawer.grid(row=3, column=0, sticky="ew")

        self._set_log_open(self.ui.get("log_open", True), save=False)
        self.show_page(self.ui.get("page") or "fetch")

    def _build_sidebar(self, parent):
        side = self.sidebar = ctk.CTkFrame(parent, fg_color=self.c("card"), corner_radius=0,
                                           width=self._sidebar_width())
        side.pack_propagate(False)
        self.label(side, "ytbatch", "h1").pack(anchor="w", padx=24, pady=(26, 0))
        self.label(side, "YouTube backlog triage", "small").pack(anchor="w", padx=24, pady=(0, 24))
        self.nav_btns = {}
        for key, name, icon, _sub in PAGES:
            btn = ctk.CTkButton(side, text=f"{icon}    {name}", anchor="w", height=42,
                                corner_radius=10, font=self.fonts["nav"],
                                fg_color="transparent", hover_color=self.c("hover"),
                                text_color=self.c("muted"),
                                command=lambda k=key: self.show_page(k))
            btn.pack(fill="x", padx=12, pady=2)
            self.nav_btns[key] = btn

        # Packed bottom-up: the first side="bottom" pack lands lowest.
        help_row = ctk.CTkFrame(side, fg_color="transparent", cursor="hand2")
        help_row.pack(side="bottom", fill="x", padx=14, pady=(0, 18))
        ctk.CTkButton(help_row, text="?", width=36, height=36, corner_radius=18,
                      font=self.fonts["h2"], fg_color=self.c("sel"), hover_color=self.c("line"),
                      text_color=self.c("fg"), command=self.open_guide).pack(side="left")
        guide_lbl = self.label(help_row, "Guide", "muted", cursor="hand2")
        guide_lbl.pack(side="left", padx=10)
        for widget in (help_row, guide_lbl):
            widget.bind("<Button-1>", lambda _e: self.open_guide())

        self.side_theme = self.segmented(side, ["Light", "Dark"],
                                         command=lambda v: self.apply_theme(v.lower()))
        self.side_theme.set(self.ui["theme"].capitalize())
        self.side_theme.pack(side="bottom", fill="x", padx=14, pady=(4, 16))
        self.label(side, "Theme", "small").pack(side="bottom", anchor="w", padx=16)

        # Shown only when some file's backups pass the threshold; opens the
        # Backups card in Settings.
        self.backup_note = ctk.CTkButton(side, text="", anchor="w", height=34, corner_radius=9,
                                         font=self.fonts["small"], fg_color=self.c("sel"),
                                         hover_color=self.c("line"), text_color=self.c("skip"),
                                         command=lambda: self.show_page("settings"))
        return side

    def _refresh_backup_note(self):
        """Sidebar notice and Settings rows from the current copy counts.
        Cheap: a directory listing, no hashing."""
        if not hasattr(self, "backup_note") or not self.backup_note.winfo_exists():
            return
        try:
            info = backups.status(self.cfg)
        except OSError:
            return
        limit = self.backup_threshold()
        over = [s for s in info if s["count"] > limit]
        if over:
            names = ", ".join(f"{s['label']} ({s['count']})" for s in over)
            self.backup_note.configure(text=f"⚠  Backups over {limit}: {names}")
            if not self.backup_note.winfo_manager():
                self.backup_note.pack(side="bottom", fill="x", padx=14, pady=(0, 10))
        else:
            self.backup_note.pack_forget()
        rows = getattr(self, "backup_rows", None)
        if rows is not None and rows.winfo_exists():
            for child in rows.winfo_children():
                child.destroy()
            for s in info:
                newest = s["newest"].strftime("%Y-%m-%d %H:%M") if s["newest"] else "none yet"
                text = (f"{s['label']}:  {s['count']} cop{'y' if s['count'] == 1 else 'ies'}  ·  "
                        f"{s['bytes'] / 1_048_576:.1f} MB  ·  newest {newest}")
                self.label(rows, text, text_color=self.c("skip" if s["count"] > limit else "fg")
                           ).pack(anchor="w", pady=2)

    def backup_threshold(self):
        try:
            return max(1, int(self.ui.get("backup_threshold") or backups.DEFAULT_THRESHOLD))
        except (TypeError, ValueError):
            return backups.DEFAULT_THRESHOLD

    def _on_backup_threshold(self, *_args):
        try:
            value = max(1, int(float(self.backup_threshold_var.get())))
        except ValueError:
            return
        if value != self.backup_threshold():
            self.ui["backup_threshold"] = value
            self.save_ui()
            self._refresh_backup_note()

    def on_clear_backups(self):
        info = backups.status(self.cfg)
        extra = sum(max(0, s["count"] - 1) for s in info)
        if not extra:
            messagebox.showinfo("Backups", "Only the newest copy of each file exists - nothing to clear.")
            return
        size = sum(s["bytes"] for s in info) / 1_048_576
        if not messagebox.askokcancel(
                "Clear old backups",
                f"Delete {extra} older read-only copies?\n\n"
                f"The newest copy of each file is kept. Backups currently use {size:.1f} MB."):
            return
        try:
            removed, freed = backups.clear_old(self.cfg)
        except OSError as e:
            messagebox.showerror("Backups", f"Couldn't clear all copies\n{e}")
            removed, freed = 0, 0
        self.log_line(f"Cleared {removed} old backup copies ({freed / 1_048_576:.1f} MB); "
                      f"newest of each kept.")
        self._refresh_backup_note()

    def on_open_backups(self):
        backups.DIR.mkdir(parents=True, exist_ok=True)
        folder = str(backups.DIR)
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            messagebox.showerror("Open folder", f"Couldn't open {folder}\n{e}")

    def _build_log_drawer(self, parent):
        drawer = ctk.CTkFrame(parent, fg_color=self.c("card"), corner_radius=14,
                              border_width=1, border_color=self.c("line"))
        top = ctk.CTkFrame(drawer, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(10, 4))
        self.label(top, "Log", "h2").pack(side="left")
        self.button(top, "Hide", lambda: self._set_log_open(False), kind="ghost",
                    width=64).pack(side="right")
        self.button(top, "Clear", self.on_clear, kind="ghost", width=64).pack(side="right", padx=4)
        self.log = ctk.CTkTextbox(drawer, height=int(self.ui.get("log_height") or 180),
                                  fg_color=self.c("log_bg"), text_color=self.c("log_fg"),
                                  font=self.fonts["mono"], corner_radius=10, wrap="none",
                                  scrollbar_button_color=self.c("muted"),
                                  scrollbar_button_hover_color=self.c("fg"))
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        # CTkTextbox wraps a tk.Text but forwards only part of its API; the
        # \r line-rewrite and trimming below need index arithmetic on the real one.
        self._log_text = self.log._textbox
        self._log_text.configure(state="disabled", padx=10, pady=8)
        return drawer

    def _build_status_bar(self, parent):
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        self.label(bar, kind="muted", textvariable=self.status_var).pack(side="left")
        self.log_toggle = self.button(bar, "Hide log", self._toggle_log, kind="ghost", width=96)
        self.log_toggle.pack(side="right")
        self.stop_btn = self.button(bar, "Stop", self.on_stop, width=84)
        self.stop_btn.configure(state="disabled")
        self.stop_btn.pack(side="right", padx=8)
        # Built but not packed: _set_progress shows them only while a run is
        # actually reporting [n/total].
        self.progress = ctk.CTkProgressBar(bar, width=240, height=8, corner_radius=4,
                                           fg_color=self.c("line"), progress_color=self.c("accent"))
        self.progress.set(0)
        self.progress_lbl = self.label(bar, "", "muted")
        self._set_progress(self._progress_state)
        return bar

    def _set_progress(self, state):
        """(done, total) shows the bar with a count beside it; None hides
        both. Hidden rather than parked at 0: CTkProgressBar still paints its
        rounded end cap in the accent colour at 0, which read as a run that
        had started when nothing was running."""
        self._progress_state = state
        if not state or not state[1]:
            self.progress.pack_forget()
            self.progress_lbl.pack_forget()
            return
        done, total = state
        self.progress.set(min(done / total, 1.0))
        self.progress_lbl.configure(text=f"{done}/{total}")
        if not self.progress.winfo_manager():
            # side="right" packs leftwards, so 'after' the Stop button puts
            # the bar just left of it and the count just left of the bar.
            self.progress.pack(side="right", padx=8, after=self.stop_btn)
            self.progress_lbl.pack(side="right", padx=(8, 0), after=self.progress)

    def open_guide(self):
        guide.Guide(self)

    def show_page(self, key):
        if key not in self.pages:
            key = "fetch"
        for name, page in self.pages.items():
            if name != key:
                page.grid_remove()
        self.pages[key].grid()
        for name, btn in self.nav_btns.items():
            on = name == key
            btn.configure(fg_color=self.c("sel") if on else "transparent",
                          text_color=self.c("fg" if on else "muted"))
        _k, title, _icon, sub = next(p for p in PAGES if p[0] == key)
        self.page_title.configure(text=title)
        self.page_sub.configure(text=sub)
        self.ui["page"] = key

    def _set_log_open(self, open_, save=True):
        self.ui["log_open"] = bool(open_)
        for widget in (self.drawer, self.grip):
            widget.grid() if open_ else widget.grid_remove()
        self.log_toggle.configure(text="Hide log" if open_ else "Show log")
        if open_:
            self._fit_drawer()
        if save:
            self.save_ui()

    # The page keeps at least this much height; the drawer gives way first.
    # Scaled by font size: a flat 240 px left the Review table with room for
    # its headings and no rows once the font was 23.
    PAGE_MIN_HEIGHT = 240

    def _page_min_height(self):
        return max(self.PAGE_MIN_HEIGHT, 18 * self.font_size())

    def _fit_drawer(self, _event=None):
        """Shows the drawer at the saved height or at what's left after the
        page's minimum, whichever is smaller. The saved height isn't touched,
        so a bigger window gets the full drawer back. A 690 px drawer with a
        23 px font used to squeeze the page to nothing in a 900 px window."""
        body = getattr(self, "body", None)
        if body is None or not self.ui.get("log_open", True):
            return
        body.update_idletasks()
        chrome = max(0, self.drawer.winfo_reqheight() - self.log.winfo_reqheight())
        room = (body.winfo_height() - self.header.winfo_reqheight() - 16
                - self._page_min_height() - self.grip.winfo_reqheight() - chrome)
        height = max(80, min(int(self.ui.get("log_height") or 180), room))
        if height != self._drawer_h:
            self._drawer_h = height
            self.log.configure(height=height)

    def _toggle_log(self):
        self._set_log_open(not self.ui.get("log_open", True))

    def _grip_start(self, e):
        self._grip_y, self._grip_h = e.y_root, int(self.ui.get("log_height") or 180)

    def _grip_drag(self, e):
        # Dragging up grows the drawer, since it sits below the handle.
        self.ui["log_height"] = max(80, min(700, self._grip_h - (e.y_root - self._grip_y)))
        self._fit_drawer()

    def _build_fetch_page(self, holder):
        page = ctk.CTkFrame(holder, fg_color="transparent")

        body = self.card(page, "Watchlist", "Reads video IDs from a playlist through a browser "
                                            "you're logged in to. Close that browser first.")
        body.grid_columnconfigure(1, weight=1)
        self.label(body, "Browser").grid(row=0, column=0, sticky="w", padx=(0, 16), pady=6)
        browser_names = ["System default"] + [label for label, _ in grab_watchlist.BROWSERS]
        self.option(body, self.browser_var, browser_names, width=220).grid(
            row=0, column=1, sticky="w", pady=6)
        self.label(body, "Playlist").grid(row=1, column=0, sticky="w", padx=(0, 16), pady=6)
        self.entry(body, self.playlist_var).grid(row=1, column=1, sticky="ew", pady=6)
        self.grab_btn = self.button(body, "Fetch watchlist", self.on_fetch_watchlist, action=True)
        self.grab_btn.grid(row=1, column=2, padx=(10, 0), pady=6)
        self.watchlist_lbl = self.label(body, "", "small")
        self.watchlist_lbl.grid(row=2, column=1, sticky="w", pady=(2, 0))

        body = self.card(page, "Transcript routing",
                         "How transcript requests get past YouTube's rate limits.")
        for row, (value, name, desc) in enumerate(MODES):
            ctk.CTkRadioButton(body, text=name, value=value, variable=self.mode_var,
                               font=self.fonts["body"], text_color=self.c("fg"),
                               fg_color=self.c("accent"), hover_color=self.c("accent_hi"),
                               border_color=self.c("muted")).grid(row=row, column=0,
                                                                  sticky="w", pady=5)
            self.label(body, desc, "muted").grid(row=row, column=1, sticky="w", padx=(18, 0))

        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.pack(fill="x", pady=(2, 0))
        self.run_btn = self.button(actions, "Run fetch", self.on_run, kind="primary",
                                   action=True, width=140)
        self.run_btn.pack(side="left")
        self.button(actions, "Open output folder", self.on_open_folder).pack(side="right")
        return page

    STATS = (("screened", "Screened", "fg"), ("left", "Left", "fg"), ("watch", "Watch", "watch"),
             ("read", "Read", "read"), ("skip", "Skip", "skip"), ("decided", "Decided", "fg"),
             ("rated", "Rated", "fg"))

    def _build_screen_page(self, holder):
        page = ctk.CTkFrame(holder, fg_color="transparent")

        body = self.card(page, "Screen with AI")
        self.screen_sub = self.label(body, "", "muted")
        self.screen_sub.pack(anchor="w", pady=(0, 12))
        row = FlowRow(body, gap=12)
        row.pack(fill="x")
        row.add(self.label(row, "Videos"))
        row.add(Stepper(self, row, self.batch_var, 0, 10000))
        row.add(self.button(row, "Run", self.on_screen, kind="primary", action=True, width=110))
        row.add(self.label(row, "0 = everything left, until the limit runs out", "small"))
        row.add(self.button(row, "Open Claude terminal", self.on_claude_terminal, action=True), gap=24)

        body = self.card(page, "Progress")
        self.stat_lbls = {}
        for i, (key, name, role) in enumerate(self.STATS):
            body.grid_columnconfigure(i, weight=1, uniform="stat")
            tile = ctk.CTkFrame(body, fg_color=self.c("bg"), corner_radius=12)
            tile.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 10, 0))
            num = ctk.CTkLabel(tile, text="-", font=self.fonts["stat"], text_color=self.c(role))
            num.pack(pady=(14, 0))
            self.label(tile, name, "small", anchor="center").pack(pady=(0, 14))
            self.stat_lbls[key] = num

        body = self.card(page, "Skill and output",
                         "The skill is the screening prompt. The output is the markdown it "
                         "writes, and the page rendered from that.")
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x")
        for i, (text, cmd) in enumerate((("Edit skill", self.on_edit_skill),
                                         ("Render + open", self.on_render),
                                         ("Backfill from titles", self.on_backfill))):
            self.button(row, text, cmd, action=True).pack(side="left", padx=(0 if i == 0 else 8, 0))
        return page

    # Column id -> (heading, default width, min width, alignment). Everything
    # is centred except the title: titles vary wildly in length, and centred
    # ragged-both text gives the eye no left edge to scan down.
    REVIEW_COLS = (
        ("num", "#", 55, 44, "center"),
        ("date", "Date", 100, 96, "center"),  # below 96 the day digit gets clipped
        ("title", "Title", 380, 120, "w"),
        ("channel", "Channel", 160, 80, "center"),
        ("views", "Views", 82, 62, "center"),
        ("ai", "AI", 72, 52, "center"),
        ("status", "You", 128, 70, "center"),
        ("rating", "Rating", 76, 58, "center"),
    )

    def _build_review_page(self, holder):
        from tkinter import ttk

        page = ctk.CTkFrame(holder, fg_color="transparent")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)

        bar = FlowRow(page, gap=18)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        bar.add(self.switch(bar, "Hide decided", self.hide_decided, self.refresh_review))
        bar.add(self.label(bar, "Click a heading to sort, again to reverse. Ctrl- or shift-click "
                                "to pick several.", "small"))
        bar.add(self.button(bar, "Refresh", self.refresh_review))

        table = ctk.CTkFrame(page, fg_color=self.c("card"), corner_radius=14,
                             border_width=1, border_color=self.c("line"))
        table.grid(row=1, column=0, sticky="nsew")
        inner = ctk.CTkFrame(table, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=12, pady=12)
        inner.grid_rowconfigure(0, weight=1)
        inner.grid_columnconfigure(0, weight=1)

        cols = tuple(c[0] for c in self.REVIEW_COLS)
        # extended, not browse: ctrl-click adds, shift-click takes a run, and
        # Save then applies one decision to the whole selection.
        self.tree = ttk.Treeview(inner, columns=cols, show="headings", height=12,
                                 selectmode="extended")
        for col, name, width, minwidth, anchor in self.REVIEW_COLS:
            # Heading anchor is separate from the column's: setting only the
            # column leaves every header hugging the left while its cells sit
            # centred underneath.
            self.tree.heading(col, text=name, anchor=anchor,
                              command=lambda c=col: self.on_sort_column(c))
            # Every column stretches: with only one stretching, dragging any
            # other separator was immediately undone by the re-layout.
            self.tree.column(col, width=self._saved_widths.get(col, width),
                             minwidth=minwidth, stretch=True, anchor=anchor)
        self._style_tree()
        scroll = ctk.CTkScrollbar(inner, command=self.tree.yview, fg_color="transparent",
                                  button_color=self.c("line"), button_hover_color=self.c("muted"))
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns", padx=(6, 0))
        self.tree.bind("<<TreeviewSelect>>", self.on_select_video)
        self.tree.bind("<Double-1>", lambda _e: self.on_open_video())
        self.tree.bind("<Configure>", self._fit_columns)
        # Ctrl-A has no default binding on a Treeview, and the stock Text
        # binding would fire first if this were left to the toplevel.
        for seq in ("<Control-a>", "<Control-A>"):
            self.tree.bind(seq, self.on_select_all)

        body = self.card(page, grid=dict(row=2, column=0, sticky="ew", pady=(12, 0)))
        row = FlowRow(body, gap=12)
        row.pack(fill="x")
        self.status_seg = row.add(self.segmented(row, [name for _, name in STATUSES],
                                                 command=self._on_status_pick))
        row.add(self.label(row, "Rating"), gap=20)
        row.add(Stepper(self, row, self.rating_var, 1, 10, step=0.5, fmt="{:g}",
                        start=float(self.DEFAULT_RATING)), gap=8)
        row.add(self.button(row, "Save", self.on_save_decision, kind="primary", width=96), gap=16)
        row.add(self.button(row, "Open video", self.on_open_video), gap=8)
        self.sel_lbl = row.add(self.label(row, "", "small"), gap=16)
        self._sync_status_seg()

        self.review_lbl = self.label(page, "", "small")
        self.review_lbl.grid(row=3, column=0, sticky="w", pady=(10, 0))
        return page

    def _style_tree(self):
        """ttk has no light/dark pairs, so the table is repainted from the
        live palette on every theme change."""
        for slug, colour in theme.verdicts().items():
            self.tree.tag_configure(f"ai-{slug}", foreground=colour)
        # Faint zebra striping: long rows of same-coloured text lose their line.
        self.tree.tag_configure("odd", background=theme.mix(theme.P["card"], theme.P["fg"], 0.04))

    def _fit_columns(self, event):
        """Saved widths from a wider window push the last columns off the
        edge, and Tk never shrinks them on its own. Scale down only when they
        overflow, so a deliberate drag on a roomy table isn't undone."""
        widths = {c[0]: int(self.tree.column(c[0], "width")) for c in self.REVIEW_COLS}
        total = sum(widths.values())
        if event.width < 200 or total <= event.width + 1:
            return
        factor = event.width / total
        for col, _name, _w, minwidth, _a in self.REVIEW_COLS:
            self.tree.column(col, width=max(minwidth, int(widths[col] * factor)))

    def _build_settings_page(self, holder):
        page = ctk.CTkScrollableFrame(holder, fg_color="transparent",
                                      scrollbar_button_color=self.c("line"),
                                      scrollbar_button_hover_color=self.c("muted"))

        body = self.card(page, "Paths", "Blank fields are re-detected on each start.",
                         padx=(0, 8), action=("Reset to defaults", self.reset_paths))
        body.grid_columnconfigure(1, weight=1)
        rows = (("_home", "Corpus folder"), ("skill_path", "Screener SKILL.md"),
                ("claude_bin", "claude binary"), ("screening_md", "Screening markdown"))
        for row, (key, name) in enumerate(rows):
            self.label(body, name).grid(row=row, column=0, sticky="w", padx=(0, 16), pady=5)
            self.entry(body, self.setting_vars[key]).grid(row=row, column=1, sticky="ew", pady=5)
        self.label(body, "Browser").grid(row=len(rows), column=0, sticky="w", padx=(0, 16), pady=5)
        self.option(body, self.browser_pref, screen.find_browsers(), width=220).grid(
            row=len(rows), column=1, sticky="w", pady=5)

        self._build_ai_settings(page)

        body = self.card(page, "Appearance", "Everything here applies and saves as you "
                                             "change it.",
                         padx=(0, 8), action=("Reset to defaults", self.reset_appearance))
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x")
        self.label(row, "Theme").pack(side="left", padx=(0, 12))
        self.settings_theme = self.segmented(row, ["Light", "Dark"],
                                             command=lambda v: self.apply_theme(v.lower()))
        self.settings_theme.set(self.ui["theme"].capitalize())
        self.settings_theme.pack(side="left")
        self.switch(row, "Fullscreen (F11)", self.fullscreen_var,
                    lambda: self.set_fullscreen(self.fullscreen_var.get())).pack(side="left", padx=24)
        self.button(row, "Edit colours...", lambda: colorpicker.ColorEditor(self)).pack(side="left")

        row = ctk.CTkFrame(body, fg_color="transparent")
        row.pack(fill="x", pady=(14, 0))
        self.label(row, "Font").pack(side="left", padx=(0, 12))
        family_box = self.combo(row, self.font_family_var, ["Default"] + self._families,
                                width=280, command=lambda _v: self.apply_fonts())
        family_box.pack(side="left")
        # Typed names apply on Enter or leaving the field, not per keystroke.
        family_box.bind("<Return>", lambda _e: self.apply_fonts())
        family_box.bind("<FocusOut>", lambda _e: self.apply_fonts())
        self.label(row, "Size").pack(side="left", padx=(24, 12))
        Stepper(self, row, self.font_size_var, self.FONT_MIN, self.FONT_MAX).pack(side="left")
        self.label(row, "Body text in pixels; headings scale with it.",
                   "small").pack(side="left", padx=12)

        body = self.card(page, "Backups",
                         "Read-only copies of metadata.json, progress.json and the screening "
                         "markdown, taken whenever one changes. Nothing deletes them except "
                         "Clear old copies, which keeps the newest of each.", padx=(0, 8))
        self.backup_rows = ctk.CTkFrame(body, fg_color="transparent")
        self.backup_rows.pack(fill="x")
        row = FlowRow(body, gap=12)
        row.pack(fill="x", pady=(12, 0))
        row.add(self.button(row, "Clear old copies", self.on_clear_backups))
        row.add(self.button(row, "Open backups folder", self.on_open_backups))
        row.add(self.label(row, "Notify above"), gap=24)
        row.add(Stepper(self, row, self.backup_threshold_var, 1, 999))
        row.add(self.label(row, "copies of any one file", "small"))
        self._refresh_backup_note()

        row = ctk.CTkFrame(page, fg_color="transparent")
        row.pack(fill="x", pady=(0, 8), padx=(0, 8))
        self.button(row, "Save settings", self.on_save_settings, kind="primary",
                    width=150).pack(side="left")
        self.button(row, "Reset all settings to defaults",
                    self.reset_all_settings).pack(side="right")
        return page

    def _build_ai_settings(self, page):
        """Subscription or API, one active at a time. The switch saves on
        click - picking a mode is the decision, and a mode that only takes
        hold after a separate Save reads as selected while the old one still
        runs. Each side keeps its own model and effort."""
        body = self.card(page, "Screening AI",
                         "Subscription opens an interactive CLI session in a terminal. API "
                         "calls a provider directly. Switching takes effect at once.",
                         padx=(0, 8), action=("Reset to defaults", self.reset_ai))
        body.grid_columnconfigure(0, weight=1)

        self.ai_mode_seg = self.segmented(
            body, ["Subscription", "API"],
            command=lambda v: (self.ai_mode.set("api" if v == "API" else "subscription"),
                               self._on_ai_mode()))
        self.ai_mode_seg.set("API" if self.ai_mode.get() == "api" else "Subscription")
        self.ai_mode_seg.grid(row=0, column=0, sticky="w", pady=(0, 14))

        def rows(frame, spec):
            frame.grid_columnconfigure(1, weight=1)
            for r, (text, widget, sticky) in enumerate(spec):
                self.label(frame, text).grid(row=r, column=0, sticky="w", padx=(0, 16), pady=5)
                widget.grid(row=r, column=1, sticky=sticky, pady=5)

        sub = self.ai_sub_frame = ctk.CTkFrame(body, fg_color="transparent")
        agent_box = self.option(sub, self.ai_agent, list(screen.AGENTS),
                                command=lambda _v: self._on_ai_agent(), width=180)
        self.ai_agent_model_box = self.combo(sub, self.ai_agent_model, [], width=340)
        self.ai_agent_effort_box = self.combo(sub, self.ai_agent_effort, [], width=180)
        rows(sub, (("CLI", agent_box, "w"),
                   ("Binary", self.entry(sub, self.ai_bin), "ew"),
                   ("Model", self.ai_agent_model_box, "w"),
                   ("Effort", self.ai_agent_effort_box, "w")))
        self.ai_sub_hint = self.label(sub, "", "small")
        self.ai_sub_hint.grid(row=4, column=1, sticky="w")

        api = self.ai_api_frame = ctk.CTkFrame(body, fg_color="transparent")
        provider_box = self.option(api, self.ai_provider, list(config.PROVIDERS),
                                   command=lambda _v: self._on_ai_provider(), width=180)
        self.ai_key_entry = self.entry(api, self.ai_key, show="*")
        self.ai_api_model_box = self.combo(api, self.ai_api_model, [], width=340)
        self.ai_api_effort_box = self.combo(api, self.ai_api_effort, [], width=180)
        rows(api, (("Provider", provider_box, "w"),
                   ("API key", self.ai_key_entry, "ew"),
                   ("Model", self.ai_api_model_box, "w"),
                   ("Effort", self.ai_api_effort_box, "w")))
        self.ai_api_hint = self.label(api, "", "small")
        self.ai_api_hint.grid(row=4, column=1, sticky="w")

        batch = ctk.CTkFrame(body, fg_color="transparent")
        batch.grid(row=2, column=0, sticky="w", pady=(14, 0))
        self.label(batch, "Videos per batch").pack(side="left", padx=(0, 12))
        Stepper(self, batch, self.ai_batch, 1, 100).pack(side="left")

        self._on_ai_agent(initial=True)
        self._on_ai_provider(initial=True)
        self._on_ai_mode(initial=True)

    def _on_ai_mode(self, initial=False):
        mode = self.ai_mode.get()
        shown, hidden = ((self.ai_api_frame, self.ai_sub_frame) if mode == "api"
                         else (self.ai_sub_frame, self.ai_api_frame))
        hidden.grid_remove()
        shown.grid(row=1, column=0, sticky="ew")
        if initial or self.cfg.get("screen_mode") == mode:
            return
        self.cfg["screen_mode"] = mode
        try:
            config.save(self.cfg)
        except OSError as e:
            messagebox.showerror("Settings", f"Couldn't write config.json\n{e}")
            return
        self.log_line(f"Screening now uses: {'API' if mode == 'api' else 'subscription'}.")
        self.refresh_screen_counts()

    def _on_ai_agent(self, initial=False):
        """Model and effort clear on a real switch - a claude model id handed
        to codex fails at launch, later and more confusingly than a blank."""
        name, prev = self.ai_agent.get(), self._ai_prev_agent
        if not initial:
            self.ai_bins[prev] = self.ai_bin.get().strip().strip('"').strip("'")
            if name != prev:
                self.ai_agent_model.set("")
                self.ai_agent_effort.set("")
        self._ai_prev_agent = name
        _exe, _argv, efforts, models = screen.AGENTS[name]
        found = screen.agent_bin(self.cfg, name)
        # On a rebuild the field may hold an unsaved edit; keep it.
        if not initial or not self.ai_bin.get():
            self.ai_bin.set(self.ai_bins.get(name) or found)
        self.ai_agent_model_box.configure(values=models)
        self.ai_agent_effort_box.configure(values=efforts)
        self.ai_sub_hint.configure(
            text=("Opens an interactive session in a terminal in Untracked/. "
                  if found else f"{name} is not installed. ")
                 + "Blank model or effort = the CLI's default.")

    def _on_ai_provider(self, initial=False):
        pid = self.ai_provider.get()
        if not initial and pid != self._ai_prev_provider:
            self.ai_api_model.set("")
            self.ai_api_effort.set("")
        self._ai_prev_provider = pid
        spec = config.PROVIDERS.get(pid, {})
        if not initial or not self.ai_key.get():
            self.ai_key.set(self.cfg.get("keys", {}).get(pid, ""))
        self.ai_key_entry.configure(state="disabled" if spec.get("local") else "normal")
        models = [m for m, _ in spec.get("models", [])]
        if spec.get("local"):
            # Localhost answers or refuses instantly, so asking on every
            # switch costs nothing and shows what is actually loaded.
            try:
                models = config.live_models(config.runtime(self.cfg, pid)) or models
            except Exception:
                pass
        self.ai_api_model_box.configure(values=models)
        self.ai_api_effort_box.configure(
            values=config.EFFORT_LEVELS if spec.get("has_effort") else [])
        self.ai_api_hint.configure(
            text=spec.get("label", "")
                 + ("" if spec.get("local") else f"  -  key from {spec.get('key_url', '')}"))

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
        log = self._log_text
        at_bottom = log.yview()[1] > 0.999  # only autoscroll if already following
        log.configure(state="normal")
        # The pipeline's live counters are "\r...", i.e. rewrite the current
        # line. A Text widget has no cursor-return, so emulate it: a \r means
        # the next visible chunk replaces the last line instead of appending.
        for part in re.split(r"(\r\n|\r|\n)", text):
            if not part:
                continue
            if part == "\r":
                self._overwrite_line = True
            elif part in ("\n", "\r\n"):
                log.insert("end", "\n")
                self._overwrite_line = False
            else:
                if self._overwrite_line:
                    log.delete("end-1c linestart", "end-1c")
                    self._overwrite_line = False
                log.insert("end", part)
                self._update_progress(part)
        # Trim oldest lines so a multi-thousand-video run can't grow unbounded.
        excess = int(log.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if excess > 0:
            log.delete("1.0", f"{excess + 1}.0")
        log.configure(state="disabled")
        if at_bottom:
            log.see("end")

    def _update_progress(self, line):
        m = PROGRESS_RE.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if total:
                self._set_progress((done, total))

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

    def _grab_source(self):
        """(browser token, playlist url), or None after telling the user why."""
        token = self._selected_browser_token()
        if not token:
            messagebox.showerror("Browser", "Couldn't detect the default browser. Pick one explicitly.")
            return None
        playlist = self.playlist_var.get().strip().strip('"').strip("'")
        return token, playlist or grab_watchlist.DEFAULT_PLAYLIST

    def _grab(self, token, playlist):
        """Worker-thread half of Fetch watchlist. False when nothing came back,
        so a chained fetch doesn't go on to run against the old file."""
        print(f"Fetching watchlist from {token}...")
        ids = grab_watchlist.grab(token, playlist)
        if not ids:
            print("No IDs returned. Are you logged in on that browser, and is it fully closed?")
            return False
        with open(batch_fetch.INPUT, "w", encoding="utf-8") as f:
            f.write("\n".join(ids) + "\n")
        print(f"Wrote {len(ids)} unique IDs to {batch_fetch.INPUT}")
        return True

    def on_fetch_watchlist(self):
        if self._busy():
            return
        source = self._grab_source()
        if source:
            self._start(lambda: self._grab(*source), "Fetching watchlist...")

    def on_run(self):
        if self._busy():
            return
        if not os.path.isfile(batch_fetch.INPUT):
            messagebox.showerror("No watchlist", "watchlist.txt not found. Fetch the watchlist first.")
            return
        grab_first = False
        if watchlist_digest() == self.ui.get("last_run_watchlist"):
            choice = self._ask_unchanged_watchlist()
            if choice == "cancel":
                return
            grab_first = choice == "fetch"
        source = self._grab_source() if grab_first else None
        if grab_first and not source:
            return
        mode = self.mode_var.get()

        def work():
            if source:
                # A failed grab (browser open, cookies locked) must not fall
                # through to fetching the list the user just said was stale.
                if not self._grab(*source):
                    return
                print()
            # Recorded before the run, not after: a stopped or crashed run
            # still used this list, and the next press should still warn.
            self.ui["last_run_watchlist"] = watchlist_digest()
            self.save_ui()
            batch_fetch.run_pipeline(mode, self.stop_event)

        self._start(work, "Fetching metadata and transcripts...")

    def _ask_unchanged_watchlist(self):
        """Modal: 'fetch', 'continue' or 'cancel'. Closing the window cancels."""
        win = ctk.CTkToplevel(self.root)
        win.title("Watchlist unchanged")
        win.configure(fg_color=self.c("bg"))
        win.resizable(False, False)
        win.transient(self.root)
        choice = {"v": "cancel"}

        def pick(v):
            choice["v"] = v
            win.destroy()

        ctk.CTkLabel(
            win, justify="left", wraplength=460, font=self.fonts["body"],
            text_color=self.c("fg"),
            text="watchlist.txt is byte-for-byte the same as the list the last "
                 "fetch used. The watchlist doesn't seem to have changed - maybe "
                 "you forgot to fetch it first?",
        ).pack(padx=20, pady=(20, 14), anchor="w")
        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=20, pady=(0, 18))
        # c() is a (light, dark) pair, and the dark theme's green is pale, so
        # the label colour is picked per mode rather than fixed white.
        green = self.c("watch")
        on_green = tuple("#ffffff" if theme.contrast("#ffffff", g) >= theme.contrast("#000000", g)
                         else "#000000" for g in green)
        hover = tuple(theme.mix(g, t, 0.15) for g, t in zip(green, on_green))
        # Packed right to left: Fetch ends up rightmost.
        fetch = ctk.CTkButton(bar, text="Fetch", command=lambda: pick("fetch"), height=36,
                              corner_radius=9, font=self.fonts["body"], fg_color=green,
                              hover_color=hover, text_color=on_green)
        fetch.pack(side="right")
        self.button(bar, "Continue without fetching", lambda: pick("continue")).pack(
            side="right", padx=(0, 8))
        self.button(bar, "Cancel", lambda: pick("cancel")).pack(side="right", padx=(0, 8))

        win.protocol("WM_DELETE_WINDOW", lambda: pick("cancel"))
        win.bind("<Escape>", lambda _e: pick("cancel"))
        win.bind("<Return>", lambda _e: pick("fetch"))
        win.after(100, lambda: (win.lift(), fetch.focus_set(), win.grab_set()))
        self.root.wait_window(win)
        return choice["v"]

    def on_stop(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.status_var.set("Stopping - finishing the current request...")
            self.stop_btn.configure(state="disabled")

    def on_clear(self):
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")
        if not self._run_active:
            self._set_progress(None)  # a running job keeps its count

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

    def _backend_summary(self):
        cfg = self.cfg
        if screen.backend(cfg) == screen.API:
            pcfg = config.runtime(cfg, cfg.get("screen_provider") or cfg.get("provider"),
                                  cfg.get("screen_api_model", ""))
            return f"Using the {pcfg['provider']} API, model {pcfg['model'] or 'not set'}."
        name = screen.backend(cfg)
        model = cfg.get("screen_agent_model") or "its default model"
        return f"Using {name} ({model}) in a terminal session, working in Untracked/."

    def refresh_screen_counts(self):
        try:
            corpus = screen.load_corpus(queued_only=True)
        except (OSError, ValueError):
            corpus = None
        total = None if corpus is None else len(corpus)
        queued_ids = None if corpus is None else [rec["id"] for _pos, rec in corpus]
        s = progress.summary(total, queued_ids)
        values = {"screened": s["screened"], "left": "?" if s["left"] is None else s["left"],
                  "watch": s["verdict_watch"], "read": s["verdict_read"],
                  "skip": s["verdict_skip"], "decided": s["decided"], "rated": s["rated"]}
        for key, value in values.items():
            self.stat_lbls[key].configure(text=str(value))
        self.screen_sub.configure(text=self._backend_summary())

    def on_screen(self):
        if self._busy():
            return
        try:
            n = int(float(self.batch_var.get()))
            if n < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Batch size", "Enter a whole number, or 0 for all.")
            return
        cfg = self.cfg
        label = "all remaining" if n == 0 else str(n)

        if screen.backend(cfg) == screen.API:
            def work():
                screen.run_api(n, cfg, self.stop_event)

            self._start(work, f"Screening {label} videos via API...")
            return

        # Only a blocking launcher can say a session is still open; elsewhere a
        # second run is allowed and simply takes over the watch.
        if (self._agent_blocks and self._agent_proc is not None
                and self._agent_proc.poll() is None):
            messagebox.showinfo("Screen", "A screening terminal is already open.")
            return
        try:
            proc, ids, out_path, summary, blocks = screen.launch_agent(n, cfg)
        except screen.ScreenError as e:
            messagebox.showerror("Screen", str(e))
            return
        except OSError as e:
            messagebox.showerror("Screen", f"Couldn't launch the terminal\n{e}")
            return
        self._agent_gen += 1
        self._agent_proc, self._agent_watch, self._agent_blocks = proc, (ids, out_path), blocks
        self._agent_seen = -1
        self.log_line(summary)
        self.status_var.set("Screening in the terminal - results record as they land.")
        gen = self._agent_gen
        self.root.after(self.AGENT_POLL_MS, lambda: self._poll_agent(gen))

    AGENT_POLL_MS = 5000

    def _poll_agent(self, gen):
        """The terminal session can't report back, so the output file is the
        channel: re-parsed on a timer, and once more when the window closes.
        record_results is idempotent, so re-marking the same ids is harmless.

        Where the launcher exits straight away (Windows, macOS) its process
        can't mark the end, so the watch runs until every requested video
        has a verdict, or until a newer run replaces it. A session stopped
        early therefore keeps a quiet 5-second file read going; cheap, and
        it still catches verdicts if the session is resumed by hand."""
        if gen != self._agent_gen:
            return
        ids, out_path = self._agent_watch
        try:
            # The AI CLI writes the screening file, not the app, so this poll
            # is where its changes get their read-only copy.
            if backups.snapshot(out_path):
                self._refresh_backup_note()
            got = screen.record_results(out_path, ids)
        except OSError:
            got = None
        if got is not None and got != self._agent_seen:
            self._agent_seen = got
            self.refresh_screen_counts()
        if self._agent_blocks:
            done = self._agent_proc.poll() is not None
        else:
            done = got == len(ids)
        if not done:
            self.root.after(self.AGENT_POLL_MS, lambda: self._poll_agent(gen))
            return
        self.log_line((f"Screening terminal closed. Recorded {got or 0} of {len(ids)}."
                       if self._agent_blocks else f"All {len(ids)} requested videos recorded."))
        self.status_var.set("Idle.")
        self._agent_proc, self._agent_seen = None, -1
        self.refresh_review()

    def on_claude_terminal(self):
        """Claude runs in a real terminal emulator rather than in here: tkinter
        has no ANSI or alt-screen handling, and on Wayland there is no XEmbed
        to borrow a real one with."""
        binary = screen.claude_bin(self.cfg)
        if not binary:
            messagebox.showerror("claude", "claude not found. Set its path in Settings.")
            return
        try:
            screen.open_in_terminal(paths.home(), [binary])
        except screen.ScreenError as e:
            messagebox.showerror("No terminal", str(e))
        except OSError as e:
            messagebox.showerror("Terminal", f"Couldn't open a terminal\n{e}")

    def on_edit_skill(self):
        """Plain text, no validation. The skill is a prompt, not a config file -
        anything that tried to parse it would go stale the moment it changed."""
        path = screen.skill_path(self.cfg)
        if not path or not os.path.isfile(path):
            messagebox.showerror("Skill", "Screener skill not found. Set its path in Settings.")
            return
        win = ctk.CTkToplevel(self.root)
        win.title(path)
        win.geometry("900x700")
        win.configure(fg_color=self.c("bg"))
        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(side="bottom", fill="x", padx=16, pady=(8, 14))
        box = ctk.CTkTextbox(win, wrap="word", undo=True, font=self.fonts["mono"],
                             fg_color=self.c("card"), text_color=self.c("fg"),
                             border_width=1, border_color=self.c("line"), corner_radius=12)
        box.pack(fill="both", expand=True, padx=16, pady=(16, 0))
        text = box._textbox
        text.configure(padx=14, pady=12)

        with open(path, encoding="utf-8") as f:
            text.insert("1.0", f.read())
        text.edit_reset()
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

        self.button(bar, "Save", save, kind="primary", width=96).pack(side="right")
        self.button(bar, "Close", win.destroy, width=96).pack(side="right", padx=(0, 8))
        win.after(100, win.lift)

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
                open(md, encoding="utf-8").read(), render_screening.corpus_positions())
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
        """Rows come from progress.json, one per entry - never from the
        corpus. Built the other way round, a review only existed on screen
        while its video was in metadata.json, and losing the metadata hid
        every review. The corpus only supplies fresher details and the queue
        position; each entry's own snapshot covers what the corpus lacks."""
        try:
            corpus = screen.load_corpus()
            corpus_error = None
        except (OSError, ValueError) as e:
            corpus, corpus_error = [], e
        self._corpus_by_id = {rec["id"]: (pos, rec) for pos, rec in corpus}
        if corpus:
            # Keeps each entry's snapshot current, so the next time the corpus
            # is missing the table still has today's title and view count.
            progress.sync_videos(rec for _, rec in corpus)
        data = progress.load()

        rows = []
        for vid, entry in data.items():
            rating = entry.get("rating")
            # "Decided" is anything carrying a rating, sentinel included -
            # which is the whole reason a skip stores "-" rather than nothing.
            if self.hide_decided.get() and rating not in (None, ""):
                continue
            pos, rec = self._corpus_by_id.get(vid, ("", None))
            info = {**(entry.get("video") or {}), **progress.video_snapshot(rec or {})}
            rows.append((vid, {
                # No queue position - archived, or not in the corpus at all -
                # reads as a dash: "kept, but not in your watchlist right now".
                "num": pos if pos != "" else progress.UNRATED,
                "date": _pretty_date(info.get("upload_date")),
                "title": info.get("title") or vid,
                "channel": info.get("channel", ""),
                "views": _short_count(info.get("view_count")),
                "ai": entry.get("ai_verdict", ""),
                "status": (entry.get("status") or "").replace("_", " "),
                "rating": "" if rating is None else rating,
            }))

        rows.sort(key=lambda r: _sort_key(self._sort_col, r[1], self._sort_desc),
                  reverse=self._sort_desc)

        self.tree.delete(*self.tree.get_children())
        for i, (vid, row) in enumerate(rows):
            verdict = row["ai"]
            tags = ((f"ai-{verdict}",) if verdict else ()) + (("odd",) if i % 2 else ())
            self.tree.insert("", "end", iid=vid,
                             values=tuple(row[c[0]] for c in self.REVIEW_COLS), tags=tags)
        self._mark_sort_heading()

        s = progress.summary(len(corpus))
        mean = "-" if s["mean_rating"] is None else s["mean_rating"]
        shown = f"{len(rows)} shown  -  " if len(rows) != s["screened"] else ""
        missing = sum(1 for vid in data if vid not in self._corpus_by_id)
        note = (f"No corpus at {paths.home()} ({corpus_error}); showing saved details  -  "
                if corpus_error else
                f"{missing} not in metadata.json, shown from saved details  -  " if missing else "")
        self.review_lbl.configure(
            text=f"{note}{shown}{s['decided']} decided of {s['screened']} screened  -  "
                 f"watched {s['watched_full']} / scrubbed {s['scrubbed']} / "
                 f"read {s['read_summary']} / skipped {s['skipped']}  -  "
                 f"mean rating {mean} over {s['rated']}")
        self._refresh_backup_note()  # a saved decision may have added a progress.json copy

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
        arrow = " ▾" if self._sort_desc else " ▴"
        for col, label, _w, _m, anchor in self.REVIEW_COLS:
            self.tree.heading(col, anchor=anchor,
                              text=label + (arrow if col == self._sort_col else ""))

    def _selected_videos(self):
        return list(self.tree.selection())

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

    def _on_status_pick(self, name):
        self.status_choice.set(next(slug for slug, n in STATUSES if n == name))

    def _sync_status_seg(self):
        seg = getattr(self, "status_seg", None)
        if seg is not None and seg.winfo_exists():
            seg.set(dict(STATUSES).get(self.status_choice.get(), ""))

    def _on_status_change(self, *_args):
        """Skipping and scoring are mutually exclusive - a skip stopped at
        the takeaway, so there is no video to score. Without this the midpoint
        default would quietly record a 5 for every skip."""
        self._sync_status_seg()
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
                progress.set_decision(vid, status, raw or None,
                                      video=self._corpus_by_id.get(vid, ("", None))[1])
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
        self._open_url(f"https://www.youtube.com/watch?v={vids[0]}")

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
        """Live: CTk widgets already hold both colours and just switch; only
        the ttk table has to be repainted."""
        self.ui["theme"] = mode
        ctk.set_appearance_mode(mode)
        self._theme_apply()
        self._style_tree()
        for seg in (self.side_theme, self.settings_theme):
            seg.set(mode.capitalize())
        self.save_ui()

    def recolor(self):
        """After a colour edit. The widgets' colour pairs were fixed when they
        were made, so they are rebuilt; the variables survive, so the inputs,
        the page and the log carry over."""
        self._load_palettes()
        self._theme_apply()
        log = self._log_text.get("1.0", "end-1c")
        selection = self._selected_videos()
        self.shell.destroy()
        self._build_widgets()  # re-applies self._progress_state
        self._log_text.configure(state="normal")
        self._log_text.insert("1.0", log)
        self._log_text.configure(state="disabled")
        self._log_text.see("end")
        self.refresh_watchlist_count()
        self.refresh_screen_counts()
        self.refresh_review()
        still = [v for v in selection if self.tree.exists(v)]
        if still:
            self.tree.selection_set(still)
        if self._run_active:
            for btn in self._action_btns:
                btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")

    def set_fullscreen(self, on):
        self.root.attributes("-fullscreen", bool(on))
        self.ui["fullscreen"] = bool(on)
        self.fullscreen_var.set(bool(on))
        self.save_ui()

    def toggle_fullscreen(self):
        self.set_fullscreen(not self.root.attributes("-fullscreen"))

    # ---------- resets ----------

    # config.json keys each settings panel owns. API keys, the analysis
    # pipeline's provider/model and the corpus folder are deliberately absent:
    # the first two are credentials and choices made outside this page, and
    # resetting the corpus would point the app at a different data/ on restart.
    PATH_KEYS = ("skill_path", "claude_bin", "screening_md", "browser")
    AI_KEYS = ("screen_mode", "screen_agent", "screen_agent_model", "screen_agent_effort",
               "screen_bins", "screen_provider", "screen_api_model", "screen_api_effort",
               "screen_batch")
    APPEARANCE_KEYS = ("theme", "fullscreen", "colors", "font_family", "font_size")

    def _confirm_reset(self, what):
        return messagebox.askokcancel(
            "Reset to defaults", f"Reset {what} to defaults?\n\nThis is saved straight away.")

    def _write_config(self, keys):
        for key in keys:
            default = config.DEFAULTS[key]
            self.cfg[key] = dict(default) if isinstance(default, dict) else default
        try:
            config.save(self.cfg)
        except OSError as e:
            messagebox.showerror("Settings", f"Couldn't write config.json\n{e}")
            return False
        return True

    def _reset_paths(self):
        if not self._write_config(self.PATH_KEYS):
            return False
        for key in ("skill_path", "claude_bin", "screening_md"):
            self.setting_vars[key].set({"skill_path": screen.find_skill(),
                                        "claude_bin": screen.find_claude(),
                                        "screening_md": screen.screening_path(self.cfg)}[key])
        found = screen.find_browsers()
        self.browser_pref.set(found[0] if found else "")
        return True

    def _reset_ai(self):
        if not self._write_config(self.AI_KEYS):
            return False
        cfg = self.cfg
        self.ai_mode.set("subscription")
        self.ai_agent.set(screen.backend(cfg))
        self.ai_bins = {}
        self.ai_bin.set("")  # empty so the initial pass re-detects it
        self.ai_agent_model.set("")
        self.ai_agent_effort.set("")
        self.ai_provider.set(cfg.get("provider") or config.DEFAULT_PROVIDER)
        self.ai_key.set("")
        self.ai_api_model.set("")
        self.ai_api_effort.set("")
        self.ai_batch.set(str(screen.batch_size(cfg)))
        self._ai_prev_agent, self._ai_prev_provider = self.ai_agent.get(), self.ai_provider.get()
        self.ai_mode_seg.set("Subscription")
        self._on_ai_agent(initial=True)
        self._on_ai_provider(initial=True)
        self._on_ai_mode(initial=True)
        return True

    def _reset_appearance_state(self):
        """Settings only; the caller repaints. Custom presets and recent
        colours survive - they are things you made, not settings."""
        for key in self.APPEARANCE_KEYS:
            default = settings.DEFAULTS[key]
            self.ui[key] = dict(default) if isinstance(default, dict) else default
        self.font_family_var.set("Default")
        self.font_size_var.set(str(self.font_size()))
        self.root.attributes("-fullscreen", False)
        self.fullscreen_var.set(False)
        ctk.set_appearance_mode(self.ui["theme"])
        self._theme_apply()
        for kind, (step, weight) in self.FONT_STEPS.items():
            self.fonts[kind].configure(
                family=self.mono_font if kind == "mono" else self.ui_font,
                size=max(8, self.font_size() + step), weight=weight)

    def reset_paths(self):
        if self._confirm_reset("the paths") and self._reset_paths():
            self.log_line("Paths reset to defaults.")
            self.refresh_screen_counts()

    def reset_ai(self):
        if self._confirm_reset("the screening AI") and self._reset_ai():
            self.log_line("Screening AI reset to defaults.")
            self.refresh_screen_counts()

    def reset_appearance(self):
        if not self._confirm_reset("the appearance (theme, colours, font)"):
            return
        self._reset_appearance_state()
        self.save_ui()
        self.recolor()  # colour pairs are baked into widgets; rebuild to drop the old ones
        self.log_line("Appearance reset to defaults.")

    def reset_all_settings(self):
        if not messagebox.askokcancel(
                "Reset all settings",
                "Reset every setting on this page to defaults?\n\n"
                "Kept: API keys, the corpus folder, your custom colour presets, "
                "and the window size.\nThis is saved straight away."):
            return
        if not (self._reset_paths() and self._reset_ai()):
            return
        self._reset_appearance_state()
        for key in ("log_open", "log_height", "review_sort", "review_desc", "review_widths",
                    "guide_geometry"):
            default = settings.DEFAULTS[key]
            self.ui[key] = dict(default) if isinstance(default, dict) else default
        self._sort_col, self._sort_desc = self.ui["review_sort"], self.ui["review_desc"]
        self._saved_widths = {}
        self.save_ui()
        self.recolor()
        self.log_line("All settings reset to defaults.")

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
            self.cfg["screen_batch"] = max(1, int(float(self.ai_batch.get())))
        except ValueError:
            messagebox.showerror("Settings", "Videos per batch must be a whole number.")
            return
        self.cfg["screen_mode"] = self.ai_mode.get()
        agent = self.ai_agent.get()
        self.cfg["screen_agent"] = agent
        self.cfg["screen_agent_model"] = self.ai_agent_model.get().strip()
        self.cfg["screen_agent_effort"] = self.ai_agent_effort.get().strip()
        typed = self.ai_bin.get().strip().strip('"').strip("'")
        # Only persist a path that differs from detection, same rule as the
        # fields above: a detected path goes stale when the CLI moves.
        self.ai_bins[agent] = "" if typed == shutil.which(screen.AGENTS[agent][0]) else typed
        pid = self.ai_provider.get()
        self.cfg["screen_provider"] = pid
        self.cfg["screen_api_model"] = self.ai_api_model.get().strip()
        self.cfg["screen_api_effort"] = self.ai_api_effort.get().strip()
        key = self.ai_key.get().strip().strip('"').strip("'")
        if key:
            self.cfg.setdefault("keys", {})[pid] = key
        self.cfg["screen_bins"] = {k: v for k, v in self.ai_bins.items() if v}
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
        self._set_progress(None)  # appears with the run's first [n/total]
        self.status_var.set(status)
        for btn in self._action_btns:
            btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        # A run's output is the reason to look at the log; a hidden drawer
        # would make it look like nothing happened.
        if not self.ui.get("log_open", True):
            self._set_log_open(True)

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
        self._set_progress(None)
        self._refresh_backup_note()
        self.status_var.set("Stopped." if self.stop_event.is_set() else "Done.")
        self.refresh_watchlist_count()
        self.refresh_screen_counts()
        self.refresh_review()

    def save_ui(self):
        """Writes user settings now, not just on close - colour edits should
        survive a crash or a kill."""
        try:
            settings.save(self.ui)
        except OSError as e:
            self.log_line(f"Couldn't save {settings.PATH}: {e}")

    def _save_ui(self):
        try:
            full = bool(self.root.attributes("-fullscreen"))
            self.ui.update({
                "fullscreen": full,
                # A fullscreen window reports the screen as its geometry, which
                # would strand the next non-fullscreen session at that size.
                "geometry": self.ui.get("geometry", "") if full else self.root.geometry(),
                "review_sort": self._sort_col,
                "review_desc": self._sort_desc,
                "review_widths": {c[0]: int(self.tree.column(c[0], "width"))
                                  for c in self.REVIEW_COLS},
            })
            self.ui.pop("sash", None)  # from the paned layout, now meaningless
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
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
