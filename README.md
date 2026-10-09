# bounty-watch

Polls the public Superteam Earn API every 30 minutes on GitHub Actions and
sends a Telegram message the first time a listing matches my filters.

Filters (in `bounty_watch.py`, near the top):

- not `isWinnersAnnounced`
- deadline in the future and at least 24 hours away
- 40 submissions or fewer
- region Global or Greece
- not content-only (CONTENT / DESIGN / GROWTH / COMMUNITY)

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
