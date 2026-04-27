# Changelog

All notable changes to insto. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/spec/v2.0.0.html). Entries from 0.1.1 onward will be assembled from Conventional Commits by [release-please](https://github.com/googleapis/release-please).

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
