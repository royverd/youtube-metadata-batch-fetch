# youtube-metadata-batch-fetch

Pulls metadata and transcripts for a YouTube playlist, then has an LLM describe each video so you can triage a backlog.

Each stage is resumable and writes its own file. A rate-limit, a crash, or Ctrl-C costs you the item in flight and nothing else. The desktop app (`ytb-gui`) wraps the whole loop - fetch, AI screening, and recording what you actually watched - and opens a short guide on first launch.

Co-authored by Claude

## Install

```bash
git clone <your-remote> youtube-metadata-batch-fetch
cd youtube-metadata-batch-fetch
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

That is the whole install where Python and tkinter are already present. Activate the venv (`source .venv/bin/activate`) in each new shell before running any `ytb-*` command, or call them directly as `.venv/bin/ytb-gui`.

The venv is not optional on most current systems. Installing into the system Python fails with:

```
error: externally-managed-environment
```

That is the OS (Debian/Ubuntu, Homebrew on macOS, Arch, and others) protecting the Python its own tools depend on, per PEP 668. A venv sidesteps it; `--break-system-packages` also silences it, but can break OS tools, so don't.

Per system:

### Windows

Install Python from [python.org](https://www.python.org/downloads/) and tick **Add python.exe to PATH**; the installer includes tkinter by default. Then, in PowerShell:

```powershell
git clone <your-remote> youtube-metadata-batch-fetch
cd youtube-metadata-batch-fetch
py -m pip install -e .
py -m ytbatch.gui
```

If pip warns that its `Scripts` folder is not on PATH, the `ytb-*` commands and `yt-dlp` won't be found; add that folder to PATH, or keep using `py -m ytbatch.<module>` and `py -m yt_dlp`. Subscription screening opens Windows Terminal (standard on Windows 11), or a plain `cmd` window where it isn't installed.

### macOS

Homebrew's Python ships without tkinter and refuses a system-wide `pip install`, so install tkinter alongside it and use a virtual environment:

```bash
brew install python python-tk git
git clone <your-remote> youtube-metadata-batch-fetch
cd youtube-metadata-batch-fetch
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
ytb-gui
```

The python.org installer includes tkinter, if you'd rather use that. Run `source .venv/bin/activate` in each new shell before the `ytb-*` commands. Reading Chrome's cookies triggers a Keychain prompt; allow it. Subscription screening opens Terminal.app, and the first time macOS asks whether the app's Python may control Terminal — allow that too.

### Linux

tkinter is a separate system package, and newer Debian/Ubuntu refuse a system-wide `pip install`, so use a virtual environment there:

```bash
sudo dnf install python3-tkinter            # Fedora
sudo apt install python3-tk python3-venv    # Debian / Ubuntu

git clone <your-remote> youtube-metadata-batch-fetch
cd youtube-metadata-batch-fetch
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
ytb-gui
```

Install `xdg-utils` for default-browser detection. For the colour editor's eyedropper, install PyGObject (`python3-gobject` on Fedora, `python3-gi` on Debian/Ubuntu) and create the venv with `--system-site-packages` so it can see it.

### Arch Linux

Arch's `python` package leaves Tk out, so a missing `tkinter` / `_tkinter` module means the `tk` package isn't installed. System-wide `pip install` is refused, so use a venv:

```bash
sudo pacman -S python tk git
git clone <your-remote> youtube-metadata-batch-fetch
cd youtube-metadata-batch-fetch
python -m venv .venv
source .venv/bin/activate
pip install -e .
ytb-gui
```

A `No module named 'ytbatch'` (or `customtkinter`, `yt_dlp`, `pydantic`) error means the command ran outside the venv: activate it first, or run `.venv/bin/ytb-gui`. Optional: `xdg-utils` for default-browser detection, and `python-gobject` for the eyedropper, with the venv created as `python -m venv --system-site-packages .venv` so it can see it.

### All systems

Editable, and run from the repo root — `data/` and `prompts/` resolve from the working directory, not from wherever pip put the code. `YTBATCH_HOME` overrides that if you want a second corpus against one install.

Add the Anthropic SDK only if you intend to use that provider; every other provider goes over plain `requests`:

```bash
pip install -e ".[anthropic]"
```

Python 3.9+ (`str.removeprefix`), setuptools 77+ to build. Developed and run on 3.14.2 with yt-dlp 2026.7.4, youtube-transcript-api 1.2.4, pydantic 2.13.4, customtkinter 6.0.0.

### Dependencies

- **yt-dlp** comes with the install above, which is the point of listing it — both fetch stages shell out to the binary rather than importing it, so what matters is that it lands on PATH.
- **tkinter**, for `ytb-gui`. It ships with Python on Windows and macOS; on Linux it is a system package (`python3-tkinter` on Fedora, `python3-tk` on Debian/Ubuntu). CustomTkinter, which the app is built on, comes with the install above.
- **A browser you're logged into YouTube on, fully closed.** Stage 1 reads its cookie database directly; a running browser holds a lock on it.
- **For screening, either an AI CLI or an API key.** Subscription mode runs `claude`, `codex`, `gemini` or `opencode` in a new terminal window: Windows Terminal or `cmd` on Windows, Terminal.app on macOS, and on Linux the first of konsole, gnome-terminal, alacritty, kitty, foot or xterm. API mode and stage 3 use a provider from [`src/ytbatch/config.py`](src/ytbatch/config.py), ordered cheapest-first; Ollama and LM Studio run locally with no key.
- **The screener skill** ships with the repo at [`.claude/skills/youtube-video-screener/SKILL.md`](.claude/skills/youtube-video-screener/SKILL.md). It is the screening prompt. The app finds it by walking up from the corpus folder, so a clone uses this copy; point Settings → Screener SKILL.md elsewhere to use your own.
- **PyGObject**, optional and Linux-only, for the colour editor's eyedropper. It reads a screen pixel through the XDG desktop portal, which is the only way to on Wayland. Without it — including on Windows and macOS — the button says so and the rest of the editor works.
- **Cross-platform, with soft edges.** Default-browser detection (option `0` in stage 1, System default in the app) reads the registry on Windows and `xdg-settings` on Linux; on macOS, and on Linux without `xdg-utils`, pick the browser explicitly. The fetch stages were developed on Windows; the app and screening were developed on Fedora KDE (Wayland) and have not been run on Windows or macOS.

## Usage

```bash
ytb-gui                # the desktop app: fetch, screen, review, settings
ytb-fetch              # stages 1+2: playlist -> data/metadata.json + data/metadata.csv
ytb-analyze            # optional stage 3, CLI only: transcripts -> data/analysis.db
ytb-show               # optional stage 4, CLI only: read what stage 3 wrote
ytb-render             # a hand-written screening table -> a browsable HTML page
ytb-render-screening   # the screener skill's markdown -> a browsable HTML page
ytb-watchlist          # stage 1 alone
```

Every one of those is also reachable as `python -m ytbatch.<module>` (`ytbatch.gui`, `ytbatch.batch_fetch`, `ytbatch.analyze`, `ytbatch.show`, `ytbatch.render`, `ytbatch.render_screening`, `ytbatch.grab_watchlist`) if you'd rather not depend on the console scripts being on PATH. Running a module by file path does not work — the package-relative imports need the package name to exist.

### ytb-gui

A sidebar with four pages and a log drawer shared by all of them. The **?** at the bottom of the sidebar reopens the guide.

| Page | What it does |
|---|---|
| Fetch | Stages 1 and 2: pick a browser and playlist, choose transcript routing, run. Stop lands mid-delay rather than after the current wait. |
| Screen | Has an AI screen the next N unscreened videos against the screener skill. `0` means everything left. Shows screened/left/verdict counts, edits the skill, renders the results to HTML. |
| Review | One row per screened video. Record what you did (watched in full, scrubbed, read the summary, skipped) and a 1-10 rating to one decimal, for one row or a multi-selection. Double-click opens the video. |
| Settings | Paths, the screening AI, appearance. Each panel has Reset to defaults, plus one reset for everything. |

Screening runs one of two ways, switched in Settings → Screening AI:

- **Subscription** opens an interactive CLI session in a terminal, working in `Untracked/`. The batch is cut out of `metadata.json` into small files in `Untracked/screen_batches/` first, so the model never opens the 20 MB corpus, and the session is pointed at a `PROMPT.md` written beside them. With `0` it works through the whole backlog until the usage limit stops it. The app re-reads the output every 5 seconds and records verdicts as they land — until the window closes on Linux, and until every requested video has a verdict on Windows and macOS, where the app can't tell when the window closes.
- **API** sends one request per batch to the chosen provider and appends the result itself. It stops at the first fatal error — a rejected key, a missing model, or a spent quota — keeping everything already recorded.

Appearance changes apply as you make them: light/dark, a colour editor with a picker, an eyedropper and presets (Nord, Dracula, Gruvbox, Solarized, Catppuccin, Discord, OBS and others, plus your own), font family and size.

### ytb-fetch

`ytb-fetch` runs the watchlist grab first, every time — it prompts for a browser and a playlist URL (`0` for Watch Later), then for how transcript requests get routed:

| Mode | Route |
|---|---|
| `0` | Origin IP first, then `data/proxies.txt` plus an auto-refreshed pool of free public proxies. Default. |
| `1` | Origin IP first, then a Webshare account (needs the env vars below). |
| `2` | Origin IP only, spaced out per the delay tiers. No fallback. |

To skip the watchlist grab and run the fetch alone, call it directly:

```bash
python -c "from ytbatch import batch_fetch; batch_fetch.run_pipeline('0')"
```

### ytb-analyze and ytb-show

Optional, and command-line only - the app does not use them. They are a separate route from the app's AI screening: stage 3 writes a structured description per video into `data/analysis.db`, while screening writes verdicts to the screening markdown and `data/progress.json`. Neither needs the other. A full stage 3 run against a live provider has not been exercised recently; `ytb-show` on an empty database just says to run `ytb-analyze` first.

Stage 3 takes two flags:

| Flag | Effect |
|---|---|
| `--config` | Re-prompt for provider, key, model and effort. Asks the provider what it actually serves rather than trusting the hardcoded list. |
| `--models` | Print the live model catalogue for the current key and exit. |

Stage 4 takes a video ID, or any substring of a title or subject:

```bash
ytb-show                 # one line per analyzed video, densest first
ytb-show 7xTGNNLPyMI     # that video, in full
ytb-show transformer     # every video whose title or subject matches
```

```
1 analyzed. Pass an id or part of a title to see one in full.

  7xTGNNLPyMI  444424     0m  How Large Language Models like ChatGPT are bui

  axes order: dept brea rigo sour prer dens
```

The six digits are the axis ratings from [`prompts/describe_v1.txt`](prompts/describe_v1.txt): depth, breadth, rigor, sourcing, prerequisites, density, each 1-5. Full view adds the description, who it's for, who it isn't for, a padding estimate, and one verbatim transcript quote per axis — the quotes exist so a rating can be checked against the video instead of taken on faith.

### ytb-render and ytb-render-screening

Two renderers for two screening formats, same page style: search, verdict filters and a compact mode.

```bash
ytb-render screening.md                  # a nine-column table you wrote -> screening.html
ytb-render-screening                     # the configured screening markdown -> .html beside it
ytb-render-screening screening.md out.html
```

`ytb-render` takes a Markdown table, one row per video. Everything above the table becomes
the page header, the chip counts are tallied from the verdict column, and card
ids are the row numbers — so editing a row and re-running is the whole update
process. Columns, in order:

| # | Title — Channel (duration) | Type | Visual dep. | Signal | Summary | Goal | For / Not for | Verdict |

The verdict cell has to open with a bolded `**Watch full**`, `**Read summary**`
or `**Skip**`; that word picks the badge and the filter bucket, and anything
else lands the row in "Read summary" without complaining. A row with the wrong
number of columns is an error rather than a mangled card — pipes inside a cell
need escaping as `\|`.

`ytb-render-screening` takes what the screener skill writes instead: one `<details>` block per video, each opening with `<!-- n: 25 --><!-- id: VIDEO_ID -->` so it can be matched back to the corpus. It is what the app's Render + open button runs.

## Configuration

`data/config.json` holds the provider choice, per-provider keys, per-provider model, effort level, and the app's paths and screening choices. Keys are stored per provider, so switching back and forth doesn't mean re-pasting. It is gitignored, and nothing ever prints a key in full. Blank paths mean "detect at startup", so a moved binary is re-found rather than remembered wrong.

Precedence: the provider's env var beats the stored key, but a key typed at the prompt beats both and is what gets saved. An env-supplied key is never written to disk.

`~/.config/ytbatch/user_settings.json` holds per-machine app state — window size, theme, colours and presets, font, table layout — kept outside the corpus so it survives pointing the app at a different one. `~/.config/ytbatch/home` records the corpus folder chosen in Settings.

| Variable | Used by |
|---|---|
| `YTBATCH_HOME` | All stages. Directory holding `data/` and `prompts/`. Beats the folder chosen in Settings; defaults to the working directory |
| `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, `GROQ_API_KEY`, `XAI_API_KEY`, `CEREBRAS_API_KEY`, `ANTHROPIC_API_KEY` | stage 3 and API screening, one per provider |
| `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` | the same; Cloudflare's endpoint is account-scoped |
| `WEBSHARE_PROXY_USERNAME` + `WEBSHARE_PROXY_PASSWORD` | stage 2, routing mode 1 |

`data/proxies.txt` (optional, gitignored) is a hand-edited list for mode 0 — one `scheme://[user:pass@]host:port` per line, `#` comments ignored. Manual entries are always tried, on the assumption you added them deliberately.

## How it works

**Resume is per-item, not per-run.** Stage 2 keys `data/metadata.json` by video ID and only fetches IDs it has never seen; videos that leave the watchlist stay in it, flagged as archived. Transcripts marked `disabled` or `none` are permanent and never retried; `error` is transient and retries next run. Stage 3 keys `data/analysis.db` on four columns — transcript hash, prompt file hash, `provider:model`, effort — and re-analyzes exactly the videos whose key moved. Screening is recorded per video id in `data/progress.json`, and only ids that actually appear in the output get marked, so a run that dies halfway never flags a video as screened.

**The origin IP is treated differently from proxies.** youtube-transcript-api scrapes YouTube directly, so heavy use gets the IP blocked for hours to a day. The origin IP gets spaced requests (3-10s, scaled to batch size, jittered) and tolerates a few transient errors before reacting. Proxies get neither: no delay, and any single failure rotates immediately, because a mostly-dead free pool isn't worth protecting and burning three real videos on a dead proxy is worse than skipping it. Outcomes persist to `data/free_proxies.json` with the latency of the successful fetch, so the next run tries known-good fastest-first and never re-tries a proven-dead one.

**The output shape is pinned in one place.** [`schema.py`](src/ytbatch/schema.py) defines it; Anthropic gets the generated JSON Schema, Gemini gets a flattened version its OpenAPI subset accepts, OpenAI-compatible providers get it pasted into the system prompt. Every response is validated back through Pydantic, so a provider that honours the schema loosely fails on that video instead of writing half-populated rows.

**Adding a provider is a config edit.** Anything speaking the OpenAI chat-completions shape is picked up from its `base_url` with no new code — only Anthropic and Gemini have their own adapters in [`providers.py`](src/ytbatch/providers.py). Ollama and LM Studio are two such entries pointing at localhost.

**The screening prompt is a file, not code.** The screener skill is read fresh on every run, so editing it - by hand or with Edit skill in the app - changes the output with nothing in the Python to update.

**Paths resolve from one module.** [`paths.py`](src/ytbatch/paths.py) is the only place that decides where anything lives. Before the package layout every module pinned its own directory off `__file__`, which stops working the moment pip can move the code somewhere you'd never want your transcripts written.

## Layout

```
src/ytbatch/            the package - stages, provider adapters, schema, store, paths, the app
src/ytbatch/templates/  the screening page shell the renderers fill in
prompts/                describe_v1.txt; its content hash is the analysis cache key
data/                   everything generated, plus the corpus itself
Untracked/              screening working area (gitignored)
archive/                the pre-metadata transcript fetcher, kept for reference
```

| File | Written by | Contents |
|---|---|---|
| `data/watchlist.txt` | stage 1 | One video ID per line |
| `data/metadata.json` | stage 2 | Full record per video, transcript text included |
| `data/metadata.csv` | stage 2 | Same metadata columns plus transcript status and segment count — never the text |
| `data/failures.csv` | stage 2 | IDs yt-dlp couldn't extract, with its actual reason and an oEmbed-recovered title where one exists |
| `data/analysis.db` | stage 3 | SQLite, one row per described video |
| `data/progress.json` | the app | AI verdict per screened video, plus what you did with it and your rating |
| `data/config.json` | stage 3, the app | Provider, keys, models, effort, app paths and screening choices. Gitignored |
| `data/free_proxies.json` | stage 2 | Proxy pool with per-proxy status and measured latency. Gitignored |
| `data/proxies.txt` | you | Optional manual proxy list. Gitignored |
| `Untracked/screening.md` | the screening AI | One `<details>` block per video; `data/screening.md` if `Untracked/` has none |
| `Untracked/screen_batches/` | the app | Per-batch slices of the corpus, a copy of the skill and the session's `PROMPT.md`, rewritten each run |

## Limitations

- **Run it from the repo root** unless `YTBATCH_HOME` is set or a corpus folder is chosen in Settings. The console scripts work from anywhere, but "anywhere" is where `data/` gets created.
- **The metadata fetch has no proxy support.** Only transcripts route through the pool. When yt-dlp hits the bot gate it fails, and those videos land in `data/failures.csv` with the reason — 5 of 67 in the last run, all `Sign in to confirm you're not a bot`. Passing cookies to that call would fix it and currently isn't wired up.
- **Free proxies are mostly dead.** The pool is public lists; a run can walk dozens of entries without one answering. Mode 1 or 2 is the reliable path.
- **A block lasts hours to a day.** Stage 2 stops rather than grinding through it — waiting it out mid-run doesn't work. Re-run after it clears, or switch modes.
- **Only the claude CLI has been run for screening, and only on Linux.** The codex, gemini and opencode flags, and the Windows Terminal, `cmd` and Terminal.app launches, are built from each tool's documented syntax and have not been exercised.
- **Local models truncate silently.** Ollama's default context is far shorter than a batch of transcripts. Raise it (`OLLAMA_CONTEXT_LENGTH`, or the context setting in LM Studio) and lower Videos per batch, or the verdicts are made on cut transcripts with no error.
- **Videos without a usable transcript are skipped entirely** by stage 3. Whether they should get a degraded description from metadata alone is still open.
- **Length verdicts and padding estimates come from the transcript only.** No frames are analyzed, so the `on_screen` field is the model's guess at how much substance it can't see.
- **The Review table is a ttk widget**, since CustomTkinter has no table; it is restyled to match but lacks rounded corners and hover.
- **No tests and no CI.** The verification story is the per-axis transcript quotes in the output, not a suite.
- [`archive/transcript_batch_fetch.py`](archive/transcript_batch_fetch.py) is the pre-metadata version, kept for reference. It is not part of the package and does not participate in the pipeline.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 royverd.
