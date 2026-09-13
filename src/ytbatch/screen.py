"""
screen.py

Drives Claude Code over the next N unscreened videos and records what came
back. No model code of its own: the screening prompt lives in the
youtube-video-screener SKILL.md and is read fresh by Claude on every run, so
editing the skill changes the output with nothing here to update.

The subprocess runs with --output-format stream-json so the log pane shows
progress as it happens; -p text mode holds everything back until the end,
which on a ten-video run means ten silent minutes.

Deps: claude on PATH (or claude_bin in config.json).
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import paths, progress

METADATA = paths.data_file("metadata.json")

SKILL_REL = Path(".claude") / "skills" / "youtube-video-screener" / "SKILL.md"

# Firefox first because its split view is the intended reading setup; the rest
# keep this working on a machine that doesn't have it.
BROWSERS = ["firefox", "chromium", "chromium-browser", "google-chrome",
            "brave-browser", "vivaldi", "falkon"]

TERMINALS = [
    ("konsole", ["--workdir", "{cwd}", "-e"]),
    ("gnome-terminal", ["--working-directory={cwd}", "--"]),
    ("alacritty", ["--working-directory", "{cwd}", "-e"]),
    ("kitty", ["--directory", "{cwd}"]),
    ("foot", ["--working-directory={cwd}"]),
    ("xterm", ["-e"]),
]

GRACE = 5  # seconds between terminate() and kill() on a stopped run


# ---------- discovery ----------

def find_skill():
    """Walks up from the working directory. The skill normally lives above the
    repo, shared across sibling projects, so a repo-relative guess misses it."""
    d = paths.home().resolve()
    for candidate in [d, *d.parents]:
        p = candidate / SKILL_REL
        if p.is_file():
            return str(p)
    return ""


def find_claude():
    return shutil.which("claude") or ""


def find_browsers():
    return [b for b in BROWSERS if shutil.which(b)]


def find_terminal():
    for name, args in TERMINALS:
        if shutil.which(name):
            return name, args
    return None, None


def skill_path(cfg):
    return cfg.get("skill_path") or find_skill()


def claude_bin(cfg):
    return cfg.get("claude_bin") or find_claude()


def screening_path(cfg):
    """Defaults to the file the corpus is already in if there is one, so an
    existing screening.md keeps being appended to rather than orphaned."""
    if cfg.get("screening_md"):
        return cfg["screening_md"]
    legacy = paths.home() / "Untracked" / "screening.md"
    if legacy.is_file():
        return str(legacy)
    return paths.data_file("screening.md")


# ---------- corpus ----------

def load_corpus(queued_only=False):
    """[(position, record)] in metadata.json order. Position is what the
    rendered page numbers by; it is written into each block because the order
    itself does not survive the next fetch.

    Only videos currently in the watchlist are numbered. metadata.json is an
    archive now and keeps everything ever fetched, so numbering the archived
    tail would hand out positions that mean nothing - they get "" instead,
    the same convention the screening markdown uses for dropped entries.

    Records written before the archive flag existed have no in_watchlist key;
    those are treated as queued, since that is what their presence meant then.
    """
    with open(METADATA, encoding="utf-8") as f:
        records = json.load(f)
    out, pos = [], 0
    for rec in records:
        if not rec.get("id"):
            continue
        if rec.get("in_watchlist", True):
            pos += 1
            out.append((pos, rec))
        elif not queued_only:
            out.append(("", rec))
    return out


def queued_count(corpus):
    return sum(1 for pos, _ in corpus if pos != "")


def next_unscreened(n, corpus=None):
    """Archived videos are never queued for screening - they are out of the
    watchlist, so spending a model call on them is spending it on something
    already decided against."""
    corpus = corpus if corpus is not None else load_corpus(queued_only=True)
    done = progress.screened_ids()
    out = []
    for pos, rec in corpus:
        if pos == "" or rec["id"] in done:
            continue
        out.append((pos, rec))
        if len(out) >= n:
            break
    return out


# ---------- the run ----------

def build_prompt(batch, skill, out_path):
    """batch is [(position, record)]. Ids and positions are handed over
    explicitly so Claude never has to guess which videos were meant, and the
    id comment is what lets the result be matched back to the corpus."""
    lines = [
        f"Read {skill} in full and follow it exactly. It is the single source "
        "of truth for the format, the fields and the verdict vocabulary; do "
        "not substitute your own structure or abbreviate it.",
        "",
        f"Screen these {len(batch)} videos from data/metadata.json, in this order. "
        "Look each one up by its id field:",
        "",
    ]
    for pos, rec in batch:
        title = (rec.get("title") or "").replace("\n", " ")
        lines.append(f"  {pos}. {rec['id']}  {title}")
    lines += [
        "",
        f"Append one <details> block per video to {out_path}, in the order listed, "
        "leaving everything already in that file untouched.",
        "",
        "Each block must open with two HTML comments before the <summary>, on the "
        "same line as <details>, exactly like this:",
        "",
        "  <details><!-- n: 25 --><!-- id: dQw4w9WgXcQ -->",
        "",
        "using that video's listed number and id. The renderer reads both; a block "
        "missing them cannot be tied back to the corpus.",
        "",
        "Work from the full transcript_text, title, description, chapters and "
        "duration of each record. Do not truncate or sample the transcript.",
        "",
        "Report nothing to me except a one-line confirmation per video as you "
        "finish it.",
    ]
    return "\n".join(lines)


def _render_event(line):
    """One stream-json line -> a log line, or None for the noise. The schema
    carries far more than a progress log needs; anything unrecognised is
    dropped rather than dumped raw."""
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return line.rstrip() or None
    kind = ev.get("type")
    if kind == "assistant":
        out = []
        for block in ev.get("message", {}).get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append(block["text"].strip())
            elif block.get("type") == "tool_use":
                name = block.get("name", "tool")
                inp = block.get("input", {})
                target = inp.get("file_path") or inp.get("path") or inp.get("command") or ""
                out.append(f"  [{name}] {str(target)[:100]}")
        return "\n".join(out) or None
    if kind == "result":
        cost = ev.get("total_cost_usd")
        turns = ev.get("num_turns")
        tail = f"  turns {turns}" if turns else ""
        tail += f"  ${cost:.2f}" if isinstance(cost, (int, float)) else ""
        return f"\nclaude finished ({ev.get('subtype', 'ok')}){tail}"
    if kind == "system" and ev.get("subtype") == "init":
        return f"claude session {ev.get('session_id', '?')}  model {ev.get('model', '?')}"
    return None


def run(n, cfg, stop_event=None):
    """Screens the next n unscreened videos. Prints as it goes - the GUI
    captures stdout, so print() is the progress channel. Returns the number of
    videos actually recorded."""
    binary = claude_bin(cfg)
    if not binary:
        print("claude not found on PATH. Set claude_bin in Settings.")
        return 0
    skill = skill_path(cfg)
    if not skill or not os.path.isfile(skill):
        print(f"Screener skill not found ({skill or 'no path set'}). Set skill_path in Settings.")
        return 0
    out_path = screening_path(cfg)

    corpus = load_corpus(queued_only=True)
    batch = next_unscreened(n, corpus)
    if not batch:
        print("Nothing left to screen.")
        return 0

    print(f"Screening {len(batch)} of {len(corpus)} videos in the watchlist")
    print(f"  skill   {skill}")
    print(f"  output  {out_path}\n")
    for pos, rec in batch:
        print(f"  {pos:>4}  {(rec.get('title') or '')[:70]}")
    print()

    cmd = [binary, "-p", build_prompt(batch, skill, out_path),
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "acceptEdits"]
    # The skill normally sits above the repo so it can be shared between
    # sibling projects, and claude refuses to read outside its working
    # directory without being told. Without this the run completes, reads
    # nothing, and writes nothing.
    skill_dir = os.path.dirname(os.path.abspath(skill))
    if os.path.commonpath([skill_dir, str(paths.home().resolve())]) != str(paths.home().resolve()):
        cmd += ["--add-dir", skill_dir]

    proc = subprocess.Popen(
        cmd, cwd=str(paths.home()), stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
        # Nothing is ever piped in, and without this claude spends three
        # seconds waiting on a stdin that never arrives.
        stdin=subprocess.DEVNULL,
        # New process group: terminating the launcher without it leaves the
        # model call running and still billing.
        start_new_session=True,
    )
    stopped = False
    try:
        for line in proc.stdout:
            if stop_event is not None and stop_event.is_set() and not stopped:
                stopped = True
                print("\nStopping claude...")
                _terminate(proc)
            rendered = _render_event(line)
            if rendered:
                print(rendered)
    finally:
        if proc.poll() is None:
            _terminate(proc)
        proc.wait()

    if proc.returncode not in (0, None) and not stopped:
        print(f"\nclaude exited {proc.returncode}.")

    recorded = record_results(out_path, {rec["id"] for _, rec in batch})
    missing = len(batch) - recorded
    print(f"\nRecorded {recorded} screening(s)."
          + (f" {missing} requested video(s) produced no block." if missing else ""))
    return recorded


def _terminate(proc):
    try:
        os.killpg(os.getpgid(proc.pid), 15)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    deadline = time.monotonic() + GRACE
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


# ---------- reading the result back ----------

def record_results(md_path, wanted_ids=None):
    """Parses the screening file for id/verdict pairs and writes them into
    progress.json. Only ids that actually appear get marked - a run that died
    halfway must not leave videos flagged as screened."""
    from . import render_screening as rs

    if not os.path.isfile(md_path):
        return 0
    text = Path(md_path).read_text(encoding="utf-8")
    count = 0
    for block in rs.BLOCK.findall(text):
        mid = rs.VID.search(block)
        if not mid:
            continue
        vid = mid.group(1)
        if wanted_ids is not None and vid not in wanted_ids:
            continue
        m = rs.SUMMARY.search(block)
        if not m:
            continue
        _, _, verdict, _ = rs.split_summary(m.group(1))
        slug = rs.SLUG.get(verdict)
        if slug is None:
            print(f"  {vid}: unknown verdict {verdict!r} - not recorded.")
            continue
        progress.mark_screened(vid, slug)
        count += 1
    return count


# YouTube titles carry typographic quotes and dashes; the write-ups were typed
# with the ASCII ones. Folding them is the difference between a match and a
# false miss, and folds nothing that could collide two real titles.
_FOLD = str.maketrans({"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
                       "\u2013": "-", "\u2014": "-", "\u2026": "..."})


def _norm(t):
    return " ".join((t or "").translate(_FOLD).split()).casefold()


def backfill_ids(md_path=None, cfg=None):
    """One-off for the blocks written before id comments existed: matches each
    block's title against metadata.json and records the verdict.

    Two passes, both refusing to guess. Exact on normalised whitespace first;
    then unique prefix, because the write-ups routinely shorten a title
    ('... | DE4' dropped, trailing clause cut). A prefix matching two corpus
    entries is skipped rather than resolved - a wrong match here silently
    attributes a screening to the wrong video, which is worse than a gap.
    """
    from . import render_screening as rs

    md_path = md_path or screening_path(cfg or {})
    text = Path(md_path).read_text(encoding="utf-8")
    corpus = load_corpus()
    exact = {}
    for _, rec in corpus:
        exact.setdefault(_norm(rec.get("title")), rec["id"])

    def match(title):
        key = _norm(title)
        if key in exact:
            return exact[key]
        if len(key) < 12:  # too short to be a safe prefix
            return None
        hits = {vid for t, vid in exact.items() if t.startswith(key)}
        return hits.pop() if len(hits) == 1 else None

    hits = misses = 0
    for block in rs.BLOCK.findall(text):
        m = rs.SUMMARY.search(block)
        if not m:
            continue
        title, _, verdict, _ = rs.split_summary(m.group(1))
        vid = match(title)
        slug = rs.SLUG.get(verdict)
        if vid and slug:
            progress.mark_screened(vid, slug)
            hits += 1
        else:
            misses += 1
            print(f"  no match: {title[:70]}")
    print(f"\nBackfilled {hits}, unmatched {misses} "
          f"(unmatched are normally videos since dropped from the corpus).")
    return hits
