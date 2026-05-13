# Roadmap

Deferred work that is still relevant after the aiograpi 0.9.x update.

## aiograpi follow-ups

### Read-only Direct inbox

Add read-only commands for Direct: list threads, show recent messages in a
thread, and search threads by participant. Do not add send, reaction, or unsend
commands to the core CLI.

Why: aiograpi 0.9.x synced the current Direct API surface, including message
requests, single-message lookup, reactions, title updates, unsend, and
voice/video attachments. For insto's OSINT surface, only the read-only subset
fits the product boundary.

Priority: P2

### Saved collections / personal feed read-only

Audit the current aiograpi collection and feed surfaces, then add read-only
commands only if the API is stable on a live-smoke burner account.

Why: the SDK has collection/feed capability, but insto does not yet have a CLI
contract for saved media or personal feed data.

Priority: P3

### Private GraphQL pagination audit

Compare the current `user_followers`, `user_following`, and media methods with
the newer private GraphQL helpers in aiograpi 0.9.x.

Why: private GraphQL may improve stability for followers, following, clips,
inbox, and search, but it may also increase ban risk. Switch only after fixture
diffs and small-limit live-smoke checks.

Priority: P2

## Existing deferred work

### Persistent watch daemon

Turn session-local `/watch` registrations into a persistent daemon with restart
recovery and a control surface from REPL / one-shot CLI.

Priority: P2

### At-rest store encryption

Encrypt `~/.insto/store.db` and snapshot backups with SQLCipher, GPG, or age
when a real multi-operator or compliance use case appears.

Priority: P2

### `/replay <N>`

Replay a previous command from `cli_history`, with an explicit whitelist for
safe commands and an option to redirect the target.

Priority: P3

### Plugin API

Expose entry points for third-party commands and backends after there is a real
external extension to design against.

Priority: P3
