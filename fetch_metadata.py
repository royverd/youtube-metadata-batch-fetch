"""
fetch_metadata.py

Reads YouTube IDs from watchlist.txt and builds one record per video, holding
both the descriptive metadata (from yt-dlp) and the transcript (from
youtube-transcript-api). Writes two files beside this script:

  metadata.json - full record per video, transcript text included.
  metadata.csv  - the metadata columns plus a transcript flag/segment count,
                  but NOT the transcript text (a whole transcript in one cell
                  is the same bloat problem as dumping raw formats).

Resumable: on re-run it reuses metadata already fetched and only fills in
missing transcripts, and it won't re-hammer videos whose transcripts are
permanently disabled/absent. So a rate-limit or crash mid-run costs nothing.

Intermediate step in the backlog pipeline: no menu, just run it. Input comes
from grab_watchlist.py; output feeds the summarizer.

Deps: yt-dlp on PATH, youtube-transcript-api.
"""

import csv
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

# YouTube titles carry emoji and non-Latin scripts; the default Windows console
# is cp1252 and print() raises UnicodeEncodeError on anything outside it. Force
# UTF-8 and replace what still can't render, so a title never crashes a long run.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    TranscriptsDisabled, NoTranscriptFound, RequestBlocked, IpBlocked,
)

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT = os.path.join(HERE, "watchlist.txt")
JSON_OUT = os.path.join(HERE, "metadata.json")
CSV_OUT = os.path.join(HERE, "metadata.csv")
FAIL_OUT = os.path.join(HERE, "failures.csv")

# yt-dlp's full dump is ~99% download plumbing (formats, automatic_captions,
# thumbnails, heatmap) that's useless for backlog triage and bloats the JSON to
# millions of lines. We whitelist the fields that actually describe the video.
# Order here IS the CSV column order. The three list-valued fields at the end
# get JSON-encoded into their cells; in the JSON they stay as real arrays.
KEEP_FIELDS = [
    "id", "title", "channel", "uploader", "channel_id", "upload_date",
    "duration", "duration_string", "view_count", "like_count", "comment_count",
    "age_limit", "availability", "live_status", "language", "webpage_url",
    "description", "categories", "tags", "chapters",
]


# CSV = the metadata columns plus transcript bookkeeping, but never the text.
# transcript_text lives only in the JSON.
CSV_COLUMNS = KEEP_FIELDS + ["transcript_status", "transcript_language", "transcript_segments"]

# Transcript states that won't change on a re-run, so resume skips them instead
# of pounding the API again. 'error' is transient (rate-limit/network) and retries.
PERMANENT_TRANSCRIPT_STATES = {"disabled", "none"}

# youtube-transcript-api scrapes YouTube directly (no key, no quota to raise), so
# heavy use gets the IP blocked - and a block lasts hours to a day, so waiting it
# out mid-run is pointless. Strategy instead: space requests wider as the batch
# grows (total volume is what earns a day-long flag), and STOP the moment we're
# clearly blocked. Numbers are heuristic - YouTube publishes none; the community
# floor is ~1s/request. Tune the tiers if you still get flagged.
DELAY_TIERS = [(50, 3.0), (150, 5.0), (300, 8.0)]  # (up to N videos, base seconds apart)
DELAY_ABOVE = 10.0               # base seconds when the batch is larger than the last tier
DELAY_JITTER = 0.4               # +/- fraction on each delay, so the cadence isn't robotic
CONSECUTIVE_ERROR_LIMIT = 5      # generic errors in a row = probably blocked, stop and report


def request_delay_base(total):
    """Pick a per-request spacing from the batch size - bigger batch, wider gap."""
    for limit, base in DELAY_TIERS:
        if total <= limit:
            return base
    return DELAY_ABOVE


def prune(rec):
    """Keep only the whitelisted fields. Missing ones become None so every
    record has the same shape - stable CSV columns, predictable JSON."""
    return {k: rec.get(k) for k in KEEP_FIELDS}

VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})")


def read_ids(path):
    """Robust to PowerShell's UTF-16-with-BOM exports as well as plain UTF-8.
    Accepts bare 11-char IDs or any common URL shape; dedupes, keeps order."""
    lines = []
    for enc in ("utf-8-sig", "utf-16"):
        try:
            with open(path, encoding=enc) as f:
                lines = [l.strip() for l in f if l.strip()]
            if lines:
                break
        except UnicodeError:
            continue

    ids = []
    for line in lines:
        line = line.strip().strip('"').strip("'")
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", line):
            ids.append(line)
        else:
            m = VIDEO_ID_RE.search(line)
            if m:
                ids.append(m.group(1))
    return list(dict.fromkeys(ids))


def fetch(ids):
    """One yt-dlp process for the whole batch. --dump-json emits newline-delimited
    JSON, one video per line, so we parse the stream as it arrives and print a
    per-item tick. -i (ignore-errors) means a private/deleted video is silently
    skipped rather than killing the run - we detect those afterward by diffing."""
    urls = [f"https://www.youtube.com/watch?v={i}" for i in ids]
    proc = subprocess.Popen(
        ["yt-dlp", "--dump-json", "--skip-download", "--ignore-errors", *urls],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # yt-dlp's progress noise; we drive our own counter
        text=True,
        encoding="utf-8",
    )

    records = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a stray non-JSON line; skip rather than crash the batch
        records.append(prune(rec))
        label = (rec.get("title") or rec.get("id") or "?")
        # \r overwrites the same line - the running titles are throwaway noise,
        # the CSV is the real record. Pad to a fixed width so a shorter title
        # doesn't leave tail characters from a longer previous one.
        status = f"[{len(records)}/{len(ids)}] {label}"
        print(f"\r{status[:78]:<78}", end="", flush=True)
    proc.wait()
    print()  # close off the live line before the summary
    return records


ENGLISH_VARIANTS = ("en", "en-US", "en-GB", "en-orig")


def fetch_transcript(api, video_id):
    """Returns (text, segment_count, status, language). Prefers English, but
    falls back to whatever track exists (e.g. Greek) so non-English videos stay
    in the pipeline - the language code records which one we got.

    status:
      'ok'       - got a transcript
      'disabled' - captions turned off for this video (permanent)
      'none'     - no tracks at all (permanent)
      'blocked'  - YouTube is blocking this IP; the caller should STOP, not retry
      'error'    - some other transient failure

    A block can strike on either the list() call or the track fetch, so both are
    guarded. 'blocked' is deliberately distinct from 'error': one bad video is
    noise, but a block means every following request is doomed until the IP clears
    (hours to a day) - the caller stops on it rather than grinding through."""
    try:
        tracks = api.list(video_id)
    except (RequestBlocked, IpBlocked):
        return "", 0, "blocked", ""
    except TranscriptsDisabled:
        return "", 0, "disabled", ""
    except Exception:
        return "", 0, "error", ""

    try:
        track = tracks.find_transcript(ENGLISH_VARIANTS)
    except NoTranscriptFound:
        # No English - take the first available track of any language.
        track = next(iter(tracks), None)
    if track is None:
        return "", 0, "none", ""

    try:
        raw = track.fetch().to_raw_data()
    except (RequestBlocked, IpBlocked):
        return "", 0, "blocked", ""
    except Exception:
        return "", 0, "error", ""
    text = " ".join(seg["text"].replace("\n", " ") for seg in raw)
    return text, len(raw), "ok", track.language_code


def load_existing(path):
    """Prior output keyed by id, for resume. A corrupt/half-written file just
    means we start fresh rather than crash - the data is re-fetchable."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return {r["id"]: r for r in json.load(f) if r.get("id")}
    except (json.JSONDecodeError, OSError, KeyError):
        return {}


def oembed_title(video_id):
    """Last-ditch title lookup via the public oEmbed endpoint - no key, and it
    often still answers for age/region-restricted videos that full extraction
    can't reach. Returns None for genuinely private/deleted (oEmbed 404s too)."""
    url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode({
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "format": "json",
    })
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("title")
    except Exception:
        return None


def write_failures(missing, path):
    """Every failed ID gets a row with a clickable URL no matter what; oEmbed
    fills in a title where it still can. status distinguishes 'restricted but
    identifiable' from 'gone'."""
    rows = []
    for i in missing:
        title = oembed_title(i)
        rows.append({
            "id": i,
            "url": f"https://www.youtube.com/watch?v={i}",
            "title": title or "",
            "status": "title-only" if title else "unavailable",
        })
        print(f"  {i} - {title or '(no public metadata - private/deleted)'}")
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "url", "title", "status"])
        writer.writeheader()
        writer.writerows(rows)
    return rows


def resilient_write(write_fn, path):
    """Windows locks a file that's open in Excel, and open('w') then raises
    PermissionError - which would throw away a multi-minute fetch. Fall back to
    a timestamped sibling and keep going rather than lose the data."""
    try:
        write_fn(path)
        return path
    except PermissionError:
        base, ext = os.path.splitext(path)
        alt = f"{base}_{datetime.now():%Y%m%d_%H%M%S}{ext}"
        write_fn(alt)
        print(f"  {os.path.basename(path)} was locked (open in Excel?) - wrote {os.path.basename(alt)} instead")
        return alt


def write_json(records, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def write_csv(records, path):
    """Fixed columns from CSV_COLUMNS. extrasaction='ignore' drops the big
    transcript_text field from the sheet. List/dict cells (categories, tags,
    chapters) are JSON-encoded so they survive as one cell instead of breaking
    the row - the JSON file keeps them navigable if you need the structure."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            row = {
                k: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
                for k, v in rec.items()
            }
            writer.writerow(row)


def transcript_pass(records):
    """Fill in transcripts for records that don't have a settled one yet.
    Reuses 'ok' transcripts and skips permanently disabled/absent ones, so a
    resume run only works on what's actually outstanding.

    Spacing scales with the batch size. Stops immediately on a block (explicit
    'blocked', or a short run of generic errors) - a YouTube IP block lasts hours
    to a day, so there's nothing to wait out mid-run. Progress is saved either
    way (records are mutated in place and written by the caller), so a later
    re-run resumes exactly where this left off. Ctrl-C is safe for the same reason."""
    todo = [r for r in records
            if r.get("transcript_status") not in ({"ok"} | PERMANENT_TRANSCRIPT_STATES)]
    if not todo:
        print("Transcripts: nothing outstanding.")
        return

    base = request_delay_base(len(todo))
    est_min = round(len(todo) * base / 60)
    print(f"Transcripts: {len(todo)} to fetch, ~{base:.0f}s apart (est ~{est_min} min). "
          f"Ctrl-C is safe - progress is saved.")
    api = YouTubeTranscriptApi()
    consecutive_errors = 0
    stopped = None  # reason string once we decide the IP is blocked

    try:
        for n, rec in enumerate(todo, 1):
            text, segs, status, lang = fetch_transcript(api, rec["id"])
            rec["transcript_text"] = text
            rec["transcript_segments"] = segs
            rec["transcript_status"] = status
            rec["transcript_language"] = lang
            tag = f"{status}/{lang}" if lang else status
            label = f"[{n}/{len(todo)}] {tag:11} {rec.get('title') or rec['id']}"
            print(f"\r{label[:78]:<78}", end="", flush=True)

            # Two block signals, both meaning the IP is blocked (hours-to-a-day),
            # not that these videos are bad. Stop rather than grind; resume later.
            if status == "blocked":
                stopped = "YouTube is blocking this IP (RequestBlocked/IpBlocked)."
                break
            if status == "error":
                consecutive_errors += 1
                if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                    stopped = f"{consecutive_errors} errors in a row - the IP looks blocked."
                    break
            else:
                consecutive_errors = 0

            if n < len(todo):
                time.sleep(random.uniform(base * (1 - DELAY_JITTER), base * (1 + DELAY_JITTER)))
    except KeyboardInterrupt:
        stopped = "interrupted (Ctrl-C)."

    counts = {"ok": 0, "disabled": 0, "none": 0, "error": 0, "blocked": 0}
    for r in todo:
        s = r.get("transcript_status")
        if s in counts:
            counts[s] += 1

    print()
    print(f"  ok {counts['ok']} | disabled {counts['disabled']} | none {counts['none']} | "
          f"error {counts['error']} | blocked {counts['blocked']}")

    if stopped:
        outstanding = sum(1 for r in todo
                          if r.get("transcript_status") not in ({"ok"} | PERMANENT_TRANSCRIPT_STATES))
        print()
        print(f"  STOPPED: {stopped}")
        print(f"  {outstanding} left unsettled (retryable on the next run). To get past a block:")
        print("    - wait ~24-48h for YouTube to clear this IP, then re-run (resume continues)")
        print("    - switch the transcript step to yt-dlp (not blocked on this IP)")
        print("    - route through a residential proxy (WebshareProxyConfig / GenericProxyConfig)")


def main():
    print("fetch_metadata")
    print(f"Reading IDs from {INPUT}")

    if not os.path.isfile(INPUT):
        print(f"Not found: {INPUT}. Run grab_watchlist.py first.")
        return

    ids = read_ids(INPUT)
    if not ids:
        print("No usable IDs in the input file.")
        return

    # Resume: keep metadata we already have, fetch only ids we've never seen.
    existing = load_existing(JSON_OUT)
    need_meta = [i for i in ids if i not in existing]
    print(f"{len(ids)} IDs - {len(existing)} already have metadata, {len(need_meta)} to fetch.")

    if need_meta:
        print("Fetching metadata (visits each video, slow for large lists)...")
        print()
        for rec in fetch(need_meta):
            existing[rec["id"]] = rec
        print()

    # Follow watchlist order; only ids we actually got metadata for.
    records = [existing[i] for i in ids if i in existing]

    missing = [i for i in ids if i not in existing]
    if missing:
        print(f"{len(missing)} have no metadata - recovering titles via oEmbed where possible:")
        rows = write_failures(missing, FAIL_OUT)
        recovered = sum(1 for r in rows if r["status"] == "title-only")
        print(f"  {recovered}/{len(missing)} identifiable by title; rest are private/deleted.")
        print(f"  Wrote {FAIL_OUT}")

    print()
    transcript_pass(records)

    if records:
        json_path = resilient_write(lambda p: write_json(records, p), JSON_OUT)
        csv_path = resilient_write(lambda p: write_csv(records, p), CSV_OUT)
        print()
        print(f"Wrote {json_path}")
        print(f"Wrote {csv_path}")
    else:
        print("Nothing to write.")


if __name__ == "__main__":
    main()
