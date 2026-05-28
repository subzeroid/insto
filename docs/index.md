# insto

Interactive Instagram OSINT CLI on the [HikerAPI](https://hikerapi.com/p/6k1q1388) backend and optional [aiograpi](https://github.com/subzeroid/aiograpi) logged-in backend.

![insto demo](demo.gif)

```sh
uv tool install insto
insto setup                          # paste your HikerAPI token
insto                                # REPL with welcome screen
insto -c info instagram              # one-shot lookup, no REPL
```

Two surfaces over the same command grammar:

- **REPL** — `insto` (or `insto @user` to pre-select a target) drops you into a
  prompt-toolkit session with a slash popup for every command (Claude-Code
  style), a bottom toolbar showing active target + backend + live HikerAPI
  balance, Ctrl+T / Ctrl+L / Ctrl+C keybindings, six switchable colour themes
  (`/theme` opens a live-preview picker), and live `/watch` notifications via
  `patch_stdout`.
- **One-shot** — `insto @user -c <command> [args]` runs a single slash-command
  and exits. Pipe-friendly: `--json -` writes JSON to stdout, `--csv -` does
  the same for flat-row commands, `/batch -` reads targets from stdin.

## What you get

- 50+ OSINT and operational slash-commands across profile / media / network / content / interactions / direct / saved / batch / watch.
- A killer `/dossier` command that collects a full target package (info + posts + followers + following + mutuals + hashtags + mentions + locations + wcommented + wtagged) into a structured directory with a `MANIFEST.md` summary.
- Hardened CDN streamer for media downloads: host allowlist, MIME cross-check, byte budgets, atomic writes, disk guard.
- Sqlite-backed history / snapshots / watches at `~/.insto/store.db` (mode `0600`, schema-versioned).
- OPSEC: `--proxy` flag and `HIKERAPI_PROXY` env for Tor / corp egress; secret redaction across error output and rotating log file.
- Maltego CSV exporter for graph-tooling interop.

## What it is NOT

- Not a scraper. The default backend is HikerAPI (paid, no account-ban risk). The optional `aiograpi` backend uses a real logged-in Instagram account for private-account access and carries account-ban risk.
- Not an account hijacker / DM-flooder / ban-evader.
- No AI / LLM features. No web UI.

## Anagram

`insto` is the same five letters as `osint` — open-source intelligence — rearranged. Both readings are intentional: it looks like an Instagram tool, and under the hood it is one, but the discipline is OSINT.

## Pick a backend

| | **hikerapi** (default) | **aiograpi** (`insto[aiograpi]`) |
|---|---|---|
| Auth | API token | Instagram login + 2FA |
| Cost | Pay-per-call, [100 free requests](https://hikerapi.com/p/6k1q1388) at signup (no card) | Free |
| Account ban risk | None | Real |
| Stability | High | Brittle |
| Private-account access | No | Yes (only for accounts you follow) |

See [Backends](backends.md) for the full breakdown.

## Command surface

🔥 marks the killer-feature commands — uniquely-OSINT primitives that don't have obvious equivalents in other tools. See **[Killer features](killer-features.md)** for full examples.

| Group | Commands |
|---|---|
| **Profile** | `info` `about` `propic` `email` `phone` `export` `pinned` |
| **Media** | `posts` `reels` `reposts` `stories` `highlights` `tagged` `audio` `postinfo` 🔥 |
| **Network** | `followers` `followings` `mutuals` `intersect` 🔥 `similar` `search` 🔥 `recommended` |
| **Geo** | `locations` `where` 🔥 `place` 🔥 `placeposts` 🔥 |
| **Content** | `hashtags` `mentions` `captions` `likes` `timeline` 🔥 |
| **Interactions** | `comments` `wcommented` `wliked` `wtagged` `fans` 🔥 |
| **Discovery** | `resolve` |
| **Direct** | `direct` `direct-thread` |
| **Saved** | `collections` `saved` |
| **Watch / diff** | `watch` `unwatch` `watching` `diff` `history` |
| **Operational** | `quota` `health` `config` `purge` |
| **Session** | `target` `current` `clear` |
| **Batch / dossier** | `batch` `dossier` 🔥 |

## Where to next

- [Killer features 🔥](killer-features.md) — the OSINT-unique commands with examples.
- [OSINT recipes 📓](recipes.md) — concrete investigative scenarios as 3-5 command sequences.
- [Installation](installation.md) — `uv tool install insto`, dev sources, shell completion.
- [Basic usage](basic-usage.md) — REPL walkthrough, one-shot patterns, pipelines.
- [CLI reference](cli-reference.md) — every command with flags and examples.
- [Architecture](architecture.md) — how the layers fit together.
