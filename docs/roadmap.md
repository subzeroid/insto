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
- Prepared a burner saved-media fixture and validated generic saved media plus
  named collection media pagination on a non-empty account.
- Added aiograpi-only `/collections` and `/saved [--collection ID_OR_NAME]`
  commands. They reuse the existing `Post` DTO for saved media and expose no
  save, unsave, collection-create, edit, delete, or other mutation flows.
- `get_timeline_feed()` returns a large raw dict. Keep personal feed out of the
  command surface until it has a reduced read-only DTO and privacy-safe rendering
  contract.

Next:

- Design a reduced read-only DTO before exposing personal timeline feed.
- Keep saved command defaults small and verify them in opt-in live smoke before
  releases.

Priority: P3

### Private GraphQL pagination audit

Compare the current `user_followers`, `user_following`, and media methods with the newer private GraphQL helpers in aiograpi 0.9.x.

Why: private GraphQL may improve stability for followers, following, clips, inbox, and search, but it may also increase ban risk. Switch only after fixture diffs and small-limit live-smoke checks.

Status, 2026-05-15:

- Added an opt-in live audit at `tests/live/aiograpi_private_graphql_audit.py`.
- The audit compares current aiograpi wrappers with raw private GraphQL
  followers/following responses and the direct media GraphQL pagination path.
  It prints only counts, overlap totals, cursor presence, and sanitized errors.
- Live smoke on one burner account plus a stable public target authenticated
  successfully, but private GraphQL followers/following returned zero candidate
  rows where current wrappers returned rows, and the media GraphQL path failed
  inside aiograpi's media extractor.
- Decision: keep insto on the existing aiograpi wrapper methods. Do not switch
  followers, following, or media pagination to private GraphQL until a future
  aiograpi release shows equal id overlap and no extractor failures in this
  audit.

Next:

- Re-run the audit when aiograpi updates the private GraphQL doc ids, response
  parsing, or media normalisation.
- Only switch a backend path after the audit shows equal or better small-limit
  behavior on both a burner self-target and a stable public target.

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
