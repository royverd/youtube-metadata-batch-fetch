"""
analyze.py

Runs the description pass: reads metadata.json, sends each transcript to the
configured provider, and writes structured per-video descriptions into
analysis.db.

Output shape is pinned by schema.VideoDescription and validated on the way back,
so the prompt wording in prompts/describe_v1.txt can be rewritten freely without
breaking anything downstream. The prompt's content hash is the cache version -
editing that file re-analyzes exactly the videos it affects and nothing else,
with no version number to remember to bump.

Resumable: re-runs only touch videos whose transcript, prompt, model, provider,
or effort changed. Ctrl-C is safe; each video is committed as it lands.

Deps: requests, pydantic; anthropic only if that provider is selected.
Config: config.py, prompts on first run. Re-run with --config to change it.
"""

import json
import os
import sys
import time

from . import config, paths, providers, store

# Windows console is cp1252 and video titles carry emoji; same guard
# batch_fetch needs, same reason.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

METADATA = paths.data_file("metadata.json")
PROMPT_PATH = paths.prompt_file("describe_v1.txt")

# $ per million tokens, for the end-of-run tally only. Anything absent here
# (the Gemini free tier) reports as free rather than guessing at a number.
PRICING = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
}


def load_prompt():
    with open(PROMPT_PATH, encoding="utf-8") as f:
        return f.read()


def load_metadata():
    with open(METADATA, encoding="utf-8") as f:
        return json.load(f)


def build_user_content(rec):
    """Title and description carry the video's own claim about itself, which is
    exactly what the length and audience judgments get measured against - so
    they go in even though the transcript dwarfs them."""
    parts = [f"TITLE: {rec.get('title') or '(none)'}"]
    if rec.get("duration"):
        parts.append(f"DURATION: {rec['duration'] // 60} minutes")
    if rec.get("description"):
        parts.append(f"\nDESCRIPTION:\n{rec['description']}")
    chapters = rec.get("chapters") or []
    if chapters:
        titles = " | ".join(c.get("title", "") for c in chapters if c.get("title"))
        parts.append(f"\nCHAPTERS: {titles}")
    parts.append(f"\nTRANSCRIPT:\n{rec.get('transcript_text') or ''}")
    return "\n".join(parts)


def ask_limit(available):
    """A bounded first run is the point - five videos is enough to tell whether
    the prompt is working before spending the whole corpus on wording you are
    about to change."""
    while True:
        raw = input(f"How many videos to analyze? [{available} = all]: ").strip()
        if raw == "":
            return available
        if raw.isdigit() and int(raw) > 0:
            return min(int(raw), available)
        print(f"  Enter a positive number, or blank for all {available}.")


def plan(conn, usable, prompt_version, cfg):
    """Split the corpus into already-current and needs-work, and record why
    anything is stale. When a re-run unexpectedly redoes everything the answer
    is nearly always that the prompt file moved, and this surfaces that."""
    todo, cached, reasons = [], 0, {}
    tag = f"{cfg['provider']}:{cfg['model']}"
    for rec in usable:
        thash = store.text_hash(rec["transcript_text"])
        if store.cache_state(conn, rec["id"], thash, prompt_version, tag, cfg["effort"]):
            cached += 1
            continue
        for r in store.stale_reasons(conn, rec["id"], thash, prompt_version, tag, cfg["effort"]):
            reasons[r] = reasons.get(r, 0) + 1
        todo.append((rec, thash))
    return todo, cached, reasons


def main():
    print("ytb-analyze")
    print("Describes each video from its transcript and writes data/analysis.db.")
    print(f"Config: data/{os.path.basename(config.CONFIG_PATH)}  Prompt: prompts/{os.path.basename(PROMPT_PATH)}")
    print()

    cfg = config.resolve(force_prompt="--config" in sys.argv)

    if "--models" in sys.argv:
        # Hardcoded model lists rot as providers rotate their line-ups; ask.
        print()
        for line in providers.list_models(cfg):
            print(f"  {line}")
        return

    if not os.path.isfile(METADATA):
        print(f"Not found: {METADATA}. Run ytb-fetch first.")
        return
    if not os.path.isfile(PROMPT_PATH):
        print(f"Not found: {PROMPT_PATH}.")
        return

    prompt_text = load_prompt()
    prompt_version = store.text_hash(prompt_text)
    records = load_metadata()

    # Videos with no usable transcript are skipped rather than guessed at.
    # Whether they get a degraded description from metadata alone is still open.
    usable = [r for r in records if r.get("transcript_status") == "ok" and r.get("transcript_text")]
    no_transcript = len(records) - len(usable)

    conn = store.connect()
    todo, cached, reasons = plan(conn, usable, prompt_version, cfg)

    print(f"{len(records)} videos, {len(usable)} with transcripts"
          f"{f', {no_transcript} without (skipped)' if no_transcript else ''}.")
    effort_note = f" at {cfg['effort']} effort" if cfg["effort"] != "n/a" else ""
    print(f"{cached} already current, {len(todo)} to analyze via {cfg['provider']}/{cfg['model']}{effort_note}.")
    if reasons:
        print(f"  re-analyzing because: {', '.join(f'{k} changed ({v})' for k, v in reasons.items())}")
    if not todo:
        print("Nothing outstanding.")
        conn.close()
        return

    todo = todo[:ask_limit(len(todo))]

    try:
        describe = providers.make_describer(cfg)
    except providers.FatalProviderError as e:
        print(f"Cannot start: {e}")
        conn.close()
        return

    print()
    pacing = ""
    if cfg["provider"] == "gemini":
        rpm = providers.GEMINI_RPM.get(cfg["model"], providers.GEMINI_DEFAULT_RPM)
        pacing = f" Paced to {rpm}/min for the free tier, so expect ~{len(todo) / rpm:.0f} min."
    print(f"Analyzing {len(todo)}. Ctrl-C is safe - each video is saved as it lands.{pacing}")

    ok = failed = 0
    tin = tout = tcache_r = tcache_w = 0
    stopped = None
    t0 = time.time()
    tag = f"{cfg['provider']}:{cfg['model']}"

    try:
        for i, (rec, thash) in enumerate(todo, 1):
            label = f"[{i}/{len(todo)}] {rec.get('title') or rec['id']}"
            print(f"\r{label[:78]:<78}", end="", flush=True)
            try:
                result, usage = describe(prompt_text, build_user_content(rec))
            except providers.FatalProviderError as e:
                stopped = f"{e}."
                break
            except providers.ProviderError as e:
                failed += 1
                note = f"  failed: {rec['id']} ({e})"
                print(f"\r{note[:78]:<78}")
                continue
            except Exception as e:
                # A schema-validation failure lands here. One bad video should
                # not end a run that is otherwise working.
                failed += 1
                note = f"  failed: {rec['id']} ({type(e).__name__})"
                print(f"\r{note[:78]:<78}")
                continue

            store.save(conn, rec["id"], thash, prompt_version, tag, cfg["effort"], result, usage)
            ok += 1
            tin += usage["input_tokens"]
            tout += usage["output_tokens"]
            tcache_r += usage["cache_read"]
            tcache_w += usage["cache_write"]
    except KeyboardInterrupt:
        stopped = "interrupted (Ctrl-C)."

    print()
    print(f"  described {ok} | failed {failed} | {(time.time() - t0) / 60:.1f} min")

    total_in = tin + tcache_r + tcache_w
    if cfg["model"] in PRICING:
        rate_in, rate_out = PRICING[cfg["model"]]
        cost = (tin * rate_in + tcache_w * rate_in * 1.25 + tcache_r * rate_in * 0.1
                + tout * rate_out) / 1_000_000
        money = f"~${cost:.2f}"
    else:
        money = "free tier"
    print(f"  tokens in {total_in:,} (cache read {tcache_r:,}) | out {tout:,} | {money}")

    if stopped:
        print()
        print(f"  STOPPED: {stopped}")
        print(f"  {len(todo) - ok - failed} not attempted. Re-run to continue - progress is saved.")

    n, _, _ = store.totals(conn)
    print(f"  analysis.db now holds {n} videos.")
    conn.close()


if __name__ == "__main__":
    main()
