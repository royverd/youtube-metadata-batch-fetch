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

Deps: none.
"""

import os
from pathlib import Path

ENV_HOME = "YTBATCH_HOME"


def home():
    return Path(os.environ.get(ENV_HOME) or Path.cwd())


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
