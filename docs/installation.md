# Installation

`insto` is a Python ≥ 3.11 CLI distributed via PyPI.

## From PyPI (recommended)

```sh
uv tool install insto
```

`uv` puts the `insto` script into a self-contained venv and exposes it on `$PATH`. Same effect with pip:

```sh
pipx install insto
# or
python -m pip install --user insto
```

## With shell completion

```sh
uv tool install 'insto[completion]'        # adds shtab dependency
insto --print-completion zsh > ~/.zsh/_insto
# or for bash:
insto --print-completion bash | sudo tee /etc/bash_completion.d/insto
```

`insto --print-completion` without `[completion]` prints a hint and exits non-zero.

## From a checkout (development)

```sh
git clone git@github.com:subzeroid/insto.git
cd insto
uv sync --extra dev
uv run insto --help
```

See [Contributing](contributing.md) for the full dev workflow (lint, types, tests, release).

## First run

```sh
insto setup
```

Interactive wizard. Writes `~/.insto/config.toml` (mode `0600`) with:

- `hiker.token` — your [HikerAPI](https://hikerapi.com) access key.
- `hiker.proxy` (optional) — `http://`, `https://`, or `socks5h://` proxy URL.
- `output_dir` — where downloads and exports land (resolved to absolute).
- `db_path` — where the sqlite store lives (default `~/.insto/store.db`).

Token can also live in:

- `--hiker-token <value>` — per-call flag (overrides everything else).
- `HIKERAPI_TOKEN` env — overrides the toml file.

Precedence: **flag > env > toml**. Same shape for `--proxy` / `HIKERAPI_PROXY` / `[hiker].proxy`.

`~/.insto/` is created mode `0700`; `store.db` and `config.toml` are `0600`. The setup wizard refuses to write a world-readable file.
