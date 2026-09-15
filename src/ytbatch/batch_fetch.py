"""
batch_fetch.py

Reads YouTube IDs from data/watchlist.txt and builds one record per video,
holding both the descriptive metadata (from yt-dlp) and the transcript (from
youtube-transcript-api). Writes two files into data/:

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
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

from datetime import datetime, timedelta

from . import grab_watchlist, paths

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

INPUT = paths.data_file("watchlist.txt")
JSON_OUT = paths.data_file("metadata.json")
CSV_OUT = paths.data_file("metadata.csv")
FAIL_OUT = paths.data_file("failures.csv")
PROXIES_FILE = paths.data_file("proxies.txt")

# Machine-managed pool for mode 2, separate from the hand-edited proxies.txt.
# proxyscrape is the primary source - it tests proxies itself and reports
# liveness ('alive') and measured latency ('timeout'), so filtering/ordering
# happens at fetch time instead of us ever having to learn it the hard way.
# proxifly is kept as a cheap fallback (no filtering/latency data of its own)
# in case proxyscrape's API ever changes shape or goes away - cheap enough to
# leave in even though proxyscrape is expected to make it redundant.
FREE_PROXIES_FILE = paths.data_file("free_proxies.json")
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
# A proxy that has fetched a transcript gets this many failures before it is
# written off, and a written-off one that had worked is tried again after the
# cooldown. One strike was permanent before: all 13 proxies that ever worked
# ended up marked failed and were never offered again. Untested proxies stay
# one-strike - most on the free lists are dead on arrival.
PROXY_OK_STRIKES = 3
PROXY_RETRY_HOURS = 24
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
# Archive bookkeeping. in_watchlist is the live-queue flag that used to be
# implicit in a record's mere presence, back when the file was pruned.
ARCHIVE_FIELDS = ["in_watchlist", "first_seen", "last_seen"]

CSV_COLUMNS = (KEEP_FIELDS + ["transcript_status", "transcript_language",
                              "transcript_segments"] + ARCHIVE_FIELDS)

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


YT_DLP_ERROR_RE = re.compile(r"ERROR:\s*\[youtube\]\s*([A-Za-z0-9_-]{11}):\s*(.+)")


def fetch(ids, stop_event=None):
    """One yt-dlp process for the whole batch. --dump-json emits newline-delimited
    JSON, one video per line, so we parse the stream as it arrives and print a
    per-item tick. -i (ignore-errors) means a private/deleted/bot-gated video is
    silently skipped rather than killing the run - we detect those afterward by
    diffing, but "silently" used to mean actually silent: stderr went to DEVNULL,
    so the real per-video reason (private, deleted, "sign in to confirm you're
    not a bot", etc.) was thrown away and never surfaced. It's drained on a
    background thread instead now - concurrently, so a large batch's error
    volume can't fill the OS pipe buffer and deadlock against the stdout read -
    and yt-dlp's own "ERROR: [youtube] <id>: <reason>" lines are parsed back into
    a per-video dict so the actual cause can be shown, not just the fact of failure.

    stop_event (set by the GUI's Stop button) kills the yt-dlp process rather
    than waiting for a long batch to finish - whatever already streamed back is
    still returned and kept, since it's a resumable partial result like any other."""
    urls = [f"https://www.youtube.com/watch?v={i}" for i in ids]
    proc = subprocess.Popen(
        ["yt-dlp", "--dump-json", "--skip-download", "--ignore-errors", *urls],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",  # yt-dlp's stderr has thrown non-UTF-8 bytes before (e.g. curly quotes) - never crash on it
    )

    stderr_lines = []
    stderr_thread = threading.Thread(target=lambda: stderr_lines.extend(proc.stderr), daemon=True)
    stderr_thread.start()

    records = []
    for line in proc.stdout:
        if stop_event is not None and stop_event.is_set():
            proc.terminate()
            print("\n  stopped - keeping the metadata that already came back.")
            break
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
    stderr_thread.join()
    print()  # close off the live line before the summary

    errors = {}
    for line in stderr_lines:
        m = YT_DLP_ERROR_RE.search(line)
        if m:
            errors[m.group(1)] = m.group(2).strip()
    return records, errors


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
    from. Every mode with a fallback available puts the origin IP first (it's
    free and often still working) and proxies after it as the backstop -
    Webshare's own gateway already rotates IPs internally on a 429, so
    there's nothing left for us to do once we're in it; a generic proxy pool
    is the one case where rotation between entries is our own job.

    is_direct marks which single entry (if any) is the real origin IP, so
    transcript_pass can keep protecting *that* one's reputation (spaced,
    tolerant of a few transient errors) while treating every actual proxy as
    disposable (no delay, zero tolerance) - a distinction that pool size
    alone can't make once the origin IP is just one entry among several.

    keys carries the raw proxy URL behind each entry (None for the origin IP
    and for Webshare, which have nothing to persist) so a failure can be
    recorded back into free_proxies.json after the run - and so rotate() can
    skip anything already proven bad this run instead of possibly cycling
    back to it."""

    def __init__(self, apis, labels, keys=None, is_direct=None):
        self.apis = apis
        self.labels = labels
        self.keys = keys or [None] * len(apis)
        self.is_direct = is_direct or [False] * len(apis)
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

    @property
    def on_direct(self):
        return self.is_direct[self.idx]

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
    """Atomic, like the corpus: a truncated pool file loads as empty and
    throws away every latency and strike count learned so far."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".free_proxies-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(pool_data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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

    Newly-seen proxies are added as 'untested'. Proxies already proven 'ok'
    or 'failed' keep that status rather than being reset.

    The download's own order is kept as pool_data["latest"]: proxyscrape's
    alive, TLS-capable list sorted by its measured latency, then proxifly.
    build_pool tries untested proxies in that order and skips any no longer
    listed. Before, untested proxies were tried in the order first seen, so
    leftovers from earlier downloads and the unfiltered fallback list came
    first - the best-ranked live proxy sat around position 600 of 12,000."""
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
    if fresh_urls:
        # An empty download (both sites down) keeps the previous order rather
        # than marking every proxy as no longer listed.
        pool_data["latest"] = fresh_urls
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
    No-op for modes 0/1 (keys are all None there, nothing to persist).

    A proxy with a recorded latency has worked before and gets
    PROXY_OK_STRIKES failures before it is marked failed; an untested one is
    marked failed on its first. failed_at starts the retry cooldown.
    Successes are applied after failures, so a proxy that both failed and
    worked this run ends up ok with its strikes cleared."""
    proxies = pool_data.setdefault("proxies", {})
    now = datetime.now().isoformat()
    changed = False
    for key in pool.failed_keys:
        info = proxies.get(key)
        if info is None:
            continue
        proven = "latency_ms" in info
        info["fails"] = info.get("fails", 0) + 1
        if not proven or info["fails"] >= PROXY_OK_STRIKES:
            info["status"] = "failed"
            info["failed_at"] = now
        changed = True
    for key in pool.ok_keys:
        info = proxies.get(key)
        if info is None:
            continue
        info["status"] = "ok"
        info["fails"] = 0
        info.pop("failed_at", None)
        if key in pool.latencies:
            info["latency_ms"] = round(pool.latencies[key] * 1000)
        changed = True
    if changed:
        save_free_proxy_pool(pool_data, path)


def select_transcript_mode():
    """Numbered pick for how transcript requests get routed. Mirrors
    grab_watchlist.py's browser picker: 0 is the safe default, and it
    re-prompts on bad input rather than guessing what you meant."""
    print("How should transcript requests be routed?")
    print("  0) Generic  - origin IP first, then your proxies.txt entries plus an")
    print("                auto-refreshed pool of free public proxies, rotating")
    print("                immediately on any failure once past the origin (default).")
    print("  1) Webshare - origin IP first, then your Webshare rotating-residential-")
    print("                proxy account (env vars WEBSHARE_PROXY_USERNAME /")
    print("                WEBSHARE_PROXY_PASSWORD).")
    print("  2) Direct   - this machine's own IP only, no proxy fallback, spaced")
    print("                out per the delay tiers below.")
    while True:
        choice = input("Mode [0]: ").strip()
        if choice == "":
            return "0"
        if choice in ("0", "1", "2"):
            return choice
        print("  Enter 0, 1, or 2.")


def build_pool(mode):
    """Turns the chosen mode into an ApiPool. Modes 0/1 put the origin IP
    first - it's free and often still working - and only fall through to
    proxies once that fails; mode 2 is the explicit opt-out, origin IP only,
    ever. Modes 0/1 fall back to origin-only with an explanatory message if
    their proxy source isn't actually configured yet, rather than silently
    doing nothing or crashing."""
    direct_api, direct_label = YouTubeTranscriptApi(), "default (no proxy)"

    if mode == "1":
        user = os.environ.get("WEBSHARE_PROXY_USERNAME")
        pw = os.environ.get("WEBSHARE_PROXY_PASSWORD")
        if user and pw:
            cfg = WebshareProxyConfig(proxy_username=user, proxy_password=pw)
            webshare_api = YouTubeTranscriptApi(proxy_config=cfg)
            return ApiPool([direct_api, webshare_api], [direct_label, "Webshare"],
                            keys=[None, None], is_direct=[True, False])
        print("  WEBSHARE_PROXY_USERNAME / WEBSHARE_PROXY_PASSWORD aren't set.")
        print("  Get them from https://dashboard.webshare.io/proxy/settings, set both,")
        print("  and re-run. Falling back to the default (no proxy) for this run.")
    elif mode == "2":
        return ApiPool([direct_api], [direct_label], is_direct=[True])
    else:  # mode "0", the default
        manual_urls = read_proxies(PROXIES_FILE)
        pool_data = refresh_free_proxy_pool(FREE_PROXIES_FILE)
        free_proxies = pool_data.get("proxies", {})
        # Known-good first, fastest measured latency first among those (best
        # odds of an immediate, quick success), then untested (no timing yet),
        # never anything already proven 'failed'. Latency comes from the real
        # fetch that already succeeded last time - no separate speed check.
        # Manual entries always count, on the assumption you added them on purpose.
        # Order, most likely to work first: proxies that have worked, fastest
        # first; ones that worked before and have sat out the cooldown; then
        # untested ones in the latest download's order (proxyscrape's alive
        # list by latency, then proxifly). Untested proxies no longer listed
        # are skipped, not deleted - they return if a list carries them again.
        latency = lambda u: free_proxies[u].get("latency_ms", float("inf"))
        ok = sorted((u for u, info in free_proxies.items() if info.get("status") == "ok"), key=latency)
        cutoff = datetime.now() - timedelta(hours=PROXY_RETRY_HOURS)

        def cooled_down(info):
            try:
                return datetime.fromisoformat(info["failed_at"]) <= cutoff
            except (KeyError, TypeError, ValueError):
                return True  # proven, then failed before failed_at existed

        retry = sorted((u for u, info in free_proxies.items()
                        if info.get("status") == "failed" and "latency_ms" in info and cooled_down(info)),
                       key=latency)
        latest = pool_data.get("latest")
        if latest is None:
            # A pool file from before "latest" existed: no download order to
            # use yet, so fall back to what's on file until the next refresh.
            latest = list(free_proxies)
        untested = [u for u in dict.fromkeys(latest)
                    if free_proxies.get(u, {}).get("status") == "untested"]
        stale = sum(1 for info in free_proxies.values() if info.get("status") == "untested") - len(untested)
        urls = list(dict.fromkeys(manual_urls + ok + retry + untested))
        if urls:
            proxy_apis = [YouTubeTranscriptApi(proxy_config=GenericProxyConfig(http_url=u, https_url=u),
                                                http_client=TimeoutSession(FREE_PROXY_TIMEOUT))
                          for u in urls]
            proxy_labels = [f"proxy {i + 1}/{len(urls)}" for i in range(len(urls))]
            known_bad = sum(1 for info in free_proxies.values() if info.get("status") == "failed") - len(retry)
            print(f"  Pool: {len(manual_urls)} manual + {len(ok)} known-good + {len(retry)} retrying "
                  f"after cooldown + {len(untested)} untested from the latest lists "
                  f"({stale} no longer listed, {known_bad} known-bad skipped).")
            p = ApiPool([direct_api] + proxy_apis, [direct_label] + proxy_labels,
                        keys=[None] + urls, is_direct=[True] + [False] * len(urls))
            p.pool_data, p.pool_data_path = pool_data, FREE_PROXIES_FILE  # so main() can persist results after
            return p
        print("  No usable proxies - proxies.txt is empty and the free-proxy pool came up empty too.")
        print("  Falling back to the default (no proxy) for this run.")
    return ApiPool([direct_api], [direct_label], is_direct=[True])


# The corpus's append-only record. metadata.json is a snapshot rebuilt from
# this plus itself; this file is the thing that cannot lose data.
JOURNAL = paths.data_file("metadata.journal.jsonl")

# Bookkeeping recomputed on every run. A change here alone is not new
# content, and counting it would append the whole 20 MB corpus every day.
_VOLATILE = frozenset(ARCHIVE_FIELDS)


class CorpusUnreadable(RuntimeError):
    """metadata.json exists but could not be read, and the journal has
    nothing to fall back on. Never treated as empty."""


class CorpusShrink(RuntimeError):
    """A write to metadata.json would have dropped ids already in it."""


def merge_record(old, new):
    """new's fields over old's, except that a record with no transcript yet
    never erases one that has it - a re-fetched video arrives without one.
    Returns a new dict."""
    merged = dict(old)
    merged.update(new)
    if old.get("transcript_text") and not new.get("transcript_text"):
        for key in ("transcript_text", "transcript_segments",
                    "transcript_status", "transcript_language"):
            if key in old:
                merged[key] = old[key]
    return merged


def append_journal(records, path=JOURNAL):
    """The journal's only writer, and it only ever opens the file in append
    mode. That is the entire guarantee: nothing here can truncate or rewrite
    it, so every record that ever reached disk stays recoverable whatever
    happens to metadata.json. Never add a second writer or open this path
    with "w"."""
    if not records:
        return 0
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return len(records)


def read_journal(path=JOURNAL):
    """Folds the journal into {id: record}, later lines merged over earlier.
    A torn final line - a crash mid-append - is skipped rather than fatal."""
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("id"):
                old = out.get(rec["id"])
                out[rec["id"]] = merge_record(old, rec) if old else rec
    return out


def _content(rec):
    return {k: v for k, v in rec.items() if k not in _VOLATILE}


def journal_changes(records, journal):
    """The records whose content differs from the journal's latest version."""
    return [r for r in records
            if r["id"] not in journal or _content(r) != _content(journal[r["id"]])]


def _read_snapshot(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {r["id"]: r for r in data if isinstance(r, dict) and r.get("id")}


def load_existing(path, journal_path=JOURNAL):
    """Everything known, keyed by id: the metadata.json snapshot merged over
    the journal. Neither present is a first run.

    A missing or unreadable snapshot falls back to the journal, and says so.
    With no journal either, an unreadable snapshot raises. It used to return
    {} on the grounds that the data is re-fetchable - and the run then saw
    every id as new and wrote the 3 records it had over 648. Transcripts are
    not practically re-fetchable in bulk (the IP gets blocked)."""
    journal = read_journal(journal_path)
    if not os.path.isfile(path):
        if journal:
            print(f"  {os.path.basename(path)} is missing - rebuilt {len(journal)} "
                  f"records from {os.path.basename(journal_path)}.")
        return journal
    try:
        snapshot = _read_snapshot(path)
    except (ValueError, OSError, TypeError) as e:  # ValueError covers JSON and Unicode errors
        if journal:
            print(f"  {path} could not be read ({type(e).__name__}: {e}) - "
                  f"using the {len(journal)} records in the journal.")
            return journal
        raise CorpusUnreadable(f"{path} exists but could not be read ({type(e).__name__}: {e})") from e
    merged = dict(journal)
    for vid, rec in snapshot.items():
        merged[vid] = merge_record(merged[vid], rec) if vid in merged else rec
    return merged


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


def write_failures(missing, path, errors=None):
    """Every failed ID gets a row with a clickable URL no matter what; oEmbed
    fills in a title where it still can. status distinguishes 'restricted but
    identifiable' from 'gone'. errors carries yt-dlp's own per-video reason
    (parsed from its stderr in fetch()) so the actual cause - private, deleted,
    "sign in to confirm you're not a bot", etc. - is visible instead of just
    the fact that it failed."""
    errors = errors or {}
    rows = []
    for i in missing:
        title = oembed_title(i)
        reason = errors.get(i, "")
        rows.append({
            "id": i,
            "url": f"https://www.youtube.com/watch?v={i}",
            "title": title or "",
            "status": "title-only" if title else "unavailable",
            "error": reason,
        })
        suffix = f" [{reason}]" if reason else ""
        print(f"  {i} - {title or '(no public metadata - private/deleted)'}{suffix}")
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "url", "title", "status", "error"])
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


def write_json(records, path, allow_shrink=False):
    """Temp file then replace, with the previous version kept as .bak. Writing
    in place truncated the corpus the moment open() ran, so anything that
    stopped the dump - or a run that had loaded nothing - left no copy.

    Refuses to drop an id already in the file. Checked here, at the lowest
    level, so no caller present or future can shrink the corpus by passing
    the wrong list; allow_shrink exists so a deliberate reset has to say so.
    An unreadable file can't be checked - its bytes survive in .bak and its
    records in the journal."""
    if not allow_shrink and os.path.isfile(path):
        try:
            before = set(_read_snapshot(path))
        except (ValueError, OSError, TypeError):
            before = set()
        missing = before - {r["id"] for r in records}
        if missing:
            raise CorpusShrink(f"refusing to write {path}: {len(missing)} id(s) already "
                               f"in it are not in the new data")
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".metadata-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.isfile(path):
            shutil.copy2(path, path + ".bak")
        os.replace(tmp, path)
        if os.path.abspath(path) == os.path.abspath(JSON_OUT):
            try:
                from . import backups
                backups.snapshot(path)
            except OSError as e:
                print(f"  backup copy of {os.path.basename(path)} failed: {e}")
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def transcript_pass(records, pool, stop_event=None):
    """Fill in transcripts for records that don't have a settled one yet.
    Reuses 'ok' transcripts and skips permanently disabled/absent ones, so a
    resume run only works on what's actually outstanding.

    The origin IP (whenever it's the current entry - pool.on_direct) keeps the
    original spaced-out, tolerant-of-a-few-transient-errors behavior: it's the
    one resource whose reputation is worth protecting, and that protection
    has to apply no matter how many proxies are queued up behind it. Any
    actual proxy entry gets neither: with dozens to hundreds of disposable,
    mostly-already-dead candidates, pacing doesn't protect anything we intend
    to reuse, and tolerating a few errors before reacting just means burning
    through several real videos on a proxy that was simply never going to
    answer. So there, any failure (blocked or a plain error) rotates
    immediately - this is evaluated fresh every iteration since rotation
    changes which kind of entry is current.

    On a failure, tries pool.rotate() first - if there's another proxy to fall
    back to, the same video is retried on it rather than being marked done.
    Only stops for good once rotate() reports nothing left to try. Progress is
    saved either way (records are mutated in place and written by the
    caller), so a later re-run resumes exactly where this left off. Ctrl-C is
    safe for the same reason, as is the GUI's Stop button (stop_event)."""
    todo = [r for r in records
            if r.get("transcript_status") not in ({"ok"} | PERMANENT_TRANSCRIPT_STATES)]
    if not todo:
        print("Transcripts: nothing outstanding.")
        return

    base = request_delay_base(len(todo))
    if len(pool.apis) > 1:
        est_min = round(len(todo) * base / 60)
        print(f"Transcripts: {len(todo)} to fetch via {pool.label} - starts on the origin IP "
              f"(~{base:.0f}s apart, tolerant of a few errors, est ~{est_min} min if it holds), falls "
              f"through to {len(pool.apis) - 1} more if needed (no delay there, rotates on any failure). "
              f"Ctrl-C is safe - progress is saved.")
    else:
        est_min = round(len(todo) * base / 60)
        print(f"Transcripts: {len(todo)} to fetch, ~{base:.0f}s apart (est ~{est_min} min) via "
              f"{pool.label}. Ctrl-C is safe - progress is saved.")

    consecutive_errors = 0
    stopped = None  # reason string once we decide the pool is exhausted
    i = 0

    try:
        while i < len(todo):
            if stop_event is not None and stop_event.is_set():
                stopped = "stopped by user."
                break
            rec = todo[i]
            on_direct = pool.on_direct  # evaluated fresh - rotation may have changed this since last iteration
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
                if on_direct:
                    consecutive_errors += 1
                    if consecutive_errors >= CONSECUTIVE_ERROR_LIMIT:
                        block_signal = True
                else:
                    block_signal = True  # no tolerance - a disposable proxy that errors once is done
            else:
                consecutive_errors = 0
                if status == "ok" and pool.key:
                    pool.ok_keys.add(pool.key)
                    pool.latencies[pool.key] = elapsed

            if block_signal:
                old_label = pool.label
                if status == "blocked":
                    reason = "blocked"
                elif on_direct:
                    reason = f"{consecutive_errors} errors in a row"
                else:
                    reason = "error"  # no tolerance here, so this is always the first and only one
                if pool.rotate():
                    consecutive_errors = 0
                    print(f"\n  {reason} on {old_label} - rotating to {pool.label}")
                    continue  # retry this same video on the newly-rotated proxy
                stopped = ("YouTube is blocking this IP (RequestBlocked/IpBlocked)." if status == "blocked"
                           else f"{consecutive_errors} errors in a row - the IP looks blocked." if on_direct
                           else "every proxy in the pool has now failed - none left to rotate to.")
                break

            i += 1
            if i < len(todo) and on_direct:
                delay = random.uniform(base * (1 - DELAY_JITTER), base * (1 + DELAY_JITTER))
                # wait() returns early the moment Stop is pressed, so the button
                # stays responsive through a delay that can run to ~14s.
                if stop_event is not None:
                    if stop_event.wait(delay):
                        stopped = "stopped by user."
                        break
                else:
                    time.sleep(delay)
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


def run_pipeline(mode, stop_event=None):
    """Everything after the watchlist exists: read IDs, fill in missing
    metadata, then transcripts, then write both outputs. Split out of main()
    so the CLI and the GUI drive the same code path - the only thing main()
    adds is the interactive prompts the GUI replaces with its own widgets.

    Returns True if it got as far as writing output. stop_event is threaded
    through to the two long steps; a stop still writes whatever was gathered,
    since a partial result is exactly what the resume logic expects to find."""
    pool = build_pool(mode)

    print()
    print(f"Reading IDs from {INPUT}")

    if not os.path.isfile(INPUT):
        print(f"Not found: {INPUT}. Fetch the watchlist first.")
        return False

    ids = read_ids(INPUT)
    if not ids:
        print("No usable IDs in the input file.")
        return False

    # Resume: keep metadata we already have, fetch only ids we've never seen.
    try:
        existing = load_existing(JSON_OUT)
    except CorpusUnreadable as e:
        print(f"Stopping before fetching anything: {e}")
        print(f"  Nothing was written. Restore it from {JSON_OUT}.bak if that exists,")
        print("  or move the broken file aside to start a fresh corpus deliberately.")
        return False
    need_meta = [i for i in ids if i not in existing]
    have = len(ids) - len(need_meta)
    archived_before = len(existing) - have
    print(f"{len(ids)} IDs - {have} already have metadata, {len(need_meta)} to fetch."
          + (f" ({archived_before} archived from earlier runs.)" if archived_before else ""))

    fetch_errors = {}
    if need_meta:
        print("Fetching metadata (visits each video, slow for large lists)...")
        print()
        new_records, fetch_errors = fetch(need_meta, stop_event)
        for rec in new_records:
            existing[rec["id"]] = rec
        print()

    # The file is an archive, not a mirror of the playlist. Removing a video
    # from Watch Later used to delete its metadata and its transcript on the
    # next fetch, which quietly destroyed the only copy of work already done
    # against it. Everything ever fetched is kept; membership of the live queue
    # is a flag on the record instead of its mere presence in the file.
    queued_ids = set(ids)
    today = f"{datetime.now():%Y-%m-%d}"
    for rec in existing.values():
        rec["in_watchlist"] = rec["id"] in queued_ids
        if rec["in_watchlist"]:
            rec["last_seen"] = today
        rec.setdefault("first_seen", rec.get("last_seen") or today)
        rec.setdefault("last_seen", today)

    # Follow watchlist order; only ids we actually got metadata for.
    records = [existing[i] for i in ids if i in existing]
    # Insertion order of `existing` is the order they were last written, so
    # archived entries keep their relative positions instead of reshuffling.
    archived = [r for r in existing.values() if not r["in_watchlist"]]
    everything = records + archived

    missing = [i for i in ids if i not in existing]
    # A stop mid-metadata leaves most of the batch "missing" for reasons that
    # have nothing to do with the videos, so skip the oEmbed recovery pass
    # rather than write a failures.csv blaming videos we simply never reached.
    if missing and not (stop_event is not None and stop_event.is_set()):
        print(f"{len(missing)} have no metadata - recovering titles via oEmbed where possible:")
        rows = write_failures(missing, FAIL_OUT, fetch_errors)
        recovered = sum(1 for r in rows if r["status"] == "title-only")
        print(f"  {recovered}/{len(missing)} identifiable by title; rest are private/deleted.")
        print(f"  Wrote {FAIL_OUT}")

    print()
    # Queued records only: an archived video with a transient 'error' status
    # would otherwise be retried on every run for the rest of time.
    transcript_pass(records, pool, stop_event)
    if getattr(pool, "pool_data", None) is not None:
        record_pool_results(pool, pool.pool_data, pool.pool_data_path)

    if everything:
        # Append-only, enforced mechanically rather than by this code being
        # right: (1) every record goes to the journal, which is only ever
        # opened for appending; (2) a snapshot write that would drop an id is
        # refused inside write_json. The merge below just makes the normal
        # case pass those checks - the guarantee doesn't rest on it.
        try:
            on_disk = load_existing(JSON_OUT)
        except CorpusUnreadable as e:
            append_journal(journal_changes(everything, read_journal()))
            side = f"{os.path.splitext(JSON_OUT)[0]}.unmerged-{datetime.now():%Y%m%d_%H%M%S}.json"
            write_json(everything, side)
            print(f"Not touching {JSON_OUT}: {e}")
            print(f"  This run's {len(everything)} records are in {JOURNAL} and {side}.")
            return False
        # In place, so records/archived keep pointing at the merged dicts.
        for rec in everything:
            old = on_disk.get(rec["id"])
            if old:
                merged = merge_record(old, rec)
                rec.clear()
                rec.update(merged)
        held = {r["id"] for r in everything}
        carried = [dict(r, in_watchlist=False) for vid, r in on_disk.items() if vid not in held]
        if carried:
            print(f"  Kept {len(carried)} record(s) already on disk that this run didn't load.")
            archived += carried
            everything += carried
        if archived:
            print(f"{len(records)} in the watchlist, {len(archived)} archived "
                  f"(kept with their transcripts).")
        # Journal first: if the snapshot write dies, the journal is already ahead.
        appended = append_journal(journal_changes(everything, read_journal()))
        if appended:
            print(f"  Appended {appended} new or changed record(s) to {os.path.basename(JOURNAL)}.")
        try:
            json_path = resilient_write(lambda p: write_json(everything, p), JSON_OUT)
        except CorpusShrink as e:
            print(f"Not touching {JSON_OUT}: {e}")
            print(f"  Every record from this run is in {JOURNAL}; nothing was lost.")
            return False
        csv_path = resilient_write(lambda p: write_csv(everything, p), CSV_OUT)
        print()
        print(f"Wrote {json_path}")
        print(f"Wrote {csv_path}")
        return True

    print("Nothing to write.")
    return False


def main():
    print("ytb-watchlist")
    grab_watchlist.main()
    print("ytb-fetch")
    print("Fetches yt-dlp metadata and youtube-transcript-api transcripts for every")
    print("video in data/watchlist.txt, resuming from data/metadata.json so re-runs")
    print("only fetch what's still missing.")
    print()
    run_pipeline(select_transcript_mode())


if __name__ == "__main__":
    main()
