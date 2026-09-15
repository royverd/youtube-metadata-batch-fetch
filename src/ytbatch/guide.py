"""
guide.py

The in-app guide: a short paged walkthrough of what the app does, shown on
first launch and reopened from the ? at the bottom of the sidebar.

Five pages, a few sentences each - it is read once by someone who wants to
get going, not studied. The visuals are built from the app's own widgets
rather than screenshots, so they follow the theme and colours and can't go
stale against a restyle. Some pages have a control to try; those only change
what the page says, never your data or settings.

Deps: customtkinter.
"""

import customtkinter as ctk

WRAP = 700  # px; the window is fixed-ish, and long lines are what makes help unreadable


class Guide:
    PAGES = ("welcome", "fetch", "screen", "review", "settings")

    def __init__(self, app):
        existing = getattr(app, "_guide", None)
        if existing is not None and existing.win.winfo_exists():
            existing.win.lift()
            existing.win.focus_force()
            return
        app._guide = self
        self.app = app
        self.index = 0

        win = self.win = ctk.CTkToplevel(app.root)
        win.title("ytbatch guide")
        win.geometry("900x660")
        win.minsize(780, 580)
        win.configure(fg_color=app.c("bg"))
        win.transient(app.root)
        win.protocol("WM_DELETE_WINDOW", self.close)
        win.bind("<Left>", lambda _e: self.go(self.index - 1))
        win.bind("<Right>", lambda _e: self.go(self.index + 1))
        win.bind("<Escape>", lambda _e: self.close())

        self.body = ctk.CTkFrame(win, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=36, pady=(30, 0))

        footer = ctk.CTkFrame(win, fg_color="transparent", height=60)
        footer.pack(fill="x", padx=36, pady=(8, 22))
        self.skip_btn = app.button(footer, "Skip guide", self.close, kind="ghost")
        self.skip_btn.pack(side="left")
        self.next_btn = app.button(footer, "Next", lambda: self.go(self.index + 1),
                                   kind="primary", width=130)
        self.next_btn.pack(side="right")
        self.back_btn = app.button(footer, "Back", lambda: self.go(self.index - 1), width=90)
        self.back_btn.pack(side="right", padx=8)
        self.dots = ctk.CTkFrame(footer, fg_color="transparent")
        self.dots.place(relx=0.5, rely=0.5, anchor="center")

        self.go(0)
        win.after(150, win.lift)

    # ---------- navigation ----------

    def go(self, index):
        if index >= len(self.PAGES):
            self.close()
            return
        self.index = max(0, index)
        for child in self.body.winfo_children():
            child.destroy()
        getattr(self, f"_page_{self.PAGES[self.index]}")()

        last = self.index == len(self.PAGES) - 1
        self.back_btn.configure(state="disabled" if self.index == 0 else "normal")
        self.next_btn.configure(text="Get started" if last else "Next")
        if last:
            self.skip_btn.pack_forget()
        elif not self.skip_btn.winfo_ismapped():
            self.skip_btn.pack(side="left")

        for child in self.dots.winfo_children():
            child.destroy()
        for i in range(len(self.PAGES)):
            dot = ctk.CTkFrame(self.dots, width=26 if i == self.index else 10, height=10,
                               corner_radius=5,
                               fg_color=self.app.c("accent" if i == self.index else "line"))
            dot.pack(side="left", padx=3)
            dot.configure(cursor="hand2")
            dot.bind("<Button-1>", lambda _e, i=i: self.go(i))

    def close(self):
        # Any way out counts as seen: someone who closes it on page one has
        # decided, and showing it again next launch would be nagging.
        self.app.ui["guide_seen"] = True
        self.app.save_ui()
        self.win.destroy()

    # ---------- building blocks ----------

    def _heading(self, kicker, title, lede):
        app = self.app
        app.label(self.body, kicker, "small", text_color=app.c("accent")).pack(anchor="w")
        app.label(self.body, title, "h1").pack(anchor="w", pady=(2, 4))
        app.label(self.body, lede, "muted", wraplength=WRAP).pack(anchor="w", pady=(0, 20))

    def _panel(self, parent=None, **pack):
        panel = ctk.CTkFrame(parent or self.body, fg_color=self.app.c("card"), corner_radius=14,
                             border_width=1, border_color=self.app.c("line"))
        pack.setdefault("fill", "x")
        pack.setdefault("pady", (0, 14))
        panel.pack(**pack)
        return panel

    def _chip(self, parent, text, role="sel", text_role="fg"):
        return ctk.CTkLabel(parent, text=text, font=self.app.fonts["body"], corner_radius=8,
                            fg_color=self.app.c(role), text_color=self.app.c(text_role),
                            padx=12, pady=6)

    def _points(self, parent, lines):
        for line in lines:
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x", pady=3)
            ctk.CTkFrame(row, width=6, height=6, corner_radius=3,
                         fg_color=self.app.c("accent")).pack(side="left", padx=(2, 12), pady=(9, 0),
                                                              anchor="n")
            self.app.label(row, line, wraplength=WRAP - 40).pack(side="left", fill="x")

    def _try_it(self, parent, text="Try it"):
        self.app.label(parent, text.upper(), "small", text_color=self.app.c("accent")).pack(
            anchor="w", padx=18, pady=(14, 6))

    def _open(self, key, name):
        self.app.button(self.body, f"Go to the {name} page", lambda: self.app.show_page(key),
                        kind="ghost").pack(anchor="w", pady=(4, 0))

    # ---------- pages ----------

    def _page_welcome(self):
        app = self.app
        self._heading("WELCOME", "What ytbatch does",
                      "It turns a long YouTube watchlist into a short list of what's worth "
                      "your time, in three steps. Click a step to jump to it.")

        flow = ctk.CTkFrame(self.body, fg_color="transparent")
        flow.pack(fill="x", pady=(0, 18))
        steps = (("↓", "Fetch", "Pull your playlist and every video's transcript", 1),
                 ("▶", "Screen", "An AI reads each one and gives a verdict", 2),
                 ("☰", "Review", "Record what you watched and rate it", 3))
        for i, (icon, name, line, page) in enumerate(steps):
            flow.grid_columnconfigure(i * 2, weight=1, uniform="step")
            card = ctk.CTkFrame(flow, fg_color=app.c("card"), corner_radius=14,
                                border_width=1, border_color=app.c("line"), cursor="hand2")
            card.grid(row=0, column=i * 2, sticky="nsew")
            parts = (ctk.CTkLabel(card, text=icon, font=app.fonts["stat"], text_color=app.c("accent")),
                     app.label(card, name, "h2", anchor="center"),
                     app.label(card, line, "muted", anchor="center", justify="center", wraplength=180))
            parts[0].pack(pady=(18, 0))
            parts[1].pack()
            parts[2].pack(padx=12, pady=(2, 18))
            for widget in (card, *parts):
                widget.bind("<Button-1>", lambda _e, p=page: self.go(p))
            if i < len(steps) - 1:
                ctk.CTkLabel(flow, text="→", font=app.fonts["h1"],
                             text_color=app.c("muted")).grid(row=0, column=i * 2 + 1, padx=8)

        panel = self._panel()
        inner = ctk.CTkFrame(panel, fg_color="transparent")
        inner.pack(fill="x", padx=18, pady=14)
        self._points(inner, (
            "Everything is saved as it happens, so you can stop at any point and pick up later.",
            "The log at the bottom of the window shows what is running right now.",
            "Use the arrow keys or the buttons below to move through this guide.",
        ))

    def _page_fetch(self):
        from .gui import MODES  # here, not at import: gui imports this module

        app = self.app
        self._heading("STEP 1", "Fetch",
                      "Collect your videos and everything the AI needs to judge them.")
        flow = ctk.CTkFrame(self.body, fg_color="transparent")
        flow.pack(anchor="w", pady=(0, 16))
        for i, text in enumerate(("Your playlist", "watchlist.txt  (video IDs)",
                                  "metadata.json  (details + transcripts)")):
            if i:
                ctk.CTkLabel(flow, text="→", font=app.fonts["h2"],
                             text_color=app.c("muted")).pack(side="left", padx=8)
            self._chip(flow, text).pack(side="left")

        self._points(self.body, (
            "Pick the browser you're logged in to YouTube with, and close it first. "
            "The app reads its cookies, and an open browser locks them.",
            "Click Fetch watchlist, then Run fetch. Videos you already have are skipped, "
            "so running it again only grabs what's new.",
        ))

        panel = self._panel(pady=(16, 8))
        self._try_it(panel, "Transcript routing - click one")
        desc = app.label(panel, "", wraplength=WRAP - 40)
        seg = app.segmented(panel, [name for _, name, _ in MODES],
                            command=lambda v: desc.configure(
                                text=next(d for _, n, d in MODES if n == v)))
        seg.pack(anchor="w", padx=18)
        desc.pack(anchor="w", padx=18, pady=(10, 4))
        app.label(panel, "If YouTube starts refusing transcripts, switch routes or wait - "
                         "a block usually clears within a day.", "small",
                  wraplength=WRAP - 40).pack(anchor="w", padx=18, pady=(0, 14))
        seg.set(MODES[0][1])
        desc.configure(text=MODES[0][2])
        self._open("fetch", "Fetch")

    def _page_screen(self):
        app = self.app
        self._heading("STEP 2", "Screen",
                      "An AI reads each transcript against your screening skill and gives "
                      "one of three verdicts.")
        chips = ctk.CTkFrame(self.body, fg_color="transparent")
        chips.pack(anchor="w", pady=(0, 16))
        for role, name, line in (("watch", "Watch", "worth it in full"),
                                 ("read", "Read", "the summary covers it"),
                                 ("skip", "Skip", "not for you")):
            box = ctk.CTkFrame(chips, fg_color=app.c("card"), corner_radius=12,
                               border_width=1, border_color=app.c("line"))
            box.pack(side="left", padx=(0, 10))
            ctk.CTkLabel(box, text=name, font=app.fonts["h2"], text_color=app.c(role)).pack(
                padx=18, pady=(10, 0))
            app.label(box, line, "small", anchor="center").pack(padx=18, pady=(0, 10))

        panel = self._panel()
        self._try_it(panel, "Two ways to run it - click one")
        texts = {
            "Subscription": "Opens Claude Code (or codex, gemini, opencode) in its own terminal "
                            "window and uses your subscription. Set Videos to 0 and it keeps "
                            "going until your usage limit runs out. Verdicts show up in the app "
                            "as they are written.",
            "API": "Sends the videos to a provider such as NVIDIA, Gemini, Ollama or LM Studio, "
                   "a batch at a time. It stops by itself if a quota runs out, and nothing "
                   "already finished is lost.",
        }
        desc = app.label(panel, texts["Subscription"], wraplength=WRAP - 40)
        seg = app.segmented(panel, list(texts), command=lambda v: desc.configure(text=texts[v]))
        seg.set("Subscription")
        seg.pack(anchor="w", padx=18)
        desc.pack(anchor="w", padx=18, pady=(10, 16))

        self._points(self.body, (
            "Choose between them in Settings, under Screening AI.",
            "Render + open turns the results into a searchable page in your browser.",
        ))
        self._open("screen", "Screen")

    def _page_review(self):
        app = self.app
        self._heading("STEP 3", "Review",
                      "Keep track of what you actually did with each screened video.")

        panel = self._panel()
        self._try_it(panel, "Try it - this is a demo, nothing is saved")
        row = ctk.CTkFrame(panel, fg_color=app.c("bg"), corner_radius=10)
        row.pack(fill="x", padx=18)
        app.label(row, "How transformers actually work", "body").pack(side="left", padx=14, pady=10)
        app.label(row, "Some Channel", "muted").pack(side="left", padx=10)
        ctk.CTkLabel(row, text="watch", font=app.fonts["body"],
                     text_color=app.c("watch")).pack(side="right", padx=14)

        controls = ctk.CTkFrame(panel, fg_color="transparent")
        controls.pack(fill="x", padx=18, pady=12)
        state = {"status": "Watched in full", "rating": 8.0}
        result = app.label(panel, "", "muted")

        def show():
            skipped = state["status"] == "Skipped"
            rating = "no rating" if skipped else f"rated {state['rating']:g}"
            rating_lbl.configure(text="-" if skipped else f"{state['rating']:g}")
            result.configure(text=f"Would save: {state['status']}, {rating}")

        def bump(step):
            state["rating"] = min(10.0, max(1.0, state["rating"] + step))
            show()

        seg = app.segmented(controls, ["Watched in full", "Scrubbed", "Read the summary", "Skipped"],
                            command=lambda v: (state.update(status=v), show()))
        seg.set(state["status"])
        seg.pack(side="left")
        app.label(controls, "Rating").pack(side="left", padx=(18, 8))
        app.button(controls, "−", lambda: bump(-0.5), width=34).pack(side="left")
        rating_lbl = app.label(controls, "", anchor="center", width=44)
        rating_lbl.pack(side="left")
        app.button(controls, "+", lambda: bump(0.5), width=34).pack(side="left")
        result.pack(anchor="w", padx=18, pady=(0, 14))
        show()

        self._points(self.body, (
            "Click a column heading to sort. Ctrl- or Shift-click rows to record several at once.",
            "Double-click a row to open the video. Hide decided leaves only what you haven't "
            "recorded yet.",
        ))
        self._open("review", "Review")

    def _page_settings(self):
        app = self.app
        self._heading("MAKE IT YOURS", "Settings and tips",
                      "Settings holds the file paths, the screening AI, and how the app looks.")
        grid = ctk.CTkFrame(self.body, fg_color="transparent")
        grid.pack(fill="x")
        tips = (("Theme and colours", "Switch light or dark in the sidebar. Edit colours or pick "
                                      "a preset; changes show instantly."),
                ("Font", "Change the font and its size under Appearance."),
                ("Undo it all", "Each settings panel has Reset to defaults, and one button "
                                "resets everything."),
                ("The log", "Hide the log drawer when you don't need it. It opens again when "
                            "something starts."),
                ("Fullscreen", "Press F11."),
                ("This guide", "Open it again any time from the ? at the bottom of the sidebar."))
        for i, (title, text) in enumerate(tips):
            grid.grid_columnconfigure(i % 2, weight=1, uniform="tip")
            card = ctk.CTkFrame(grid, fg_color=app.c("card"), corner_radius=12,
                                border_width=1, border_color=app.c("line"))
            card.grid(row=i // 2, column=i % 2, sticky="nsew", padx=(0, 10) if i % 2 == 0 else 0,
                      pady=(0, 10))
            app.label(card, title, "h2").pack(anchor="w", padx=16, pady=(12, 0))
            app.label(card, text, "muted", wraplength=320).pack(anchor="w", padx=16, pady=(2, 14))
        self._open("settings", "Settings")
