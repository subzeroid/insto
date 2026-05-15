# Roadmap

Deferred work that is still relevant after the aiograpi 0.9.x update.

## aiograpi follow-ups

### Saved collections / personal feed read-only

Audit the current aiograpi collection and feed surfaces, then add read-only commands only if the API is stable on a live-smoke burner account.

Why: the SDK has collection/feed capability, but insto does not yet have a CLI contract for saved media or personal feed data.

Status, 2026-05-15:

- Added an opt-in live audit at `tests/live/aiograpi_saved_feed_audit.py`.
- Live audit with two configured burner sessions passed auth, `collections()`, the
  generic saved-media surface, and `get_timeline_feed()`.
- The tested burner sessions had no saved media or named collections, so
  collection-media pagination still needs a non-empty fixture before a public
  command is designed.
- `get_timeline_feed()` returns a large raw dict. Keep personal feed out of the
  command surface until it has a reduced read-only DTO and privacy-safe rendering
  contract.

Next:

- Validate `collection_medias(<collection_id>, amount=N)` on a non-empty saved
  collection fixture.
- Decide whether saved media can reuse the existing `Post` DTO or needs a small
  saved-specific wrapper.
- Only then add an aiograpi-only read command with small default limits and no
  write, mutate, or automation flows.

Priority: P3

### Private GraphQL pagination audit

Compare the current `user_followers`, `user_following`, and media methods with the newer private GraphQL helpers in aiograpi 0.9.x.

Why: private GraphQL may improve stability for followers, following, clips, inbox, and search, but it may also increase ban risk. Switch only after fixture diffs and small-limit live-smoke checks.

Priority: P2

## Existing deferred work

### Persistent watch daemon

Turn session-local `/watch` registrations into a persistent daemon with restart recovery and a control surface from REPL / one-shot CLI.

Priority: P2

### At-rest store encryption

Encrypt `~/.insto/store.db` and snapshot backups with SQLCipher, GPG, or age when a real multi-operator or compliance use case appears.

Priority: P2

### `/replay <N>`

Replay a previous command from `cli_history`, with an explicit whitelist for safe commands and an option to redirect the target.

Priority: P3

### Plugin API

Expose entry points for third-party commands and backends after there is a real external extension to design against.

Priority: P3
