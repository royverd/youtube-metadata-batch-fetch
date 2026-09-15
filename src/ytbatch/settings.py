"""
settings.py

User interface state: window size, fullscreen, theme, the log/tab split, and
the Review table's column widths and sort.

Separate from config.json on purpose. config.json belongs to a corpus - keys,
models, which skill, which screening file - and travels with it. None of that
is true here: how big you like the window and whether you want dark mode are
per-person, per-machine, and should survive pointing the app at a different
corpus. So this lives beside the corpus marker in ~/.config/ytbatch/ instead.

Unknown keys are preserved, so a hand-added entry is not eaten on the next
write, and a corrupt file falls back to defaults rather than refusing to start:
nothing in here is worth losing a session over.

Deps: none.
"""

import json
import os
import tempfile

from . import paths

PATH = paths.HOME_MARKER.parent / "user_settings.json"

THEMES = ("light", "dark")

DEFAULTS = {
    "theme": "light",
    "fullscreen": False,
    "geometry": "",       # "WxH+X+Y", empty means size to the screen
    "page": "fetch",      # sidebar page shown on start
    "log_open": True,     # the log drawer stays open until hidden
    "log_height": 180,
    "font_family": "",    # empty = first of theme.UI_FONTS that is installed
    "font_size": 14,      # body text in pixels; headings and hints scale from it
    "guide_seen": False,  # the guide opens by itself until it has been closed once
    "guide_geometry": "", # empty = sized to the screen; set once resized by hand
    "review_sort": "num",
    "review_desc": False,
    "review_widths": {},
    "colors": {},         # {"light"|"dark": {role: "#rrggbb"}}, only what was changed
    "recent_colors": [],  # newest first, for the picker's swatch row
    "color_presets": {},  # your saved presets: {name: full palette}
}


def load():
    data = dict(DEFAULTS)
    try:
        with open(PATH, encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, json.JSONDecodeError):
        return data
    if isinstance(stored, dict):
        data.update(stored)
    return data


def save(data):
    """Atomic, like every other file the app owns that a half-write would
    corrupt. Cheap insurance - this is written on every close."""
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(PATH.parent), prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
