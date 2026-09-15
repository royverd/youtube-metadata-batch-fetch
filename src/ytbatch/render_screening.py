"""
render_screening.py

Renders a youtube-video-screener markdown file - the one with one <details>
block per video - into a standalone HTML page with search and verdict filters.

ytb-render can't do this one: it parses the nine-column screening table, and the
screener skill emits collapsed blocks instead. Same palette, different input.

Usage:
    ytb-render-screening                       -> the configured screening.md
    ytb-render-screening screening.md          -> screening.html
    ytb-render-screening screening.md out.html

Deps: none.
"""

import html
import re
import sys
from pathlib import Path

USAGE = "usage: ytb-render-screening [screening.md] [out.html]"

SLUG = {"Watch": "watch", "Read this and move on": "read", "Skip": "skip"}
_LEADING = {"watch": "watch", "read": "read", "skip": "skip"}


def verdict_slug(verdict):
    """Verdict text -> 'watch' / 'read' / 'skip', or None.

    The skill's exact words first, then the leading word alone: models add
    qualifiers - 'Watch (by chapter)', 'Watch (if you use Godot)', 'Skip -
    unless ...' - that don't change which bucket a video belongs in, and an
    exact match left those unrecorded and stopped the render. Only the first
    word decides, so 'Skip reading this' can never land in read."""
    text = (verdict or "").strip()
    if text in SLUG:
        return SLUG[text]
    # Emphasis removed everywhere, not just at the ends: '**Read** and move
    # on' otherwise yields 'read**' as its first word.
    plain = re.sub(r"[*_`]", "", text).strip().lower()
    first = re.split(r"[\s(\[\-–—:,;/.]+", plain, maxsplit=1)[0]
    return _LEADING.get(first)

BLOCK = re.compile(r"<details>\s*(.*?)\s*</details>", re.S)
SUMMARY = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.S)
# Title, channel and the rest of the summary line are split on the middot the
# skill's template uses; an em dash inside a title is safe, a middot isn't.
TITLE = re.compile(r"<b>(.*?)</b>", re.S)
INLINE = re.compile(r"\*\*(.+?)\*\*(?!\*)|(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)|`(.+?)`", re.S)
FIELD = re.compile(r"^\*\*(.+?):\*\*\s*(.*)$")
# Card numbers track the corpus, not the file - a batch starting at video 25
# should say 25. Declared once at the top of the markdown, defaults to 1.
START = re.compile(r"<!--\s*start:\s*(\d+)\s*-->")
# Corpus order is not stable between fetches, so a block can carry its own
# number. Falls back to counting from `start:` when absent.
NUM = re.compile(r"<!--\s*n:\s*([^>]{1,8}?)\s*-->")
# The video id, written by screen.py's prompt. Not rendered - it is how a
# block is tied back to metadata.json and progress.json, since titles drift
# and corpus positions do not survive a refetch.
VID = re.compile(r"<!--\s*id:\s*([A-Za-z0-9_-]{6,20})\s*-->")


def inline(text):
    """Markdown inline -> HTML, escaping the rest. Recurses so nesting survives."""
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


def split_summary(raw):
    """'<b>Title - Channel</b> · Verdict · gist' -> those three, plus channel."""
    m = TITLE.search(raw)
    head = m.group(1) if m else raw
    rest = [p.strip() for p in raw[m.end():].split("·") if p.strip()] if m else []
    title, _, channel = head.rpartition(" — ")
    if not title:
        title, channel = head, ""
    verdict = rest[0] if rest else ""
    gist = " · ".join(rest[1:])
    return title.strip(), channel.strip(), verdict, gist


def body(lines):
    """Field lines and bullet lists -> HTML. Anything else passes as a paragraph."""
    out, bullets = [], []

    def flush():
        if bullets:
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % b for b in bullets))
            bullets.clear()

    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("- "):
            bullets.append(inline(line[2:]))
            continue
        flush()
        m = FIELD.match(line)
        if m:
            label, value = m.group(1), m.group(2)
            out.append('<p class="f"><span class="l">%s</span>%s</p>'
                       % (inline(label), inline(value)))
        else:
            out.append("<p>%s</p>" % inline(line))
    flush()
    return "\n".join(out)


def preamble(text):
    """Everything before the first block: the H1 and any standing caveats."""
    out = []
    for line in text.split("<details>")[0].split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("<!--"):
            continue
        if line.startswith("# "):
            out.append("<h1>%s</h1>" % inline(line[2:]))
        else:
            out.append("<p>%s</p>" % inline(line))
    return "\n".join(out)


def corpus_positions():
    """{id: current watchlist position}, "—" for archived. None when the
    corpus can't be read, so the render still works off the stored numbers."""
    from . import screen
    try:
        return {rec["id"]: pos or "—" for pos, rec in screen.load_corpus()}
    except (OSError, ValueError):
        return None


def build(md_text, positions=None):
    """positions: {id: number} from corpus_positions(). A block's stored n:
    is only what its position was when it was screened, so any block carrying
    an id is renumbered from the live list; the stored n: is the fallback."""
    blocks = BLOCK.findall(md_text)
    if not blocks:
        raise SystemExit("No <details> blocks found.")

    m0 = START.search(md_text)
    first = int(m0.group(1)) if m0 else 1

    cards, anchors, tally = [], set(), {"watch": 0, "read": 0, "skip": 0}
    for n, block in enumerate(blocks, first):
        mn = NUM.search(block)
        if mn:
            n = mn.group(1)
        mv = VID.search(block)
        if positions is not None and mv:
            # Not in metadata.json at all reads the same as archived.
            n = positions.get(mv.group(1), "—")
        m = SUMMARY.search(block)
        if not m:
            raise SystemExit(f"Block {n} has no <summary> line.")
        title, channel, verdict, gist = split_summary(m.group(1))
        slug = verdict_slug(verdict)
        if slug is None:
            raise SystemExit(f"Block {n}: unknown verdict {verdict!r}. "
                             f"Expected one starting with Watch, Read or Skip.")
        tally[slug] += 1
        # Removed entries share the "—" label, so anchors fall back to an index.
        # A video screened twice gets the same number on both cards.
        anchor = n if str(n).isdigit() and n not in anchors else f"x{len(cards) + 1}"
        anchors.add(anchor)
        cards.append((n, CARD.format(
            slug=slug, n=n, anchor=anchor, verdict=html.escape(verdict),
            title=inline(title), channel=inline(channel), gist=inline(gist),
            body=body(block[m.end():].split("\n")))))
        print(f"  {str(n):>3}  {verdict:<20}  {title[:58]}")

    # Cards sort by corpus position so the page reads in watch order; entries
    # whose video has left the corpus keep their write-up and fall to the end.
    cards.sort(key=lambda c: (0, int(c[0])) if str(c[0]).isdigit() else (1, 0))
    cards = [c[1] for c in cards]

    return PAGE.format(
        header=preamble(md_text), cards="\n".join(cards),
        watch=tally["watch"], read=tally["read"], skip=tally["skip"],
        total=len(blocks)), len(blocks), tally


CARD = """<details class="v" data-v="{slug}" id="v{anchor}">
  <summary>
    <a class="num" href="#v{anchor}">{n}</a>
    <span class="head">
      <span class="title">{title}</span>
      <span class="sub">{channel}</span>
      <span class="gist">{gist}</span>
    </span>
    <span class="badge">{verdict}</span>
  </summary>
  <div class="detail">
{body}
  </div>
</details>"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Watchlist Screening</title>
<style>
  :root {{
    --bg: #fbfaf8; --fg: #1c1b19; --muted: #6f6b64; --line: #e4e0d9;
    --card: #ffffff; --shadow: 0 1px 2px rgba(0,0,0,.05);
    --watch: #1c6b3f; --watch-bg: #e6f3ea;
    --read: #26557f; --read-bg: #e5eef7;
    --skip: #8a3826; --skip-bg: #f7e9e5;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg: #141317; --fg: #e9e6e0; --muted: #9b968d; --line: #2c2a31;
      --card: #1c1b21; --shadow: none;
      --watch: #77cd96; --watch-bg: #16301f;
      --read: #8ab9e6; --read-bg: #16283a;
      --skip: #e79680; --skip-bg: #35201a;
    }}
  }}
  * {{ box-sizing: border-box; }}
  html {{ scroll-padding-top: 5.5rem; }}
  body {{
    margin: 0; background: var(--bg); color: var(--fg);
    font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
    overflow-wrap: anywhere;
  }}
  .wrap {{ max-width: 58rem; margin: 0 auto; padding: 0 1.25rem; }}
  header {{ padding: 2.5rem 0 1.25rem; }}
  h1 {{ font-size: clamp(1.4rem, 4vw, 2rem); margin: 0 0 1rem; letter-spacing: -.02em; }}
  header p {{ color: var(--muted); font-size: .9rem; margin: .6rem 0; max-width: 46rem; }}

  .bar {{
    position: sticky; top: 0; z-index: 20; background: var(--bg);
    border-bottom: 1px solid var(--line); padding: .65rem 0;
  }}
  .bar .wrap {{ display: flex; flex-wrap: wrap; gap: .45rem; align-items: center; }}
  input[type=search] {{
    flex: 1 1 100%; padding: .55rem .75rem; font-size: .92rem;
    border: 1px solid var(--line); border-radius: 8px;
    background: var(--card); color: var(--fg);
  }}
  .chip {{
    flex: 0 1 auto; padding: .4rem .75rem; font-size: .8rem; cursor: pointer;
    border: 1px solid var(--line); border-radius: 999px;
    background: var(--card); color: var(--muted); font-family: inherit;
  }}
  .chip[aria-pressed="true"] {{ background: var(--fg); color: var(--bg); border-color: var(--fg); }}
  .count {{ margin-left: auto; color: var(--muted); font-size: .8rem; }}

  main {{ padding: 1.25rem 0 5rem; }}
  .v {{
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 1rem 1.2rem; margin: 0 0 .7rem; box-shadow: var(--shadow);
  }}
  .v[hidden] {{ display: none; }}
  .v > summary {{
    display: flex; gap: .8rem; align-items: flex-start;
    cursor: pointer; list-style: none;
  }}
  .v > summary::-webkit-details-marker {{ display: none; }}
  .num {{ text-decoration: none;
    color: var(--muted); font-size: .8rem; font-variant-numeric: tabular-nums;
    padding-top: .15rem; min-width: 1.4rem;
  }}
  .head {{ flex: 1; min-width: 0; }}
  .title {{ font-weight: 600; display: block; }}
  .sub {{ color: var(--muted); font-size: .85rem; display: block; }}
  .gist {{ display: block; margin-top: .4rem; font-size: .93rem; }}
  .badge {{
    flex: 0 0 auto; font-size: .72rem; padding: .22rem .6rem; border-radius: 999px;
    white-space: nowrap; font-weight: 600;
  }}
  [data-v="watch"] .badge {{ color: var(--watch); background: var(--watch-bg); }}
  [data-v="read"] .badge {{ color: var(--read); background: var(--read-bg); }}
  [data-v="skip"] .badge {{ color: var(--skip); background: var(--skip-bg); }}

  .detail {{ border-top: 1px solid var(--line); margin-top: .9rem; padding-top: .9rem; }}
  .detail p {{ margin: .55rem 0; }}
  .detail ul {{ margin: .5rem 0 .9rem; padding-left: 1.1rem; }}
  .detail li {{ margin: .35rem 0; }}
  .l {{ color: var(--muted); font-size: .78rem; text-transform: uppercase;
        letter-spacing: .04em; display: block; }}
  code {{ font-size: .88em; background: var(--bg); padding: .1em .3em; border-radius: 4px; }}
</style>
</head>
<body>
<header class="wrap">
{header}
</header>

<div class="bar">
  <div class="wrap">
    <input type="search" id="q" placeholder="Search title, channel, text…" autocomplete="off">
    <button class="chip" data-f="watch" aria-pressed="false">Watch · {watch}</button>
    <button class="chip" data-f="read" aria-pressed="false">Read · {read}</button>
    <button class="chip" data-f="skip" aria-pressed="false">Skip · {skip}</button>
    <button class="chip" id="expand" aria-pressed="false">Expand all</button>
    <span class="count" id="count">{total} videos</span>
  </div>
</div>

<main class="wrap">
{cards}
</main>

<script>
  const cards = [...document.querySelectorAll('.v')];
  const q = document.getElementById('q');
  const count = document.getElementById('count');
  const chips = [...document.querySelectorAll('.chip[data-f]')];
  let active = new Set();

  function apply() {{
    const term = q.value.trim().toLowerCase();
    let shown = 0;
    for (const c of cards) {{
      const okF = !active.size || active.has(c.dataset.v);
      const okQ = !term || c.textContent.toLowerCase().includes(term);
      c.hidden = !(okF && okQ);
      if (!c.hidden) shown++;
    }}
    count.textContent = shown + (shown === cards.length ? ' videos' : ' of ' + cards.length);
  }}

  q.addEventListener('input', apply);
  for (const chip of chips) {{
    chip.addEventListener('click', () => {{
      const f = chip.dataset.f;
      active.has(f) ? active.delete(f) : active.add(f);
      chip.setAttribute('aria-pressed', active.has(f));
      apply();
    }});
  }}
  const expand = document.getElementById('expand');
  expand.addEventListener('click', () => {{
    const open = expand.getAttribute('aria-pressed') !== 'true';
    expand.setAttribute('aria-pressed', open);
    expand.textContent = open ? 'Collapse all' : 'Expand all';
    for (const c of cards) c.open = open;
  }});
</script>
</body>
</html>
"""


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        src = Path(args[0])
    else:
        # No argument means "the file the app is configured to append to",
        # which is the normal case when this runs from the GUI button.
        from . import config, screen
        src = Path(screen.screening_path(config.load()))
    if not src.is_file():
        raise SystemExit(f"No such file: {src}")
    out = Path(args[1]) if len(args) > 1 else src.with_suffix(".html")

    print(f"render_screening - {src}\n")
    page, total, tally = build(src.read_text(encoding="utf-8"), corpus_positions())
    out.write_text(page, encoding="utf-8")
    print(f"\n{out}  {total} videos  "
          f"watch {tally['watch']} / read {tally['read']} / skip {tally['skip']}")
    return str(out)


if __name__ == "__main__":
    main()
