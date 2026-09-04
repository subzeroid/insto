# Basic usage

Two surfaces, same command grammar. Anything you can do in the REPL you can do as a one-shot — including pipelines.

## REPL

```sh
insto                      # REPL
insto @instagram           # REPL with @instagram pre-selected as the target
```

You land in a prompt with a welcome panel (INSTO logotype + tips + shortcuts + recent activity + live HikerAPI balance) and a bottom toolbar showing the current target, backend, and remaining requests. Launching with `insto @user` pre-selects the target (shown in the banner + the `insto @user>` prompt); the pk resolves on first use.

```text
insto @→ /
```

Type `/` and the popup opens with every command (slash-popup like Claude Code). Tab cycles. Enter runs.

```text
/target instagram          # set the active target
/info                      # full profile dump
/posts 10                  # last 10 posts (downloads media to ./output/instagram/posts/)
/followers --csv -         # 50 followers, CSV streamed to stdout
/dossier                   # collect a full target package under output/instagram/dossier/<ts>/
```

Any single-target command also accepts an inline username — does NOT mutate the active target:

```text
/info nasa                 # one-off lookup, target stays at @instagram
```

`Ctrl+T` flashes the active target. `Ctrl+L` redraws the welcome banner. `Ctrl+C` cancels the current line. Up-arrow walks history; `Ctrl+R` is incremental search. These shortcuts are also listed in the welcome banner.

`/help` lists every command grouped by category. `/theme` (with no name) opens an interactive picker — `↑`/`↓` live-preview each theme on the banner, Enter applies; `/theme <name>` switches directly. Themes: `aiograpi` (default), `amber`, `claude`, `cyberpunk`, `hacker`, `instagram`.

`/exit`, `/quit`, or `Ctrl+D` to leave.

## One-shot

```sh
insto @instagram -c info               # → rich profile panel
insto -c info nasa                     # inline target, no REPL state
insto @nasa -c posts 5 --no-download   # URLs only, no CDN write
insto @nasa -c hashtags --json -       # JSON to stdout
insto @nasa -c followers --maltego     # Maltego CSV under output/nasa/
insto -c dossier instagram             # full target package
```

## Pipelines

```sh
# Fan a list of usernames into batched lookups
cat targets.txt | insto -c batch info -

# Pipe profile JSON through jq
insto @nasa -c info --json - | jq '.data.profile.followers'

# Count posts containing a hashtag
insto @nasa -c hashtags --csv - | awk -F, '$2=="space"{print $3}'
```

`/batch <file> info` (or `-` for stdin) runs the named command across many targets with concurrency cap 3 (override with `--concurrency`), 1s±25% jitter between starts, dedup, and JSONL resume on `output/.batch-<sha>.jsonl`. Re-running with the same file picks up where it left off; `--restart` clears resume state.

## Watching for changes

```text
/watch nasa 600            # poll every 10 minutes (300s is the floor)
/watching                  # list persisted active/paused watches
/diff nasa                 # diff vs the most recent snapshot
/unwatch nasa
```

`/watch` writes to sqlite immediately. A REPL executes the registered watches
when it owns the store's executor lock; if another REPL or daemon owns it, the
registration is still saved and that owner discovers it within about two
seconds. One-shot registration also persists without keeping the command alive:

```sh
insto @nasa -c watch 600   # save a 10-minute watch, then exit
insto watch-daemon         # foreground executor; Ctrl+C or SIGTERM stops it
```

There can be at most three active watches and the interval floor is 300 seconds.
A tick retries once. Two consecutive failed ticks pause that target across
process restarts; authentication or account-ban errors pause it immediately.
Run `/watch nasa 600` again to reactivate a paused row, or `/unwatch nasa` to
delete it. A successful tick clears the stored error counter.

On startup the daemon reports its sqlite path, recovered count, estimated
ticks/backend calls per hour, and the relevant quota/cost (HikerAPI) or
rate-limit/account (aiograpi) risk. At the minimum interval, three watches mean
36 ticks/hour and an estimated 72-108 backend calls/hour. Recovered overdue
targets are staggered by two seconds, and each target uses fixed-delay polling,
so slow calls never overlap or create catch-up bursts.

The daemon applies retention on startup and hourly: command history older than
90 days and snapshots older than 30 days are removed, keeping at most 100
snapshots per target after each pass. Counts can exceed that cap between passes.
A failed cleanup is retried at the next pass without stopping monitoring.
Startup, watch results, and warnings are flushed immediately, including when
stdout is redirected to a file or captured by a service manager.

### macOS user service

To keep monitoring after closing the terminal, install the package in a durable
environment and configure credentials with `insto setup`. Then:

```sh
insto @nasa -c watch 600
insto watch-service install
insto watch-service status
insto watch-service status --json
```

`install` creates one user LaunchAgent per canonical `INSTO_HOME`, loads it in
the current GUI login session, and enables loading at later logins. It does not
use `sudo`. A nonzero daemon exit is restarted by macOS with launchd's normal
throttling. A graceful zero-status exit stays stopped; use uninstall followed
by install to load it again. A REPL already owning the same database can delay
the service's first successful start; exit that executor and let the service
retry. The same database lock prevents overlapping executions.

The service stops at logout and cannot poll during sleep or shutdown. Running
it on a laptop is not an always-on availability guarantee. A GUI login domain
must exist to install it; a remote/headless session may not provide one.

#### Service credentials and webhook

Install uses the protected `config.toml`; it does **not** copy the current
terminal's tokens, proxy variables, or other environment settings. If your
interactive configuration is environment-only, first persist provider settings
with `insto setup`, or explicitly supply an existing private service env file:

```toml
[env]
INSTO_WATCH_WEBHOOK_URL = "https://receiver.example/hook/REPLACE_ME"
```

Create that file privately with your editor, keep it outside the repository,
and restrict it to your user before installation:

```sh
chmod 600 /absolute/path/service-env.toml
insto watch-service install --env-file /absolute/path/service-env.toml
```

Allowed keys in `[env]` are `HIKERAPI_TOKEN`, `HIKERAPI_PROXY`,
`AIOGRAPI_USERNAME`, `AIOGRAPI_PASSWORD`, `AIOGRAPI_TOTP_SEED`, and
`INSTO_WATCH_WEBHOOK_URL`. Values must be strings without NUL characters.
An empty provider value falls back to `config.toml`; an empty webhook value
disables notifications. No other sections, keys, shell commands, interpolation,
or arbitrary environment variables are accepted. Inputs are limited to 64 KiB
and must be regular, user-owned files with no group/other permissions; symlinks
are refused. Checks repeat on every service start.

This is an explicit service-only injection of the existing environment
setting. The installer stores only the file path, never its secret contents
in the plist, manifest, SQLite, or command arguments. It neither creates nor
removes your env file. Ambient proxy/CA settings are cleared in the service;
only the explicit provider proxy is used. Webhook proxy behavior is unchanged.

#### Status and recovery

`status` needs no provider credentials and makes no provider calls. It reports
installation and launchd registration separately from best-effort process
diagnostics, plus persisted active/paused watches and last successful ticks.
An installed or registered service is not necessarily healthy. Unknown macOS
diagnostic formats produce unknown process fields; a missing or unreadable
store is reported distinctly. Historical errors are shown only as present or
absent, not printed, because they could contain old credentials.

`--json` returns a `schema_version: 1` status object; successful inspection is
not a health-check assertion. Status never initializes or migrates a database.

Service logs are private and rotated under
`$INSTO_HOME/services/watch/logs/insto.log` (default home: `~/.insto`), with a
5 MiB file limit and three backups. Look there for startup/configuration
failures and watch warnings. Unsafe log destinations are refused.

The interpreter, backend, database/output/session paths, and env-file reference
are fixed at installation. Credential values are read anew on restart.
An identical install does not interrupt a loaded service. After moving the
Python environment or changing pinned paths/backend, explicitly reinstall:

```sh
insto watch-service uninstall
insto watch-service install --env-file /absolute/path/service-env.toml
```

If bootstrap fails, owned artifacts remain available for a safe retry or
uninstall. If unload cannot be confirmed, files are retained with an error;
do not manually remove a live service's files. Foreign or altered artifacts
are refused rather than silently overwritten. Uninstall preserves SQLite,
watches, snapshots, credentials, env files, logs, and stable lock files.
Linux and Windows service managers are not supported in this slice.

### Watch webhook notifications

Set `INSTO_WATCH_WEBHOOK_URL` in the environment of the REPL or watch daemon to
send a JSON notification when a watched account changes. The setting is
environment-only: it is not accepted in `config.toml`, as a CLI argument, or in
the sqlite store. Empty and unset values disable delivery. `/config` shows the
setting as only `configured` or `disabled` and never prints the URL.

Webhooks are active only in a persistent process that owns the watch executor
lock: either the REPL or `insto watch-daemon`. A one-shot command never validates
or uses the endpoint, including `insto @user -c watch`.

After a watch tick persists its new snapshot and writes its terminal result,
insto sends one HTTP POST with `Content-Type: application/json` and a version-1
event only when the current diff has a non-empty `changes` object. A first
snapshot or unchanged tick sends nothing.
`previous_usernames` supplies historical context for an otherwise real change;
it cannot trigger an event by itself.

```json
{
  "schema_version": 1,
  "event": "watch.changed",
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "username": "target",
  "observed_at": "2026-09-04T04:30:00Z",
  "changes": {
    "biography": {"old": "before", "new": "after"}
  },
  "previous_usernames": ["old_target"]
}
```

`observed_at` is UTC. A `2xx` response succeeds. Transport errors, timeouts,
`408`, `429`, and `5xx` responses get at most three total attempts, with 0.25
seconds and then 1 second between them; every attempt has a hard five-second
deadline. Redirects are not followed, and `3xx` or any other `4xx` response is
not retried. Delivery uses a direct `trust_env=False` route, so ambient proxy
and CA environment variables do not affect it. Response bodies are closed
without being read and are never logged.

Delivery failures produce only a redacted warning. They do not count as failed
watch ticks, increment the watch error streak, or pause an otherwise healthy
watch. Delivery is best-effort: an ambiguous network result can create a
duplicate, so receivers should deduplicate on `event_id`. A process crash after
snapshot persistence but before delivery can lose the event; there is no
persistent outbox.

Treat the endpoint as a secret and use a receiver you trust because account
diffs can contain sensitive data. Use HTTPS except for local testing: plain HTTP
is accepted only for `localhost` and loopback IP addresses. The endpoint is
redacted from output and logs and is never persisted.

The foreground daemon, signals, and single-executor advisory lock are POSIX-only
in this release. Use a shell, service manager, or terminal multiplexer if the
process should be restarted automatically after a machine reboot.

## Privacy

```text
/purge history              # wipe ~/.insto/store.db cli_history table
/purge snapshots --user @x  # wipe snapshots for one target
/purge cache                # delete ./output/
/config                     # show effective config + per-key source (flag / env / toml / default)
/quota                      # fresh /sys/balance hit
/health                     # backend ping + last error + schema-drift counter
```

`/purge` always interactively confirms unless `--yes` is passed.
