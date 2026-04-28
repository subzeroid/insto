# Backends

`insto` is split between command surface and backend. The backend is the only layer that touches a third-party API; everything above it consumes DTOs (`Profile`, `Post`, `User`, `Comment`, ...) and never sees a raw HikerAPI / aiograpi dict.

The contract lives in `insto/backends/_base.py:OSINTBackend`. Two implementations ship as of 0.2.0:

| | **hiker** (default install) | **aiograpi** (`insto[aiograpi]`) |
|---|---|---|
| Authentication | API token | Username + password (+ 2FA) |
| Cost | Pay-per-call | Free |
| Account ban risk | None | Real |
| Stability | High | Brittle (Instagram churn) |
| Sees private accounts you follow | No | Yes |
| Sees DMs / saved feed | No | Yes (planned) |
| Quota visibility | Yes (`/sys/balance`) | No |
| Install footprint | base | `pip install 'insto[aiograpi]'` |

## Pick a backend

Default is **hiker**. Switch to aiograpi when you need data behind Instagram's login wall — private profiles you follow, saved feed, posts on accounts that 403 from logged-out HTTP. For OSINT on public profiles, hiker is the right choice nine times out of ten and carries no account-ban risk.

You can flip backends mid-session at any time by editing `~/.insto/config.toml` (or running `insto setup` again):

```toml
backend = "aiograpi"

[hiker]
token = "hk_live_..."          # kept around in case you flip back

[aiograpi]
username = "your.handle"
password = "..."
totp_seed = "JBSWY3DP..."      # optional 2FA seed
session_path = "/Users/you/.insto/aiograpi.session.json"
```

Precedence is **flag > env > toml > default** for every key:

| Key | Flag | Env |
|---|---|---|
| `backend` | _(no flag yet)_ | `INSTO_BACKEND` |
| `hiker.token` | `--hiker-token` | `HIKERAPI_TOKEN` |
| `hiker.proxy` | `--proxy` | `HIKERAPI_PROXY` |
| `aiograpi.username` | _(no flag)_ | `AIOGRAPI_USERNAME` |
| `aiograpi.password` | _(no flag)_ | `AIOGRAPI_PASSWORD` |
| `aiograpi.totp_seed` | _(no flag)_ | `AIOGRAPI_TOTP_SEED` |

## hiker — HikerAPI

Authenticates with a [HikerAPI](https://hikerapi.com) token. Pay-per-call, no Instagram login, no account-ban risk.

```sh
insto setup                                            # token wizard
export HIKERAPI_TOKEN=hk_live_...                      # or env var
insto -c info instagram --hiker-token=hk_live_...      # or per-call flag
```

What the backend handles:

- **Quota & balance** — read from `/sys/balance` on REPL startup and before every `/quota`. Surfaced in the welcome banner and the bottom toolbar.
- **Retries** — `with_retry` decorator. `RateLimited` (with `retry_after`) and `Transient` retry with exponential backoff + jitter; `AuthInvalid`, `QuotaExhausted`, `SchemaDrift`, `Banned` propagate immediately.
- **Schema drift** — every mapper raises `SchemaDrift(endpoint, missing_field)` instead of `KeyError` when HikerAPI's documented fields move. Counter shown in `/health`.
- **Cursor safety** — every `iter_*` method has a 1000-page hard cap so a server-side cursor loop cannot DOS the operator.
- **Proxy** — `--proxy` / `HIKERAPI_PROXY` / `[hiker].proxy` plumbed through `httpx`. `socks5h://` (Tor) supported.

### HikerAPI 403

HikerAPI proxies Instagram's HTTP statuses verbatim — when a call returns 403, that's Instagram refusing the request, not HikerAPI billing or scope. You'll see this most often on:

- `/similar` — Instagram retired the public suggested-users endpoint.
- Anything against an account that requires a logged-in session to read.

The fix is to switch to the aiograpi backend for that one command.

## aiograpi — Instagram private API (logged-in)

Optional, install separately:

```sh
uv tool install 'insto[aiograpi]'
pipx install 'insto[aiograpi]'
pip install 'insto[aiograpi]'           # in a venv only — see installation.md
```

Then run `insto setup`, pick `aiograpi`, paste your Instagram username + password, and (optionally) the TOTP seed for 2FA.

What works on aiograpi:

- `/info`, `/posts`, `/reels`, `/stories`, `/highlights`, `/followers`, `/followings`, `/mutuals`, `/comments`, `/captions`, `/likes`, `/wcommented`, `/hashtags`, `/mentions`, `/locations`, `/dossier`.
- Reads private profiles you follow.
- Login is **lazy** — the constructor stores credentials, the actual `client.login()` fires on the first network call. The session is then dumped to `~/.insto/aiograpi.session.json` (mode `0600`); subsequent runs reuse it without re-authenticating.

What does NOT work on aiograpi 0.7:

- `/similar` — Instagram retired the public suggested-users endpoint. No backend supports it.
- `/tagged` — aiograpi 0.7 does not expose `user_tag_medias`. Use the hiker backend for this command.

Both raise a clear `BackendError("...needs hiker backend")` instead of returning silently empty.

### Account-ban risk

aiograpi authenticates as a real Instagram user. Heavy use, fast scrapes, and especially `/dossier` runs against many targets in a row look like automation to Instagram's anti-abuse systems. Mitigations baked in:

- `/batch` defaults to concurrency 3 with 1s ± 25% jitter between target starts.
- `RateLimitError`, `PleaseWaitFewMinutes`, and `ClientThrottledError` translate to `RateLimited(retry_after)` and back off.
- `AccountSuspended` and `FeedbackRequired` translate to the typed `Banned` error so the CLI surfaces it cleanly instead of a stack trace.

For real OSINT work, **use a dedicated burner account** — never your personal Instagram. Insto's defaults are conservative, but Instagram's rules are theirs to enforce.

### 2FA (TOTP)

If your account has 2FA enabled, paste the **TOTP seed** (the 32-char base32 string Instagram shows when you set up an authenticator app) during `insto setup`. aiograpi will generate the 6-digit code on demand. Insto stores the seed in `~/.insto/config.toml` (mode `0600`) and registers it with the global redaction set so it never reaches error output or the rotating log file.

Don't have the seed? Either re-enable 2FA in Instagram's settings to capture it, or skip 2FA on the burner account.

## Visibility states

`Profile.access` is one of:

| State | hiker | aiograpi |
|---|---|---|
| `public` | ✓ | ✓ |
| `private` | ✓ | ✓ |
| `followed` | n/a | ✓ (private profile you follow) |
| `blocked` | n/a | ✓ (only via aiograpi error response) |
| `deleted` | ✓ | ✓ |

Commands that strictly need a logged-in account (DMs, saved feed, posts of a private profile you follow) carry a `requires=("followed",)` annotation. They run cleanly on aiograpi; on hiker they exit with a typed message.
