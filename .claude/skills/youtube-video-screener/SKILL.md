---
name: youtube-video-screener
description: Compress a YouTube video down to its essence from the transcript so the user can decide whether to watch it, read the summary and move on, or skip it. Covers what the video actually is, what it gives you and what it doesn't, and a suggested verdict. Use whenever the user pastes a YouTube transcript or link and wants it summarized, screened, triaged, or asks "is this worth watching" / "should I watch this" / "summarize this video" / "go through my backlog". Handles one video or several in a row; each is judged on its own. Not for non-YouTube content.
---

# YouTube Video Screener

One job: say what the video is, accurately and compressed, so the user can make the call. The verdict is a suggestion at the end, not the point.

Each video is judged standalone. Never rank videos against each other or let one video's quality colour another's write-up, even when several arrive together.

## Inputs

Transcript plus title at minimum. Use channel, description, duration, upload date, view count if given — each helps, none is required. Title only, no transcript: say so, and screen from metadata alone flagged as guesswork.

## What to produce per video

**Takeaway** — the thing the video is actually about and what it concludes, in the plainest words available. First line of the body, never skipped, never folded into the bullets below: if the bullets are the only place the point appears, the takeaway is missing. Not "the video discusses X"; state X.

Length follows content, not a cap. One sentence where one carries it, three or four where the video's point genuinely has that many moving parts — a couple of extra sentences here cost almost nothing to read and can multiply what the reader actually walks away with. Stop at the point where it starts restating the bullets; past there it's a second summary and the scan value is gone.

Write it last, from the finished bullets, even though it's presented first. Deriving the essence and then compressing it into the takeaway gets a takeaway that reflects the whole video; writing it first and then filling in bullets gets one that reflects whatever the intro happened to say.

It appears twice by design: clipped to a phrase in the collapsed line so the file scans, then in full as the body's opening line.

**What it is** — a sentence or two describing the setup, then format and runtime. Who is in it and what they are (host and channel, guest and why they're there), what they're actually doing for the runtime, and the shape it takes. The reader should be able to picture the video from this alone.

A label triplet is not enough. "Project vlog, two hosts, 21:43" tells you nothing; "Linus and a colleague run Ethernet through an entire house on camera, working out room by room what needs a drop and hitting the termination problems as they go — project vlog, 21:43" is the same length and actually describes something.

Judge from the transcript's content, not the title's framing; titles overclaim. Identify people only as far as the transcript, channel, or description supports — no imported reputation or credentials the material doesn't evidence.

**The essence** — the actual content, compressed hard. Usually 3 to 8 bullets. Include only what changes what the reader knows or would do:
- the central claim or conclusion
- the arguments, findings, or steps holding it up
- exact numbers, names, results — never rounded into vagueness
- conditions or caveats that would flip the conclusion if dropped
- anything genuinely novel or counter-intuitive, even in passing
- jokes and asides that carry a real claim. A crack about a company's habitual behaviour, pinned to a specific incident, is an argument delivered as a joke — keep the claim and the incident it rests on, and keep the phrasing where the phrasing is what makes the point land. The test is whether it still says something once stated plainly: if yes it's content, if it evaporates it was a bit, cut it. Keeping it because it was funny is the failure mode; so is stripping it because it was funny.

Cut the rest outright rather than condensing it: intros, outros, sponsor reads, repetition, hedging, meta-talk about the video, examples that add nothing beyond the claim they illustrate. Lead with the conclusion; keep the video's original order only where sequence is load-bearing (a procedure, a proof, a story whose steps depend on each other).

If the transcript is padded, the write-up compresses that padding away instead of mirroring it. If it's thin, garbled, or mostly noise, say that plainly rather than manufacturing something that reads more substantial than the source.

**What you get, what you don't** — two or three lines. What the video actually hands over and at what level (prior knowledge it assumes, depth, tone, patience required), then what it doesn't: promised and absent, implied by the title or thumbnail and absent, or attempted and botched. Close with a grade on the promise: meets / partially meets / falls short / bait.

Don't invent a promise the video never made, and don't list the audiences it isn't for — everyone unnamed is the complement set, so writing it out means inventing specificity the transcript doesn't support. "What you don't get" is anchored to what the video sets up: something the title, thumbnail, or intro promised outright, something a reasonable viewer would expect from that framing, or something it attempted and fell short on. Absences with no such anchor are not gaps — a tutorial that doesn't give you a cannelloni recipe isn't withholding anything.

**What the transcript can't see** — only when it applies, and kept separate from the field above: that one is the video falling short, this one is the transcript not showing it. If the payload lives in visuals (a demo, a chart's actual numbers, on-screen code or results, gameplay), name the specific gap next to the point it affects. Dense deictic language clustering in a section ("look at this", "as you can see") is the tell. Heavy b-roll and reaction cam are not a gap; nothing is lost by not seeing them.

**Verdict** — Watch / Read this and move on / Skip, one line of reasoning.
- Watch when the delivery itself carries value (visuals, pacing, entertainment) or density is high enough that compression loses too much.
- Read and move on when the content is real but transfers fine as text.
- Skip when it's bait, padded to nothing, or too shallow to be worth either.

Then the case against it, in one line: the strongest argument for the next-best option, at full strength. Build it from the same evidence, phrased as someone who'd actually make that call would phrase it, not as a foil built to lose. If the counter-case survives scrutiny better than the verdict did, switch the verdict and say the first read didn't hold.

Only where a real contender exists. On an obvious bait video or an obvious must-watch there is no live alternative, and manufacturing one is padding wearing transparency's coat — say the call is unambiguous and stop.

Don't pick the middle option to avoid committing. If a visual gap is unresolved, "Read and move on" can't be issued confidently: say Watch (or scrub through) or flag the verdict as lower-confidence with the gap named.

## Output

A markdown file in the working directory, one file per screening session. If a file already exists for this session, append the new video to it rather than starting a fresh one, and re-present the same file.

One collapsible block per video, so the file scans top-to-bottom closed and opens only where wanted:

```
<details>
<summary><b>Title — Channel</b> · Verdict · takeaway, clipped to a phrase</summary>

**Takeaway:** in full, as long as the point needs

**What it is:** a sentence or two on who's in it and what they're doing, then format and runtime

**The essence:**
- ...

**What you get, what you don't:** what it hands over and at what level; what its framing set up and didn't deliver → grade

**Transcript can't see:** only if applicable

**Verdict:** action, why; then the case for the next-best option in one line, or a note that the call is unambiguous

</details>
```

The collapsed line has to stand alone: title, verdict, takeaway. That line alone should be enough to decide on most videos.

Chat reply: one line (count and verdict tally) plus the file. Don't restate any of the content in chat.

## Guardrails

- The user decides. Map the video honestly; the write-up must hold up for someone who disagrees with the verdict.
- Channel size and view count are not evidence about the content.
- Don't editorialize or add tone the video didn't have. Reporting a joke's claim is extraction; writing your own is not.
- No fixed length. Two bullets is correct for a two-point video; don't pad toward looking thorough.
- Never smooth away a caveat that changes the conclusion in order to be brief.
