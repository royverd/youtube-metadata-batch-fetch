"""
show.py

Prints what analyze.py wrote, so a description can actually be read and judged.
No arguments lists every analyzed video one line each; a video id, or a
substring of a title, prints that one in full.

This is the review surface until the web UI exists - the point of it is checking
whether NOT FOR names a real group, whether LENGTH commits to a number, and
whether the axis quotes actually support their ratings.

Deps: none. Reads analysis.db and metadata.json.
"""

import json
import os
import sys

import store

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
METADATA = os.path.join(HERE, "metadata.json")

BARS = {1: "|....", 2: "||...", 3: "|||..", 4: "||||.", 5: "|||||"}


def titles():
    """id -> (title, minutes), for labelling rows the db doesn't carry."""
    if not os.path.isfile(METADATA):
        return {}
    with open(METADATA, encoding="utf-8") as f:
        return {r["id"]: (r.get("title") or r["id"], (r.get("duration") or 0) // 60)
                for r in json.load(f) if r.get("id")}


def wrap(text, width=76, indent="    "):
    words, line, out = (text or "").split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(indent + line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(indent + line)
    return "\n".join(out)


def show_one(rec, meta):
    title, minutes = meta.get(rec["video_id"], (rec["video_id"], 0))
    print(f"{title}")
    print(f"  {minutes} min | https://youtu.be/{rec['video_id']} | {rec['model']}")
    print()
    print(f"  SUBJECT  {rec['subject']}")
    print(f"  FORMAT   {rec['format']}")
    print()
    print(wrap(rec["description"], indent="  "))
    print()
    print("  FOR")
    print(wrap(rec["audience_for"]))
    print("  NOT FOR")
    print(wrap(rec["audience_not_for"]))
    print()
    pad = rec["padding_fraction"]
    print(f"  LENGTH   {pad:.0%} not substance")
    print(wrap(rec["length_verdict"]))
    print()
    for axis in store.AXES:
        val = rec[axis]
        quote = (rec["evidence"] or {}).get(axis, "")
        print(f"  {axis:<14}{BARS.get(val, '?')} {val}")
        if quote:
            print(wrap(f'"{quote}"', indent="                 "))
    print()
    print(f"  ON SCREEN  {rec['on_screen']}")
    print(wrap(rec["on_screen_note"]))
    print()
    print(f"  KEYWORDS   {', '.join(rec['keywords'])}")
    print("  UNDETERMINED")
    print(wrap(rec["undetermined"]))


def main():
    conn = store.connect()
    rows = store.load_all(conn)
    conn.close()
    if not rows:
        print("analysis.db is empty. Run analyze.py first.")
        return

    meta = titles()
    query = " ".join(sys.argv[1:]).strip().lower()

    if not query:
        # Sorted by density so the compressed ones surface first - the closest
        # thing to a useful ranking until novelty-vs-corpus exists.
        rows.sort(key=lambda r: -(r["density"] or 0))
        print(f"{len(rows)} analyzed. Pass an id or part of a title to see one in full.")
        print()
        for r in rows:
            title, minutes = meta.get(r["video_id"], (r["video_id"], 0))
            axes = "".join(str(r[a] or "?") for a in store.AXES)
            print(f"  {r['video_id']}  {axes}  {minutes:>4}m  {(r['subject'] or '')[:46]:<46}")
        print()
        print(f"  axes order: {' '.join(a[:4] for a in store.AXES)}")
        return

    hits = [r for r in rows
            if query == r["video_id"].lower()
            or query in meta.get(r["video_id"], ("", 0))[0].lower()
            or query in (r["subject"] or "").lower()]
    if not hits:
        print(f"No analyzed video matches {query!r}.")
        return
    for i, r in enumerate(hits):
        if i:
            print()
        show_one(r, meta)


if __name__ == "__main__":
    main()
