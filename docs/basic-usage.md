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
