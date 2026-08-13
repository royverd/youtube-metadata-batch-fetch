"""
gui.py

Desktop front end for the two fetch stages: grab_watchlist (playlist -> IDs)
and batch_fetch (IDs -> metadata.json + metadata.csv, transcripts included).
Nothing here reimplements the pipeline - it collects the settings the CLI
would have prompted for, then calls the same functions.

tkinter on purpose: it ships with Python, so the GUI adds no dependency to a
project whose whole install is currently "yt-dlp and youtube-transcript-api".

The work runs on a background thread with stdout piped into the log pane, so
the window stays responsive and Stop lands immediately - including mid-delay,
since the transcript pass waits on the stop event rather than sleeping.

Run: python gui.py
Deps: same as batch_fetch (yt-dlp on PATH, youtube-transcript-api). No extras.
"""

import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import batch_fetch
import grab_watchlist

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

        root.title("YouTube Batch Transcript Fetch")
        root.geometry("900x680")
        root.minsize(760, 560)

        self._build_widgets()
        self.refresh_watchlist_count()
        self.root.after(POLL_MS, self._drain_log)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- layout ----------

    def _build_widgets(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # Step 1: watchlist
        step1 = ttk.LabelFrame(outer, text="1. Watchlist", padding=10)
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
        self.grab_btn = ttk.Button(step1, text="Fetch Watchlist", command=self.on_fetch_watchlist)
        self.grab_btn.grid(row=1, column=2, sticky="e", pady=2)

        self.watchlist_lbl = ttk.Label(step1, text="", foreground="#555")
        self.watchlist_lbl.grid(row=2, column=1, sticky="w", padx=(6, 0), pady=(4, 0))

        # Step 2: routing
        step2 = ttk.LabelFrame(outer, text="2. Transcript routing", padding=10)
        step2.pack(fill="x", pady=(10, 0))
        self.mode_var = tk.StringVar(value="0")
        for row, (value, name, desc) in enumerate(MODES):
            ttk.Radiobutton(step2, text=name, value=value,
                            variable=self.mode_var).grid(row=row, column=0, sticky="w")
            ttk.Label(step2, text=desc, foreground="#555").grid(
                row=row, column=1, sticky="w", padx=(10, 0))

        # Actions
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 0))
        self.run_btn = ttk.Button(actions, text="Run Fetch", command=self.on_run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Clear Log", command=self.on_clear).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Open Output Folder",
                   command=self.on_open_folder).pack(side="right")

        # Log
        logframe = ttk.LabelFrame(outer, text="Log", padding=6)
        logframe.pack(fill="both", expand=True, pady=(10, 0))
        self.log = tk.Text(logframe, wrap="none", height=18, background="#1e1e1e",
                           foreground="#e6e6e6", insertbackground="#e6e6e6",
                           font=("Consolas", 9), state="disabled")
        yscroll = ttk.Scrollbar(logframe, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=yscroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

        # Status
        status = ttk.Frame(outer)
        status.pack(fill="x", pady=(8, 0))
        self.status_var = tk.StringVar(value="Idle.")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")
        self.progress = ttk.Progressbar(status, length=220, mode="determinate")
        self.progress.pack(side="right")

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
        try:
            if sys.platform == "win32":
                os.startfile(batch_fetch.HERE)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", batch_fetch.HERE])
            else:
                subprocess.Popen(["xdg-open", batch_fetch.HERE])
        except Exception as e:
            messagebox.showerror("Open folder", f"Couldn't open {batch_fetch.HERE}\n{e}")

    # ---------- worker lifecycle ----------

    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _start(self, work, status):
        self.stop_event.clear()
        self.progress.configure(value=0)
        self.status_var.set(status)
        self.run_btn.configure(state="disabled")
        self.grab_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        def runner():
            # Redirect only for this thread's duration; the worker is the sole
            # writer while it runs, so a process-wide swap is safe here.
            real_stdout = sys.stdout
            sys.stdout = QueueWriter(self.log_queue)
            try:
                work()
            except FileNotFoundError:
                print("yt-dlp not found on PATH. Install it first: pip install yt-dlp")
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
        self.run_btn.configure(state="normal")
        self.grab_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Stopped." if self.stop_event.is_set() else "Done.")
        self.refresh_watchlist_count()

    def _on_close(self):
        if self._busy():
            if not messagebox.askokcancel("Quit", "A fetch is still running. Stop it and quit?"):
                return
            self.stop_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
