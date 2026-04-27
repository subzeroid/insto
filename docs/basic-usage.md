# Basic usage

Two surfaces, same command grammar. Anything you can do in the REPL you can do as a one-shot — including pipelines.

## REPL

```sh
insto
```

You land in a prompt with a welcome panel (INSTO logotype + tips + recent activity + live HikerAPI balance) and a bottom toolbar showing the current target, backend, and remaining requests.

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

`Ctrl+T` flashes the active target. `Ctrl+L` redraws the welcome banner. Up-arrow walks history; `Ctrl+R` is incremental search.

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
/watch nasa 10m            # poll every 10 minutes (5m is the floor)
/watching                  # list active watches
/diff nasa                 # diff vs the most recent snapshot
/unwatch nasa
```

Watches are session-local — they die when you exit. Persistent watches (daemon mode) are deferred to v0.2.

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
