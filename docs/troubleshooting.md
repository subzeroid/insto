# Troubleshooting

## "no HIKERAPI_TOKEN configured"

You ran `insto` without setting up a token. Either:

```sh
insto setup                            # interactive wizard, writes ~/.insto/config.toml
# or
export HIKERAPI_TOKEN=hk_live_...
# or
insto -c info instagram --hiker-token=hk_live_...
```

Precedence is **flag > env > toml**.

## "balance: pending" in the welcome banner

The startup `/sys/balance` call hasn't returned yet. By default `insto` waits up to 2 s for this; on a slow network the banner falls back to "balance: pending" and the actual figure shows up the next time you run `/quota`.

If it stays "pending":

- Network blocked — try `--proxy http://your-proxy:8080`.
- Token wrong — `/health` will surface `AuthInvalid` on the next call.

## "schema drift in <endpoint>: missing field <f>"

HikerAPI changed the shape of one of its responses. The mapper refused to silently fill a default — this is a `SchemaDrift` exception, not a bug in your invocation.

What to do:

1. `/health` shows the schema-drift counter for this session.
2. Open an issue with the command you ran and the failing field name.
3. Workarounds typically land in a `0.1.x` patch.

## "filesystem does not support xattr; tagging skipped"

Showing once on macOS over an NFS-mounted output directory. `insto` is trying to set `com.apple.metadata:kMDItemUserTags=insto` on every downloaded media file so they're greppable in Finder. NFS doesn't carry xattr; the file is downloaded fine, only the tag is skipped.

No action needed.

## REPL: typing `/` doesn't open a popup

The slash popup needs a TTY. If you launched `insto` with stdin piped (e.g. inside a build script), prompt_toolkit prints `Warning: Input is not a terminal (fd=0).` and runs without the popup. Use one-shot CLI for scripts:

```sh
echo /info instagram | insto              # ← does not open popup
insto -c info instagram                   # ← script-friendly
```

If you have a TTY but the popup still doesn't show:

- Make sure your terminal renders 256-color or truecolor; Apple Terminal in 16-color mode won't show the muted help-meta column.
- If you're on `tmux`, `set -g default-terminal "tmux-256color"` is the usual fix.

## Disk full mid-`/dossier`

`/dossier` checks `shutil.disk_usage` ≥ 2 GB before starting. If you hit "disk full" during a download anyway, the CDN streamer aborts that file's `<pk>.part` (no rename), but already-downloaded files stay. `MANIFEST.md` is written with `partial: true`. Free space and run `/dossier` again — it doesn't dedupe, it writes to a new timestamped subdir.

## `/batch` keeps re-running already-processed targets

Resume state for a given input file lives in `output/.batch-<sha>.jsonl` where the sha hashes the input file's contents. If you tweaked the input list, the sha changes and resume doesn't see prior progress.

To force a clean run regardless of file content:

```sh
/batch users.txt info --restart
```

## "sqlite is locked — another insto session running?"

`migrate_to_latest()` is taking a `BEGIN IMMEDIATE` lock and another `insto` process is also starting. Wait for the other process to finish startup, or close it. The retry budget is 100ms / 250ms / 500ms.

If no other `insto` is running, your `~/.insto/store.db` got into a `journal_mode=wal` state from a previous crash:

```sh
sqlite3 ~/.insto/store.db 'PRAGMA wal_checkpoint(TRUNCATE);'
```

## `/health` shows `last_error: AuthInvalid`

Your HikerAPI token was rotated or revoked. Run `insto setup` again to paste a fresh one. The token is only stored in `~/.insto/config.toml` (mode `0600`); rotation is the right hygiene move after any leak.
