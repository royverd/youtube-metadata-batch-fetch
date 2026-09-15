"""
render.py

Turns a screening table written as Markdown into the browsable HTML page -
search box, verdict filters, compact mode, one card per video.

The page used to be hand-edited, which meant the numbers in the header, the
counts on the filter chips and the rows in the table could all disagree with
each other and nothing would notice. Here the Markdown is the only source of
truth: the header comes from the document's own preamble, the chip counts are
tallied from the verdicts, and the card ids are the row numbers. Re-running
after an edit is the whole update process.

Expected table columns, in order:

    # | Title - Channel (duration) | Type | Visual dep. | Signal | Summary |
    Goal | For / Not for | Verdict

The verdict cell must open with a bolded **Watch full**, **Read summary** or
**Skip** - that word picks the badge and the filter bucket, so a typo there
silently lands the row in "Read summary".

Usage:
    ytb-render screening.md            -> screening.html
    ytb-render screening.md out.html

Deps: none. Reads the Markdown file and templates/screening.html.
"""

import html
import re
import sys
from pathlib import Path

TEMPLATE = Path(__file__).parent / "templates" / "screening.html"

USAGE = "usage: ytb-render <table.md> [out.html]"

SLUG = {"Watch full": "watch-full", "Read summary": "read-summary", "Skip": "skip"}
BADGE = {v: k for k, v in SLUG.items()}

ROW = re.compile(r"^\| \d+ \|")
# Cells split on unescaped pipes only - titles and summaries carry \| inside them.
CELL = re.compile(r"(?<!\\)\|")
# Bold closes on ** not followed by a third *, so `**a *b***` nests instead of
# swallowing the inner emphasis and leaving a stray asterisk behind.
INLINE = re.compile(r"\*\*(.+?)\*\*(?!\*)|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|`(.+?)`", re.S)

CARD = """<article class="v" data-v="{slug}" id="v{n}">
  <div class="head">
    <a class="num" href="#v{n}">{n}</a>
    <div class="title">
      <h2>{title}</h2>
      <p class="sub">{sub}</p>
    </div>
    <span class="badge">{badge}</span>
  </div>
  <div class="tags"><span>{kind}</span><span>Visual dep. {vis}</span></div>
  <p class="signal">{signal}</p>
  <div class="detail">
    <p class="summary">{summary}</p>
    <div class="grid">
      <section><h3>Goal → Delivery</h3><p>{goal}</p></section>
      <section><h3>For / Not for</h3><p>{aud}</p></section>
      <section><h3>Verdict</h3><p>{verdict}</p></section>
    </div>
  </div>
</article>"""

FILTERS = """<div class="bar">
  <div class="wrap">
    <input type="search" id="q" placeholder="Search title, channel, summary…" autocomplete="off">
    <button class="chip" data-f="watch-full" aria-pressed="false">Watch full · {watch}</button>
    <button class="chip" data-f="read-summary" aria-pressed="false">Read summary · {summary}</button>
    <button class="chip" data-f="skip" aria-pressed="false">Skip · {skip}</button>
    <button class="chip" id="dense" aria-pressed="false">Compact</button>
    <span class="count" id="count">{total} videos</span>
  </div>
</div>
"""


def cells(row):
    return [c.strip().replace(r"\|", "|") for c in CELL.split(row)[1:-1]]


def inline(text):
    """Markdown inline -> HTML, escaping everything else. Recurses through
    emphasis so nested spans survive; code spans stay literal."""
    out, i = [], 0
    for m in INLINE.finditer(text):
        out.append(html.escape(text[i:m.start()], quote=True))
        if m.group(1) is not None:
            out.append("<strong>%s</strong>" % inline(m.group(1)))
        elif m.group(2) is not None:
            out.append("<em>%s</em>" % inline(m.group(2)))
        else:
            out.append("<code>%s</code>" % html.escape(m.group(3), quote=True))
        i = m.end()
    out.append(html.escape(text[i:], quote=True))
    return "".join(out)


def split_title(cell):
    """'Title - Channel (8:27, Greek)' -> title, channel, '8:27, Greek'.

    Channel is whatever follows the last em dash, so em dashes inside a title
    are safe. A row without one keeps the whole cell as the title rather than
    guessing."""
    m = re.search(r"\s*\(([^()]*)\)\s*$", cell)
    meta = m.group(1) if m else ""
    head = cell[:m.start()] if m else cell
    title, _, channel = head.rpartition(" — ")
    if not title:
        title, channel = head, ""
    return title.strip(), channel.strip(), meta.strip()


def verdict_slug(cell):
    m = re.match(r"\*\*(.+?)\*\*", cell)
    return SLUG.get(m.group(1), "read-summary") if m else "read-summary"


def preamble(lines):
    """Everything above the table: the H1 plus the standing caveats. Emitted as
    the page header so the document explains itself without the explanation
    being duplicated here."""
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("|"):
            break
        if line.startswith("# "):
            out.append("<h1>%s</h1>" % inline(line[2:]))
        else:
            out.append("<p>%s</p>" % inline(line))
    return '<header class="wrap">\n%s\n</header>' % "\n".join(out)


def build(md_text):
    lines = md_text.split("\n")
    rows = [l for l in lines if ROW.match(l)]
    if not rows:
        raise SystemExit("No table rows found - expected lines starting '| 1 |'.")

    cards, tally = [], {"watch-full": 0, "read-summary": 0, "skip": 0}
    for row in rows:
        c = cells(row)
        if len(c) < 9:
            raise SystemExit(f"Row {c[0] if c else '?'} has {len(c)} columns, expected 9.")
        num, title_cell, kind, vis, signal, summary, goal, aud, verdict = c[:9]
        title, channel, meta = split_title(title_cell)
        slug = verdict_slug(verdict)
        tally[slug] += 1
        cards.append(CARD.format(
            slug=slug, n=num, badge=BADGE[slug],
            title=inline(title),
            sub=inline(" · ".join(x for x in (channel, meta) if x)),
            kind=inline(kind), vis=inline(vis), signal=inline(signal),
            summary=inline(summary), goal=inline(goal), aud=inline(aud),
            verdict=inline(verdict)))

    page = TEMPLATE.read_text(encoding="utf-8")
    page = page.replace("{{HEADER}}", preamble(lines))
    page = page.replace("{{FILTERS}}", FILTERS.format(
        watch=tally["watch-full"], summary=tally["read-summary"],
        skip=tally["skip"], total=len(rows)))
    page = page.replace("{{CARDS}}", "\n".join(cards))
    return page, len(rows), tally


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        raise SystemExit(USAGE)

    src = Path(args[0])
    if not src.is_file():
        raise SystemExit(f"No such file: {src}")
    out = Path(args[1]) if len(args) > 1 else src.with_suffix(".html")

    page, total, tally = build(src.read_text(encoding="utf-8"))
    out.write_text(page, encoding="utf-8")
    print(f"{out}  {total} videos  "
          f"watch {tally['watch-full']} / read {tally['read-summary']} / skip {tally['skip']}")


if __name__ == "__main__":
    main()
