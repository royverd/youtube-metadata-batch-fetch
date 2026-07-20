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

Intermediate step in the backlog pipeline. Input comes from grab_watchlist.py;
output feeds the summarizer. The one interactive bit is a startup prompt for
how to route transcript requests (direct / Webshare / a proxies.txt pool),
since that's the step YouTube rate-limits per IP.

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
from datetime import datetime, timedelta

import requests

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
from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT = os.path.join(HERE, "watchlist.txt")
JSON_OUT = os.path.join(HERE, "metadata.json")
CSV_OUT = os.path.join(HERE, "metadata.csv")
FAIL_OUT = os.path.join(HERE, "failures.csv")
PROXIES_FILE = os.path.join(HERE, "proxies.txt")

# Machine-managed pool for mode 2, separate from the hand-edited proxies.txt.
# proxyscrape is the primary source - it tests proxies itself and reports
# liveness ('alive') and measured latency ('timeout'), so filtering/ordering
# happens at fetch time instead of us ever having to learn it the hard way.
# proxifly is kept as a cheap fallback (no filtering/latency data of its own)
# in case proxyscrape's API ever changes shape or goes away - cheap enough to
# leave in even though proxyscrape is expected to make it redundant.
FREE_PROXIES_FILE = os.path.join(HERE, "free_proxies.json")
PROXYSCRAPE_URL = "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&format=json"
PROXIFLY_URL = "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.json"
# These lists reportedly churn every 1-30 minutes, so caching for hours would
# just mean re-trying proxies that died long ago; 30 min balances that against
# not re-fetching on every single re-run of a resumed batch.
FREE_PROXY_REFRESH_MINUTES = 30
# youtube-transcript-api never sets a request timeout anywhere internally -
# confirmed by reading its source - so a proxy that connects but never
# responds would hang forever instead of failing fast. FREE_PROXY_TIMEOUT is
# injected via TimeoutSession specifically for mode 2, where most candidates
# are expected to be dead and need to fail fast so rotation can move on.
FREE_PROXY_TIMEOUT = 8

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


class TimeoutSession(requests.Session):
    """A plain requests.Session has no default per-call timeout, and
    youtube-transcript-api never passes one - so without this, a dead-but-not-
    actively-refusing proxy hangs forever. Injects a default timeout on every
    request unless the caller explicitly overrides it."""

    def __init__(self, timeout):
        super().__init__()
        self._default_timeout = timeout

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", self._default_timeout)
        return super().request(*args, **kwargs)


class ApiPool:
    """One or more YouTubeTranscriptApi instances to draw transcript requests
    from. Default and Webshare modes are pools of exactly one - Webshare's own
    gateway already rotates IPs internally when it hits a 429, so there's
    nothing left for us to do there. A generic proxy pool is the one case
    where rotation is our job: each GenericProxyConfig is just one fixed
    proxy with no rotation of its own, so transcript_pass rotates through the
    pool itself whenever the current entry fails.

    keys carries the raw proxy URL behind each entry (None for the single-IP
    modes, which have nothing to persist) so a failure can be recorded back
    into free_proxies.json after the run - and so rotate() can skip anything
    already proven bad this run instead of possibly cycling back to it."""

    def __init__(self, apis, labels, keys=None):
        self.apis = apis
        self.labels = labels
        self.keys = keys or [None] * len(apis)
        self.idx = 0
        self.failed_keys = set()
        self.ok_keys = set()
        self.latencies = {}  # proxy URL -> seconds for its successful fetch, for next run's ordering

    @property
    def current(self):
        return self.apis[self.idx]

    @property
    def label(self):
        return self.labels[self.idx]

    @property
    def key(self):
        return self.keys[self.idx]

    def rotate(self):
        """Advance to the next proxy that hasn't already failed this run,
        wrapping around. False if there's only one entry, or every other
        entry in the pool has already failed - nothing left to try."""
        if len(self.apis) <= 1:
            return False
        if self.keys[self.idx]:
            self.failed_keys.add(self.keys[self.idx])
        for _ in range(len(self.apis)):
            self.idx = (self.idx + 1) % len(self.apis)
            if self.keys[self.idx] is None or self.keys[self.idx] not in self.failed_keys:
                return True
        return False


def read_proxies(path):
    """One proxy URL per line for mode 2 (scheme://[user:pass@]host:port).
    Blank lines and #-comments are ignored."""
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]


def load_free_proxy_pool(path):
    """Persisted record of every free proxy we've ever seen and what happened
    last time we tried it. Corrupt/missing just means starting empty, same
    resilience pattern as load_existing() for metadata.json."""
    if not os.path.isfile(path):
        return {"last_refreshed": None, "proxies": {}}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"last_refreshed": None, "proxies": {}}


def save_free_proxy_pool(pool_data, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pool_data, f, indent=2)


def fetch_proxyscrape_urls():
    """Primary source. Filtered to ssl=True (https-capable) since YouTube is
    all-TLS - a proxy without CONNECT/TLS support fails every request here
    regardless of speed - and alive=True (proxyscrape's own live-tested
    flag), sorted by their measured 'timeout' ascending. All of that comes
    from proxyscrape's own data; nothing here is us testing anything."""
    try:
        with urllib.request.urlopen(PROXYSCRAPE_URL, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  couldn't fetch proxyscrape: {type(e).__name__}")
        return []
    candidates = [p for p in data.get("proxies", []) if p.get("ssl") and p.get("alive")]
    candidates.sort(key=lambda p: p.get("timeout", float("inf")))
    return [f"http://{p['ip']}:{p['port']}" for p in candidates if p.get("ip") and p.get("port")]


def fetch_proxifly_urls():
    """Cheap fallback with no filtering/latency data of its own - only
    matters if proxyscrape's API ever changes shape or goes away."""
    try:
        with urllib.request.urlopen(PROXIFLY_URL, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [p["proxy"] for p in data if p.get("proxy")]
    except Exception as e:
        print(f"  couldn't fetch proxifly (fallback): {type(e).__name__}")
        return []


def fetch_free_proxy_urls():
    """proxyscrape first (already filtered to https-capable+alive, sorted by
    its own measured latency), then proxifly appended as a fallback -
    dict.fromkeys dedupes while preserving that priority order."""
    urls = fetch_proxyscrape_urls() + fetch_proxifly_urls()
    return list(dict.fromkeys(urls))


def refresh_free_proxy_pool(path):
    """Re-grabs the free proxy lists only if the persisted pool is missing or
    older than FREE_PROXY_REFRESH_MINUTES - these lists churn fast enough that
    caching much longer would mean re-trying proxies that died hours ago, but
    re-fetching on every single re-run of a resumed batch would be wasteful.

    Newly-seen proxies are appended as 'untested'. Proxies we've already
    proven 'ok' or 'failed' keep that status rather than being reset - the
    whole point is not re-trying something we already know is dead."""
    pool_data = load_free_proxy_pool(path)
    last = pool_data.get("last_refreshed")
    stale = last is None or (
        datetime.now() - datetime.fromisoformat(last) > timedelta(minutes=FREE_PROXY_REFRESH_MINUTES)
    )
    if not stale:
        return pool_data

    print(f"  refreshing free proxy pool (last grabbed: {last or 'never'})...")
    fresh_urls = fetch_free_proxy_urls()
    proxies = pool_data.setdefault("proxies", {})
    added = 0
    for u in fresh_urls:
        if u not in proxies:
            proxies[u] = {"status": "untested"}
            added += 1
    pool_data["last_refreshed"] = datetime.now().isoformat()
    save_free_proxy_pool(pool_data, path)
    print(f"  {len(fresh_urls)} seen, {added} new, {len(proxies) - added} already known.")
    return pool_data


def record_pool_results(pool, pool_data, path):
    """Writes this run's outcomes back into the persisted free-proxy pool -
    anything that failed is marked so future runs skip it outright; anything
    that worked is marked 'ok' (plus the latency of that successful fetch, so
    build_pool can try the fastest known-good proxies first next time - no
    separate speed-check pass needed, it's just the real fetch's own timing).
    No-op for modes 0/1 (keys are all None there, nothing to persist)."""
    proxies = pool_data.setdefault("proxies", {})
    changed = False
    for key in pool.failed_keys:
        if key in proxies:
            proxies[key]["status"] = "failed"
            changed = True
    for key in pool.ok_keys:
        if key in proxies:
            proxies[key]["status"] = "ok"
            if key in pool.latencies:
                proxies[key]["latency_ms"] = round(pool.latencies[key] * 1000)
            changed = True
    if changed:
        save_free_proxy_pool(pool_data, path)


def select_transcript_mode():
    """Numbered pick for how transcript requests get routed. Mirrors
    grab_watchlist.py's browser picker: 0 is the safe default, and it
    re-prompts on bad input rather than guessing what you meant."""
    print("How should transcript requests be routed?")
    print("  0) Default  - this machine's own IP, spaced out per the delay tiers below.")
    print("  1) Webshare - your Webshare rotating-residential-proxy account")
    print("                (env vars WEBSHARE_PROXY_USERNAME / WEBSHARE_PROXY_PASSWORD).")
    print("  2) Generic  - your own proxies.txt entries plus an auto-refreshed pool of")
    print("                free public proxies; rotates to the next one immediately")
    print("                on any failure (most are dead/already blocked - expect that).")
    while True:
        choice = input("Mode [0]: ").strip()
        if choice == "":
            return "0"
        if choice in ("0", "1", "2"):
            return choice
        print("  Enter 0, 1, or 2.")


def build_pool(mode):
    """Turns the chosen mode into an ApiPool. Modes 1/2 fall back to the
    default (no proxy) with an explanatory message if they aren't actually
    configured yet, rather than silently doing nothing or crashing."""
    if mode == "1":
        user = os.environ.get("WEBSHARE_PROXY_USERNAME")
        pw = os.environ.get("WEBSHARE_PROXY_PASSWORD")
        if user and pw:
            cfg = WebshareProxyConfig(proxy_username=user, proxy_password=pw)
            return ApiPool([YouTubeTranscriptApi(proxy_config=cfg)], ["Webshare"])
        print("  WEBSHARE_PROXY_USERNAME / WEBSHARE_PROXY_PASSWORD aren't set.")
        print("  Get them from https://dashboard.webshare.io/proxy/settings, set both,")
        print("  and re-run. Falling back to the default (no proxy) for this run.")
    elif mode == "2":
        manual_urls = read_proxies(PROXIES_FILE)
        pool_data = refresh_free_proxy_pool(FREE_PROXIES_FILE)
        free_proxies = pool_data.get("proxies", {})
        # Known-good first, fastest measured latency first among those (best
        # odds of an immediate, quick success), then untested (no timing yet),
        # never anything already proven 'failed'. Latency comes from the real
        # fetch that already succeeded last time - no separate speed check.
        # Manual entries always count, on the assumption you added them on purpose.
        ok = sorted((u for u, info in free_proxies.items() if info.get("status") == "ok"),
                    key=lambda u: free_proxies[u].get("latency_ms", float("inf")))
        untested = [u for u, info in free_proxies.items() if info.get("status") == "untested"]
        urls = list(dict.fromkeys(manual_urls + ok + untested))
        if urls:
            apis = [YouTubeTranscriptApi(proxy_config=GenericProxyConfig(http_url=u, https_url=u),
                                          http_client=TimeoutSession(FREE_PROXY_TIMEOUT))
                    for u in urls]
            labels = [f"proxy {i + 1}/{len(urls)}" for i in range(len(urls))]
            print(f"  Pool: {len(manual_urls)} manual + {len(ok)} known-good + {len(untested)} "
                  f"untested free proxies ({len(free_proxies) - len(ok) - len(untested)} known-bad skipped).")
            p = ApiPool(apis, labels, keys=urls)
            p.pool_data, p.pool_data_path = pool_data, FREE_PROXIES_FILE  # so main() can persist results after
            return p
        print("  No usable proxies - proxies.txt is empty and the free-proxy pool came up empty too.")
        print("  Falling back to the default (no proxy) for this run.")
    return ApiPool([YouTubeTranscriptApi()], ["default (no proxy)"])


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


def transcript_pass(records, pool):
    """Fill in transcripts for records that don't have a settled one yet.
    Reuses 'ok' transcripts and skips permanently disabled/absent ones, so a
    resume run only works on what's actually outstanding.

    Single-IP pools (modes 0/1) keep the original spaced-out, tolerant-of-a-
    few-transient-errors behavior - there's exactly one IP whose reputation is
    worth protecting, and delay is what protects it. Multi-proxy pools (mode
    2) drop both: with dozens to hundreds of disposable, mostly-already-dead
    candidates, pacing doesn't protect anything we intend to reuse, and
    tolerating a few errors before reacting just means burning through several
    real videos on a proxy that was simply never going to answer. So there,
    any failure (blocked or a plain error) rotates immediately.

    On a failure, tries pool.rotate() first - if there's another proxy to fall
    back to, the same video is retried on it rather than being marked done.
    Only stops for good once rotate() reports nothing left to try. Progress is
    saved either way (records are mutated in place and written by the
    caller), so a later re-run resumes exactly where this left off. Ctrl-C is
    safe for the same reason."""
    todo = [r for r in records
            if r.get("transcript_status") not in ({"ok"} | PERMANENT_TRANSCRIPT_STATES)]
    if not todo:
        print("Transcripts: nothing outstanding.")
        return

    rotating_pool = len(pool.apis) > 1
    base = request_delay_base(len(todo))
    if rotating_pool:
        print(f"Transcripts: {len(todo)} to fetch via {pool.label} (pool of {len(pool.apis)}) - "
              f"no artificial delay, rotates immediately on any failure. Ctrl-C is safe - progress is saved.")
    else:
        est_min = round(len(todo) * base / 60)
        print(f"Transcripts: {len(todo)} to fetch, ~{base:.0f}s apart (est ~{est_min} min) via "
              f"{pool.label}. Ctrl-C is safe - progress is saved.")

    consecutive_errors = 0
    stopped = None  # reason string once we decide the pool is exhausted
    i = 0

    try:
        while i < len(todo):
            rec = todo[i]
            t0 = time.time()
            text, segs, status, lang = fetch_transcript(pool.current, rec["id"])
            elapsed = time.time() - t0
            rec["transcript_text"] = text
            rec["transcript_segments"] = segs
            rec["transcript_status"] = status
            rec["transcript_language"] = lang
            tag = f"{status}/{lang}" if lang else status
            label = f"[{i + 1}/{len(todo)}] {tag:11} {rec.get('title') or rec['id']} ({pool.label})"
            print(f"\r{label[:78]:<78}", end="", flush=True)

            block_signal = status == "blocked"
            if status == "error":
                if rotating_pool:
                    block_signal = True  # no tolerance - a disposable proxy that errors once is done
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                        block_signal = True
            else:
                consecutive_errors = 0
                if status == "ok" and pool.key:
                    pool.ok_keys.add(pool.key)
                    pool.latencies[pool.key] = elapsed

            if block_signal:
                old_label = pool.label
                if status == "blocked":
                    reason = "blocked"
                elif rotating_pool:
                    reason = "error"  # no tolerance here, so this is always the first and only one
                else:
                    reason = f"{consecutive_errors} errors in a row"
                if pool.rotate():
                    consecutive_errors = 0
                    print(f"\n  {reason} on {old_label} - rotating to {pool.label}")
                    continue  # retry this same video on the newly-rotated proxy
                stopped = ("YouTube is blocking this IP (RequestBlocked/IpBlocked)." if status == "blocked"
                           else f"{consecutive_errors} errors in a row - the IP looks blocked." if not rotating_pool
                           else "every proxy in the pool has now failed - none left to rotate to.")
                break

            i += 1
            if i < len(todo) and not rotating_pool:
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
        print("    - re-run and pick mode 1 (Webshare) or 2 (your own proxy pool) instead")


def main():
    print("fetch_metadata")
    print("Fetches yt-dlp metadata and youtube-transcript-api transcripts for every")
    print("video in watchlist.txt, resuming from metadata.json so re-runs only fetch")
    print("what's still missing.")
    print()
    mode = select_transcript_mode()
    pool = build_pool(mode)

    print()
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
    transcript_pass(records, pool)
    if getattr(pool, "pool_data", None) is not None:
        record_pool_results(pool, pool.pool_data, pool.pool_data_path)

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
