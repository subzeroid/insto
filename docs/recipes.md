# OSINT recipes

Concrete investigative scenarios — each is a 3–5 command sequence that combines `insto` primitives into something useful. Copy-paste, adjust the target, read the output.

> **Scope.** These recipes assume a legitimate investigative interest: due diligence, security research, journalism, brand-protection, or your own digital-footprint audit. `insto` is a tool, not a license to surveil people you don't have a reason to investigate.

---

## 1. Where does this account *probably* live?

> Use case: anonymous account with no city in bio, but enough geotagged posts.

```sh
insto -c info @target               # baseline — public/private, country in user_about
insto -c where @target --limit 100  # the actual answer
insto -c timeline @target --limit 100   # cross-check via posting hours
```

What to read:

- `/where` **anchor** — the most-frequent geotagged place. If 30%+ of geotagged posts cluster on one location, that's a strong "home" signal.
- `/where` **radius_km** < 50 → metro-area-bound (lives there). 50–500 → regional. 500+ → traveller; the anchor is "frequent destination" not "home".
- `/timeline` peak hour in UTC → time-zone confirmation. A posting peak at 18:00–22:00 local time is the human-after-work pattern; convert from UTC to confirm the city.

Cross-check signal: `/info`'s `country` field comes from IG's own user_about (which IG infers from device/IP, not bio). When `/where`'s anchor and `/info`'s country agree, confidence is high.

---

## 2. Map a brand's superfans into a Maltego graph

> Use case: brand-protection / influencer research — who actually engages, not just follows.

```sh
insto @brand -c fans --limit 50 --maltego        # 50 posts → top 20 fans, weighted score
insto @brand -c wcommented --limit 50 --maltego  # commenter axis only
insto @brand -c wliked --limit 50 --maltego      # liker axis only
```

Output goes to `output/<brand>/` as three Maltego CSVs. Import each into a separate Maltego graph and overlay — accounts that show up in **all three** (high score on `/fans` + present in `/wcommented` + present in `/wliked`) are the brand's true ambassadors.

Then walk one level out:

```sh
insto @top_fan -c info --json -                  # quick check: human or bot?
insto @top_fan -c posts 5 --no-download --json - # 5 most-recent: do they post about the brand?
insto -c intersect top_fan another_top_fan       # do the top fans follow each other?
```

If the top fans all follow each other and post overlapping content, you've found the organic community. If they don't, the engagement is paid / coordinated.

---

## 3. Detect coordinated inauthentic behaviour

> Use case: investigative journalism / platform integrity — is a "movement" actually one operator with sockpuppets?

```sh
# Suspicious accounts you want to test for coordination:
insto -c intersect handle_a handle_b --limit 5000 --maltego
insto -c intersect handle_a handle_c --limit 5000 --maltego
insto -c intersect handle_b handle_c --limit 5000 --maltego
```

Then for each suspicious account:

```sh
insto -c timeline @handle_a --limit 100 --json -
insto -c timeline @handle_b --limit 100 --json -
insto -c timeline @handle_c --limit 100 --json -
```

The signals:

- **Posting cadence** — sockpuppets run from the same machine usually share a timezone *and* a posting hour pattern. Identical hour-of-day histograms across "different" accounts is a red flag.
- **Follower overlap** — large `/intersect` result between accounts that publicly distance themselves from each other suggests shared audience pumping, possibly shared operator.
- **Cross-engagement** — run `/wliked` on @handle_a and check if @handle_b / @handle_c appear at the top. Self-engagement is a tell.

For brand-protection, repeat with the brand's recent praise-comments accounts — coordinated review-fraud follows the same pattern.

---

## 4. Geo-OSINT from a single screenshot

> Use case: you have a permalink (or just a screenshot URL) and want everything `insto` can derive.

```sh
insto -c postinfo "https://www.instagram.com/p/DXPduuvEY7S/"   # full Post DTO
# → reveals: owner, taken_at, location_name+pk, hashtags, mentions, media URL
```

If the post has a location pk:

```sh
insto -c placeposts <location_pk> --limit 50 --json -    # 50 other posts at the same place
```

Then go one level out via the owner:

```sh
insto @<owner> -c where --limit 50          # do they keep posting from this place?
insto @<owner> -c timeline --limit 50       # is this their typical posting time?
insto @<owner> -c followers 200             # the social context
```

This chain takes you from a single image URL to a complete picture: who posted, when (date + time-of-day pattern), where (this post + their other locations), and who their immediate network is.

---

## 5. Build an evidence archive before a post is deleted

> Use case: posts get deleted; reputational claims need a snapshot you control.

```sh
insto @<target> -c dossier --maltego                # full target package, Maltego-ready
insto -c postinfo "<post_url>" --json -                              > post-snapshot.json
insto @<target> -c posts 100 --json output/<target>/posts-100.json   # last-100-posts archive
insto @<target> -c stories --json -                                  > stories.json
```

`/dossier` writes `MANIFEST.md` with `captured_at` and `schema` so the archive is self-describing. The Maltego CSVs make the network graph re-importable a year later. `--json -` streams to stdout so you can also pipe through `jq` and into your case-management tool.

For a watch loop ("notify me when this account changes"):

```sh
insto                       # REPL
> /watch @target 600        # poll every 10 min (5-min floor)
> /diff @target              # show changes since last snapshot
```

Snapshots accumulate in `~/.insto/store.db` — `/history` shows the running command log so you can prove provenance later.

---

## 6. Find the human behind a brand account

> Use case: regulatory/legal due diligence — accounts hide behind PR. Who runs them?

```sh
insto @brand -c info                            # bio for clues, business category
insto @brand -c about                            # creation date — when was it set up?
insto @brand -c followings --limit 200 --csv -   # who DOES the brand follow?
```

Brand accounts following ~10–30 accounts are the tell: those are usually the operator's personal accounts, family, employees, agency. Filter the CSV to accounts with low follower counts (real people) vs verified accounts (just clout-following).

Then for each suspicious-personal account:

```sh
insto @suspect -c info --json -                           # baseline
insto @suspect -c where --limit 50                        # are they at the same place as the brand?
insto -c intersect brand suspect --limit 5000             # shared audience density
```

A small private account that:

- the brand follows
- shares 50%+ followers with the brand
- geo-fingerprints to the same city as the brand's HQ

…is almost always the operator. Cross-check with LinkedIn / corporate filings before publishing.

---

## 7. Audit your own digital footprint

> Use case: pre-employment, journalism, security review — before someone runs OSINT on *you*, run it on yourself.

```sh
insto @<your_handle> -c info        # everything public about you in one view
insto @<your_handle> -c where --limit 200    # how identifiable is your geo footprint?
insto @<your_handle> -c timeline --limit 200 # what does your routine look like?
insto @<your_handle> -c hashtags --limit 100 # which hashtags identify you?
insto @<your_handle> -c mentions --limit 100 # who do you talk to publicly?
```

Then audit the dossier to see what you're leaking:

```sh
insto @<your_handle> -c dossier --no-download --json -
```

Triage actions: tighten location-tagging defaults, audit which posts have geotags, untag yourself from public posts, review `/wcommented` to see who frequents your comments and whether you'd want them to.

---

## OPSEC reminders

- All commands respect `--proxy socks5h://127.0.0.1:9050` for Tor egress. The `socks5h` (not `socks5`) routes DNS through Tor too.
- `insto setup --non-interactive` in CI/automation contexts: no terminal prompts, fails loudly when secrets are missing.
- Tokens, passwords, TOTP seeds are auto-redacted from every error message and log line via `_redact.redact_secrets`. They never reach a traceback even when an exception's `args` carries them.
- Local-only state at `~/.insto/` (mode `0700` dir, `0600` files). No phone-home, no telemetry. Run `insto -c purge cache` to wipe downloads when finished.
