"""
screen.py

Screens unscreened videos against the youtube-video-screener SKILL.md and
records what came back. No model code of its own: the prompt lives in the
skill, so editing it changes the output with nothing here to update.

Two ways to run a screen, picked by screen_mode in config.json:

  agent CLI  (claude, codex, gemini, opencode) - an interactive session opens
             in a real terminal, working in Untracked/. You can watch it,
             steer it, or let it run into the subscription's usage limit.
  api        one of config.PROVIDERS, called directly; the app appends the
             returned markdown itself. Stops at the first fatal provider
             error, which is what a spent quota looks like.

Either way the batch is cut out of metadata.json into small per-batch files
first. metadata.json is ~20 MB; making the model open it for every batch is
where the tokens and the wall-clock went.

Deps: the chosen CLI on PATH (or its path in Settings), or an API key.
"""

import json
import os
import shutil
from pathlib import Path

from . import paths, progress

METADATA = paths.data_file("metadata.json")

SKILL_REL = Path(".claude") / "skills" / "youtube-video-screener" / "SKILL.md"

# Firefox first because its split view is the intended reading setup; the rest
# keep this working on a machine that doesn't have it.
BROWSERS = ["firefox", "chromium", "chromium-browser", "google-chrome",
            "brave-browser", "vivaldi", "falkon"]

# Launchers that block until the window closes. That lifetime is the only
# signal the GUI gets that a session ended, so konsole needs --separate (it
# otherwise hands off to a running instance and exits at once) and
# gnome-terminal needs --wait for the same reason.
TERMINALS = [
    ("konsole", ["--separate", "--workdir", "{cwd}", "-e"]),
    ("gnome-terminal", ["--wait", "--working-directory={cwd}", "--"]),
    ("alacritty", ["--working-directory", "{cwd}", "-e"]),
    ("kitty", ["--directory", "{cwd}"]),
    ("foot", ["--working-directory={cwd}"]),
    ("xterm", ["-e"]),
]

DEFAULT_BATCH = 10

# Only what the screening reads. Segments, tags and counters are most of the
# file's bulk and none of the judgment.
BATCH_FIELDS = ("id", "title", "channel", "upload_date", "duration_string",
                "description", "chapters", "transcript_text", "transcript_status")


def _claude_argv(binary, prompt, model, effort, add_dirs):
    # Prompt goes before --add-dir: that flag is variadic and would swallow it.
    argv = [binary, prompt, "--permission-mode", "acceptEdits"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    for d in add_dirs:
        argv += ["--add-dir", d]
    return argv


def _codex_argv(binary, prompt, model, effort, add_dirs):
    argv = [binary, "--sandbox", "workspace-write"]
    if model:
        argv += ["-m", model]
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    for d in add_dirs:
        argv += ["--add-dir", d]
    return argv + [prompt]


def _gemini_argv(binary, prompt, model, effort, add_dirs):
    # Gemini CLI has no effort flag; the setting is ignored rather than faked.
    argv = [binary, "--approval-mode", "auto_edit"]
    if model:
        argv += ["-m", model]
    for d in add_dirs:
        argv += ["--include-directories", d]
    return argv + ["-i", prompt]


def _opencode_argv(binary, prompt, model, effort, add_dirs):
    # No extra-directory flag; an output file outside Untracked/ will prompt.
    argv = [binary, "--prompt", prompt]
    if model:
        argv += ["--model", model]  # provider/model, e.g. ollama/qwen3
    if effort:
        argv += ["--variant", effort]
    return argv


# name -> (binary on PATH, argv builder, effort choices, model suggestions).
# Blank model/effort means "whatever the CLI defaults to". Model lists are
# suggestions only - the field is free text, and these CLIs rename models
# faster than this file changes.
AGENTS = {
    "claude": ("claude", _claude_argv, ["low", "medium", "high", "xhigh", "max"],
               ["opus", "sonnet", "haiku", "claude-opus-5", "claude-sonnet-5"]),
    "codex": ("codex", _codex_argv, ["minimal", "low", "medium", "high"], []),
    "gemini": ("gemini", _gemini_argv, [], ["gemini-2.5-pro", "gemini-2.5-flash"]),
    "opencode": ("opencode", _opencode_argv, [], []),
}
API = "api"
BACKENDS = list(AGENTS) + [API]


class ScreenError(RuntimeError):
    """A run that cannot start; the message is meant for the user."""


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
    return agent_bin(cfg, "claude")


def backend(cfg):
    if cfg.get("screen_mode") == "api":
        return API
    return cfg.get("screen_agent") if cfg.get("screen_agent") in AGENTS else "claude"


def agent_bin(cfg, name):
    """claude_bin predates the backend choice and is still honoured."""
    override = (cfg.get("screen_bins") or {}).get(name, "")
    if not override and name == "claude":
        override = cfg.get("claude_bin", "")
    return override or shutil.which(AGENTS[name][0]) or ""


def batch_size(cfg):
    try:
        return max(1, int(cfg.get("screen_batch") or DEFAULT_BATCH))
    except (TypeError, ValueError):
        return DEFAULT_BATCH


def work_dir():
    d = paths.home() / "Untracked"
    d.mkdir(exist_ok=True)
    return d


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
    """n of 0 means everything left. Archived videos are never queued - they
    are out of the watchlist, so a model call on them is spent on something
    already decided against."""
    corpus = corpus if corpus is not None else load_corpus(queued_only=True)
    done = progress.screened_ids()
    out = []
    for pos, rec in corpus:
        if pos == "" or rec["id"] in done:
            continue
        out.append((pos, rec))
        if n and len(out) >= n:
            break
    return out


def write_batches(batch, size):
    """Cuts the batch into Untracked/screen_batches/batch_NNN.json. Old files
    are cleared first: a leftover batch_014 from a longer previous run would
    otherwise be picked up as part of this one."""
    d = work_dir() / "screen_batches"
    d.mkdir(exist_ok=True)
    for old in d.glob("batch_*.json"):
        old.unlink()
    files = []
    for i in range(0, len(batch), size):
        chunk = [dict({"n": pos}, **{k: rec.get(k) for k in BATCH_FIELDS})
                 for pos, rec in batch[i:i + size]]
        p = d / f"batch_{i // size + 1:03d}.json"
        p.write_text(json.dumps(chunk, ensure_ascii=False, indent=1), encoding="utf-8")
        files.append(p)
    return files


def _stage_skill(skill):
    """A copy inside the working directory, so no CLI needs permission to read
    above it. Taken at launch, which is when the original would be read anyway."""
    dst = work_dir() / "screen_batches" / "SKILL.md"
    shutil.copyfile(skill, dst)
    return dst


# ---------- prompts ----------

BLOCK_RULES = [
    "Each video gets one <details> block. It must open with two HTML comments "
    "before the <summary>, on the same line as <details>, exactly like this:",
    "",
    "  <details><!-- n: 25 --><!-- id: dQw4w9WgXcQ -->",
    "",
    "using that video's n and id fields. The renderer reads both; a block "
    "missing them cannot be tied back to the corpus.",
    "",
    "Work from the full transcript_text, title, description, chapters and "
    "duration_string of each record. Do not truncate or sample the transcript.",
]


def build_agent_prompt(files, total, skill, out_path, cwd):
    rel = lambda p: os.path.relpath(p, cwd)
    lines = [
        f"Read {rel(skill)} in full and follow it exactly. It is the single source "
        "of truth for the format, the fields and the verdict vocabulary; do not "
        "substitute your own structure or abbreviate it.",
        "",
        f"There are {total} videos to screen, split across {len(files)} batch file(s). "
        "Each file is a JSON list of records, already in screening order:",
        "",
        *[f"  {rel(p)}" for p in files],
        "",
        "Work through the files in that order. For each file: read it once, screen "
        f"every video in it, then append its blocks to {rel(out_path)} before "
        "opening the next file. Never re-read a finished file, and leave everything "
        "already in the output untouched.",
        "",
        *BLOCK_RULES,
        "",
        "Keep going without asking me between batches. After each file, report "
        "only one line: which file is done and how many videos it held.",
    ]
    return "\n".join(lines)


def build_api_prompt(skill_text):
    return "\n".join([
        skill_text,
        "",
        "The user message is a JSON list of video records. Reply with the "
        "markdown blocks for them, one per record in the order given, and "
        "nothing else - no preamble, no code fence.",
        "",
        *BLOCK_RULES,
    ])


# ---------- the run ----------

def _prepare(n, cfg):
    skill = skill_path(cfg)
    if not skill or not os.path.isfile(skill):
        raise ScreenError(f"Screener skill not found ({skill or 'no path set'}). "
                          "Set skill_path in Settings.")
    corpus = load_corpus(queued_only=True)
    batch = next_unscreened(n, corpus)
    if not batch:
        raise ScreenError("Nothing left to screen.")
    return skill, screening_path(cfg), corpus, batch


def launch_agent(n, cfg):
    """Opens the configured agent CLI in a terminal, working in Untracked/.
    Returns (process, wanted ids, output path, summary line); the caller owns
    watching the process and recording results."""
    import subprocess

    name = backend(cfg)
    binary = agent_bin(cfg, name)
    if not binary:
        raise ScreenError(f"{name} not found on PATH. Set its path in Settings.")
    term, targs = find_terminal()
    if not term:
        raise ScreenError("No supported terminal emulator found. Tried: "
                          + ", ".join(t for t, _ in TERMINALS))

    skill, out_path, corpus, batch = _prepare(n, cfg)
    cwd = work_dir()
    files = write_batches(batch, batch_size(cfg))
    staged = _stage_skill(skill)
    Path(out_path).touch()

    out_dir = os.path.dirname(os.path.abspath(out_path))
    add_dirs = [] if Path(out_dir).resolve() == cwd.resolve() else [out_dir]
    prompt = build_agent_prompt(files, len(batch), staged, out_path, cwd)
    argv = AGENTS[name][1](binary, prompt, cfg.get("screen_agent_model", "").strip(),
                           cfg.get("screen_agent_effort", "").strip(), add_dirs)
    cmd = [term] + [a.format(cwd=cwd) for a in targs] + argv
    proc = subprocess.Popen(cmd, cwd=str(cwd), start_new_session=True)
    summary = (f"{name} screening {len(batch)} of {len(corpus)} videos in "
               f"{len(files)} batch(es), in {cwd}")
    return proc, {rec["id"] for _, rec in batch}, out_path, summary


def run_api(n, cfg, stop_event=None):
    """Screens through a config.PROVIDERS backend, one request per batch.
    Prints as it goes - the GUI captures stdout. Each batch is appended and
    recorded before the next is sent, so a quota running out mid-backlog
    loses nothing already paid for."""
    from . import config, providers

    try:
        skill, out_path, corpus, batch = _prepare(n, cfg)
    except ScreenError as e:
        print(e)
        return 0
    pcfg = config.runtime(cfg, cfg.get("screen_provider") or cfg.get("provider"),
                          cfg.get("screen_api_model", "").strip(),
                          cfg.get("screen_api_effort", "").strip())
    local = config.PROVIDERS[pcfg["provider"]].get("local")
    if local and not pcfg["model"]:
        print(f"No model picked for {pcfg['provider']}. Choose one in Settings "
              "(the list fills from the running server).")
        return 0
    if not pcfg["api_key"] and not local:
        print(f"No API key for {pcfg['provider']}. Add one in Settings.")
        return 0

    size = batch_size(cfg)
    system = build_api_prompt(Path(skill).read_text(encoding="utf-8"))
    chunks = [batch[i:i + size] for i in range(0, len(batch), size)]
    print(f"Screening {len(batch)} of {len(corpus)} videos via {pcfg['provider']} "
          f"{pcfg['model']}, {len(chunks)} batch(es)")
    print(f"  output  {out_path}\n")

    recorded = 0
    for i, chunk in enumerate(chunks, 1):
        if stop_event is not None and stop_event.is_set():
            print("Stopped.")
            break
        records = [dict({"n": pos}, **{k: rec.get(k) for k in BATCH_FIELDS})
                   for pos, rec in chunk]
        print(f"[{i}/{len(chunks)}] {len(chunk)} videos...")
        try:
            text = providers.complete_text(
                pcfg, system, json.dumps(records, ensure_ascii=False))
        except providers.FatalProviderError as e:
            print(f"  stopping: {e}")
            break
        except providers.ProviderError as e:
            print(f"  batch failed, moving on: {e}")
            continue
        with open(out_path, "a", encoding="utf-8") as f:
            f.write("\n\n" + text.strip() + "\n")
        got = record_results(out_path, {rec["id"] for _, rec in chunk})
        recorded += got
        print(f"  recorded {got}/{len(chunk)}")

    print(f"\nRecorded {recorded} of {len(batch)} screening(s).")
    return recorded


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
_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"',
                       "–": "-", "—": "-", "…": "..."})


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
