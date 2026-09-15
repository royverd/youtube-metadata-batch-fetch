"""
colorpicker.py

Colour customisation for the two themes: an editor window listing every
colour role with presets, a picker dialog (saturation/brightness square, hue
strip, RGB, hex, swatches), and a desktop eyedropper.

Built from the App's CustomTkinter widget factories so it matches the main
window. The square and hue strip are plain tk.Canvas - CTk has no pixel
surface - and Tk's own colorchooser on Linux is a bare RGB slider box.

The eyedropper goes through the XDG desktop portal (Screenshot.PickColor).
On Wayland nothing else can read a pixel outside the app's own window, and the
portal is what KDE and GNOME both implement. It needs PyGObject, which Fedora
ships with its system Python; without it the button says so instead of
failing.

Deps: customtkinter. PyGObject for the eyedropper.
"""

import colorsys
import queue
import random
import threading
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from . import theme

SQUARE = 220
HUE_H = 16
RECENT_MAX = 12


class PickerUnavailable(RuntimeError):
    pass


def _grab(win):
    """CTkToplevel maps a beat after creation, and grabbing an unmapped
    window raises."""
    try:
        win.grab_set()
    except tk.TclError:
        pass


def _swatch(app, parent, colour, size=22, on_click=None):
    sw = ctk.CTkFrame(parent, width=size, height=size, corner_radius=6, fg_color=colour,
                      border_width=1, border_color=app.c("line"))
    if on_click:
        sw.configure(cursor="hand2")
        sw.bind("<Button-1>", lambda _e: on_click(colour))
    return sw


# ---------- eyedropper ----------

def _portal_pick(timeout=120):
    """Blocks until the user clicks a pixel or cancels. Returns '#rrggbb', or
    None on cancel. Runs its own GLib context: Tk owns the main thread and
    never iterates GLib's default loop, so signals parked there would never
    be delivered."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError) as e:
        raise PickerUnavailable("PyGObject is not available to this Python") from e

    ctx = GLib.MainContext.new()
    ctx.push_thread_default()
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        token = f"ytbatch{random.randrange(1 << 30)}"
        # The request path is predictable, which is what lets the Response
        # subscription exist before the call - subscribing after it races a
        # fast click.
        sender = bus.get_unique_name()[1:].replace(".", "_")
        path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        loop = GLib.MainLoop.new(ctx, False)
        result = {}

        def on_response(_conn, _sender, _path, _iface, _signal, params):
            code, results = params.unpack()
            result["code"], result["color"] = code, results.get("color")
            loop.quit()

        sub = bus.signal_subscribe("org.freedesktop.portal.Desktop",
                                   "org.freedesktop.portal.Request", "Response",
                                   path, None, Gio.DBusSignalFlags.NONE, on_response)
        try:
            bus.call_sync("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                          "org.freedesktop.portal.Screenshot", "PickColor",
                          GLib.Variant("(sa{sv})", ("", {"handle_token": GLib.Variant("s", token)})),
                          GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 10000, None)
        except GLib.Error as e:
            bus.signal_unsubscribe(sub)
            raise PickerUnavailable(f"desktop portal refused: {e.message}") from e

        timer = GLib.timeout_source_new_seconds(timeout)
        timer.set_callback(lambda *_: (loop.quit(), False)[1])
        timer.attach(ctx)
        loop.run()
        timer.destroy()
        bus.signal_unsubscribe(sub)
    finally:
        ctx.pop_thread_default()

    if result.get("code") != 0 or not result.get("color"):
        return None  # 1 = cancelled, 2 = ended some other way, missing = timed out
    return theme.rgb_to_hex(*(c * 255 for c in result["color"]))


def pick_from_screen(widget, on_color):
    """Runs the portal off the Tk thread and hands the colour back on it."""
    q = queue.Queue()

    def work():
        try:
            q.put(("ok", _portal_pick()))
        except Exception as e:  # anything here is "no colour", never a crash
            q.put(("err", str(e)))

    threading.Thread(target=work, daemon=True).start()

    def poll():
        try:
            kind, value = q.get_nowait()
        except queue.Empty:
            widget.after(100, poll)
            return
        if kind == "err":
            messagebox.showerror("Eyedropper", f"Couldn't pick from the screen.\n{value}",
                                 parent=widget.winfo_toplevel())
        elif value:
            on_color(value)

    widget.after(100, poll)


# ---------- picker dialog ----------

class PickerDialog:
    """Modal. on_done(hex) fires on OK only; Cancel leaves everything as it was."""

    def __init__(self, app, parent, initial, on_done, swatches=(), recent=(), against=None):
        self.app = app
        self.on_done = on_done
        self.against = against
        self.initial = theme.normalize_hex(initial) or "#000000"
        r, g, b = (c / 255 for c in theme.hex_to_rgb(self.initial))
        self.h, self.s, self.v = colorsys.rgb_to_hsv(r, g, b)
        self._hue_drawn = None
        self._syncing = False

        win = self.win = ctk.CTkToplevel(parent)
        win.title("Pick a colour")
        win.configure(fg_color=app.c("bg"))
        win.transient(parent.winfo_toplevel())
        win.resizable(False, False)
        body = ctk.CTkFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=18, pady=18)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="n")
        self.sv = tk.Canvas(left, width=SQUARE, height=SQUARE, highlightthickness=0,
                            cursor="crosshair", background=theme.P["bg"])
        self.sv.pack()
        self.sv_img = tk.PhotoImage(width=SQUARE, height=SQUARE)
        self.sv.create_image(0, 0, image=self.sv_img, anchor="nw")
        self.sv_mark = self.sv.create_oval(0, 0, 0, 0, outline="#ffffff", width=2)
        self.hue = tk.Canvas(left, width=SQUARE, height=HUE_H, highlightthickness=0,
                             cursor="sb_h_double_arrow", background=theme.P["bg"])
        self.hue.pack(pady=(10, 0))
        self.hue_img = tk.PhotoImage(width=SQUARE, height=HUE_H)
        row = " ".join(theme.rgb_to_hex(*(c * 255 for c in colorsys.hsv_to_rgb(x / SQUARE, 1, 1)))
                       for x in range(SQUARE))
        self.hue_img.put(" ".join("{" + row + "}" for _ in range(HUE_H)))
        self.hue.create_image(0, 0, image=self.hue_img, anchor="nw")
        self.hue_mark = self.hue.create_rectangle(0, 0, 0, 0, outline="#ffffff", width=2)
        for canvas, handler in ((self.sv, self._on_sv), (self.hue, self._on_hue)):
            canvas.bind("<Button-1>", handler)
            canvas.bind("<B1-Motion>", handler)

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="n", padx=(18, 0))
        preview = ctk.CTkFrame(right, fg_color="transparent")
        preview.pack(anchor="w")
        # Old beside new, so a drag is always judged against where it started.
        self.old_box = _swatch(app, preview, self.initial, size=48, on_click=self.set_hex)
        self.old_box.configure(width=70)
        self.old_box.pack(side="left")
        self.new_box = _swatch(app, preview, self.initial, size=48)
        self.new_box.configure(width=70)
        self.new_box.pack(side="left", padx=(6, 0))
        app.label(right, "Left is the original; click it to go back.", "small").pack(
            anchor="w", pady=(4, 10))

        fields = ctk.CTkFrame(right, fg_color="transparent")
        fields.pack(anchor="w")
        self.hex_var = tk.StringVar()
        app.label(fields, "Hex").grid(row=0, column=0, sticky="w", padx=(0, 10))
        hex_entry = app.entry(fields, self.hex_var, width=100)
        hex_entry.grid(row=0, column=1, sticky="w", pady=3)
        hex_entry.bind("<Return>", lambda _e: self._from_hex())
        hex_entry.bind("<FocusOut>", lambda _e: self._from_hex())
        self.rgb_vars = []
        for i, name in enumerate("RGB", 1):
            var = tk.StringVar()
            app.label(fields, name).grid(row=i, column=0, sticky="w", padx=(0, 10))
            app.entry(fields, var, width=70).grid(row=i, column=1, sticky="w", pady=3)
            var.trace_add("write", lambda *_: self._from_rgb())
            self.rgb_vars.append(var)

        app.button(right, "Eyedropper",
                   lambda: pick_from_screen(self.win, self.set_hex)).pack(anchor="w", pady=(12, 0))
        app.label(right, "then click anywhere on screen", "small").pack(anchor="w")

        self.contrast_lbl = app.label(right, "", "small")
        self.fix_btn = app.button(right, "Make readable", self._fix)
        if against:
            self.contrast_lbl.pack(anchor="w", pady=(12, 0))

        swatch_area = ctk.CTkFrame(body, fg_color="transparent")
        swatch_area.grid(row=1, column=0, columnspan=2, sticky="w", pady=(14, 0))
        for label, colours in (("This theme", swatches), ("Recent", recent)):
            colours = [c for c in dict.fromkeys(colours) if theme.normalize_hex(c)]
            if not colours:
                continue
            line = ctk.CTkFrame(swatch_area, fg_color="transparent")
            line.pack(anchor="w", pady=3)
            app.label(line, label, "small", width=80).pack(side="left")
            for c in colours:
                _swatch(app, line, c, on_click=self.set_hex).pack(side="left", padx=2)

        bar = ctk.CTkFrame(body, fg_color="transparent")
        bar.grid(row=2, column=0, columnspan=2, sticky="e", pady=(16, 0))
        app.button(bar, "Cancel", win.destroy, width=90).pack(side="right")
        app.button(bar, "OK", self._ok, kind="primary", width=90).pack(side="right", padx=(0, 8))
        win.bind("<Escape>", lambda _e: win.destroy())

        self._sync()
        win.after(150, lambda: _grab(win))

    def current(self):
        return theme.rgb_to_hex(*(c * 255 for c in colorsys.hsv_to_rgb(self.h, self.s, self.v)))

    def set_hex(self, value):
        value = theme.normalize_hex(value)
        if not value:
            return
        r, g, b = (c / 255 for c in theme.hex_to_rgb(value))
        h, self.s, self.v = colorsys.rgb_to_hsv(r, g, b)
        if self.s > 0:  # a grey has no hue; keep the strip where it was
            self.h = h
        self._sync()

    def _draw_square(self):
        if self._hue_drawn == self.h:
            return
        n = SQUARE - 1
        rows = []
        for y in range(SQUARE):
            v = 1 - y / n
            rows.append("{" + " ".join(
                theme.rgb_to_hex(*(c * 255 for c in colorsys.hsv_to_rgb(self.h, x / n, v)))
                for x in range(SQUARE)) + "}")
        self.sv_img.put(" ".join(rows))
        self._hue_drawn = self.h

    def _sync(self):
        """One place pushes state out to every control; the trace on the RGB
        fields would otherwise feed each update straight back in."""
        self._syncing = True
        try:
            self._draw_square()
            colour = self.current()
            x, y = self.s * (SQUARE - 1), (1 - self.v) * (SQUARE - 1)
            self.sv.coords(self.sv_mark, x - 6, y - 6, x + 6, y + 6)
            # White ring vanishes on pale colours, so it flips with brightness.
            self.sv.itemconfigure(self.sv_mark,
                                  outline="#000000" if self.v > 0.6 and self.s < 0.4 else "#ffffff")
            hx = self.h * (SQUARE - 1)
            self.hue.coords(self.hue_mark, hx - 3, 1, hx + 3, HUE_H - 1)
            self.new_box.configure(fg_color=colour)
            self.hex_var.set(colour)
            for var, c in zip(self.rgb_vars, theme.hex_to_rgb(colour)):
                var.set(str(c))
            if self.against:
                ratio = theme.contrast(colour, self.against)
                ok = ratio >= 4.5
                self.contrast_lbl.configure(
                    text=f"Contrast {ratio:.1f}:1 - " + ("readable" if ok else "hard to read"))
                if ok:
                    self.fix_btn.pack_forget()
                else:
                    self.fix_btn.pack(anchor="w", pady=(6, 0))
        finally:
            self._syncing = False

    def _on_sv(self, e):
        n = SQUARE - 1
        self.s = min(max(e.x / n, 0), 1)
        self.v = 1 - min(max(e.y / n, 0), 1)
        self._sync()

    def _on_hue(self, e):
        self.h = min(max(e.x / (SQUARE - 1), 0), 0.999)
        self._sync()

    def _from_hex(self):
        if self._syncing:
            return
        value = theme.normalize_hex(self.hex_var.get())
        if value and value != self.current():
            self.set_hex(value)

    def _from_rgb(self):
        if self._syncing:
            return
        try:
            rgb = [int(v.get()) for v in self.rgb_vars]
        except ValueError:
            return  # mid-typing; wait for a number
        if all(0 <= c <= 255 for c in rgb):
            self.set_hex(theme.rgb_to_hex(*rgb))

    def _fix(self):
        self.set_hex(theme.readable(self.current(), self.against))

    def _ok(self):
        colour = self.current()
        self.win.destroy()
        self.on_done(colour)


# ---------- editor window ----------

class ColorEditor:
    """Lists every role for one theme at a time. Every change lands at once:
    written to user settings and repainted into the main window.

    app is the gui.App: its widget factories, ui (the settings dict), recolor
    and save_ui. Only overrides are stored, so a colour reset to default
    follows any later change to the built-in palette instead of freezing a
    copy."""

    def __init__(self, app):
        existing = getattr(app, "_color_editor", None)
        if existing is not None and existing.win.winfo_exists():
            existing.win.lift()
            existing.win.focus_force()
            return
        app._color_editor = self
        self.app = app
        self.mode = tk.StringVar(value=app.ui.get("theme", "light"))
        self.preset = tk.StringVar()
        self.body = None
        win = self.win = ctk.CTkToplevel(app.root)
        win.title("Colours")
        win.geometry("980x780")
        win.minsize(820, 520)
        self._build()
        win.after(100, win.lift)

    # -- state --

    def overrides(self, mode=None):
        colors = self.app.ui.setdefault("colors", {})
        return colors.setdefault(mode or self.mode.get(), {})

    def value(self, role, mode=None):
        mode = mode or self.mode.get()
        return self.overrides(mode).get(role) or theme.PALETTES[mode][role]

    def palette(self):
        return {role: self.value(role) for role in theme.PALETTES[self.mode.get()]}

    def set(self, role, colour):
        mode = self.mode.get()
        if colour is None or colour == theme.PALETTES[mode][role]:
            self.overrides(mode).pop(role, None)
        else:
            self.overrides(mode)[role] = colour
            recent = [c for c in self.app.ui.get("recent_colors", []) if c != colour]
            self.app.ui["recent_colors"] = ([colour] + recent)[:RECENT_MAX]
        self._commit()

    def _commit(self):
        # Both themes' colours are baked into the main window's widgets, so
        # an edit to the theme not on screen still needs the rebuild.
        self.app.save_ui()
        self.app.recolor()
        self._build()

    # -- presets --

    def user_presets(self):
        return self.app.ui.setdefault("color_presets", {})

    def all_presets(self):
        """One alphabetical list, yours mixed in with the built-ins - with a
        few dozen entries, a name is faster to find than a group."""
        out = {name: pal for name, (_m, pal) in theme.PRESETS.items()}
        for name, pal in self.user_presets().items():
            out[f"{name} (yours)"] = pal
        return dict(sorted(out.items(), key=lambda kv: kv[0].casefold()))

    def _show_strip(self):
        """A row of the preset's main colours, so choosing doesn't mean
        applying each one to see it."""
        for child in self.preset_strip.winfo_children():
            child.destroy()
        pal = self.all_presets().get(self.preset.get(), {})
        for role in ("bg", "card", "fg", "accent", "watch", "read", "skip"):
            if role in pal:
                _swatch(self.app, self.preset_strip, pal[role], size=20).pack(side="left", padx=1)
        self.delete_btn.configure(
            state="normal" if self.preset.get().endswith(" (yours)") else "disabled")

    def _apply_preset(self):
        pal = self.all_presets().get(self.preset.get())
        if not pal:
            return
        mode = self.mode.get()
        base = theme.PALETTES[mode]
        # Stored as overrides like any hand edit, so Reset still means default.
        self.app.ui.setdefault("colors", {})[mode] = {
            role: c for role, c in pal.items()
            if role in base and theme.normalize_hex(c) and theme.normalize_hex(c) != base[role]}
        self._commit()

    def _save_preset(self):
        dialog = ctk.CTkInputDialog(title="Save preset", text="Name for this preset:")
        name = (dialog.get_input() or "").strip()
        if not name:
            return
        if name in self.user_presets() and not messagebox.askokcancel(
                "Save preset", f"Replace your preset \"{name}\"?", parent=self.win):
            return
        self.user_presets()[name] = self.palette()
        self.preset.set(f"{name} (yours)")
        self.app.save_ui()
        self._build()

    def _delete_preset(self):
        label = self.preset.get()
        name = label.removesuffix(" (yours)")
        if not label.endswith(" (yours)") or name not in self.user_presets():
            return
        if not messagebox.askokcancel("Delete preset", f"Delete \"{name}\"?", parent=self.win):
            return
        del self.user_presets()[name]
        self.preset.set("")
        self.app.save_ui()
        self._build()

    # -- layout --

    def _build(self):
        """Whole window from scratch: its own colours come from the palette
        being edited, same as the main window's."""
        app = self.app
        self.win.configure(fg_color=app.c("bg"))
        if self.body is not None:
            self.body.destroy()
        body = self.body = ctk.CTkFrame(self.win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=22, pady=18)

        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x")
        app.label(top, "Colours", "h1").pack(side="left")
        seg = app.segmented(top, ["Light", "Dark"],
                            command=lambda v: (self.mode.set(v.lower()), self._build()))
        seg.set(self.mode.get().capitalize())
        seg.pack(side="left", padx=20)
        app.label(top, "Changes apply and save immediately.", "small").pack(side="right")

        presets = ctk.CTkFrame(body, fg_color=app.c("card"), corner_radius=14,
                               border_width=1, border_color=app.c("line"))
        presets.pack(fill="x", pady=(14, 12))
        row = ctk.CTkFrame(presets, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=12)
        app.label(row, "Preset").pack(side="left")
        names = list(self.all_presets())
        if self.preset.get() not in names:
            self.preset.set(names[0])
        app.option(row, self.preset, names, command=lambda _v: self._show_strip(),
                   width=240).pack(side="left", padx=10)
        self.preset_strip = ctk.CTkFrame(row, fg_color="transparent")
        self.preset_strip.pack(side="left", padx=(0, 10))
        app.button(row, "Apply", self._apply_preset, kind="primary", width=80).pack(side="left")
        app.button(row, "Save as custom preset...", self._save_preset).pack(side="left", padx=6)
        self.delete_btn = app.button(row, "Delete", self._delete_preset, width=80)
        self.delete_btn.pack(side="left")
        self._show_strip()

        grid = ctk.CTkScrollableFrame(body, fg_color=app.c("card"), corner_radius=14,
                                      border_width=1, border_color=app.c("line"),
                                      scrollbar_button_color=app.c("line"),
                                      scrollbar_button_hover_color=app.c("muted"))
        grid.pack(fill="both", expand=True)
        pal = self.palette()
        r = 0
        for group, roles in theme.ROLES:
            app.label(grid, group, "h2").grid(row=r, column=0, columnspan=7, sticky="w",
                                              padx=14, pady=(14 if r else 10, 4))
            r += 1
            for role, label in roles:
                self._role_row(grid, r, role, label, pal)
                r += 1

        bar = ctk.CTkFrame(body, fg_color="transparent")
        bar.pack(fill="x", pady=(12, 0))
        app.button(bar, "Reset this theme", self._reset_all).pack(side="left")
        app.button(bar, "Close", self.win.destroy, width=90).pack(side="right")

    def _role_row(self, g, row, role, label, pal):
        app = self.app
        colour = pal[role]
        changed = role in self.overrides()
        app.label(g, label + ("  *" if changed else "")).grid(
            row=row, column=0, sticky="w", padx=(22, 14), pady=3)

        sw = _swatch(app, g, colour, size=30, on_click=lambda _c: self._open_picker(role))
        sw.configure(width=52)
        sw.grid(row=row, column=1, sticky="w", pady=3)

        var = tk.StringVar(value=colour)
        entry = app.entry(g, var, width=100, height=32)
        entry.grid(row=row, column=2, sticky="w", padx=(10, 0), pady=3)

        def typed(_e=None):
            value = theme.normalize_hex(var.get())
            if value is None:
                var.set(colour)  # bad input snaps back rather than lingering
            elif value != colour:
                self.set(role, value)
        entry.bind("<Return>", typed)
        entry.bind("<FocusOut>", typed)

        app.button(g, "Pick", lambda: self._open_picker(role), width=64, height=32).grid(
            row=row, column=3, padx=(10, 0), pady=3)
        app.button(g, "Eyedropper",
                   lambda: pick_from_screen(self.win, lambda c: self.set(role, c)),
                   width=100, height=32).grid(row=row, column=4, padx=(6, 0), pady=3)
        reset = app.button(g, "Reset", lambda: self.set(role, None), kind="ghost",
                           width=64, height=32)
        reset.grid(row=row, column=5, padx=(6, 0), pady=3)
        if not changed:
            reset.configure(state="disabled")

        base = theme.CONTRAST_ON.get(role)
        if base:
            ratio = theme.contrast(colour, pal[base])
            text = f"{ratio:.1f}:1" + ("" if ratio >= 4.5 else "  hard to read")
            app.label(g, text, "small").grid(row=row, column=6, sticky="w", padx=(14, 10))

    def _open_picker(self, role):
        pal = self.palette()
        base = theme.CONTRAST_ON.get(role)
        PickerDialog(self.app, self.win, pal[role], lambda c: self.set(role, c),
                     swatches=list(pal.values()),
                     recent=self.app.ui.get("recent_colors", []),
                     against=pal[base] if base else None)

    def _reset_all(self):
        mode = self.mode.get()
        if not self.overrides(mode):
            return
        if not messagebox.askokcancel("Colours", f"Reset every {mode} colour to the default?",
                                      parent=self.win):
            return
        self.app.ui["colors"][mode] = {}
        self._commit()
