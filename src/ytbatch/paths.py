"""
paths.py

Every file the tool reads or writes, resolved in one place.

Before the package layout there was no question to answer: each module pinned
its own directory off __file__ and everything landed beside the script. Once
the code can be pip-installed, __file__ points at wherever pip put it, which is
nowhere anyone wants their transcripts written.

So the anchor is the working directory instead. Run from the repo root and you
get repo-root/data and repo-root/prompts, exactly where the flat layout put
them. Set YTBATCH_HOME to point a run at a different corpus without moving the
code - two watchlists, two databases, one install.

Cwd is still the wrong anchor for a GUI, which gets launched from a menu or a
desktop file and inherits whatever directory that happened to have. So there
is a marker file too - one line, the corpus path, written by the app's
Settings tab. Precedence: YTBATCH_HOME, then the marker, then cwd.

What there is NOT is a search. An earlier version walked up looking for the
nearest directory containing a data/, which is wrong whenever more than one
exists: this repo alone carries a legacy src/data/ and a stale Untracked/data/
beside the real one, and the walk cheerfully fetched 621 videos into the wrong
one. Guessing which corpus was meant is worse than being told, so the anchor
is always explicit - env, marker, or where you stand.

Deps: none.
"""

import os
from pathlib import Path

ENV_HOME = "YTBATCH_HOME"

# Not in data/ on purpose - it is what tells us where data/ is.
HOME_MARKER = Path.home() / ".config" / "ytbatch" / "home"


def home():
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env)
    marked = read_marker()
    if marked:
        return Path(marked)
    return Path.cwd()


def data_dir():
    """Created on demand - a fresh clone has no data/ until something writes
    into it. Callers hold the result as a module constant, so the directory is
    fixed at import time; changing YTBATCH_HOME mid-process does nothing."""
    d = home() / "data"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Callers resolve this at import time, so the alternative is a traceback
        # out of pathlib before main() ever runs - useless for a typo'd env var.
        raise SystemExit(f"Can't create {d}: {e.strerror}. "
                         f"Check {ENV_HOME}, or run from a writable directory.")
    return d


def data_file(name):
    return str(data_dir() / name)


def prompt_file(name):
    """Not created on demand: a missing prompt is a real error worth reporting,
    not something to paper over with an empty file."""
    return str(home() / "prompts" / name)


def read_marker():
    """The path the app was told to use, or "" if there is none. A marker
    pointing somewhere that no longer exists is ignored rather than obeyed -
    the fallbacks below it are always better than a guaranteed ENOENT."""
    try:
        value = HOME_MARKER.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return value if value and Path(value).is_dir() else ""


def write_marker(path):
    """Persists the corpus location across launches. Takes effect on restart:
    every module resolves its paths once, at import."""
    HOME_MARKER.parent.mkdir(parents=True, exist_ok=True)
    HOME_MARKER.write_text(str(path).strip(), encoding="utf-8")
