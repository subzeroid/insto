# Changelog

All notable changes to insto. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/spec/v2.0.0.html). Entries from 0.1.1 onward will be assembled from Conventional Commits by [release-please](https://github.com/googleapis/release-please).

## [0.7.2](https://github.com/subzeroid/insto/compare/v0.7.1...v0.7.2) (2026-04-29)


### Documentation

* OSINT recipes page — 7 multi-step investigative scenarios ([e776d4c](https://github.com/subzeroid/insto/commit/e776d4c2c754dc29f13d2105610812f48766216d))
* refresh demo.gif (/info + /where + /fans) + Killer features page ([df556b0](https://github.com/subzeroid/insto/commit/df556b04f193754a8e99f8e013917224a568cb9d))

## [0.7.1] - 2026-04-29

### Added

- **`/where <user>` 🔥** — geo-fingerprint analytic. Walks the target's recent posts, extracts GPS coordinates from every geotag, computes the **anchor** (most-frequent place — proxy for "where they live or work"), the **centroid** (arithmetic mean of all geotag points), and the **spread radius** (max haversine distance from centroid). Top-N places rendered as a horizontal bar chart. On @ferrari (15/30 posts geotagged): anchor = Maranello (47%), centroid in N. Italy, max radius 9.5k km — Maranello is HQ, the rest is racing circuits and ski trips.
- **`Post.location_lat` / `Post.location_lng`** fields. The IG payload always carried these but the mappers were dropping them. Now extracted by both backends; new `_opt_float` helper handles NaN / coercion errors. Snapshot serialisation is unaffected (snapshot diff already serialised the whole Post via `dataclasses.asdict`).

### Changed

- Command surface table in the README marks **🔥 killer-feature commands** so newcomers can immediately spot the OSINT-unique primitives (`/dossier`, `/where`, `/intersect`, `/fans`, `/place`, `/placeposts`, `/postinfo`, `/search`, `/timeline`) vs the trivial wrappers.
- `/locations` moved from "Content" group to a new "Geo" group alongside `/where`, `/place`, `/placeposts` — they all answer "where" rather than "what about the captions".

## [0.7.0] - 2026-04-29

### Added — five new OSINT commands

- **`/place <query>`** — search Instagram places by free text (e.g. `Tbilisi`, `Eiffel Tower`). Returns matched locations with name + GPS + IG location pk, plus a hint to `/placeposts` for posts at that place. CSV / JSON / Maltego (`maltego.GPS`) export.
- **`/placeposts <pk>`** — top posts at a given Instagram location pk. Reveals who, what, when at a place — the geo-OSINT primitive. Pairs with `/place` to find the pk.
- **`/postinfo <ref>`** — resolve a media reference (full URL / shortcode / numeric pk) to the full Post DTO. Caption, owner, taken_at, hashtags, mentions, location, media URL. Evidence collection: paste a permalink, get metadata. No active target needed.
- **`/pinned <user>`** — list the target's pinned posts (Instagram caps at 3). What the account *wants* you to see first.
- **`/reposts <user>`** — posts the target has reposted (IG's repost surface). Reveals amplification network. **HikerAPI only**; aiograpi has no equivalent yet.

### Added — DTO

- **`Place`** — new `dataclass` for IG locations: `pk`, `name`, `address`, `city`, `lat`, `lng`, `facebook_id`. Returned by `backend.search_places`, consumed by `backend.iter_place_posts`.

### Backend ABC

- Five new methods: `iter_user_pinned`, `iter_user_reposts`, `get_post_by_ref`, `search_places`, `iter_place_posts`. Default implementations raise `NotImplementedError` so third-party backends extending `OSINTBackend` keep compiling. HikerAPI implements all five; aiograpi implements four (no reposts).

## [0.6.2] - 2026-04-29

### Fixed

- Spinner no longer overlaps the rich Panel on commands that print straight to stdout (`/info`, `/about`, `/resolve`, ...). Two changes: (1) `ctx.print` now stops the spinner before writing, so any first user-facing output cleans the spinner line; (2) `_stop_spinner` erases the line synchronously instead of waiting for the spinner loop's next tick, so a fast follow-up panel doesn't render on top of a half-painted frame.

## [0.6.1] - 2026-04-29

### Added

- **`/about <user>`** — surface the raw IG `user_about` payload as its own command (joined date, country, former usernames, verified flag). Cheaper than `/info` (one fewer call) and JSON-exportable for pipelines that want only this slice.
- **`/info` shows the avatar URL** as an OSC-8 hyperlink ("open"); modern terminals (iTerm2, kitty, Apple Terminal) make it clickable. The full signed CDN URL is hidden behind the link so the panel stays tidy. Hint to `/propic` for offline downloads.
- **`/info` surfaces more `user_about` fields**: `created` (account-joined date), `former usernames`. Were silently dropped before because the renderer was looking for the wrong keys.

## [0.6.0] - 2026-04-29

### Added

- **`/intersect <a> <b>`** — followers(@a) ∩ followers(@b). New OSINT primitive: shared audience between two targets reveals communities, staff, family, PR networks. Both follower fetches run concurrently (different pks so the backend serves in parallel). Default window 1000 per side; `--limit N` to widen, `--limit 0` for unbounded. Maltego export uses `{a}_{b}` as a synthetic target dir so the file colocates with other artifacts.
- **`/timeline <user>`** — posting cadence histogram. 24-bucket hour-of-day sparkline + 7-bucket day-of-week bar chart over the last N posts. Reveals timezone (sleeping hours), human vs scheduler-driven posting, weekday vs weekend rhythm. UTC-bucketed; JSON export carries the raw bucket arrays for further analysis.
- **/dossier section progress bar** — 9-step tqdm bar that ticks once per section completes. Pairs with the v0.5.5 spinner: spinner runs during pre-flight, hands off to the bar once sections start.
- **`insto setup --non-interactive`** — config wizard driven entirely by env vars + existing config without prompts. Errors loudly when a required field is missing. Pattern for CI / automation: `INSTO_BACKEND=hiker HIKERAPI_TOKEN=$T insto setup --non-interactive`.

### Performance

- **`/fans`, `/wliked`, `/wcommented` are ~5× faster.** Per-post fan-out now uses `asyncio.Semaphore(5)` + `asyncio.as_completed` instead of a serial loop. `/fans @nasa --limit 5` drops from ~17 s to 8.5 s on the same backend response time. Concurrency cap is conservative re: HikerAPI's 7-15 rps per-account limit and aiograpi's per-session throttle. Order-independent (Counter aggregation) so `as_completed` is safe.

### Documentation

- Command surface now renders as a 10-row table (group / commands / one-line description) instead of a paragraph list. Easier to skim when you want to know what's in `/fans` vs `/recommended` vs `/intersect`.
- One-shot examples in the README cover the new commands (`/search`, `/fans`, `/recommended`, `/intersect`, `/timeline`).
- `INSTO_BACKEND` env-var description backfilled (the prior text said "set to `fake` for the e2e suite" — it's been accepting `hiker` / `aiograpi` for a year).

## [0.5.5] - 2026-04-29

### Added

- **Spinner on stderr** during command execution — pipx-style Braille wheel that paints `⢿ fans...` while the backend call sequence runs, before the per-post tqdm bar (or instead of it, for commands like `/info` / `/resolve` / `/recommended` that don't have a per-iteration loop). Auto-stops when `track()` starts a tqdm bar (tqdm reuses the same line). Honours `--no-progress` and auto-suppresses on non-TTY. Closes the silent-pause-then-output gap that made commands feel hung even on fast networks.

### Fixed

- `/theme<Tab>` selecting a choice now actually inserts `/theme instagram` instead of `/themeinstagram`. v0.5.4's leading-space approach turned out to drop the space on some prompt-toolkit code paths; v0.5.5 yields the choices as full-token replacements (`text="/theme instagram"`, `start_position=-len("/theme")`) so prompt-toolkit's apply step deletes the typed `/theme` and inserts the complete `/theme instagram`. Bulletproof regardless of which apply path the menu uses.

## [0.5.4] - 2026-04-29

### Fixed

- v0.5.3 fixed `/theme<Tab>` to *show* the theme choices inline, but accepting one produced `/themeinstagram` (no separator) — prompt-toolkit inserted the completion at the cursor without the implicit space. Now the choices include a leading space in their completion text, so `/theme<Tab>` → arrow to `instagram` → Enter produces `/theme instagram`.

## [0.5.3] - 2026-04-29

### Fixed

- Slash popup now yields argument choices for the typed-exactly command without requiring a trailing space. Before: typing `/theme` and pressing Tab only re-inserted `/theme` itself; you had to type a space to get `claude` / `instagram` / `aiograpi`. After: `/theme<Tab>` shows the themes inline. Same for `/purge` (history / snapshots / cache).

## [0.5.2] - 2026-04-29

### Added

- **Progress bars** on long-running aggregation commands (`/fans`, `/wliked`, `/wcommented`). tqdm-based, writes to stderr so JSON / CSV stdout stays clean. Auto-suppresses on non-TTY (CI logs, piped invocations) without configuration; manual override via the new `--no-progress` global flag.
- New dependency: `tqdm >= 4.66`. Same pattern `insta-dl` uses for download progress.

### Why

`/fans --limit 50` makes 100 backend round-trips per invocation — easily a minute of silent waiting. The bar shows ETA + per-step rate, which is what operators actually need to decide between "wait" and "Ctrl-C, narrow the window".

## [0.5.1] - 2026-04-29

### Added

- **`--backend {hiker,aiograpi}`** global flag — per-invocation backend override. Same `flag > env > toml > default` precedence the rest of the config uses, so `INSTO_BACKEND=…` and `[backend]` in `config.toml` keep working.

### Changed

- `/fans` output now uses ❤️ / 💬 emoji for the per-channel breakdown instead of the cryptic `2L+0C` shorthand. Both the table column (`(❤️3 💬0)`) and the Maltego Notes field (`❤️2 💬3`). Easier to read at a glance and survives copy-paste into chat / docs.

## [0.5.0] - 2026-04-29

### Added

- **`/wliked`** — top likers across the active target's recent N posts. Symmetric to the existing `/wcommented` (which counts commenters). One post-window pull, one likers call per post; default window 50, customisable via `--limit`. Flat-row CSV / JSON / Maltego export — same shape as `/wcommented`. Both backends (uses the existing `iter_post_likers` ABC).
- **`/fans`** — composite ranking of likers + commenters as a single "superfans" view. Score = `likes + 3*comments` (a comment is ~3x more effortful than a tap-to-like). Output table shows score with the breakdown (`11  (2L + 3C)`); JSON envelope, CSV with `rank,user,likes,comments,score` columns, and Maltego CSV with score-as-weight and `"2L+3C"`-style Notes for human-readable Maltego node labels.
- New analytics primitives: `count_wliked` (mirrors `count_wcommented` for likers), `count_fans` (composes both into a weighted ranking), `FanRow` and `FansResult` dataclasses.

### Performance / cost notes

- `/fans` makes `2N` backend calls per invocation (one likers + one comments call per post). On the default 50-post window that's 100 backend round-trips per `/fans` run — pass `--limit 10` for cheaper sampling. /wliked is half that cost (just likers).

## [0.4.0] - 2026-04-29

### Added

- **`/resolve <url>`** — expand an Instagram short-link (`instagram.com/share/...`) to its canonical URL via a HEAD request through the logged-in session. aiograpi only; HikerAPI raises a clear "switch backend" error.
- **`/audio <track_id>`** — list clips that use a given audio asset, with full Post DTOs (code, taken_at, owner). Both backends — hiker via `track_by_id_v2`, aiograpi via `track_info_by_id`. Bypasses the preview-only `track_stream_*` surface that returned skeleton media (no `code` field, breaks `/post`-style integrations).
- **`/recommended`** — IG's category-based account recommendations for the active target. aiograpi only (the surface needs a logged-in session). For `@ferrari` returns 30 automotive-category accounts (Porsche creators, etc.).

All three commands route their results through the existing JSON / CSV / Maltego export pipeline — `/recommended --maltego` writes `recommended.maltego.csv` with `maltego.Person` rows.

### Changed

- `[aiograpi]` extra now requires `aiograpi >= 0.8.5` (unchanged in this release; the wave-2 endpoints all landed in the 0.8.x series).
- New `OSINTBackend` ABC methods: `resolve_short_url`, `iter_audio_clips`, `get_recommended`. Default implementations raise `NotImplementedError` so third-party backends extending the ABC don't need to implement everything to keep compiling.

## [0.3.0] - 2026-04-29

### Added

- **`/search <query>`** — find Instagram accounts by free-text query (username, brand, location, etc). New top-level command with `--limit`, `--csv`, `--json`, and `--maltego` exports. Works on both backends via `fbsearch_accounts_v2` (HikerAPI 0.1.0+ / aiograpi 0.8.1+). Big OSINT win: insto could already drill into a *known* target, but couldn't *discover* unknown handles by name. `/search` closes that gap.
- **`/similar` fallback chain on aiograpi** — when the primary `chaining()` private endpoint refuses a target ("Not eligible for chaining" / 403), insto now falls through to `user_related_profiles_gql` (public-graphql `edge_chaining`). Live test: `@ferrari` returns 80 suggestions on aiograpi, where the equivalent hiker call 403s. Needs aiograpi ≥ 0.8.5.
- **`resolve_target` fallback on aiograpi** — when the public `user_id_from_username` path JSON-decode-fails (HTML challenge, intermittent gating), insto retries through `user_web_profile_info_v1` (private-host route, carries the logged-in session). Same target, more reliable plumbing.

### Changed

- `[aiograpi]` extra now requires `aiograpi >= 0.8.5` (was `>= 0.8.0`) — the release that landed `fbsearch_accounts_v2`, `user_related_profiles_gql`, and `user_web_profile_info_v1`. Settled at install time.
- aiograpi-side `chaining` and `fbsearch_accounts_v2` responses are now run through `aiograpi.extractors.extract_user_short` before the insto mapper. The SERP / chaining endpoints return raw IG dicts (`pk_id` / `id` instead of `pk`); the upstream extractor reconciles them. Without this wrap, every `/similar` and `/search` row crashed with `SchemaDrift: missing field 'pk'`.

## [0.2.2] - 2026-04-29

### Added

- **`tests/live/smoke.py`** — structured live smoke against the real HikerAPI, eight REQ checks + one OPT (`/similar`). Skips with exit 0 when `HIKERAPI_TOKEN_TEST` is unset, so it's safe in any release-prep gate. Costs ~10 calls, single-digit cents. See `CONTRIBUTING.md`.
- **`/health` observability** — extends the existing quota / drift output with per-call latency p50/p95/max, cumulative call count, and a breakdown of `BackendError` subtypes seen this session. Instrumented at the `_call` boundary in both backends; latencies are kept in a 1000-slot ring (~8 KB).
- **`/dossier --maltego`** — promised in the README since v0.1, finally implemented. Each maltego-eligible section (followers, following, mutuals, hashtags, mentions, locations, wcommented, wtagged) writes a `.maltego.csv` with the standard `Type, Value, Weight, Notes, Properties` shape; profile.json + posts.json keep their JSON form. Verified live on both hiker and aiograpi backends.

### Fixed

- `iter_hashtag_posts` was silently broken on the real HikerAPI: hashtag responses use `response.sections[*].layout_content.medias[*].media`, not the generic `users/items/comments` keys, and the cursor at the top level is `next_page_id` (a base64 envelope), not the inner `next_max_id` (a hex string the server rejects). The live smoke caught this — mocks always passed because the fakes mirrored the wrong shape.

## [0.2.1] - 2026-04-28

### Added

- `/tagged` and `/similar` now work on the aiograpi backend. The first via `usertag_medias_v1` (already in aiograpi 0.7 — the previous "not exposed" stub was wrong), the second via the freshly-landed `chaining` + `fetch_suggestion_details` in [aiograpi 0.8.0](https://github.com/subzeroid/aiograpi/releases/tag/0.8.0). All 14 `OSINTBackend` methods now land on both backends.

### Changed

- `[aiograpi]` extra now requires `aiograpi >= 0.8.0` (was `>= 0.7.2`) — the upstream release that adds the chaining endpoints.
- `aiograpi.exceptions.InvalidTargetUser` ("Not eligible for chaining.") is mapped to a clear `BackendError("target not eligible: ...")` instead of falling through to a generic transient.

### Documentation

- `docs/backends.md` matrix simplified — no per-backend gaps remain. The "HikerAPI 403" note now correctly describes `/similar` as per-target flaky (Instagram refuses chaining for some user_ids), not "endpoint retired".

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
