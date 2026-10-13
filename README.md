# bounty-watch

Polls the public Superteam Earn API every 30 minutes on GitHub Actions and
sends a Telegram message the first time a listing matches my filters.

Filters (in `bounty_watch.py`, near the top):

- not `isWinnersAnnounced`
- deadline in the future and at least 24 hours away
- **25 submissions or fewer**

That is the whole filter. Every part of it is justified by the measured data
below; nothing is left in on judgement.

## Win rate by submission count

From `harvest.py`: **1,848 winners across 317 listings, measured 2026-10-13**.
If you are reading this months later, re-run `harvest.py` and recompute — these
numbers are a snapshot, not a constant.

| Submissions in listing | Win rate |
| --- | --- |
| 0–10 | **38.2%** |
| 11–25 | **21.9%** |
| 26–50 | 9.7% |
| 51–100 | 8.0% |
| 101–250 | 4.1% |
| 251+ | 2.1% |
| *base rate, all submissions* | *4.6%* |

`MAX_SUBMISSIONS = 25` sits exactly on the cliff: 21.9% at 11–25 against 9.7%
at 26–50. Since the cap is 25, only the first two bands can ever appear in an
alert.

**Skills are not filtered on.** Content appears in 86.7% of winning listings
and 84.7% of losing ones — a lift of 1.02, i.e. no signal whatsoever. The old
content-only filter discarded most of the board in exchange for nothing. Skills
are shown in the alert because they are worth reading, not worth rejecting on.

Region is deliberately **not** a filter: country-locked listings are the least
crowded, so they are worth judging by hand. Every alert states the region, and
flags anything other than Global as region-locked. Alerts are sent
most-crowded first, so the least-crowded one is the newest message in the chat,
and each one carries its win-rate band, with the 0–10 band flagged as TOP BAND.

`status` is **not** used: the API returns `"OPEN"` for every listing, including
ones whose winners were announced months ago.

## Credentials

Both come from the environment, never from the source:

| Variable   | Meaning                        |
| ---------- | ------------------------------ |
| `TG_TOKEN` | Telegram bot token (@BotFather) |
| `TG_CHAT`  | Telegram chat id                |

In Actions they are repo secrets. Locally, copy `.env.example` to `.env` and
fill it in — `.env` is gitignored and real environment variables always take
precedence over it, so a stale local file can never shadow a secret in CI.

## Running locally

```bash
pip install -r requirements.txt
python bounty_watch.py --once     # one scan, then exit
python bounty_watch.py            # loop forever, every POLL_MINUTES
python bounty_watch.py --prime    # mark everything matching as seen, no alerts
```

Without `TG_TOKEN`/`TG_CHAT` set it prints the alerts instead of sending them,
which is the safe way to test a filter change.

## Files

| File              | In git? | Why                                                      |
| ----------------- | ------- | -------------------------------------------------------- |
| `seen.json`       | yes     | The state. Committed after every run or the same bounty alerts forever. |
| `alerts.jsonl`    | yes     | One line per alert sent. Small, and the audit trail of what I was told. |
| `raw_log.jsonl`   | **no**  | ~18 KB per scan = ~26 MB/month. Gitignored; disabled entirely in CI via `RAW_LOG_KEEP=0`, capped at the last 50 lines locally. |
| `.env`            | **no**  | Credentials.                                             |

## Scheduling realities

**Cron fires late.** GitHub's scheduled runs queue behind everyone else's;
`*/30` means "about every 30-60 minutes". Irrelevant here — the filter already
requires 24h of runway before a deadline.

**Schedules are switched off after 60 days of repo inactivity.** Commits pushed
by the built-in `GITHUB_TOKEN` are bot activity and do not reliably reset that
clock, so the constant `seen.json` commits are not a guarantee. Two fixes, pick
either:

1. **Free, zero setup:** click *Run workflow* on bounty-watch once a month.
   GitHub also emails you before it disables the schedule — one click on
   *Enable workflow* revives it.
2. **Hands-off:** add a classic PAT with `repo` scope as the secret
   `KEEPALIVE_PAT`. `keepalive.yml` then pushes one commit a month authored by
   you, which does count as activity. Without that secret the workflow is a
   no-op that just prints a reminder.

## Is it still alive?

- **Actions tab** → bounty-watch: there should be a run within the last hour.
  If the newest run is days old, the schedule was disabled — re-enable it.
- **Commits**: `state:` commits appear whenever a listing is seen or rejected.
- Green runs with `0 new alert(s)` are normal and mean the filters found
  nothing new, not that it is broken.

## What a Superteam API change looks like

The run goes **red** and you get a GitHub failure email. Specifically:

- endpoint gone, 500, or no longer a JSON array → `ERROR fetching list` /
  `unexpected list shape`, exit 1
- `isWinnersAnnounced` or `deadline` renamed or dropped → `ERROR: API schema
  changed`, exit 1. This check exists because without it the filters would
  reject every listing and the thing would sit there quietly alerting nothing.

Softer failures that stay green, and the log line to look for:

- `region`/`skills` missing from the details endpoint → everything gets
  `skip ...: region=` and no alerts arrive.
- details endpoint failing → alerts still arrive, tagged
  `⚠️ UNVERIFIED`, and you check the region yourself.

So: red = structure changed. Green and silent for a week while Earn clearly has
open bounties = check a `skip` line.


---

# harvest.py — what actually wins

Collects the submissions that won completed Superteam Earn listings, so
"what wins" can be measured. Runs once a day via `.github/workflows/harvest.yml`
and commits its data back. A listing is harvested once, after its winners are
announced, and never re-fetched (`harvested.json`).

## The two endpoints, discovered rather than assumed

**Enumerating completed listings** — `?type=bounty` only ever returns open
ones, and `isWinnersAnnounced`, `type=project` and `type=grant` are all
ignored as query params. The archive is reachable through a different route:

```
GET /api/listings/live?take=50&deadline=<ISO8601>
```

Found by downloading the listing page's JS chunks and grepping for how the
client builds URLs — the paths are template literals, so searching for quoted
`"/api/..."` strings finds nothing at all. Measured behaviour:

| Param | Reality |
| --- | --- |
| `take` | Works, capped at 50 (`take=200` returns 50) |
| `deadline` | **A cursor.** Returns `deadline >= value`, ascending, inclusive — so each page repeats one item. Paginate on the last deadline. |
| `skip`, `page`, `order` | **Silently ignored** — byte-identical payloads |

Every item has `isWinnersAnnounced: true`. Despite the name `live`, this is the
completed archive: 1,464 listings as of 2026-10-13. And `status` is `"OPEN"`
here too, so it remains useless.

**Fetching a listing's submissions** — there is **no public JSON API** for the
list. The Submissions tab is the server-rendered page
`/earn/listing/[slug]/submission`, so its data arrives as Next.js props:

```
GET /_next/data/<buildId>/earn/listing/<slug>/submission.json?slug=<slug>
```

One request returns `pageProps.submission[]` *and* `pageProps.bounty` with the
full listing metadata, so it covers both. `buildId` changes on every deploy and
a stale one 404s, so it is scraped fresh each run, re-scraped once on a 404,
and failing that the script parses `__NEXT_DATA__` out of the HTML page, which
needs no buildId.

`/api/submission/get/` exists but takes a single id and serves the submit form,
not a public list. `/api/listings/<id>/submission-count/` returns a bare integer.

## What the API does not expose

Recorded as explicit `null` rather than quietly omitted:

- **follower / reputation figures.** A submission's user object carries only
  `id`, `username`, `firstName`, `lastName`, `photo`. The only engagement
  number available anywhere is the submission's own like count.
- **submission text.** Public submissions expose `link` only.

## Files

| File | Contents |
| --- | --- |
| `harvested.json` | Slugs already done. Committed back, like `seen.json`. |
| `data/listings.jsonl` | One row per listing: sponsor, region, skills, rewards, prize count, total submissions. |
| `data/submissions-<YYYY-MM>.jsonl` | One row per submission, join to listings on `listing_slug`. |
| `data/raw-<YYYY-MM>.jsonl.gz` | Raw `pageProps`, gzipped, so an unparsed field can be recomputed instead of re-scraped. |
| `data/harvest_failures.jsonl` | Every failed fetch, with stage and error. Failures are never marked harvested, so they retry. |

**Why sharded and normalised.** Measured: 127,066 submissions across the
archive. One fat self-contained row per submission came to 1.16 KB, i.e.
~147 MB — past GitHub's 100 MB per-file limit. Worse, git stores a complete new
copy of an append-only file on every commit, so a daily-committed 147 MB file
is fatal within a week. Sharding by announcement month means a daily commit
rewrites ~1 MB, and moving listing metadata out of every row (it was repeated
on all 87 rows of a listing) took rows to 571 bytes, ~72 MB for the lot.

## Running it

```bash
python harvest.py              # HARVEST_MAX listings (default 120)
python harvest.py --max 300    # bigger bite; backfill spreads over days
```

Each run prints one summary line: listings checked, newly harvested, winners
recorded, and how many remain to backfill. A failed fetch is logged and skipped
without being marked harvested, so the next run retries it.
