# Roadmap

Deferred work that is still relevant after the aiograpi 0.9.x update.

## aiograpi follow-ups

### Saved collections shipped; personal feed intentionally not exposed

Saved collections/media are exposed as read-only commands. Personal timeline feed is intentionally not part of insto's command surface.

Why: `get_timeline_feed()` is personalized to the logged-in account, mixes target media with recommended users, feed controls, and other account-specific rows, and has weaker OSINT provenance than target-scoped commands.

Status, 2026-05-15:

- Added an opt-in live audit at `tests/live/aiograpi_saved_feed_audit.py`.
- Live audit with two configured burner sessions passed auth, `collections()`, the
  generic saved-media surface, and `get_timeline_feed()`.
- Prepared a burner saved-media fixture and validated generic saved media plus
  named collection media pagination on a non-empty account.
- Added aiograpi-only `/collections` and `/saved [--collection ID_OR_NAME]`
  commands. They reuse the existing `Post` DTO for saved media and expose no
  save, unsave, collection-create, edit, delete, or other mutation flows.
- `get_timeline_feed()` passed basic auth and shape checks, but returns a large
  raw dict with mixed feed wrappers, pagination/session flags, and
  account-personalized recommendations.
- Decision: do not add `/feed`, `/timeline`, or a personal-feed DTO unless a
  concrete OSINT use case appears.

Next:

- Keep saved command defaults small and verify them in opt-in live smoke before
  releases.
- Reopen personal-feed design only if a user can name target-scoped OSINT
  questions that it answers better than existing commands.

Priority: deferred

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

### Live `/info` e2e in CI

End-to-end coverage of the aiograpi backend through insto's real entrypoint,
exercised automatically in CI.

Why: the saved-feed and private-GraphQL audits drive `AiograpiBackend`
directly and run only by hand. Nothing proved the full
`cli → config → backend → command` chain (including a real login with TOTP)
against a live account, and no live test ran in CI.

Status, 2026-06-20:

- Added `tests/live/aiograpi_info_e2e.py`: spawns
  `insto @instagram -c info --json -` for a pooled `TEST_ACCOUNTS_URL`
  account, performs a full aiograpi login (incl. TOTP), and asserts the JSON
  profile (`username == instagram`, `pk == 25025320`). It reuses each
  account's `client_settings` as a seeded session and honours an explicit
  `IG_PROXY` only.
- Added offline unit coverage in `tests/test_aiograpi_info_e2e.py` for the
  pure helpers (skip-clean path, TOTP extraction, env wiring, session seeding).
- Wired a `live-test` job into `.github/workflows/ci.yml`: runs on
  push / `workflow_dispatch` to `subzeroid/insto` only (forks/PRs never receive
  the secret), gated on the `TEST_ACCOUNTS_URL` repository secret. The script
  self-skips (exit 0) when the secret is absent.

Next:

- Extend the e2e to a self-profile resolve (the logged-in account's own
  username) if a regression there is ever suspected.
- The two `aiograpi_*_audit.py` scripts stay manual; revisit folding their
  surfaces into CI only if they prove stable enough not to flake.

Priority: shipped

## Existing deferred work

### Persistent watch daemon

Turn session-local `/watch` registrations into a persistent daemon with restart recovery and a control surface from REPL / one-shot CLI.

Use case: a local long-running monitor for a small set of important targets,
so profile snapshots and `/diff` history keep moving after the REPL exits or
the machine restarts. It should capture target changes such as username,
biography, external URL, public contact fields, follower/following/media
counts, avatar/banner hash, and recent post ids for provenance.

Operating envelope:

- Keep the current max of 3 active watches and 300-second interval floor.
- Worst case at the floor is 36 ticks/hour; each tick currently does roughly
  one profile read plus one recent-posts read.
- Practical default is 1-3 targets at 10-60 minute intervals.
- HikerAPI users pay in quota/cost; aiograpi users pay in rate-limit and
  account-ban risk. The daemon must keep that visible.

Non-goals for the first slice: broad discovery crawling, high-volume watch
lists, a network service, an HTTP API, telemetry, phone-home, or lowering the
interval floor outside tests.

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
