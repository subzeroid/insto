# Installation

`insto` is a Python ≥ 3.11 CLI distributed via PyPI.

## From PyPI (recommended)

Pick one — all three install `insto` into an isolated venv and put it on `$PATH`:

```sh
uv tool install insto                       # if you have uv
pipx install insto                          # if you have pipx
brew install pipx && pipx install insto     # macOS, nothing yet
```

### With the aiograpi backend

`insto[aiograpi]` adds the optional [aiograpi](https://github.com/subzeroid/aiograpi) dependency so you can authenticate as a real Instagram user (private accounts you follow, login-walled endpoints) instead of paying per call against HikerAPI. Pick the same install path you would for the bare CLI, with the extras marker added:

```sh
uv tool install 'insto[aiograpi]'
pipx install 'insto[aiograpi]'
pip install 'insto[aiograpi]'                # in a venv only — see PEP 668 below
```

After install, `insto setup` will offer `backend (hiker | aiograpi)` and prompt for the credentials of the chosen backend. See [Backends](backends.md) for the trade-offs and the account-ban risk.

### What about `pip install insto`?

It does **not** work out of the box on modern systems and that is intentional, not an `insto` bug.

Since pip 23.0, [PEP 668](https://peps.python.org/pep-0668/) instructs pip to refuse system-wide installs on Python distributions marked as "externally managed" — Homebrew Python on macOS, the system Python on Debian 12+, Ubuntu 23.04+, Fedora 38+, etc. You will see:

```text
error: externally-managed-environment
× This environment is externally managed
```

Two ways forward:

- **Recommended:** use `pipx` or `uv tool install`, see above. Each CLI lives in its own venv, so `insto`'s deps cannot break unrelated tools (or your OS Python).
- **Inside a venv** (developer flow): `python -m venv .venv && source .venv/bin/activate && pip install insto`. The PEP 668 mark is per-Python, and venv Pythons are not marked.
- **Override** (not recommended): `pip install --break-system-packages insto`. The flag does what it says.

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
