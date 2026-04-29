# CLI reference

Every command works in the REPL (with `/` prefix) and as a one-shot (`insto [@user] -c <cmd> [args]`). The single grammar is intentional — the REPL is just a prompt around the same dispatcher.

## Global flags

Every command inherits these via the global parser. Flag conflicts (e.g. `--json` and `--csv`) raise a `CommandUsageError` with a clear message.

| Flag | Effect |
|---|---|
| `--json [DEST]` | Write versioned JSON envelope (`{"_schema": "insto.v1", "command": ..., "target": ..., "captured_at": ..., "data": ...}`). `DEST = -` writes to stdout, `DEST` omitted writes to `output/<user>/<cmd>.json`. |
| `--csv [DEST]` | Same, flat-row commands only. CSV has no `_schema` envelope (just header + rows). |
| `--maltego [DEST]` | Maltego entity-import CSV (`Type, Value, Weight, Notes, Properties`). Limited to commands with a canonical entity per row. |
| `--output-format {json,csv,maltego}` | Long form of the three above. |
| `--limit N` | Override per-command default. `N=0` means no cap (only honored where the command itself disables the cap). |
| `--no-download` | Media commands print URLs and skip CDN streaming. |
| `--yes` | Skip interactive confirmations (`/batch` over 25 targets, `/purge`). |
| `--proxy URL` | Per-call proxy. Schemes: `http://`, `https://`, `socks5h://` (Tor). |
| `--hiker-token TOKEN` | Per-call HikerAPI token override. |
| `--backend {hiker,aiograpi}` | Per-invocation backend selector (overrides `$INSTO_BACKEND` and `config.toml`). |
| `--no-progress` | Suppress tqdm bars + `⢿ <cmd>...` spinner on long-running commands. |
| `--non-interactive` | `insto setup` only — read every value from env vars without prompts. CI/automation. |
| `--verbose` / `--debug` | Bump log level (writes to `~/.insto/logs/insto.log`, rotated). |

## Top-level subcommands

```sh
insto                                  # REPL (default)
insto -c info instagram                # one-shot; inline target
insto setup                            # interactive config wizard
insto --print-completion {bash|zsh}    # emit completion script (needs insto[completion])
insto --version
insto --help
```

## Slash-command groups

### Target

| Command | Purpose |
|---|---|
| `/target <user>` | Set the active session target (pre-resolves pk for fail-fast). |
| `/current` | Print the active target. |
| `/clear` | Drop the active target. |

### Profile

Inline target: `/info instagram`. Active target used otherwise.

| Command | Purpose |
|---|---|
| `/info` | Full profile + `user_about` payload (folded into the panel). Avatar URL is rendered as a clickable link; `created` and `country` come from the about call. |
| `/about` | Raw `user_about` slice on its own (joined date, country, former usernames). One backend call instead of two. |
| `/propic` | Download HD profile picture. |
| `/pinned` | Pinned posts (Instagram caps at 3). |
| `/email` / `/phone` | Public contact fields if present. |
| `/export` | Profile + about as JSON (always JSON, ignores `--csv`). |

### Media

| Command | Purpose |
|---|---|
| `/stories` | Active stories (24h TTL). |
| `/highlights [--download N]` | List highlights; `--download N` fetches items of the Nth one. |
| `/posts [N]` | Last N feed posts (default 12). |
| `/reels [N]` | Last N reels — pulled from feed and filtered (default 10). |
| `/tagged [N]` | Posts where the target is tagged (default 10). |
| `/reposts [N]` | Posts the target reposted via IG's repost surface (HikerAPI only). |
| `/audio <track_id> [N]` | Clips that use a given audio asset id. |
| `/postinfo <ref>` | Resolve a media URL / shortcode / pk → full `Post` DTO. No active target needed. |

### Network

| Command | Purpose |
|---|---|
| `/followers [N]` | First N followers (default 50). |
| `/followings [N]` | First N accounts the target follows (default 50). |
| `/mutuals` | Followers ∩ following of one target. Default 1000 per side; `--limit 0` for no cap. |
| `/intersect <a> <b>` | Followers(@a) ∩ followers(@b) — cross-target shared audience. |
| `/similar` | IG's "Suggested for you" list (`chaining` API). |
| `/recommended` | IG's category-based recommendations (business / creator accounts). aiograpi only. |
| `/search <query> [N]` | Free-text account search (no active target needed). |

### Content analysis

All content commands operate on a bounded window (default 50 posts; override with `--limit`). The output header always names the window so a result of 3 hashtags is not mistaken for the user's only 3 hashtags ever.

| Command | Purpose |
|---|---|
| `/hashtags` | Top hashtags in captions. |
| `/mentions` | Top @-mentions (caption + usertags). |
| `/captions` | Dump captions of recent posts. |
| `/likes` | Aggregate like-count stats. |
| `/timeline` | Posting cadence histogram — hour-of-day sparkline + day-of-week bars. |

### Geo

| Command | Purpose |
|---|---|
| `/locations` | Top geo-tagged location names from the active target's posts. |
| `/where` | 🔥 Geo fingerprint of the active target — anchor (most-frequent place), centroid, max radius, top places bar chart. |
| `/place <query> [N]` | Search Instagram places by free text → name + GPS + IG location pk. |
| `/placeposts <pk> [N]` | Top posts at a given Instagram location pk. |

### Interactions

All take a bounded post window (default 50). `/wliked` and `/fans` make `N` (or `2N`) backend calls — pass `--limit 10` for cheaper sampling. Concurrency capped at 5 internally.

| Command | Purpose |
|---|---|
| `/comments [post_code]` | Comments for one post code, or aggregate across the window. |
| `/wcommented` | Top users commenting on the target's recent posts. |
| `/wliked` | Top users liking the target's recent posts. |
| `/wtagged` | Top users who tagged the target in their posts. |
| `/fans` | 🔥 Composite ranking: `score = likes + 3×comments`. Output shows ❤️ + 💬 breakdown. |

### Discovery

| Command | Purpose |
|---|---|
| `/resolve <url>` | Expand `instagram.com/share/...` short-link → canonical URL. aiograpi only. |

### Batch

```sh
/batch <file> <subcommand> [subcommand-args]
/batch - info                  # read targets from stdin (one per line)
```

- Concurrency cap 3 (override `--concurrency N`, hard ceiling 10).
- 1s ± 25% jitter between target starts.
- Resume across re-runs via `output/.batch-<sha>.jsonl`. `--restart` clears.
- Confirms above 25 targets unless `--yes`.
- Graceful exit on `QuotaExhausted` (saves resume state).

### Watch / diff / history

| Command | Purpose |
|---|---|
| `/watch <user> [interval]` | Add a session-scoped watch (≥ 5 min interval, max 3 active). |
| `/unwatch <user>` | Stop watching. |
| `/watching` | List active watches with their state (`ok` / `paused`). |
| `/diff <user>` | Diff current profile vs the most recent stored snapshot. |
| `/history [N]` | Last N rows of `cli_history`. |

### Operational

| Command | Purpose |
|---|---|
| `/quota` | Fresh `/sys/balance` snapshot — requests left, USD balance, rate cap. |
| `/health` | Backend ping + last error + schema-drift counter. |
| `/config` | Effective config + per-key source (flag / env / toml / default). |
| `/purge {history,snapshots,cache} [--user @u]` | Wipe one of the local stores. Always confirms unless `--yes`. |
| `/help` | List every registered command with its one-line description. |
| `/exit`, `/quit` | Leave the REPL (also Ctrl+D). |

### `/dossier`

```sh
/dossier
/dossier --no-download         # everything except media
insto -c dossier instagram --limit 50
```

Collects a full target package under `output/<user>/dossier/<ts>/`:

```text
output/instagram/dossier/2026-04-27T14:35Z/
├── MANIFEST.md                # human-readable summary (with partial=true if quota ran out)
├── profile.json
├── posts.json
├── followers.csv
├── following.csv
├── mutuals.csv
├── hashtags.csv
├── mentions.csv
├── locations.csv
├── wcommented.csv
├── wtagged.csv
└── posts/                     # media files (skipped with --no-download)
```

Pre-flight gates:

- Profile must be `public`. `private` / `blocked` / `deleted` aborts before any other request — no directory created.
- Disk free must be ≥ 2 GB. Otherwise abort.

Independent sections fan out via `asyncio.gather(return_exceptions=True)` — if one section errors (`RateLimited`, `QuotaExhausted`), the rest still complete and `MANIFEST.md` flags `partial: true` with the failure list.
