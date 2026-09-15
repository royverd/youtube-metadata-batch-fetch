"""
progress.py

Owns data/progress.json: what the user did with each video and what they
thought of it. Nothing else in the project writes this file.

It is deliberately not a table in analysis.db and not a field in
metadata.json. Both of those are regenerated - analysis rows go stale when a
prompt hash changes, metadata.json is rewritten wholesale by every fetch - and
a hand-entered rating that took a two-hour watch to earn cannot be allowed to
ride along with anything disposable.

Keyed on video id. Corpus position is not stable between fetches and has
already been observed to reorder and drop entries mid-project.

Standalone: each entry carries a "video" snapshot (title, channel, date,
views, duration, url) alongside the verdict and decision, so a review can be
shown and understood without metadata.json. The snapshot is refreshed from
the corpus when it is available, and a field is only ever overwritten by a
non-empty value - losing metadata once erased every review from the Review
table, which read titles from the corpus alone.

Deps: none.
"""

import json
import os
import tempfile
from datetime import date

from . import paths

PATH = paths.data_file("progress.json")

# scrubbed is the honest middle: segments watched, neither committed to nor
# written off. It is a decision, so it counts as decided.
STATUSES = ("skipped", "scrubbed", "watched_full", "read_summary")

# Ratings carry one decimal place. Whole numbers are stored as ints so the
# file keeps reading as 7 rather than 7.0 - a cosmetic difference in JSON, but
# this file is meant to be opened and edited by hand.
RATING_PLACES = 1

# The three verdicts the screener skill emits, normalised. Stored so a chart
# can compare what the model said against what the user actually did.
VERDICTS = ("watch", "read", "skip")

# A skipped video was never watched, so a 1-10 score would be a lie. It still
# has to read as "dealt with" though, since that is what the Review tab filters
# on, so it gets a sentinel instead of an empty cell.
UNRATED = "-"


def load():
    """Missing or corrupt file means empty, never an exception on startup -
    but a corrupt file is reported, since unlike config this one is not
    re-enterable and silence would hide a real loss."""
    if not os.path.isfile(PATH):
        return {}
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  progress.json unreadable ({e}) - continuing with an empty history. "
              f"The file has NOT been overwritten.")
        return {}
    return data if isinstance(data, dict) else {}


def save(data):
    """Temp file in the same directory, then os.replace. A half-written
    progress.json is the one failure mode that loses work nothing can rebuild,
    so the swap is atomic or it doesn't happen."""
    d = os.path.dirname(PATH)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".progress-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PATH)
    except BaseException:
        # Includes KeyboardInterrupt on purpose: leaving a stray .progress-*
        # beside the real file is worse than the interrupt itself.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get(video_id):
    return load().get(video_id, {})


# What an entry keeps about the video itself - enough to list it, find it and
# open it with no corpus at all. Transcripts stay in metadata.json.
VIDEO_FIELDS = ("title", "channel", "upload_date", "duration_string", "view_count", "webpage_url")


def video_snapshot(record):
    """The VIDEO_FIELDS of a metadata record (or any dict), empties dropped."""
    return {k: record[k] for k in VIDEO_FIELDS
            if isinstance(record, dict) and record.get(k) not in (None, "")}


def _merge_video(entry, video):
    """Folds video fields into entry["video"]. Returns True if anything
    changed. Blanks never overwrite: a thinner source (a write-up with only a
    title) must not wipe what a fuller one already stored."""
    fresh = video_snapshot(video or {})
    if not fresh:
        return False
    current = entry.get("video") if isinstance(entry.get("video"), dict) else {}
    merged = {**current, **fresh}
    if merged == current:
        return False
    entry["video"] = merged
    return True


def _update(video_id, fields, video=None):
    """Read-modify-write the whole file. 600 entries is a few hundred KB and
    the alternative is a database; existing keys survive so the file stays
    hand-extensible."""
    data = load()
    entry = data.get(video_id, {})
    entry.update(fields)
    _merge_video(entry, video)
    data[video_id] = entry
    save(data)
    return entry


def sync_videos(records):
    """Refreshes the video snapshot of every entry the given metadata records
    cover, in one read and at most one write. records is any iterable of
    metadata dicts. Returns how many entries changed."""
    data = load()
    by_id = {r["id"]: r for r in records if isinstance(r, dict) and r.get("id")}
    changed = sum(1 for vid, entry in data.items()
                  if vid in by_id and _merge_video(entry, by_id[vid]))
    if changed:
        save(data)
    return changed


def mark_screened(video_id, ai_verdict, screened_at=None, video=None):
    """Keeps the first screened_at. Recording re-reads the whole output
    every 5 seconds while a session runs, and stamping today on each pass
    rewrote every video's real screening date to the latest pass."""
    if ai_verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {ai_verdict!r}; expected one of {VERDICTS}")
    fields = {"ai_verdict": ai_verdict}
    if screened_at or not get(video_id).get("screened_at"):
        fields["screened_at"] = screened_at or date.today().isoformat()
    return _update(video_id, fields, video)


def set_decision(video_id, status, rating=None, video=None):
    """rating is the user's own 1-10, UNRATED for a skip, or None for
    'decided but not yet scored'."""
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; expected one of {STATUSES}")
    if status == "skipped" and rating in (None, "", UNRATED):
        rating = UNRATED
    elif rating in ("", None):
        rating = None
    elif rating != UNRATED:
        rating = parse_rating(rating)
    return _update(video_id, {
        "status": status,
        "rating": rating,
        "decided_at": date.today().isoformat(),
    }, video)


def parse_rating(value):
    """Accepts 7, "7", 7.5 or "7,5" -> 7 or 7.5. Rejects anything finer than
    one decimal rather than silently rounding it: a 7.25 typed on purpose means
    the scale was misunderstood, and saying so beats storing something else."""
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError(f"rating {value!r} is not a number")
    if not 1 <= number <= 10:
        raise ValueError(f"rating {number:g} out of range 1-10")
    if round(number, RATING_PLACES) != number:
        raise ValueError(f"rating {number:g} has more than {RATING_PLACES} decimal place")
    return int(number) if number.is_integer() else number


def is_rating(value):
    """The sentinel is a str and bools are ints, so neither counts as a score."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def screened_ids():
    return {vid for vid, e in load().items() if e.get("ai_verdict")}


def summary(total=None):
    """Counts for the status line. total is the corpus size when the caller
    knows it - progress.json only knows about videos it has seen."""
    data = load()
    screened = sum(1 for e in data.values() if e.get("ai_verdict"))
    out = {
        "screened": screened,
        "left": None if total is None else max(0, total - screened),
        "decided": sum(1 for e in data.values() if e.get("status")),
        "rated": sum(1 for e in data.values() if is_rating(e.get("rating"))),
    }
    for v in VERDICTS:
        out[f"verdict_{v}"] = sum(1 for e in data.values() if e.get("ai_verdict") == v)
    for s in STATUSES:
        out[s] = sum(1 for e in data.values() if e.get("status") == s)
    # The sentinel is deliberately excluded from the mean - it is a marker,
    # not a score, and averaging it in would drag every statistic down.
    ratings = [e["rating"] for e in data.values() if is_rating(e.get("rating"))]
    out["mean_rating"] = round(sum(ratings) / len(ratings), 2) if ratings else None
    return out
