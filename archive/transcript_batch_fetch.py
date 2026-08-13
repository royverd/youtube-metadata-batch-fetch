"""
transcript_batch_fetch.py

Pulls transcripts + titles for a batch of YouTube videos and writes them
to JSON and/or CSV, structured for feeding into an LLM summarizer.

Deps: youtube-transcript-api (pip install youtube-transcript-api --break-system-packages)
Config persisted to transcript_fetch_config.json next to this script.
"""

import json
import csv
import os
import re
import sys
import urllib.request
import urllib.parse
from datetime import datetime

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcript_fetch_config.json")

VIDEO_ID_RE = re.compile(
    r"(?:v=|youtu\.be/|embed/|shorts/)([A-Za-z0-9_-]{11})"
)


def load_config():
    """Load persisted settings, or fall back to sane defaults on first run."""
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"output_dir": "", "output_format": "json"}


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def prompt_output_dir(current):
    """Prompt for a valid directory, re-prompting on bad input. Empty keeps current."""
    while True:
        raw = input(f"Output directory [{current or './output'}]: ").strip().strip('"').strip("'")
        if not raw:
            path = current or "./output"
        else:
            path = raw
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError as e:
            print(f"  Can't use that path ({e}). Try again.")


def prompt_output_format(current):
    while True:
        choice = input(f"Output format - json / csv / both [{current or 'json'}]: ").strip().lower()
        if not choice:
            return current or "json"
        if choice in ("json", "csv", "both"):
            return choice
        print("  Enter json, csv, or both.")


def extract_video_id(line):
    """Accept a raw 11-char ID or any common YouTube URL shape."""
    line = line.strip().strip('"').strip("'")
    if not line:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", line):
        return line
    match = VIDEO_ID_RE.search(line)
    return match.group(1) if match else None


def fetch_title(video_id):
    """No-key metadata via the public oEmbed endpoint. Returns None on failure."""
    url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode({
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "format": "json",
    })
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("title")
    except Exception:
        return None


def fetch_transcript_text(api, video_id):
    """Returns (full_text, segment_count) or raises on failure."""
    fetched = api.fetch(video_id, languages=("en",))
    raw = fetched.to_raw_data()
    full_text = " ".join(seg["text"].replace("\n", " ") for seg in raw)
    return full_text, len(raw)


def read_lines_any_encoding(path):
    """PowerShell's `>` writes UTF-16LE with a BOM; bash redirects write UTF-8.
    Try both rather than assuming one, so exports from either shell just work."""
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            with open(path, "r", encoding=encoding) as f:
                lines = [l.strip() for l in f if l.strip()]
            if lines:
                return lines
        except UnicodeError:
            continue
    return []


def read_input_file():
    while True:
        raw = input("Path to text file with one video URL/ID per line: ").strip().strip('"').strip("'")
        if os.path.isfile(raw):
            lines = read_lines_any_encoding(raw)
            if lines:
                return lines
            print("  File is empty or unreadable. Try again.")
        else:
            print("  Not a file. Try again.")


def write_outputs(records, output_dir, output_format):
    """Timestamped filenames so nothing gets silently overwritten."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    written = []

    if output_format in ("json", "both"):
        path = os.path.join(output_dir, f"transcripts_{stamp}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        written.append(path)

    if output_format in ("csv", "both"):
        path = os.path.join(output_dir, f"transcripts_{stamp}.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["video_id", "title", "url", "segment_count", "transcript_text"])
            writer.writeheader()
            writer.writerows(records)
        written.append(path)

    return written


def run_batch(config):
    lines = read_input_file()
    api = YouTubeTranscriptApi()

    records = []
    failures = []

    for i, line in enumerate(lines, 1):
        video_id = extract_video_id(line)
        if not video_id:
            print(f"[{i}/{len(lines)}] SKIP - unrecognized line: {line.strip()[:60]}")
            failures.append((line.strip(), "unrecognized ID/URL"))
            continue

        try:
            title = fetch_title(video_id) or "(title unavailable)"
            text, seg_count = fetch_transcript_text(api, video_id)
            records.append({
                "video_id": video_id,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "segment_count": seg_count,
                "transcript_text": text,
            })
            print(f"[{i}/{len(lines)}] OK    - {title[:60]}")
        except (TranscriptsDisabled, NoTranscriptFound):
            print(f"[{i}/{len(lines)}] FAIL  - {video_id} (no transcript available)")
            failures.append((video_id, "no transcript available"))
        except Exception as e:
            print(f"[{i}/{len(lines)}] FAIL  - {video_id} ({e})")
            failures.append((video_id, str(e)))

    print()
    print(f"Done: {len(records)} succeeded, {len(failures)} failed out of {len(lines)}.")
    if failures:
        print("Failures:")
        for vid, reason in failures:
            print(f"  {vid} - {reason}")

    if records:
        paths = write_outputs(records, config["output_dir"], config["output_format"])
        print()
        print("Written:")
        for p in paths:
            print(f"  {p}")
    else:
        print("Nothing to write.")


def main():
    print("transcript_batch_fetch")
    print("Fetches YouTube transcripts + titles into JSON/CSV for summarization.")
    print(f"Config: {CONFIG_PATH}")

    config = load_config()
    print(f"Loaded settings - output_dir: {config['output_dir'] or '(not set)'}, format: {config['output_format']}")
    print()

    if not config["output_dir"]:
        config["output_dir"] = prompt_output_dir(config["output_dir"])
        save_config(config)

    while True:
        print()
        print("1) Fetch transcripts from a file of URLs/IDs")
        print("2) Change output directory")
        print("3) Change output format")
        print("4) Exit")
        choice = input("> ").strip()

        if choice == "1":
            run_batch(config)
        elif choice == "2":
            config["output_dir"] = prompt_output_dir(config["output_dir"])
            save_config(config)
        elif choice == "3":
            config["output_format"] = prompt_output_format(config["output_format"])
            save_config(config)
        elif choice == "4":
            sys.exit(0)
        else:
            print("Enter 1-4.")


if __name__ == "__main__":
    main()
