# youtube-metadata-batch-fetch

Pulls metadata and transcripts for a YouTube playlist, then has an LLM describe each video so you can triage a backlog.

Four stages, each one resumable and each writing its own file. A rate-limit, a crash, or Ctrl-C costs you the item in flight and nothing else.

## Install

```bash
git clone <your-remote> youtube-metadata-batch-fetch
cd youtube-metadata-batch-fetch
pip install -e .
```

Editable, and run from the repo root — `data/` and `prompts/` resolve from the working directory, not from wherever pip put the code. `YTBATCH_HOME` overrides that if you want a second corpus against one install.

Add the Anthropic SDK only if you intend to use that provider; every other provider goes over plain `requests`:

```bash
pip install -e ".[anthropic]"
```

Python 3.9+ (`str.removeprefix`), setuptools 77+ to build. Developed and run on 3.14.2 with yt-dlp 2026.7.4, youtube-transcript-api 1.2.4, pydantic 2.13.4.

### Dependencies

- **yt-dlp** comes with the install above, which is the point of listing it — both fetch stages shell out to the binary rather than importing it, so what matters is that it lands on PATH.
- **A browser you're logged into YouTube on, fully closed.** Stage 1 reads its cookie database directly; a running browser holds a lock on it.
- **An API key** for stage 3. The provider list in [`src/ytbatch/config.py`](src/ytbatch/config.py) is ordered cheapest-first and the top three are free tiers (NVIDIA NIM, Cloudflare Workers AI, OpenRouter `:free`).
- **Windows.** [`grab_watchlist.py`](src/ytbatch/grab_watchlist.py) imports `winreg` at module scope to detect the default browser, and both [`batch_fetch.py`](src/ytbatch/batch_fetch.py) and [`gui.py`](src/ytbatch/gui.py) import it. Stages 3 and 4 are portable; stages 1 and 2 are not.

## Usage

```bash
ytb-fetch      # stages 1+2: playlist -> data/metadata.json + data/metadata.csv
ytb-analyze    # stage 3: transcripts -> data/analysis.db
ytb-show       # stage 4: read what stage 3 wrote
ytb-gui        # stages 1+2 with a window instead of prompts
ytb-watchlist  # stage 1 alone
```

Every one of those is also reachable as `python -m ytbatch.<module>` (`ytbatch.batch_fetch`, `ytbatch.analyze`, `ytbatch.show`, `ytbatch.gui`, `ytbatch.grab_watchlist`) if you'd rather not depend on the console scripts being on PATH. Running a module by file path does not work — the package-relative imports need the package name to exist.

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

`ytb-gui` covers stages 1 and 2 with a Stop button that lands mid-delay rather than after the current wait.

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

## Configuration

`data/config.json` is written on first run of stage 3 and holds the provider choice, per-provider keys, per-provider model, and effort level. Keys are stored per provider, so switching back and forth doesn't mean re-pasting. It is gitignored, and nothing ever prints a key in full.

Precedence: the provider's env var beats the stored key, but a key typed at the prompt beats both and is what gets saved. An env-supplied key is never written to disk.

| Variable | Used by |
|---|---|
| `YTBATCH_HOME` | All stages. Directory holding `data/` and `prompts/`. Defaults to the working directory |
| `NVIDIA_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, `GROQ_API_KEY`, `XAI_API_KEY`, `CEREBRAS_API_KEY`, `ANTHROPIC_API_KEY` | stage 3, one per provider |
| `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` | stage 3; Cloudflare's endpoint is account-scoped |
| `WEBSHARE_PROXY_USERNAME` + `WEBSHARE_PROXY_PASSWORD` | stage 2, routing mode 1 |

`data/proxies.txt` (optional, gitignored) is a hand-edited list for mode 0 — one `scheme://[user:pass@]host:port` per line, `#` comments ignored. Manual entries are always tried, on the assumption you added them deliberately.

## How it works

**Resume is per-item, not per-run.** Stage 2 keys `data/metadata.json` by video ID and only fetches IDs it has never seen. Transcripts marked `disabled` or `none` are permanent and never retried; `error` is transient and retries next run. Stage 3 keys `data/analysis.db` on four columns — transcript hash, prompt file hash, `provider:model`, effort — and re-analyzes exactly the videos whose key moved. Editing the prompt re-analyzes all of them; editing one transcript re-analyzes one. There is no version number to remember to bump.

**The origin IP is treated differently from proxies.** youtube-transcript-api scrapes YouTube directly, so heavy use gets the IP blocked for hours to a day. The origin IP gets spaced requests (3-10s, scaled to batch size, jittered) and tolerates a few transient errors before reacting. Proxies get neither: no delay, and any single failure rotates immediately, because a mostly-dead free pool isn't worth protecting and burning three real videos on a dead proxy is worse than skipping it. Outcomes persist to `data/free_proxies.json` with the latency of the successful fetch, so the next run tries known-good fastest-first and never re-tries a proven-dead one.

**The output shape is pinned in one place.** [`schema.py`](src/ytbatch/schema.py) defines it; Anthropic gets the generated JSON Schema, Gemini gets a flattened version its OpenAPI subset accepts, OpenAI-compatible providers get it pasted into the system prompt. Every response is validated back through Pydantic, so a provider that honours the schema loosely fails on that video instead of writing half-populated rows.

**Adding a provider is a config edit.** Anything speaking the OpenAI chat-completions shape is picked up from its `base_url` with no new code — only Anthropic and Gemini have their own adapters in [`providers.py`](src/ytbatch/providers.py).

**Paths resolve from one module.** [`paths.py`](src/ytbatch/paths.py) is the only place that decides where anything lives. Before the package layout every module pinned its own directory off `__file__`, which stops working the moment pip can move the code somewhere you'd never want your transcripts written.

## Layout

```
src/ytbatch/     the package - stages, provider adapters, schema, store, paths
prompts/         describe_v1.txt; its content hash is the analysis cache key
data/            everything generated, plus the corpus itself
archive/         the pre-metadata transcript fetcher, kept for reference
```

| File | Written by | Contents |
|---|---|---|
| `data/watchlist.txt` | stage 1 | One video ID per line |
| `data/metadata.json` | stage 2 | Full record per video, transcript text included |
| `data/metadata.csv` | stage 2 | Same metadata columns plus transcript status and segment count — never the text |
| `data/failures.csv` | stage 2 | IDs yt-dlp couldn't extract, with its actual reason and an oEmbed-recovered title where one exists |
| `data/analysis.db` | stage 3 | SQLite, one row per described video |
| `data/config.json` | stage 3 | Provider, keys, models, effort. Gitignored |
| `data/free_proxies.json` | stage 2 | Proxy pool with per-proxy status and measured latency. Gitignored |
| `data/proxies.txt` | you | Optional manual proxy list. Gitignored |

## Limitations

- **Windows only** for stages 1 and 2, as above.
- **Run it from the repo root** unless `YTBATCH_HOME` is set. The console scripts work from anywhere, but "anywhere" is where `data/` gets created.
- **The metadata fetch has no proxy support.** Only transcripts route through the pool. When yt-dlp hits the bot gate it fails, and those videos land in `data/failures.csv` with the reason — 5 of 67 in the last run, all `Sign in to confirm you're not a bot`. Passing cookies to that call would fix it and currently isn't wired up.
- **Free proxies are mostly dead.** The pool is public lists; a run can walk dozens of entries without one answering. Mode 1 or 2 is the reliable path.
- **A block lasts hours to a day.** Stage 2 stops rather than grinding through it — waiting it out mid-run doesn't work. Re-run after it clears, or switch modes.
- **Videos without a usable transcript are skipped entirely** by stage 3. Whether they should get a degraded description from metadata alone is still open.
- **Length verdicts and padding estimates come from the transcript only.** No frames are analyzed, so the `on_screen` field is the model's guess at how much substance it can't see.
- **No tests and no CI.** The verification story is the per-axis transcript quotes in the output, not a suite.
- [`archive/transcript_batch_fetch.py`](archive/transcript_batch_fetch.py) is the pre-metadata version, kept for reference. It is not part of the package and does not participate in the pipeline.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 royverd.
