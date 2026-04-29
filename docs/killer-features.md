# Killer features

A short tour of the commands that make `insto` worth installing. Each has a copy-pasteable one-shot example, real output, and what to do with it.

> Looking for **multi-step investigative scenarios** rather than per-command examples? See [OSINT recipes](recipes.md) — 7 concrete chains (find someone's likely city, detect sockpuppet rings, evidence-archive a post, audit your own footprint, …).

---

## 🔥 `/where` — geo fingerprint

> *Where does this target actually live?*

Walks the target's recent posts, extracts GPS from every geotag, and computes an **anchor place** (the most-frequent location — proxy for "home" or "office"), the **centroid** of all geotag points, and the **max spread radius**.

```sh
insto -c where ferrari --limit 30
```

```text
@ferrari geo fingerprint — 15 of 30 posts geotagged

  anchor:    Maranello  (44.5256°N, 10.8664°E) — 7 posts (47%)
  centroid:  43.623°N, 1.559°E — max radius 9575 km

  Maranello                         7  ██████████████████████████████
  Big Sky, Montana                  2  █████████
  Zandvoort                         1  ████
  Niseko, Japan                     1  ████
  Korea                             1  ████
  ...
```

**Read the result:** Maranello is the Ferrari HQ — 47% of geotagged posts there is the strongest possible "home" signal. Big Sky / Niseko / Zandvoort are travel (racing circuits, ski). The 9 575 km radius makes the centroid meaningless (Italy ↔ Japan span pulls the mean).

JSON export carries `anchor`, `centroid_lat`, `centroid_lng`, `radius_km`, `places[*]` for downstream tooling.

---

## 🔥 `/dossier` — full target package

> *Everything I know about this account, in one folder.*

Composite command: `/info` + `/posts` + `/followers` + `/following` + `/mutuals` + `/hashtags` + `/mentions` + `/locations` + `/wcommented` + `/wtagged`. Sections fan out concurrently; one section's failure (rate-limit, 403) doesn't kill the rest — `MANIFEST.md` flags `partial: true` with the failure list.

```sh
insto -c dossier ferrari --no-download              # everything except media
insto -c dossier ferrari --maltego                  # Maltego-importable CSV per section
```

```text
output/ferrari/dossier/2026-04-29T11:08Z/
├── MANIFEST.md
├── profile.json
├── posts.json
├── followers.maltego.csv          # with --maltego, otherwise followers.csv
├── following.maltego.csv
├── mutuals.maltego.csv
├── hashtags.maltego.csv
├── mentions.maltego.csv
├── locations.maltego.csv
├── wcommented.maltego.csv
├── wtagged.maltego.csv
└── posts/                          # media, skipped with --no-download
```

Pre-flight: `private` / `blocked` / `deleted` profiles abort *before* any directory is created. Disk free below 2 GB also aborts. Progress bar ticks 1/9 → 9/9 as sections finish.

---

## 🔥 `/intersect` — shared followers

> *Who follows both of these targets?*

Takes two usernames; pulls the first N followers of each (default 1000) and intersects by `pk`. Reveals shared communities, employees, family, or PR networks.

```sh
insto -c intersect ferrari mclaren --limit 5000 --maltego
```

```text
@ferrari ∩ @mclaren: 412 shared followers (out of 5000 / 5000 analysed)
```

The Maltego CSV under `output/ferrari_mclaren/intersect.maltego.csv` is ready to import as `maltego.Person` nodes.

**Trick:** for an OPSEC investigation against an anonymous target with no obvious bio, `/intersect` against the two accounts they're known to interact with often surfaces the operator's actual handle in the overlap.

---

## 🔥 `/fans` — superfan ranking

> *Who actually engages with this account, weighted by effort?*

Across the last N posts, counts who liked + who commented and ranks by `score = likes + 3×comments` (a comment is ~3× more effortful than a heart-tap). Output shows the per-channel breakdown so the engagement profile is visible at a glance.

```sh
insto @nasa -c fans --limit 20
```

```text
Top fans of @nasa (last 20 of 20 posts, score = ❤️ + 3*💬):
  @bigfan_42        67  (❤️17 💬16+1L+0C +50)
  @space_lover      54  (❤️12 💬14)
  @comet_chaser     38  (❤️11 💬9)
  ...
```

Cost: 2N backend calls (one likers + one comments per post). Default 50 → 100 round-trips. Pass `--limit 10` for cheap sampling. Concurrency capped at 5.

JSON export carries `{username, likes, comments, score, rank}` per row.

---

## 🔥 `/place` + `/placeposts` — geo discovery

> *Find an Instagram location, then see who posts there.*

Two-step OSINT primitive: search a place by free text, then list top media at the matched pk.

```sh
insto -c place tbilisi 5
```

```text
places matching 'tbilisi' (5)
┏━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┓
┃ # ┃ pk               ┃ name             ┃ city        ┃ lat,lng          ┃
┡━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━┩
│ 1 │ 456682401795583  │ Tbilisi, Georgia │ —           │ 41.6883, 44.8089 │
│ 2 │ 214413140        │ Tbilisi, Georgia │ —           │ 41.7422, 44.7988 │
│ 3 │ 2186950754745384 │ Tbilisi, Georgea │ Tbilisi     │ 41.6940, 44.8011 │
│ 4 │ 1008248419       │ Tbilisi          │ —           │ 41.7167, 44.7833 │
│ 5 │ 767262872        │ Tbilisi          │ —           │ 41.7948, 44.7930 │
└───┴──────────────────┴──────────────────┴─────────────┴──────────────────┘
→ /placeposts <pk> to see top posts at a location
```

```sh
insto -c placeposts 456682401795583 30
```

Returns the 30 most-engaging posts geotagged at that pk. Full Post DTOs — JSON-exportable, screen-renderable, drop into a Maltego `maltego.Photo` graph if you want.

---

## 🔥 `/postinfo` — URL → metadata

> *I have a permalink. Give me the post's metadata.*

Resolves any of: `https://www.instagram.com/p/<code>/`, the bare shortcode (`DXPduuvEY7S`), or a numeric pk (`3877448558815842002`) into the full Post DTO. No active target needed — useful for evidence chains where you have a URL from a screenshot but don't know whose account it's from.

```sh
insto -c postinfo DXPduuvEY7S
```

```text
╭───────────────────── post DXPduuvEY7S ─────────────────────╮
│  pk         3877448558815842002                            │
│  code       DXPduuvEY7S                                    │
│  type       carousel                                       │
│  taken at   2026-04-26                                     │
│  owner      @nasa                                          │
│  likes      234 187                                        │
│  comments   1 412                                          │
│  location   —                                              │
│  caption    Two stars dance close in the Pillars of …      │
│  hashtags   #NASA, #Moon, #Besties                         │
│  mentions   @astro_reid                                    │
│  media      https://scontent.cdninstagram.com/...          │
╰────────────────────────────────────────────────────────────╯
```

JSON export gives the full DTO including `media_urls[]` for evidence-archive use.

---

## 🔥 `/timeline` — posting cadence

> *When does this target post?*

24-bucket hour-of-day sparkline + 7-bucket day-of-week bar chart over the last N posts. Reveals timezone (sleeping hours), human vs scheduler-driven posting, weekday vs weekend rhythm.

```sh
insto @nasa -c timeline --limit 30
```

```text
@nasa posting cadence — 30 posts (2026-04-11 → 2026-04-28)

  hour 00 → 23 (UTC, peak 20h): ▃ ▃  ▁       ▁▄▃ ▁▅▁█▄▁▄

  Mon   6  ██████████████████████████
  Tue   7  ██████████████████████████████
  Wed   5  █████████████████████
  Thu   1  ████
  Fri   6  ██████████████████████████
  Sat   3  █████████████
  Sun   2  █████████
```

@nasa peaks at 20:00 UTC = ~16:00 ET — US East Coast working hours. The Mon-Fri concentration with a Thursday dip is staffer rhythm, not scheduler.

---

## 🔥 `/search` — discovery search

> *I don't know the username yet. I have a bio fragment / brand name.*

Full Instagram account SERP — feeds free text to `fbsearch_accounts_v2`. Returns matched accounts in IG's relevance order.

```sh
insto -c search ferrari 5 --maltego
```

```text
search 'ferrari' (5)
1  ferrari            (verified)  Ferrari
2  scuderiaferrari    (verified)  Scuderia Ferrari HP
3  ferrari_mumbai     (verified)  Navnit Motors Ferrari
4  ferrari_canada                 Ferrari of Canada
5  ferrari_la                     Ferrari of Beverly Hills
```

Maltego CSV is ready for import as `maltego.Person` nodes.

---

## OPSEC notes

- **Tor proxy**: `--proxy socks5h://127.0.0.1:9050` works on every command. `socks5h` (not `socks5`) means DNS goes through the proxy too.
- **Logged-in vs logged-out**: HikerAPI default = no IG account, no ban risk. `aiograpi` extra = real IG session, can hit private profiles you follow but **comes with account-ban risk**. See [Backends](backends.md).
- **Secrets**: HikerAPI tokens, aiograpi passwords, and TOTP seeds are redacted from every error message and log line via `_redact.redact_secrets`. Never appears in tracebacks even when a tool prints them.
- **Local-only state**: Everything insto stores (config, sqlite, logs) lives at `~/.insto/`, mode `0700` directory + `0600` files. No phone-home, no telemetry.
