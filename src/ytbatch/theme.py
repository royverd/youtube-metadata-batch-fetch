"""
theme.py

One place for how the app looks, in light and dark.

ttk has no dark theme and no equivalent of prefers-color-scheme - every
bundled theme is light, and "default" is Motif-era besides. "clam" is the only
one that takes colour and padding instructions seriously, so both palettes are
clam with every surface repainted.

Colours match templates/screening.html's :root and its dark media query, so
moving between the app and the rendered page doesn't feel like two products.
Kept literal rather than parsed out of the CSS: two files that must agree beat
one file that must be lexed.

Deps: none - ttk ships with Python.
"""

import tkinter.font as tkfont
from tkinter import ttk

LIGHT = {
    "bg": "#fbfaf8", "card": "#ffffff", "fg": "#1c1b19", "muted": "#6f6b64",
    "line": "#e4e0d9", "sel": "#e8e3da", "accent": "#26557f",
    "accent_fg": "#ffffff", "accent_hi": "#2f6493", "accent_lo": "#1d4364",
    "hover": "#f3efe8", "disabled": "#b5b0a8",
    "log_bg": "#16151a", "log_fg": "#e9e6e0",
    "watch": "#1c6b3f", "read": "#26557f", "skip": "#8a3826",
}

DARK = {
    "bg": "#141317", "card": "#1c1b21", "fg": "#e9e6e0", "muted": "#9b968d",
    "line": "#2c2a31", "sel": "#32303a", "accent": "#8ab9e6",
    "accent_fg": "#10151c", "accent_hi": "#a3caee", "accent_lo": "#6f9fcb",
    "hover": "#24232a", "disabled": "#5c5960",
    "log_bg": "#0f0e12", "log_fg": "#e9e6e0",
    "watch": "#77cd96", "read": "#8ab9e6", "skip": "#e79680",
}

PALETTES = {"light": LIGHT, "dark": DARK}

# Editor order and wording. The keys are internal; nobody choosing a colour
# should need to know what "sel" means.
ROLES = [
    ("Surfaces", [("bg", "Window background"), ("card", "Fields, buttons, table"),
                  ("line", "Borders and separators"), ("sel", "Selected row"),
                  ("hover", "Button under the mouse")]),
    ("Text", [("fg", "Main text"), ("muted", "Labels and hints"),
              ("disabled", "Greyed-out text")]),
    ("Accent", [("accent", "Accent"), ("accent_fg", "Text on accent"),
                ("accent_hi", "Accent, hovered"), ("accent_lo", "Accent, pressed")]),
    ("Log", [("log_bg", "Log background"), ("log_fg", "Log text")]),
    ("Verdicts", [("watch", "Watch"), ("read", "Read"), ("skip", "Skip")]),
]

# role -> the surface it sits on, for the contrast readout. Surfaces are left
# out: a background has nothing single to be legible against.
CONTRAST_ON = {"fg": "bg", "muted": "bg", "disabled": "bg", "accent_fg": "accent",
               "log_fg": "log_bg", "watch": "card", "read": "card", "skip": "card",
               "accent": "bg"}


def normalize_hex(text):
    """'#abc', 'ABCDEF', ' #aabbcc ' -> '#aabbcc', or None if it isn't a colour."""
    t = (text or "").strip().lstrip("#").lower()
    if len(t) == 3:
        t = "".join(c * 2 for c in t)
    if len(t) != 6 or any(c not in "0123456789abcdef" for c in t):
        return None
    return "#" + t


def hex_to_rgb(h):
    h = normalize_hex(h) or "#000000"
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def rgb_to_hex(r, g, b):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c))) for c in (r, g, b))


def contrast(a, b):
    """WCAG 2 contrast ratio, 1.0-21.0. 4.5 is the floor for body text."""
    def lum(h):
        out = []
        for c in hex_to_rgb(h):
            c /= 255
            out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def readable(color, against, target=4.5):
    """Nearest version of `color` that reaches `target` against `against`,
    keeping its hue. Blends toward black or white, whichever end gains
    contrast. Stepping brightness instead stalls on colours already at full
    brightness (#ff5555 on a dark card), which is how three dark presets
    shipped an unreadable skip colour."""
    end = "#000000" if contrast("#000000", against) > contrast("#ffffff", against) else "#ffffff"
    for step in range(101):
        cand = mix(color, end, step / 100)
        if contrast(cand, against) >= target:
            return cand
    return end

# The live palette. Rebound by apply(); widgets that ttk can't style - Text,
# mainly - read their colours from here.
P = dict(LIGHT)

# First hit wins. Inter for the UI because it was drawn for screens at small
# sizes; Noto Sans is the Fedora default and always present.
UI_FONTS = ("Inter", "Cantarell", "Noto Sans", "DejaVu Sans", "Segoe UI")
MONO_FONTS = ("Hack", "JetBrains Mono", "Noto Sans Mono", "DejaVu Sans Mono", "Consolas")


def _pick(candidates, available):
    for name in candidates:
        if name in available:
            return name
    return candidates[-1]


def mix(a, b, t):
    """t=0 is a, t=1 is b."""
    return rgb_to_hex(*(x + (y - x) * t for x, y in zip(hex_to_rgb(a), hex_to_rgb(b))))


def derive(bg, card, fg, muted, line, accent, green, blue, red):
    """Full palette from the nine colours a scheme actually defines. The
    in-between roles are blends, and anything that must stay legible is
    pushed to readable() rather than trusted - scheme colours are chosen for
    code editors, not for text on a button."""
    dark = contrast("#ffffff", bg) > contrast("#000000", bg)
    shade = "#000000" if dark else "#ffffff"
    accent_fg = "#ffffff" if contrast("#ffffff", accent) >= contrast("#000000", accent) else "#000000"
    # The log stays dark in light themes too, same as the built-in light one.
    log_bg = mix(bg, "#000000", 0.3) if dark else mix(fg, "#000000", 0.35)
    return {
        "bg": bg, "card": card, "fg": readable(fg, bg, 7), "muted": readable(muted, bg),
        "line": line, "sel": mix(card, fg, 0.14), "hover": mix(bg, fg, 0.06),
        "disabled": mix(bg, fg, 0.35),
        "accent": accent, "accent_fg": accent_fg,
        "accent_hi": mix(accent, "#ffffff" if dark else shade, 0.15),
        "accent_lo": mix(accent, "#000000", 0.18),
        "log_bg": log_bg, "log_fg": readable("#e9e6e0" if not dark else fg, log_bg, 7),
        "watch": readable(green, card), "read": readable(blue, card), "skip": readable(red, card),
    }


# name -> (the theme it suits, palette). Applying one to the other theme is
# allowed; the suggestion only decides the default in the editor's list.
# Base colours are the schemes' published values.
PRESETS = {
    "Default light": ("light", dict(LIGHT)),
    "Default dark": ("dark", dict(DARK)),
    "Nord": ("dark", derive("#2e3440", "#3b4252", "#eceff4", "#d8dee9", "#434c5e",
                            "#88c0d0", "#a3be8c", "#81a1c1", "#bf616a")),
    "Dracula": ("dark", derive("#282a36", "#343746", "#f8f8f2", "#6272a4", "#44475a",
                               "#bd93f9", "#50fa7b", "#8be9fd", "#ff5555")),
    "Gruvbox dark": ("dark", derive("#282828", "#32302f", "#ebdbb2", "#a89984", "#504945",
                                    "#fabd2f", "#b8bb26", "#83a598", "#fb4934")),
    "Gruvbox light": ("light", derive("#fbf1c7", "#f9f5d7", "#3c3836", "#7c6f64", "#d5c4a1",
                                      "#b57614", "#79740e", "#076678", "#9d0006")),
    "Solarized dark": ("dark", derive("#002b36", "#073642", "#eee8d5", "#93a1a1", "#0e4b5a",
                                      "#268bd2", "#859900", "#2aa198", "#dc322f")),
    "Solarized light": ("light", derive("#fdf6e3", "#eee8d5", "#073642", "#657b83", "#e0d9c3",
                                        "#268bd2", "#859900", "#2aa198", "#dc322f")),
    "Catppuccin Mocha": ("dark", derive("#1e1e2e", "#313244", "#cdd6f4", "#a6adc8", "#45475a",
                                        "#cba6f7", "#a6e3a1", "#89b4fa", "#f38ba8")),
    "Catppuccin Latte": ("light", derive("#eff1f5", "#e6e9ef", "#4c4f69", "#6c6f85", "#ccd0da",
                                         "#8839ef", "#40a02b", "#1e66f5", "#d20f39")),
    # Discord's are its long-standing client colours (blurple #5865f2 and the
    # #313338 family) - Discord publishes no palette, so these are as used,
    # not as documented.
    "Discord dark": ("dark", derive("#313338", "#2b2d31", "#dbdee1", "#949ba4", "#3f4147",
                                    "#5865f2", "#23a55a", "#00a8fc", "#f23f43")),
    "Discord light": ("light", derive("#ffffff", "#f2f3f5", "#313338", "#5c5e66", "#e3e5e8",
                                      "#5865f2", "#23a55a", "#006ce7", "#f23f43")),
    # OBS values from frontend/data/themes/Yami*.o?t in obs-studio master.
    # Yami Light and Rachni only override part of Yami, so their text colours
    # fall back to Yami's.
    "OBS Yami": ("dark", derive("#1d1f26", "#272a33", "#ffffff", "#969696", "#3c404d",
                                "#284cb8", "#25a231", "#476bd7", "#c01c37")),
    "OBS Yami Light": ("light", derive("#d3d3d3", "#e5e5e5", "#000000", "#505050", "#c1c1c1",
                                       "#8cb5ff", "#25a231", "#7aa4f3", "#c01c37")),
    "OBS Rachni": ("dark", derive("#31363b", "#232629", "#ffffff", "#969696", "#2a2e32",
                                  "#914c67", "#00804f", "#00bcd4", "#f06092")),
    "High contrast dark": ("dark", derive("#000000", "#0d0d0d", "#ffffff", "#d0d0d0", "#5a5a5a",
                                          "#ffd400", "#3cff6e", "#5cc8ff", "#ff5c5c")),
    "High contrast light": ("light", derive("#ffffff", "#ffffff", "#000000", "#303030", "#7a7a7a",
                                            "#0033cc", "#006b1f", "#0033cc", "#b00000")),
}


def resolved(mode, overrides=None):
    """The built-in palette for `mode` with the user's overrides on top.
    Anything malformed or unknown is ignored rather than trusted - a
    hand-edited settings file must not be able to crash the startup."""
    pal = dict(PALETTES.get(mode, LIGHT))
    for role, value in ((overrides or {}).get(mode) or {}).items():
        if role in pal and normalize_hex(value):
            pal[role] = normalize_hex(value)
    return pal


def families(root):
    """Installed font families, sorted, without the '@' vertical-text
    duplicates some platforms list."""
    return sorted({f for f in tkfont.families(root) if not f.startswith("@")}, key=str.casefold)


def apply(root, mode="light", overrides=None, paint_root=True, font_family="", font_size=14):
    """Repaints every ttk style for `mode` and returns (ui_font, mono_font).
    Safe to call again on a running app: ttk restyles live, so only raw Text
    widgets need touching up by the caller.

    overrides is {mode: {role: "#rrggbb"}} from user settings. paint_root is
    off under CustomTkinter, which owns the window background itself."""
    P.clear()
    P.update(resolved(mode, overrides))

    available = set(tkfont.families(root))
    # A chosen family that has since been uninstalled falls back silently;
    # Tk would otherwise substitute something arbitrary.
    ui = font_family if font_family in available else _pick(UI_FONTS, available)
    mono = _pick(MONO_FONTS, available)

    # Named fonts are what unstyled widgets (menus, Text, dialogs) pick up, so
    # setting these reaches the parts ttk.Style never touches. Negative =
    # pixels, matching CustomTkinter's sizing.
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family=ui, size=-font_size)
    tkfont.nametofont("TkFixedFont").configure(family=mono, size=-font_size)

    style = ttk.Style(root)
    style.theme_use("clam")
    if paint_root:
        root.configure(background=P["bg"])

    style.configure(".", background=P["bg"], foreground=P["fg"],
                    fieldbackground=P["card"], bordercolor=P["line"],
                    lightcolor=P["bg"], darkcolor=P["bg"],
                    focuscolor=P["accent"], font=(ui, 10))
    style.configure("TFrame", background=P["bg"])
    style.configure("TLabel", background=P["bg"], foreground=P["fg"])
    style.configure("Muted.TLabel", background=P["bg"], foreground=P["muted"], font=(ui, 9))
    style.configure("Heading.TLabel", background=P["bg"], font=(ui, 12, "bold"))

    style.configure("TLabelframe", background=P["bg"], bordercolor=P["line"],
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=P["bg"], foreground=P["muted"],
                    font=(ui, 9, "bold"))

    style.configure("TNotebook", background=P["bg"], bordercolor=P["line"],
                    tabmargins=(0, 6, 0, 0))
    style.configure("TNotebook.Tab", background=P["bg"], foreground=P["muted"],
                    padding=(18, 9), font=(ui, 10), borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", P["card"])],
              foreground=[("selected", P["fg"])],
              expand=[("selected", (0, 0, 0, 1))])

    style.configure("TButton", background=P["card"], foreground=P["fg"],
                    bordercolor=P["line"], relief="flat", padding=(14, 7), font=(ui, 10))
    style.map("TButton",
              background=[("pressed", P["sel"]), ("active", P["hover"]),
                          ("disabled", P["bg"])],
              foreground=[("disabled", P["disabled"])],
              bordercolor=[("active", P["muted"])])
    style.configure("Accent.TButton", background=P["accent"], foreground=P["accent_fg"],
                    bordercolor=P["accent"])
    style.map("Accent.TButton",
              background=[("pressed", P["accent_lo"]), ("active", P["accent_hi"]),
                          ("disabled", P["line"])],
              foreground=[("disabled", P["disabled"])])

    style.configure("TEntry", fieldbackground=P["card"], foreground=P["fg"],
                    bordercolor=P["line"], padding=6, insertcolor=P["fg"])
    style.map("TEntry", bordercolor=[("focus", P["accent"])])
    for widget in ("TSpinbox", "TCombobox"):
        style.configure(widget, fieldbackground=P["card"], foreground=P["fg"],
                        background=P["card"], bordercolor=P["line"],
                        selectbackground=P["sel"], selectforeground=P["fg"],
                        insertcolor=P["fg"], arrowcolor=P["fg"], arrowsize=12, padding=5)
        # clam maps its own light colours onto readonly/focus/disabled, which
        # outrank configure() - every state has to be overridden explicitly.
        style.map(widget,
                  fieldbackground=[("readonly", P["card"]), ("disabled", P["bg"]),
                                   ("focus", P["card"]), ("", P["card"])],
                  background=[("readonly", P["card"]), ("disabled", P["bg"]),
                              ("", P["card"])],
                  foreground=[("disabled", P["disabled"]), ("readonly", P["fg"]),
                              ("", P["fg"])],
                  selectbackground=[("readonly focus", P["sel"]), ("", P["sel"])],
                  selectforeground=[("", P["fg"])],
                  bordercolor=[("focus", P["accent"]), ("", P["line"])])

    # The dropdown list is a classic Tk Listbox that ttk never styles. The
    # option database covers popdowns created from now on; repaint_popdowns()
    # covers the ones already built before a live theme switch.
    for opt, key in (("background", "card"), ("foreground", "fg"),
                     ("selectBackground", "accent"), ("selectForeground", "accent_fg")):
        root.option_add(f"*TCombobox*Listbox.{opt}", P[key])
    root.option_add("*TCombobox*Listbox.font", (ui, 10))
    repaint_popdowns(root)
    style.configure("TCheckbutton", background=P["bg"], foreground=P["fg"],
                    focuscolor=P["bg"], indicatorcolor=P["card"])
    style.configure("TRadiobutton", background=P["bg"], foreground=P["fg"],
                    focuscolor=P["bg"], indicatorcolor=P["card"])
    for widget in ("TCheckbutton", "TRadiobutton"):
        style.map(widget, indicatorcolor=[("selected", P["accent"])],
                  background=[("active", P["bg"])])

    style.configure("TProgressbar", background=P["accent"], troughcolor=P["line"],
                    bordercolor=P["line"], thickness=6)
    style.configure("TScrollbar", background=P["line"], troughcolor=P["bg"],
                    bordercolor=P["bg"], arrowcolor=P["muted"], relief="flat")
    style.map("TScrollbar", background=[("active", P["muted"])])
    style.configure("TPanedwindow", background=P["bg"])
    style.configure("Sash", sashthickness=8, gripcount=0, background=P["line"])

    # rowheight has to clear the font's line box or rows clip on the descender.
    # Negative sizes are pixels. CustomTkinter sizes its text in pixels, and a
    # point size here rendered the table half again as large as everything
    # around it on a scaled display, truncating half the columns.
    style.configure("Treeview", background=P["card"], fieldbackground=P["card"],
                    foreground=P["fg"], bordercolor=P["card"], borderwidth=0,
                    rowheight=round(font_size * 2.3), font=(ui, -font_size))
    # Headings sit on the card, not the window: a bg-coloured strip across
    # the top of a card reads as a gap in it.
    style.configure("Treeview.Heading", background=P["card"], foreground=P["muted"],
                    font=(ui, -(font_size - 1), "bold"), relief="flat", padding=(8, 9))
    style.map("Treeview.Heading", background=[("active", P["hover"])])
    style.map("Treeview", background=[("selected", P["sel"])],
              foreground=[("selected", P["fg"])])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    return ui, mono


def repaint_popdowns(root):
    """Walks every Combobox and recolours its popdown listbox if Tk has
    already created it. Popdowns are built lazily on first open, so an unopened
    one is skipped here and picks the option database up when it is built."""
    from tkinter import TclError

    def walk(w):
        if w.winfo_class() == "TCombobox":
            try:
                pop = w.tk.call("ttk::combobox::PopdownWindow", str(w))
                w.tk.call(f"{pop}.f.l", "configure",
                          "-background", P["card"], "-foreground", P["fg"],
                          "-selectbackground", P["accent"], "-selectforeground", P["accent_fg"])
            except TclError:
                pass
        for child in w.winfo_children():
            walk(child)

    walk(root)


def verdicts():
    return {"watch": P["watch"], "read": P["read"], "skip": P["skip"]}
