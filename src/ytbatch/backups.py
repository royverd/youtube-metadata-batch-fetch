"""
backups.py

Read-only copies of the files that can't be rebuilt: metadata.json (the
corpus, transcripts included), progress.json (every verdict, decision and
rating) and the screening markdown (the AI write-ups - expensive in tokens).

Every time one of them changes, a copy goes to data/backups/ named
<name>.<YYYYMMDD-HHMMSS><ext> and is made read-only. A copy is only taken
when the content differs from the newest existing copy, so repeated saves of
the same data cost nothing. Copies are never modified or deleted by the
program except through clear_old(), which the user triggers by hand and which
always keeps the newest copy of each file, so there is always one read-only
copy identical to the current file.

Deps: none.
"""

import hashlib
import os
import re
import shutil
import stat
from datetime import datetime
from pathlib import Path

from . import paths

DIR = paths.data_dir() / "backups"
DEFAULT_THRESHOLD = 15


def tracked(cfg=None):
    """(label, path) for every file that gets copies. screening.md's location
    comes from config, so it is resolved at call time."""
    from . import screen  # here, not at import: screen -> progress -> backups

    return [("metadata.json", Path(paths.data_file("metadata.json"))),
            ("progress.json", Path(paths.data_file("progress.json"))),
            ("screening.md", Path(screen.screening_path(cfg or {})))]


def _pattern(path):
    path = Path(path)
    return re.compile(rf"^{re.escape(path.stem)}\.\d{{8}}-\d{{6}}(?:-\d+)?{re.escape(path.suffix)}$")


def copies(path):
    """This file's copies, oldest first. Timestamps sort lexically."""
    if not DIR.is_dir():
        return []
    rx = _pattern(path)
    return sorted(p for p in DIR.iterdir() if rx.match(p.name))


def _digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.digest()


def snapshot(path):
    """Copies path into DIR, read-only, if its content differs from the
    newest copy. Returns the new copy's path, or None when nothing was
    needed. Never raises for a missing source - there is nothing to keep."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        return None
    existing = copies(path)
    if existing and _digest(existing[-1]) == _digest(path):
        return None
    DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = DIR / f"{path.stem}.{stamp}{path.suffix}"
    n = 1
    while target.exists():  # two changes inside one second
        target = DIR / f"{path.stem}.{stamp}-{n}{path.suffix}"
        n += 1
    tmp = DIR / f".{target.name}.tmp"
    shutil.copy2(path, tmp)
    os.chmod(tmp, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    os.replace(tmp, target)
    return target


def snapshot_all(cfg=None):
    """Snapshots every tracked file; returns the copies made."""
    made = []
    for _label, path in tracked(cfg):
        try:
            copy = snapshot(path)
        except OSError as e:
            print(f"  backup of {path.name} failed: {e}")
            continue
        if copy:
            made.append(copy)
    return made


def status(cfg=None):
    """Per tracked file: label, source, copy count, total bytes, newest copy time."""
    out = []
    for label, path in tracked(cfg):
        found = copies(path)
        newest = datetime.fromtimestamp(found[-1].stat().st_mtime) if found else None
        out.append({"label": label, "path": path, "count": len(found),
                    "bytes": sum(p.stat().st_size for p in found), "newest": newest})
    return out


def clear_old(cfg=None):
    """Deletes every copy except the newest of each file. Returns (removed,
    bytes freed). The only code path that deletes a copy, and only ever run
    from the user's button."""
    removed = freed = 0
    for _label, path in tracked(cfg):
        for old in copies(path)[:-1]:
            size = old.stat().st_size
            # Read-only files can't be unlinked on Windows; harmless elsewhere.
            os.chmod(old, stat.S_IRUSR | stat.S_IWUSR)
            old.unlink()
            removed += 1
            freed += size
    return removed, freed
