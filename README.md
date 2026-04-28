# insto

Interactive Instagram OSINT CLI on the [HikerAPI](https://hikerapi.com) backend.

![demo](docs/demo.gif)

Two surfaces over the same command grammar:

- **REPL** — `insto` drops you into a prompt-toolkit session with tab-completion,
  a bottom toolbar (active target, backend, quota), and live `/watch`
  notifications. Visually similar to the Claude Code welcome screen.
- **One-shot** — `insto @user -c <command> [args]` runs a single slash-command
  and exits. Pipe-friendly: `--json -` writes to stdout, `--csv -` does the
  same for flat commands, `/batch -` reads targets from stdin.

## Install

Requires Python ≥ 3.11. Pick the install path that matches how you keep
other CLIs:

```sh
uv tool install insto              # uv users — fastest, no venv to manage
pipx install insto                 # pip users — same effect, classic tool
brew install pipx && pipx install insto   # macOS, no Python yet
```

Or from a checkout (development):

```sh
git clone git@github.com:subzeroid/insto.git
cd insto
uv sync && uv run insto --help     # editable inside .venv
# or:  uv tool install --editable .   to put `insto` on $PATH
```

> ℹ️ **Bare `pip install insto` does not work on modern systems by default**
> (PEP 668 — Homebrew Python, Debian 12+, Ubuntu 23.04+ all reject system-wide
> pip writes). Use `pipx` or `uv tool install` — both create an isolated
> venv per CLI, no manual sourcing.

## Setup

```sh
insto setup
```

Interactive wizard. Writes `~/.insto/config.toml` (mode `0600`) with your
HikerAPI token, output directory, sqlite store path, and optional proxy.
The token is read with `getpass` so it does not echo to the terminal; pass
`-` for the proxy to clear a previously-saved value.

Token precedence is **flag > env (`HIKERAPI_TOKEN`) > config.toml**; the same
precedence applies to the proxy (`--proxy`, `HIKERAPI_PROXY`,
`[hiker].proxy`). `socks5h://` (Tor) and `http://` proxies are both
supported.

### Environment variables

| Variable          | Purpose                                                             |
|-------------------|---------------------------------------------------------------------|
| `HIKERAPI_TOKEN`  | API token (overrides `[hiker].token` in config.toml)                |
| `HIKERAPI_PROXY`  | Proxy URL (overrides `[hiker].proxy`)                               |
| `INSTO_HOME`      | Override the default `~/.insto/` config root                        |
| `INSTO_BACKEND`   | Set to `fake` for the network-free backend used by the e2e suite    |

## Examples

REPL:

```text
$ insto
                                Tips for getting started
  ___ _   _ ____ _____ ___      /target <user>  set OSINT target
 |_ _| \ | / ___|_   _/ _ \     /info           full profile dump
  | ||  \| \___ \ | || | | |    /help           list all commands
  | || |\  |___) || || |_| |
 |___|_| \_|____/ |_| \___/     Recent activity
                                @nasa
 i n s t o  ⇋  o s i n t        @instagram
 instagram tool · open-source intel
                                hiker · 14.7M requests left · $4,417 · 15 rps cap

insto @→ /
```

Type `/` and the popup opens with every command (Slack / Claude Code style):

```text
insto @→ /info
> /target ferrari
> /info
> /posts 10                   # last 10 feed posts, media saved under output/ferrari/posts/
> /posts 10 --no-download     # URLs only, no CDN write
> /followers 500 --csv followers.csv
> /diff
> /watch ferrari 600          # poll every 10 minutes (5 min floor)
> /dossier                    # collect a full target package
> /quit
```

`/info <user>` is also valid as inline form — runs the lookup without
mutating the active session target. Same for every single-target
command (`/posts nasa 5`, `/dossier nasa`, ...).

One-shot:

```sh
insto @ferrari -c info
insto -c info instagram                                    # inline target, no REPL state
insto @ferrari -c posts 10 --json -                        # 10 posts, JSON to stdout
insto @ferrari -c followers 500 --csv followers.csv
insto @ferrari -c followers 200 --maltego                  # Maltego CSV under output/ferrari/
cat targets.txt | insto -c batch - info --yes              # stdin pipe + non-interactive
insto -c dossier instagram                                 # full target package
```

`-c <cmd>` consumes the rest of `argv` as the slash-command's arguments,
so `-c batch targets.txt info` runs `batch targets.txt info` (one `-c`
per invocation). `--yes` is required when `/batch` reads from stdin or
when the target list exceeds the confirmation threshold.

### Global flags

| Flag                            | Purpose                                                  |
|---------------------------------|----------------------------------------------------------|
| `-c / --cmd <name> [args...]`   | One-shot mode: run a single slash-command and exit       |
| `-i / --interactive`            | Force the REPL even when a target is provided            |
| `--proxy <url>`                 | Override `HIKERAPI_PROXY` for this invocation            |
| `--json [PATH or -]`            | Write the JSON envelope (default path, file, or stdout)  |
| `--csv  [PATH or -]`            | Same for flat-row commands                               |
| `--maltego [PATH or -]`         | Maltego entity-import CSV (alias for `--output-format maltego`) |
| `--output-format {json,csv,maltego}` | Explicit format selector                            |
| `--limit N` / `--no-download`   | Per-command paging cap and media opt-out                 |
| `--yes / -y`                    | Skip confirmation prompts (required for `/batch -`)      |
| `--verbose` / `--debug`         | Logging level for `~/.insto/logs/insto.log`              |
| `--version`                     | Print the version and exit                               |
| `--print-completion {bash,zsh}` | Emit a shell-completion script                           |

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

## Documentation

Full docs at <https://subzeroid.github.io/insto/>:

- [Installation](https://subzeroid.github.io/insto/installation/)
- [Basic usage](https://subzeroid.github.io/insto/basic-usage/)
- [CLI reference](https://subzeroid.github.io/insto/cli-reference/)
- [Backends](https://subzeroid.github.io/insto/backends/)
- [Architecture](https://subzeroid.github.io/insto/architecture/)
- [Troubleshooting](https://subzeroid.github.io/insto/troubleshooting/)

Contributing: see [CONTRIBUTING.md](CONTRIBUTING.md). Security policy: [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
