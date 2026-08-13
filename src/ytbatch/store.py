"""
store.py

SQLite store for per-video analysis results. Sits alongside metadata.json
rather than replacing it: metadata.json stays the ingest contract that
batch_fetch owns, analysis.db holds everything derived from it.

The four cache columns (transcript_hash, prompt_version, model, effort) are
what make re-runs cheap. Any of them changing invalidates that video and only
that video - edit the prompt and you re-analyze 65 videos; edit one transcript
and you re-analyze one.

Deps: none (sqlite3 is stdlib).
"""

import hashlib
import json
import sqlite3
from datetime import datetime

from . import paths

DB_PATH = paths.data_file("analysis.db")

AXES = ["depth", "breadth", "rigor", "sourcing", "prerequisites", "density"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis (
    video_id         TEXT PRIMARY KEY,
    transcript_hash  TEXT NOT NULL,
    prompt_version   TEXT NOT NULL,
    model            TEXT NOT NULL,
    effort           TEXT NOT NULL,
    subject          TEXT,
    format           TEXT,
    description      TEXT,
    audience_for     TEXT,
    audience_not_for TEXT,
    length_verdict   TEXT,
    padding_fraction REAL,
    depth            INTEGER,
    breadth          INTEGER,
    rigor            INTEGER,
    sourcing         INTEGER,
    prerequisites    INTEGER,
    density          INTEGER,
    evidence         TEXT,
    on_screen        TEXT,
    on_screen_note   TEXT,
    keywords         TEXT,
    undetermined     TEXT,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    analyzed_at      TEXT
);
"""


def text_hash(text):
    """Short digest - collision risk at 65 or 65k videos is not a real concern,
    and a 16-char column stays readable when you are eyeballing the table."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def cache_state(conn, video_id, transcript_hash, prompt_version, model, effort):
    """Returns None if this video needs analysis, else the reason it doesn't.

    Reporting *why* something was skipped beats a bare boolean: when a re-run
    unexpectedly re-analyzes everything, the answer is almost always that the
    prompt file changed, and this makes that visible instead of mysterious.
    """
    row = conn.execute(
        "SELECT transcript_hash, prompt_version, model, effort FROM analysis WHERE video_id = ?",
        (video_id,),
    ).fetchone()
    if row is None:
        return None
    for label, stored, current in (
        ("transcript", row["transcript_hash"], transcript_hash),
        ("prompt", row["prompt_version"], prompt_version),
        ("model", row["model"], model),
        ("effort", row["effort"], effort),
    ):
        if stored != current:
            return None  # stale on at least one axis - redo it
    return "cached"


def stale_reasons(conn, video_id, transcript_hash, prompt_version, model, effort):
    """Which cache columns moved, for the run summary. Empty list = new video."""
    row = conn.execute(
        "SELECT transcript_hash, prompt_version, model, effort FROM analysis WHERE video_id = ?",
        (video_id,),
    ).fetchone()
    if row is None:
        return []
    changed = []
    for label, stored, current in (
        ("transcript", row["transcript_hash"], transcript_hash),
        ("prompt", row["prompt_version"], prompt_version),
        ("model", row["model"], model),
        ("effort", row["effort"], effort),
    ):
        if stored != current:
            changed.append(label)
    return changed


def save(conn, video_id, transcript_hash, prompt_version, model, effort, result, usage):
    """Upsert one analysis. Committed per video so Ctrl-C mid-run costs at most
    the video in flight - same guarantee transcript_pass gives."""
    axes = result.get("axes", {})
    conn.execute(
        """
        INSERT INTO analysis (
            video_id, transcript_hash, prompt_version, model, effort,
            subject, format, description, audience_for, audience_not_for,
            length_verdict, padding_fraction,
            depth, breadth, rigor, sourcing, prerequisites, density,
            evidence, on_screen, on_screen_note, keywords, undetermined,
            input_tokens, output_tokens, analyzed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(video_id) DO UPDATE SET
            transcript_hash=excluded.transcript_hash,
            prompt_version=excluded.prompt_version,
            model=excluded.model,
            effort=excluded.effort,
            subject=excluded.subject,
            format=excluded.format,
            description=excluded.description,
            audience_for=excluded.audience_for,
            audience_not_for=excluded.audience_not_for,
            length_verdict=excluded.length_verdict,
            padding_fraction=excluded.padding_fraction,
            depth=excluded.depth, breadth=excluded.breadth, rigor=excluded.rigor,
            sourcing=excluded.sourcing, prerequisites=excluded.prerequisites,
            density=excluded.density,
            evidence=excluded.evidence,
            on_screen=excluded.on_screen,
            on_screen_note=excluded.on_screen_note,
            keywords=excluded.keywords,
            undetermined=excluded.undetermined,
            input_tokens=excluded.input_tokens,
            output_tokens=excluded.output_tokens,
            analyzed_at=excluded.analyzed_at
        """,
        (
            video_id, transcript_hash, prompt_version, model, effort,
            result.get("subject"), result.get("format"), result.get("description"),
            result.get("audience_for"), result.get("audience_not_for"),
            result.get("length_verdict"), result.get("padding_fraction"),
            axes.get("depth"), axes.get("breadth"), axes.get("rigor"),
            axes.get("sourcing"), axes.get("prerequisites"), axes.get("density"),
            json.dumps(result.get("evidence") or {}, ensure_ascii=False),
            result.get("on_screen"), result.get("on_screen_note"),
            json.dumps(result.get("keywords") or [], ensure_ascii=False),
            result.get("undetermined"),
            usage.get("input_tokens", 0), usage.get("output_tokens", 0),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def load_all(conn):
    """Every analysis row as a plain dict, JSON columns decoded."""
    out = []
    for row in conn.execute("SELECT * FROM analysis ORDER BY video_id"):
        rec = dict(row)
        for col in ("evidence", "keywords"):
            try:
                rec[col] = json.loads(rec[col]) if rec[col] else ({} if col == "evidence" else [])
            except json.JSONDecodeError:
                rec[col] = {} if col == "evidence" else []
        out.append(rec)
    return out


def totals(conn):
    row = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o FROM analysis"
    ).fetchone()
    return row["n"], row["i"], row["o"]
