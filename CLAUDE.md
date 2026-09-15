# youtube-metadata-batch-fetch

Project notes for Claude Code. The global ruleset in ~/.claude/CLAUDE.md still applies; this file only adds what is specific to this repo. What the code does and why is in README.md and the git log - read those first, don't restate them here.

## Data is irreplaceable - treat it that way

- `data/` is gitignored and is the only copy of the corpus. `data/progress.json` (verdicts, decisions, ratings) and the screening markdown (`Untracked/screening.md`, token-expensive AI write-ups) cannot be rebuilt at all; transcripts in `data/metadata.json` can only be re-fetched slowly and get the IP blocked.
- Never modify, move or delete anything in `data/`, `Untracked/` or `~/.config/ytbatch/` without asking first, and back the file up (`cp -p` to a timestamped name) before any write.
- `data/metadata.journal.jsonl` is append-only by construction: `append_journal()` is its only writer and opens it with `"a"`. Never add another writer or open it with `"w"`. `write_json()` refuses to drop ids from `metadata.json`; don't bypass that with `allow_shrink`.
- `data/backups/` holds read-only copies made by `backups.py`. The program never deletes them except through the user's Clear old copies button.
- History: on 2026-09-15 a fetch treated an unreadable `metadata.json` as empty and overwrote 648 records with 3. Root cause of the read failure was never found. The journal, shrink guard and backups exist because of it.

## Working rules

- Test against a scratch corpus: `YTBATCH_HOME=<scratchpad dir>` with its own `data/`. Assert `paths.home()` points there before writing anything.
- GUI tests read the real `~/.config/ytbatch/user_settings.json`; copy it aside first and restore it afterwards.
- Stub network calls (yt-dlp, proxies, oEmbed, AI providers) in tests. Launching an AI CLI session spends the user's usage limit - don't.
- For GUI changes, run the app and read a `spectacle -b -n -a -o <png>` screenshot; unit checks alone have missed real layout bugs.
- Commit only when the user says so. Follow the `git-commit-message` skill (Conventional Commits, scope, body says why), split unrelated concerns, stage files by name (never `git add .`), push to `master`. Push with `GIT_SSH_COMMAND="ssh -o BatchMode=yes"` so a locked SSH key fails fast instead of hanging; if it fails, ask the user to run `ssh-add ~/.ssh/id_ed25519`.
- README changes follow the `github-readme` skill.

## Where things are

- Skills: `../.claude/skills/` (`github-readme`, `git-commit-message`, `youtube-video-screener`). The screener skill used by the app is the repo copy at `.claude/skills/youtube-video-screener/SKILL.md` (config `skill_path` is blank so it's detected).
- Corpus folder is set by the marker `~/.config/ytbatch/home`; `YTBATCH_HOME` overrides it.
- Two machines, same `master`, separate `data/`: this Fedora KDE (Wayland) one, and an Arch Linux one (venv install, `~/Repositories/youtube-metadata-batch-fetch`). The GUI here runs from `~/.local/bin/ytb-gui` (editable user install) and must be restarted to pick up code changes.

## Open loose ends

- Settings -> Appearance rows don't use `FlowRow` yet and can clip at large font sizes.
- `analyze.py` still prints a `\r` live counter (CLI-only, fine in a terminal).
- Never actually run: codex / gemini / opencode launchers, and the Windows (wt/cmd) and macOS (Terminal.app) terminal launch.
- Stage 3 (`ytb-analyze`) not exercised against a live provider recently.
- `analyze.py`, `show.py`, `store.py`, `schema.py` are optional CLI stages the app doesn't use; keep them unless the user decides otherwise.
