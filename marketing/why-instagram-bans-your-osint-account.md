# I added HikerAPI to Osintgram. Then I rewrote it from scratch — here's what I learned

> **Draft for dev.to / Medium / personal blog.** Cross-post anchor: GitHub
> README links here as the long-form story. Estimated reading time:
> ~7 minutes. Suggested tags: `osint`, `python`, `security`, `instagram`,
> `cli`. Filename rationale: original SEO target was "osintgram banned"
> queries; the personal-angle version below is stronger and we keep the
> filename for the long-form anchor.

---

In August 2025 I shipped [PR #2586](https://github.com/Datalux/Osintgram/pull/2586)
to [Osintgram](https://github.com/Datalux/Osintgram), the most popular
Instagram OSINT CLI on GitHub (12k stars at the time). The PR added a
`hikercli` module that replicates every Osintgram command through
[HikerAPI](https://hikerapi.com) — a third-party endpoint over Instagram's
public surface — so users no longer had to feed in their own IG
credentials and watch the account get suspended within a week. The PR
got merged.

Six months later I open-sourced [`insto`](https://github.com/subzeroid/insto):
a from-scratch rewrite of the same use cases. This post is about why I
chose the rewrite over more contributions, what surfaced as I worked on
both codebases, and what `insto` does differently as a result.

This is not "Osintgram is bad." Osintgram is a beloved, battle-tested
project that did the hard work of mapping the Instagram OSINT command
surface back when nobody else had. I'm grateful it exists. But there are
a class of decisions you can only revisit at the architecture layer —
and once I'd seen them up close, building a clean second take was
faster than retrofitting the first one.

## What the contribution surfaced

The `hikercli` PR was supposed to be an evening's work: replace each
Instagram private-API call with the HikerAPI equivalent, return the same
shape, done. It wasn't. Here's what I ran into, in roughly the order I
ran into it:

**1. There is no shared DTO layer.** Each command builds its output
inline from whatever shape the underlying API returns. Swapping
backends means hand-aligning ~25 different ad-hoc dicts. There's no
single place that says "a `Profile` has these fields, in these types,
regardless of where it came from." Every backend swap is N changes,
not one.

**2. Errors are strings, not types.** `challenge_required`,
`feedback_required`, `login_required`, rate-limit responses — they
all surface as raw exception messages from the underlying private-API
library. The CLI catches them at the command boundary with broad
`except` clauses and prints whatever the SDK said. There's no
`RateLimited(retry_after=42)` or `Banned()` type to switch on, so retry
logic and user-facing messaging both have to grep strings.

**3. Sync everything.** Every command is a synchronous Python 2/3-compat
function. Adding `/watch` (poll a target, diff against the last
snapshot) wants a long-running task and a notification surface that
co-exists with the REPL. You can bolt a thread on, but the input
loop is `cmd2`, which doesn't naturally yield to background tasks.

**4. REPL-only.** Osintgram is wonderful interactively. It is not
scriptable. There's no `osintgram -c info nasa --json -` that you
can pipe into `jq` or schedule from cron. For "I have 2,000 handles
from a leaked dataset, run `info` on each and dump CSV," you'd shell
out 2,000 times.

**5. No persisted store.** Every session starts fresh. Snapshots,
watches, command history — none of it survives a restart unless you
build that yourself.

Each of these is a five-line fix individually. Together, they're an
architecture. Retrofitting an architecture into a project with 12k
stars and an active user base means breaking everyone's workflow on a
Tuesday. So I didn't.

## What `insto` is

[`insto`](https://github.com/subzeroid/insto) is the rewrite I wanted
to write while contributing to Osintgram. Same command surface as the
hikercli module — `info`, `posts`, `followers`, `comments`,
`hashtags`, `similar`, etc. Different bones underneath:

```
cli.py / repl.py     ← argparse + prompt_toolkit, --proxy / -c flag
        ↓
commands/            ← @command-decorated async functions
        ↓
service/             ← OsintFacade, exporter, history (sqlite snapshots,
                       watches, cli history), in-process watch loop
        ↓
backends/            ← OSINTBackend ABC, HikerBackend, AiograpiBackend,
                       FakeBackend (tests). _retry, _cdn, _hiker_map.
        ↓
models.py            ← @dataclass(slots=True) DTOs (Profile / Post /
                       Story / Comment / User …)
        ↓
exceptions.py        ← BackendError taxonomy (RateLimited, AuthInvalid,
                       QuotaExhausted, SchemaDrift, Banned, Transient …)
```

Concretely, what changed because of those layers:

**Async everywhere.** Backends, facade, commands. Python 3.11+. sqlite
via stdlib `sqlite3` wrapped in `asyncio.to_thread`. Strict mypy,
ruff-clean, ~93% coverage. CI gates on all of it.

**Typed errors, one retry policy.** `with_retry` is the only place
that handles `RateLimited.retry_after` sleeps and `Transient` retries.
`AuthInvalid`, `QuotaExhausted`, `SchemaDrift`, `Banned` propagate
immediately. The CLI's `_format_error` is the only place that
converts a backend exception into a user-facing string, and every
string flows through `_redact.redact_secrets()` first so tokens
never reach the rotating log.

**Schema drift instead of `KeyError`.** Every mapper raises
`SchemaDrift(endpoint, missing_field)` when HikerAPI's documented
fields move. Counter shown in `/health`. When HikerAPI adds a field
or renames one, you get a typed signal at the boundary instead of a
500-line stack trace from the bottom of a parsing function.

**One-shot mode is first-class.** `-c <cmd>` consumes the rest of
`argv` as the slash-command's arguments:

```sh
insto @ferrari -c info --json - | jq '.followers_count'
insto @ferrari -c followers 500 --csv followers.csv
insto @ferrari -c followers 200 --maltego                  # Maltego CSV
cat targets.txt | insto -c batch - info --yes               # stdin pipe
insto -c dossier instagram                                  # full target package
```

`--json -` and `--csv -` write to stdout; `/batch -` reads targets
from stdin. The REPL is great for exploration, but real investigative
pipelines need to be schedulable.

**Persisted store.** `~/.insto/store.db` holds snapshots, watches,
and command history. `/watch nasa 600` polls every ten minutes (5 min
floor). `/diff` shows what changed since the last snapshot.
`/dossier` collects a full target package (profile + media + network
+ analytics) and emits a Maltego CSV alongside the JSON envelope.

**Two backends, one ABC.** `OSINTBackend` is the contract.
`HikerBackend` (default) needs an API token, no IG account, no ban
risk. `AiograpiBackend` (optional, `pipx install 'insto[aiograpi]'`)
authenticates with username + password + optional 2FA TOTP for the
~10% of cases where you genuinely need the logged-in surface
(private profiles you follow, saved feed). The CDN streamer
(`backends/_cdn.py`) is shared across both — HTTPS-only, host
allowlist, MIME cross-check, byte budget, atomic write, filename
sanitization, disk guard.

## Who this is for

If you're a Kali Linux hobbyist running a few `info` lookups on
public profiles, Osintgram is fine — fewer dependencies, faster install,
and the hikercli backend (the one I wrote) keeps your account out
of harm's way.

`insto` is for the workflow one notch up:

- **Investigative journalists** with dozens to hundreds of handles
  from a leak, who need to script the lookup and export to Maltego or
  CSV instead of clicking through a REPL.
- **Threat intel and brand monitoring teams** running `/watch` on a
  rotating target list, persisting snapshots, diffing daily.
- **Security researchers and pentesters** who want a typed Python
  API to import (`from insto.service import OsintFacade`) instead of
  shelling out.
- **Anyone who lost an Instagram account to over-eager scraping** and
  doesn't want to do that again.

## Quick start

```sh
pipx install insto                        # or: uv tool install insto
insto setup                               # interactive wizard
insto @nasa -c info                       # one-shot
insto                                     # drops into the REPL
```

The default `hiker` backend needs a HikerAPI token. The free tier
(100 requests after registration) covers basic exploration; paid
tiers scale from there. The `aiograpi` backend is opt-in via the
extra and uses your own IG account — recommended only on a
dedicated burner.

## Honest trade-offs

- **HikerAPI is paid.** The logged-in approach is "free" only because
  you're paying with the account's lifespan. For light use, the free
  tier is plenty. For heavy enumeration, you'll spend real money — but
  you won't be hand-rotating burner accounts every Friday.
- **Closed-source dependency.** HikerAPI is a third-party service. The
  `OSINTBackend` ABC is designed so a new provider drops in as a
  ~300-line module, but the dependency is real today.
- **Younger project.** Osintgram has 12k stars and five years of bug
  reports against real targets. `insto` has ~93% test coverage and
  strict typing, but the long tail of "Instagram returned this weird
  shape on Tuesday" is shorter. File issues — they're how the schema
  drift counter pays for itself.

## Where to find it

- **GitHub:** [github.com/subzeroid/insto](https://github.com/subzeroid/insto)
- **Docs:** [subzeroid.github.io/insto](https://subzeroid.github.io/insto/)
- **Install:** `pipx install insto` (or `uv tool install insto`)
- **MIT-licensed.**

If this is useful to your workflow, a star on GitHub helps a lot, and
bug reports against real targets are gold. If you've used the
hikercli path on Osintgram and have feature requests that didn't fit
that codebase — open an issue, that's exactly the gap `insto` is
trying to fill.

---

*`insto` is for legitimate research, journalism, security work, and
personal data audits on accounts you have authorisation to investigate.
Don't use it to harass, dox, or surveil people. Don't violate
Instagram's ToS. The MIT license disclaims warranty; the ethics are
on you.*
