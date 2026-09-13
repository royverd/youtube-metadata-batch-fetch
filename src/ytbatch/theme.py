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


def apply(root, mode="light"):
    """Repaints every ttk style for `mode` and returns (ui_font, mono_font).
    Safe to call again on a running app: ttk restyles live, so only raw Text
    widgets need touching up by the caller."""
    P.clear()
    P.update(PALETTES.get(mode, LIGHT))

    available = set(tkfont.families(root))
    ui = _pick(UI_FONTS, available)
    mono = _pick(MONO_FONTS, available)

    # Named fonts are what unstyled widgets (menus, Text, dialogs) pick up, so
    # setting these reaches the parts ttk.Style never touches.
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        tkfont.nametofont(name).configure(family=ui, size=10)
    tkfont.nametofont("TkFixedFont").configure(family=mono, size=10)

    style = ttk.Style(root)
    style.theme_use("clam")
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
                        arrowcolor=P["fg"], arrowsize=12, padding=5)
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
    style.configure("Treeview", background=P["card"], fieldbackground=P["card"],
                    foreground=P["fg"], bordercolor=P["line"], rowheight=26,
                    font=(ui, 10))
    style.configure("Treeview.Heading", background=P["bg"], foreground=P["muted"],
                    font=(ui, 9, "bold"), relief="flat", padding=(8, 7))
    style.map("Treeview.Heading", background=[("active", P["sel"])])
    style.map("Treeview", background=[("selected", P["sel"])],
              foreground=[("selected", P["fg"])])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    return ui, mono


def verdicts():
    return {"watch": P["watch"], "read": P["read"], "skip": P["skip"]}
