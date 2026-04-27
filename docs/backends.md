# Backends

`insto` is split between command surface and backend. The backend is the only layer that touches a third-party API; everything above it consumes DTOs (`Profile`, `Post`, `User`, `Comment`, ...) and never sees a raw HikerAPI dict.

The contract lives in `insto/backends/_base.py:OSINTBackend` (renamed from `InstagramBackend` in v0.1 to keep the door open for non-Instagram backends in v1.x).

## v0.1: HikerBackend (default)

Authenticates with a [HikerAPI](https://hikerapi.com) token. Pay-per-call, no Instagram login, no account-ban risk.

```sh
insto setup                                          # token wizard
export HIKERAPI_TOKEN=hk_live_...                    # or env var
insto -c info instagram --hiker-token=hk_live_...    # or per-call flag
```

What the backend handles:

- Quota & balance — read from `/sys/balance` on REPL startup and before every `/quota`. Surfaced in the welcome banner and the bottom toolbar.
- Retries — `with_retry` decorator; `RateLimited` (with `retry_after`) and `Transient` retry with exponential backoff + jitter, capped at `max_delay`. `AuthInvalid`, `QuotaExhausted`, `SchemaDrift`, `Banned` propagate immediately.
- Schema drift — every mapper raises `SchemaDrift(endpoint, missing_field)` instead of `KeyError` when HikerAPI's documented fields move. The drift counter is shown in `/health`.
- Cursor safety — every `iter_*` method has a 1000-page hard cap so a server-side cursor loop cannot DOS the operator.
- Proxy — `--proxy` / `HIKERAPI_PROXY` / `[hiker].proxy` is plumbed through HikerAPI's `httpx.AsyncClient`. `socks5h://` (Tor) supported.

## v0.2: AiograpiBackend (planned)

Ships in v0.2. Uses [`aiograpi`](https://github.com/subzeroid/aiograpi) — i.e. a real Instagram private-API client, authenticated as a regular user account.

| | hiker (v0.1) | aiograpi (v0.2) |
|---|---|---|
| Authentication | API token | Username + password (+ 2FA) |
| Cost | Paid per call | Free |
| Account ban risk | None | Real |
| Stability | High | Brittle (Instagram API churn) |
| Sees private accounts you follow | No | Yes |
| Sees DMs / saved feed | No | Yes |
| Quota visibility | Yes (`/sys/balance`) | No |

Commands that strictly need a logged-in account (DMs, saved feed, posts of a private profile you follow) are annotated with `requires=("followed",)` in v0.1 already — they error out cleanly on the hiker backend with a "needs aiograpi" message. The command layer does not branch on backend name.

## Visibility states

`Profile.access` is one of:

| State | Hiker | Aiograpi |
|---|---|---|
| `public` | ✓ | ✓ |
| `private` | ✓ | ✓ |
| `followed` | n/a | ✓ (private profile you follow) |
| `blocked` | n/a | ✓ (only via aiograpi error response) |
| `deleted` | ✓ | ✓ |

## Choosing

Default is **hiker**. Pick aiograpi when:

- You need data from a private account you legitimately follow.
- You want zero per-call cost and accept the account-ban risk.
- You're OK with brittleness — Instagram regularly breaks aiograpi.

For OSINT on public profiles, hiker is the right choice nine times out of ten.
