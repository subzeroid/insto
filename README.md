# insto

Interactive Instagram OSINT CLI on the [HikerAPI](https://hikerapi.com) backend.

Two surfaces over the same command grammar:

- **REPL** — `insto` drops you into a prompt-toolkit session with tab-completion,
  a bottom toolbar (active target, backend, quota), and live `/watch`
  notifications. Visually similar to the Claude Code welcome screen.
- **One-shot** — `insto @user -c <command> [args]` runs a single slash-command
  and exits. Pipe-friendly: `--json -` writes to stdout, `--csv -` does the
  same for flat commands, `/batch -` reads targets from stdin.

## Install

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install insto             # PyPI install (release)
# or, from a checkout:
uv sync && uv run insto --help
```

## Setup

```sh
insto setup
```

Interactive wizard. Writes `~/.insto/config.toml` (mode `0600`) with your
HikerAPI token, output directory, sqlite store path, and optional proxy.
Token precedence is **flag > env (`HIKERAPI_TOKEN`) > config.toml**; the same
precedence applies to the proxy (`--proxy`, `HIKERAPI_PROXY`,
`[hiker].proxy`). `socks5h://` (Tor) and `http://` proxies are both
supported.

## Examples

REPL:

```text
$ insto
… wasp banner …
Tips: /target @user · /info · /posts --limit 10 · /watch · /quit
> /target @ferrari
> /info
> /posts --limit 10 --download
> /followers --limit 200 --json followers.json
> /diff
> /watch posts every 30m
> /quit
```

One-shot:

```sh
insto @ferrari -c info
insto @ferrari -c posts --limit 10 --json -
insto @ferrari -c followers --limit 500 --csv followers.csv
insto -c batch targets.txt -c info --json -
insto @ferrari -c dossier --out ./dossier-ferrari
```

Pipe to `jq`:

```sh
insto @ferrari -c info --json - | jq '.username, .followers_count'
```

Shell completion (uses `argparse` via `shtab`):

```sh
insto --print-completion zsh > ~/.insto/_insto
echo 'fpath+=~/.insto && autoload -Uz compinit && compinit' >> ~/.zshrc
```

## Command surface

Profile: `info`, `propic`, `email`, `phone`, `export`.
Media: `posts`, `reels`, `stories`, `highlights`, `tagged`.
Network: `followers`, `followings`, `mutuals`, `similar`.
Content: `hashtags`, `mentions`, `locations`, `captions`, `likes`.
Interactions: `comments`, `wcommented`, `wtagged`.
Watch / diff: `watch`, `unwatch`, `watching`, `diff`, `history`.
Operational: `quota`, `health`, `config`, `purge`.
Session: `target`, `current`, `clear`.
Batch / dossier: `batch`, `dossier` (full target package: profile + media +
network + analytics, with `--maltego` CSV export).

Inside the REPL each command may be invoked with or without a leading `/`.

## Where things go

- `~/.insto/config.toml` — settings (mode `0600`).
- `~/.insto/store.db` — sqlite store: snapshots, watches, cli history.
- `~/.insto/logs/insto.log` — rotating log file (mode `0600`, secrets redacted).
- `./output/<user>/<type>/…` — downloaded media. Override with
  `[output_dir]` in config or `--out` on commands that accept it.

## Design & spec

Architecture is documented in [`docs/superpowers/specs/2026-04-27-insto-design.md`](docs/superpowers/specs/2026-04-27-insto-design.md)
(442 lines, source of truth).

Contributor docs: [`CLAUDE.md`](CLAUDE.md).

## License

MIT.
