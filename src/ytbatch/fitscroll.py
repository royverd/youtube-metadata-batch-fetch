"""
fitscroll.py

A window body that scrolls only when the window is too small for it.

CTkScrollableFrame scrolls one axis and always owns its size, which suits a
page of cards and not a whole window: the sidebar, status bar and log drawer
should sit exactly where the layout puts them whenever there is room. So the
content here follows the window down to its own natural size on each axis,
and only below that does it stop shrinking and get a scrollbar for that axis.
Nothing is ever clipped out of reach; on a big enough window nothing changes.

Natural size is the content's requested size, which Tk updates silently - a
font change grows it without any event on a widget whose size is pinned - so
it is checked on a short timer instead of trusting <Configure>. Two calls to
winfo per tick; nothing to notice.

Deps: customtkinter.
"""

import sys
import tkinter as tk
import weakref
from tkinter import ttk

import customtkinter as ctk

POLL_MS = 300
STEP_PX = 24
# How long the window size must hold still before the content is laid out
# again. Dragging an edge sends a resize per mouse move, and a full relayout
# of CustomTkinter widgets costs a few hundred ms here - so laying out on
# every one is what made dragging lag. Meanwhile only the frame follows.
# Above the ~100 ms Windows itself takes to repaint the frame per step, so a
# slow step can't let the timer fire in the middle of a drag.
RESIZE_SETTLE_MS = 150

def _coalesced_scrollbar_set(self, start_value, end_value):
    """CTkScrollbar.set redraws at once, and its _draw ends in
    update_idletasks - a full synchronous relayout of the window. Every
    scrollable view calls set() on each size change, so one step of dragging
    the window edge ran that relayout dozens of times, nested inside the
    resize: about 0.25 s of the 0.4 s each step took. Recording the values
    and drawing once when Tk next goes idle gives the same final picture."""
    self._start_value, self._end_value = float(start_value), float(end_value)
    if getattr(self, "_set_pending", False):
        return
    self._set_pending = True

    def draw():
        self._set_pending = False
        if self.winfo_exists():
            self._draw()

    self.after_idle(draw)


_ctk_scrollbar_draw = ctk.CTkScrollbar._draw


def _scrollbar_draw_without_flush(self, no_color_updates=False):
    """The same drawing minus the update_idletasks at its end. Coalescing
    set() cut how often scrollbars drew, but each draw still forced a
    whole-window relayout (~30 ms here), which resized views, which drew
    scrollbars again. Drawing to a canvas needs no flush; Tk paints it on
    the next idle like everything else."""
    canvas = self._canvas
    canvas.update_idletasks = _no_flush  # shadows the method for this call only
    try:
        _ctk_scrollbar_draw(self, no_color_updates)
    finally:
        del canvas.update_idletasks


def _no_flush():
    pass


def install_scrollbar_fix():
    """Class-level, because CTkScrollableFrame and CTkTextbox build their
    own scrollbars where nothing here can reach them. Idempotent."""
    if ctk.CTkScrollbar.set is not _coalesced_scrollbar_set:
        ctk.CTkScrollbar.set = _coalesced_scrollbar_set
        ctk.CTkScrollbar._draw = _scrollbar_draw_without_flush


def grid_hide(widget):
    """grid_remove that stays removed.

    CustomTkinter re-runs each widget's last grid/pack/place call whenever
    the scaling changes - once just after startup, and on every interface
    scale change - and grid_remove, unlike grid_forget, leaves that record in
    place. So every hidden page came back at startup, stacked under the one
    meant to show (Settings, made last, on top), along with the closed log
    drawer and whichever AI settings panel wasn't selected. Clearing the
    record keeps what Tk remembers for the next plain .grid()."""
    target = getattr(widget, "_parent_frame", widget)  # CTkScrollableFrame's outer frame
    target.grid_remove()
    if hasattr(target, "_last_geometry_manager_call"):
        target._last_geometry_manager_call = None


# One wheel dispatcher per process, not one bind_all per instance: bind_all
# can't be undone for a single handler without dropping CustomTkinter's too,
# and every Guide opened would otherwise leave a dead handler behind.
_live = weakref.WeakSet()
_bound = set()


class FitScroll:
    def __init__(self, master, bg, bar_color, bar_hover, min_size=None):
        """Packs itself into master, filling it; put content in .inner.

        min_size, if given, is called for the (width, height) below which to
        scroll, in pixels - for content that can give way on its own before
        scrolling is needed (the main window's log drawer). Default is the
        content's requested size."""
        self.min_size = min_size
        self._following = []
        self._last = None
        self._resize_job = None

        self.frame = ctk.CTkFrame(master, fg_color=bg, corner_radius=0)
        self.frame.pack(fill="both", expand=True)
        self.frame.grid_rowconfigure(0, weight=1)
        self.frame.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self.frame, highlightthickness=0, bd=0,
                                xscrollincrement=STEP_PX, yscrollincrement=STEP_PX)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar = ctk.CTkScrollbar(self.frame, orientation="vertical", command=self.canvas.yview,
                                     button_color=bar_color, button_hover_color=bar_hover)
        self.hbar = ctk.CTkScrollbar(self.frame, orientation="horizontal",
                                     command=self.canvas.xview,
                                     button_color=bar_color, button_hover_color=bar_hover)
        self.canvas.configure(yscrollcommand=self.vbar.set, xscrollcommand=self.hbar.set)
        self.inner = ctk.CTkFrame(self.canvas, fg_color=bg, corner_radius=0)
        self._item = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.set_bg(bg)

        self.canvas.bind("<Configure>", self._on_resize, add="+")
        _live.add(self)
        root = master.winfo_toplevel().nametowidget(".")
        if str(root) not in _bound:
            _bound.add(str(root))
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                root.bind_all(seq, _dispatch_wheel, add="+")
        self.frame.after(POLL_MS, self._tick)

    def set_bg(self, bg, bar_color=None, bar_hover=None):
        """The canvas is plain Tk and can't hold a light/dark pair. It only
        shows for a frame while the content catches up to a resize."""
        self.frame.configure(fg_color=bg)
        self.inner.configure(fg_color=bg)
        self.canvas.configure(background=self.frame._apply_appearance_mode(bg))
        if bar_color is not None:
            for bar in (self.vbar, self.hbar):
                bar.configure(button_color=bar_color, button_hover_color=bar_hover)

    def follow(self, scrollable):
        """A CTkScrollableFrame inside: make it request its content's width.
        Scrolling vertically, it otherwise reports a flat 200 px however wide
        its rows are, and a window narrower than those rows would clip them
        instead of scrolling here."""
        self._following.append(scrollable)

    def unfollow_all(self):
        """For a caller about to rebuild its content from scratch."""
        self._following.clear()

    def _on_resize(self, _event=None):
        if self._resize_job is not None:
            self.canvas.after_cancel(self._resize_job)
        self._resize_job = self.canvas.after(RESIZE_SETTLE_MS, self._resize_done)

    def _resize_done(self):
        self._resize_job = None
        self.sync()

    def _tick(self):
        if not self.frame.winfo_exists():
            return
        if self._resize_job is None:  # mid-drag the timer above owns it
            self.sync()
        self.frame.after(POLL_MS, self._tick)

    def sync(self):
        if not self.frame.winfo_exists():
            return
        for sf in [s for s in self._following if s.winfo_exists()]:
            want = sf.winfo_reqwidth()
            canvas = sf._parent_canvas
            if want > 1 and int(float(canvas.cget("width"))) != want:
                canvas.configure(width=want)
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return  # not mapped yet
        mw, mh = (self.min_size() if self.min_size
                  else (self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()))
        state = (cw, ch, mw, mh)
        if state == self._last:
            return
        self._last = state
        w, h = max(cw, mw), max(ch, mh)
        self.canvas.itemconfigure(self._item, width=w, height=h)
        self.canvas.configure(scrollregion=(0, 0, w, h))
        # Showing a bar takes room from the canvas, which can make the other
        # axis need one too. That only ever adds bars, so it settles in a
        # pass or two instead of flickering.
        self._show(self.vbar, mh > ch, dict(row=0, column=1, sticky="ns"), self.canvas.yview_moveto)
        self._show(self.hbar, mw > cw, dict(row=1, column=0, sticky="ew"), self.canvas.xview_moveto)

    @staticmethod
    def _show(bar, needed, where, reset):
        if needed and not bar.winfo_manager():
            bar.grid(**where)
        elif not needed and bar.winfo_manager():
            grid_hide(bar)
            reset(0)

    def overflows(self, horizontal):
        bar = self.hbar if horizontal else self.vbar
        return bool(bar.winfo_manager())

    def contains(self, widget):
        w = widget
        while w is not None:
            if w is self.inner:
                return True
            w = getattr(w, "master", None)
        return False


def _inner_scroller_takes(widget, stop, horizontal):
    """True when something between widget and stop already scrolls this way
    and still can: a page, the Review table, the log. Leaving the wheel to it
    there is what makes nested scrolling feel like one surface."""
    w = widget
    while w is not None and w is not stop:
        if isinstance(w, (tk.Canvas, tk.Text, tk.Listbox, ttk.Treeview)):
            try:
                view = w.xview() if horizontal else w.yview()
            except (tk.TclError, AttributeError):
                view = (0.0, 1.0)
            if tuple(round(v, 4) for v in view) != (0.0, 1.0):
                return True
        w = getattr(w, "master", None)
    return False


def _dispatch_wheel(event):
    widget = event.widget
    if isinstance(widget, str):  # a Tk-internal path with no Python object
        return
    horizontal = bool(event.state & 0x1)  # Shift
    for fs in list(_live):
        if not fs.frame.winfo_exists() or not fs.contains(widget):
            continue
        if not fs.overflows(horizontal) or _inner_scroller_takes(widget, fs.canvas, horizontal):
            return
        if event.num == 4:
            steps = -1
        elif event.num == 5:
            steps = 1
        elif sys.platform == "darwin":
            steps = -event.delta
        else:
            steps = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        scroll = fs.canvas.xview_scroll if horizontal else fs.canvas.yview_scroll
        scroll(steps * 3, "units")
        return
