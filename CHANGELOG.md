# Changelog

All notable changes to insto. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/spec/v2.0.0.html). Entries from 0.1.1 onward will be assembled from Conventional Commits by [release-please](https://github.com/googleapis/release-please).

## [0.2.0] - 2026-04-28

### Added

- `AiograpiBackend` over [aiograpi](https://github.com/subzeroid/aiograpi) 0.7.2, gated behind the new `insto[aiograpi]` optional install. Login is lazy, session persists to `~/.insto/aiograpi.session.json` (mode `0600`). 12 of the 14 `OSINTBackend` methods land on this backend — only `/similar` (endpoint retired upstream) and `/tagged` (aiograpi 0.7 has no `user_tag_medias`) refuse with a clear "needs hiker backend" error.
- `Config` gained `backend`, `aiograpi.username`, `aiograpi.password`, `aiograpi.totp_seed`, `aiograpi.session_path` keys. Same `flag > env > toml > default` precedence; `password` and `totp_seed` register with the global redaction set so they never reach error output or rotating logs.
- Setup wizard now asks `backend (hiker | aiograpi)` first and only prompts for the credentials relevant to that backend. Existing `hiker.token` and `aiograpi` block are preserved when you flip backends.
- `make_backend("aiograpi", ...)` — lazy import; if the user did not install `insto[aiograpi]`, returns `RuntimeError` with the exact `pip / uv / pipx install 'insto[aiograpi]'` command.
- Per-command argument completion in the slash popup: `/theme<Tab>` lists `aiograpi / claude / instagram`; `/purge<Tab>` lists `history / snapshots / cache`. Mechanism walks the command's argparse `choices=` automatically — every future command with `choices` gets completion for free.
- Slash popup keeps re-rendering across the entire first-token typing (`/`, `/i`, `/in`, `/info`) by binding every command-name character explicitly, so `complete_while_typing`'s debounce window does not flicker the popup.
- `/help` and the slash popup show positional-argument signatures (`/theme [name]`, `/posts [count]`, `/batch <file> <cmd>`).
- Three named themes (`/theme`): `claude` (Claude Code burnt orange), `instagram` (Instagram 2022+ conic gradient), `aiograpi` (purple → blue with violet accent — default since 0.2.0). The figlet INSTO logotype renders per-row gradient on themes that define one.

### Changed

- Default theme is now `aiograpi`. Existing users with `theme = "..."` in `~/.insto/config.toml` are unaffected.
- HikerAPI 403 is mapped to a clearer message — Instagram returns 403 to login-walled / region-restricted endpoints, not "your account is banned". The `Banned` exception now carries the diagnosis without the misleading `backend account is banned:` prefix.
- Welcome banner reads the live backend label from the class name (`hiker` / `aiograpi`) instead of the hard-coded `hiker · ...` literal.

### Fixed

- `Ctrl+C` in the REPL cancels the current line and re-prompts (shell / IPython convention) instead of exiting. `Ctrl+D` on an empty line still exits.
- `/theme instagram` now actually paints the figlet rows in the brand gradient — the previous fix used a Rich style string Rich could not parse (`"bold logo.0"` with a dotted theme key).

### Documentation

- `docs/backends.md` rewritten to cover both backends + the install matrix.
- README updated for `pip install 'insto[aiograpi]'` and the new welcome screenshot. `pip install` PEP 668 trap explained.

## [0.1.1] - 2026-04-28

### Fixed

- `_hiker_map` now accepts ISO-8601 strings (`2026-04-17T17:45:12Z`) for `taken_at` / `created_at` / `expiring_at` in addition to unix integers. HikerAPI's `user_medias_chunk_v1` returns the ISO shape, so `/posts`, `/dossier`, `/reels`, `/tagged` were previously raising `SchemaDrift` on every live call.
- REPL welcome banner now refreshes the HikerAPI balance synchronously before render, so the bottom toolbar shows real numbers ("14.7M requests left · $4,417 · 15 rps cap") on first paint instead of "balance: pending".
- Setup wizard resolves `output_dir` and `db_path` to absolute paths, so behaviour no longer depends on the CWD where `insto` is later invoked.

### Added

- Inline target on every single-target slash command (`/info instagram`, `/posts instagram`, `/dossier instagram`...) without mutating the active session target.
- Slash popup styling: typing `/` opens a Claude-Code-like popup of all commands with help-text in the right column. `complete_style=COLUMN`, `reserve_space_for_menu=10`, dedicated dark-slate Style for the menu.
- `/info`-style commands now thread an optional positional `target` through `with_target` / `with_pk` via the new `add_target_arg` and `compose_args` helpers in `commands/_base.py`.
- Setup wizard prompt for proxy now lists supported schemes inline: `proxy URL (http://, https://, socks5h://) (optional, '-' to clear)`.

### Changed

- Welcome banner: replaced the chafa pixel-art wasp with a typographic INSTO logotype (figlet "standard") plus the tagline `i n s t o ⇋ o s i n t · instagram tool · open-source intel`. No image deps, identical rendering on light and dark schemes, no third-party watermark to mask.
- `Quota` model gained `rate`, `amount`, `currency` fields populated from `/sys/balance`. `HikerBackend.refresh_quota()` is the new entry point; `/quota` always re-fetches before rendering.

### Documentation

- Full mkdocs Material site at https://subzeroid.github.io/insto/ — index, installation, basic-usage, cli-reference, backends, architecture, troubleshooting, contributing, changelog.
- LICENSE (MIT), CHANGELOG.md, SECURITY.md, CONTRIBUTING.md added at the repo root.
- `pyproject.toml` populated with classifiers, keywords, project URLs, and `[docs]` extras.

### CI

- `release.yml` — tag-driven build + trusted-publish to PyPI + GitHub release.
- `release-please.yml` — Conventional Commits → CHANGELOG automation.
- `docs.yml` — mkdocs deploy to GitHub Pages on `main`.
- `pr-title.yml` — semantic PR title gate.

## [0.1.0] - 2026-04-27

### Added

Initial public release.

- Interactive REPL (`insto`) with prompt_toolkit completer (slash popup of commands, like Claude Code), bottom toolbar (active target · backend · live HikerAPI balance + rate cap), Ctrl+T / Ctrl+L keybindings, history at `~/.insto/cli_history`.
- One-shot CLI (`insto @user -c <cmd> [args]`) with the same command grammar; works in shell pipelines via `--json -` / `--csv -` to stdout and `/batch -` from stdin.
- Slash-command surface across seven groups:
  - Target — `/target`, `/current`, `/clear`
  - Profile — `/info`, `/propic`, `/email`, `/phone`, `/export`
  - Media — `/stories`, `/highlights`, `/posts`, `/reels`, `/tagged`
  - Network — `/followers`, `/followings`, `/mutuals`, `/similar`
  - Content analysis — `/locations`, `/hashtags`, `/mentions`, `/captions`, `/likes`
  - Interactions — `/comments`, `/wcommented`, `/wtagged`
  - Operational — `/quota`, `/health`, `/config`, `/purge`, `/help`
- `/dossier` killer-feature: collects info + posts + followers + following + mutuals + hashtags + mentions + locations + wcommented + wtagged into one structured directory under `output/<user>/dossier/<datetime>/` with a `MANIFEST.md` summary. Sections fan out via `asyncio.gather` and tolerate partial completion on `QuotaExhausted`.
- `/batch` with concurrency cap, jittered per-target sleep, stdin support, dedup, JSONL resume on `output/.batch-<sha>.jsonl`, and graceful exit on `QuotaExhausted`.
- `/watch` in-session monitoring (max 3 active, ≥ 5 min interval, paused after 2 consecutive errors, `patch_stdout` notifications).
- `OSINTBackend` ABC with lazy backend imports. `HikerBackend` for v0.1; aiograpi planned for v0.2 — requirement annotations (`requires=("followed",)`) already in place.
- Hardened CDN streamer: host allowlist, HTTPS-only, no cross-host redirects, MIME cross-check, byte budget (per-resource and per-command), atomic write, collision suffix, disk guard, macOS xattr tagging.
- sqlite-backed history / snapshots / watches at `~/.insto/store.db` (mode `0600`) with retention prune, schema versioning, `BEGIN IMMEDIATE` migration lock.
- OPSEC: `--proxy` / `HIKERAPI_PROXY` (Tor / corp proxy), `_redact_secrets` strips tokens from errors and rotating-file log (`~/.insto/logs/insto.log`).
- Maltego CSV exporter (`--maltego` / `--output-format maltego`).
- Shell completion via `insto --print-completion {bash|zsh}` (requires `insto[completion]`).

### Notes

- Secret redaction covers stack traces, log files, and CLI error output.
- All 24 OSINT commands accept an inline target argument (`/info instagram`) without mutating the active session target.
