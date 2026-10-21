#!/usr/bin/env python3
"""
harvest.py — collect the submissions that actually won Superteam Earn
listings, so "what wins" can be measured instead of guessed.

Append-only. A listing is harvested once, after its winners are announced,
and never re-fetched.

--------------------------------------------------------------------------
THE TWO ENDPOINTS, AS DISCOVERED (not assumed) ON 2026-10-13
--------------------------------------------------------------------------

1. ENUMERATING COMPLETED LISTINGS

   GET /api/listings/live?take=50&deadline=<ISO8601>

   Found by pulling the listing page's JS chunks and grepping for how the
   client builds its URLs (the paths are template literals, so grepping for
   quoted "/api/..." strings finds nothing). The client calls it as
   liveListingsQuery with params {take, deadline, order, type, excludeIds,
   exclusiveSponsorId}.

   Measured behaviour:
     * Every item has isWinnersAnnounced == true and a winnersAnnouncedAt.
       Despite the name "live", this is the COMPLETED archive.
     * `status` is "OPEN" here too. Still useless. Still not a filter.
     * `take` works but is capped at 50 — take=200 returns 50.
     * `skip`, `page` and `order` are IGNORED: identical payloads.
     * `deadline` is a CURSOR. It returns listings with deadline >= the
       value, ascending. It is inclusive, so each page repeats one item.
       Paginate by passing the last deadline of the previous page.

2. FETCHING A LISTING'S SUBMISSIONS

   GET /_next/data/<buildId>/earn/listing/<slug>/submission.json?slug=<slug>

   There is NO public JSON API for the submission list. The Submissions tab
   is the server-rendered page /earn/listing/[slug]/submission, so its data
   arrives as Next.js server props. This is the JSON form of that page.
   /api/submission/get/ exists but takes a single id and serves the
   submit-form, not a public list.

   Returns pageProps.submission[] — id, link, isWinner, winnerPosition,
   user, like — plus pageProps.bounty with the full listing metadata
   (region, skills, rewards, sponsor), so one request covers both.

   buildId changes on every deploy and a stale one 404s, so it is scraped
   fresh each run, re-scraped once on a 404, and if that still fails we fall
   back to parsing __NEXT_DATA__ out of the HTML page, which needs no
   buildId at all.

WHAT THE API DOES NOT GIVE US, so these are recorded as null:
  * follower / reputation figures. The submission's user object carries only
    id, username, firstName, lastName, photo. The only engagement number
    available is the submission's own like count.
  * submission text or description. Public submissions expose `link` only.

--------------------------------------------------------------------------
WHY THE FILES ARE SHAPED THIS WAY
--------------------------------------------------------------------------
Measured, not guessed: the archive holds 1,464 completed listings and
127,066 submissions. One fat self-contained row per submission measured
1.16 KB, i.e. ~147 MB — over GitHub's 100 MB per-file limit, and an
append-only file that is committed daily is stored again in full by git on
every commit. So:

  * data/submissions-<YYYY-MM>.jsonl  sharded by announcement month, so a
    daily commit rewrites only the current month (~1 MB), not everything.
  * data/listings.jsonl               listing metadata once per listing
    instead of repeated on all 87 of its rows. Join on listing_slug.
  * data/raw-<YYYY-MM>.jsonl.gz       raw pageProps, gzipped (~47 -> ~6 MB).
    Appending makes a new gzip member; gzip.open reads them all.
"""

import gzip
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Reuse the scanner's logging, env loading, JSONL append and base URL.
import bounty_watch as bw

HERE = bw.HERE

# The harvested data holds third-party usernames and personal links, so it
# lives in a separate PRIVATE repo. This code stays public and auditable.
# HARVEST_DATA_DIR points at that private checkout; without it, data sits
# beside the script as before, which is what a local run does.
DATA_ROOT = Path(os.environ.get("HARVEST_DATA_DIR") or HERE)
DATA = DATA_ROOT / "data"
HARVESTED_FILE = DATA_ROOT / "harvested.json"
LISTINGS_LOG = DATA / "listings.jsonl"
FAIL_LOG = DATA / "harvest_failures.jsonl"


def shard(announced_at):
    """Month bucket for a listing, from its winnersAnnouncedAt."""
    return (announced_at or "unknown")[:7] or "unknown"


def submissions_log(announced_at):
    return DATA / f"submissions-{shard(announced_at)}.jsonl"


def append_raw_gz(announced_at, obj):
    """Append one JSON line to the month's gzipped raw log."""
    path = DATA / f"raw-{shard(announced_at)}.jsonl.gz"
    with gzip.open(path, "at", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

PAGE_SIZE = 50          # server caps take at 50
CURSOR_START = "2020-01-01T00:00:00.000Z"
MAX_PAGES = 300         # guard against a cursor that stops advancing
SLEEP = 1.0             # someone else's public API; we are not in a hurry

# Listings to harvest per run. The backfill therefore spreads over several
# days instead of thousands of requests in one burst.
MAX_PER_RUN = int(os.environ.get("HARVEST_MAX") or 120)

log = bw.log


def get(url, timeout=30):
    return requests.get(url, headers=bw.HEADERS, timeout=timeout)


def load_harvested():
    if HARVESTED_FILE.exists():
        try:
            return set(json.loads(HARVESTED_FILE.read_text()))
        except Exception as e:
            log(f"WARN: could not read harvested.json ({e}); starting empty")
    return set()


def save_harvested(done):
    tmp = HARVESTED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(done), indent=0))
    tmp.replace(HARVESTED_FILE)


def record_failure(what, slug, err):
    bw.append_jsonl(FAIL_LOG, {
        "at": datetime.now(timezone.utc).isoformat(),
        "stage": what, "slug": slug, "error": str(err)[:500],
    })
    log(f"  FAIL {what} {slug}: {str(err)[:160]}")


NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def scrape_build_id():
    """Read Next's buildId out of any page. It changes on every deploy."""
    r = get(f"{bw.BASE}/earn/")
    r.raise_for_status()
    m = NEXT_DATA_RE.search(r.text)
    if not m:
        raise RuntimeError("no __NEXT_DATA__ on /earn/ — page shape changed")
    bid = json.loads(m.group(1)).get("buildId")
    if not bid:
        raise RuntimeError("__NEXT_DATA__ carried no buildId")
    return bid


def enumerate_completed():
    """Walk the completed archive via the deadline cursor.

    Returns a dict slug -> listing stub, or None if the very first page
    failed (in which case we must not conclude "nothing to harvest").
    """
    found = {}
    cursor = CURSOR_START
    for page in range(1, MAX_PAGES + 1):
        url = f"{bw.BASE}/api/listings/live?take={PAGE_SIZE}&deadline={cursor}"
        try:
            r = get(url)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            record_failure("enumerate", f"page{page}@{cursor}", e)
            return found or None

        if not isinstance(batch, list):
            record_failure("enumerate", f"page{page}", f"not a list: {type(batch)}")
            return found or None
        if not batch:
            break

        new = 0
        for it in batch:
            slug = it.get("slug")
            if slug and slug not in found:
                found[slug] = it
                new += 1

        deadlines = sorted(x.get("deadline") or "" for x in batch)
        nxt = deadlines[-1]
        # The cursor is inclusive, so a page that adds nothing new means the
        # walk has stopped moving — bail instead of looping forever.
        if not nxt or nxt == cursor or new == 0:
            break
        cursor = nxt
        time.sleep(SLEEP)

    return found


def fetch_listing_page(slug, build_id):
    """Return (pageProps, build_id). Falls back to the HTML if needed."""
    url = (f"{bw.BASE}/_next/data/{build_id}/earn/listing/{slug}"
           f"/submission.json?slug={slug}")
    r = get(url)
    if r.status_code == 404:
        # Either a stale buildId (a deploy happened mid-run) or no such page.
        build_id = scrape_build_id()
        time.sleep(SLEEP)
        url = (f"{bw.BASE}/_next/data/{build_id}/earn/listing/{slug}"
               f"/submission.json?slug={slug}")
        r = get(url)
    if r.status_code == 200:
        return r.json().get("pageProps") or {}, build_id

    # Last resort: the HTML page itself, which needs no buildId.
    time.sleep(SLEEP)
    r = get(f"{bw.BASE}/earn/listing/{slug}/submission")
    r.raise_for_status()
    m = NEXT_DATA_RE.search(r.text)
    if not m:
        raise RuntimeError("no __NEXT_DATA__ in HTML fallback")
    return json.loads(m.group(1)).get("props", {}).get("pageProps") or {}, build_id


def listing_record(bounty, total_subs, harvested_at):
    """One record per listing — the metadata, stored once, not per submission."""
    skills = bounty.get("skills") or []
    rewards = bounty.get("rewards") or {}
    sponsor = bounty.get("sponsor") or {}
    return {
        "harvested_at": harvested_at,
        "listing_slug": bounty.get("slug"),
        "listing_id": bounty.get("id"),
        "listing_title": bounty.get("title"),
        "listing_type": bounty.get("type"),
        "listing_url": f"{bw.BASE}/earn/listing/{bounty.get('slug')}",
        "sponsor": sponsor.get("name"),
        "sponsor_slug": sponsor.get("slug"),
        "sponsor_is_verified": sponsor.get("isVerified"),
        "region": bounty.get("region"),
        "skills": [s.get("skills") for s in skills],
        "subskills": sorted({x for s in skills for x in (s.get("subskills") or [])}),
        "reward_amount": bounty.get("rewardAmount"),
        "token": bounty.get("token"),
        "rewards": rewards,
        "prize_count": len(rewards),
        "max_bonus_spots": bounty.get("maxBonusSpots"),
        "compensation_type": bounty.get("compensationType"),
        "deadline": bounty.get("deadline"),
        "published_at": bounty.get("publishedAt"),
        "winners_announced_at": bounty.get("winnersAnnouncedAt"),
        "total_submissions": total_subs,
        "description_chars": len(bounty.get("description") or ""),
    }


def flatten(bounty, sub, harvested_at):
    """One record per submission. Listing fields live in listings.jsonl,
    joined on listing_slug."""
    user = sub.get("user") or {}
    name = " ".join(x for x in (user.get("firstName"), user.get("lastName")) if x)

    return {
        "harvested_at": harvested_at,
        "listing_slug": bounty.get("slug"),
        "submission_id": sub.get("id"),
        "submission_url": f"{bw.BASE}/earn/feed/submission/{sub.get('id')}",
        "submitted_link": sub.get("link"),

        "author_username": user.get("username"),
        "author_name": name or None,
        "author_id": user.get("id"),
        # Not exposed by this endpoint — see the module docstring.
        "author_followers": None,
        "author_reputation": None,
        "like_count": len(sub.get("like") or []),

        "is_winner": bool(sub.get("isWinner")),
        "winner_position": sub.get("winnerPosition"),
        # Public submissions carry no text/description, only the link.
        "text": None,
    }


def harvest_one(slug, build_id, harvested_at):
    """Returns (n_submissions, n_winners, build_id) or raises."""
    props, build_id = fetch_listing_page(slug, build_id)
    bounty = props.get("bounty") or {}
    subs = props.get("submission") or []

    if not bounty.get("isWinnersAnnounced"):
        raise RuntimeError("winners not announced after all — not harvesting")

    announced = bounty.get("winnersAnnouncedAt")

    # Raw first, before any parsing, so an unparsed field can be recomputed
    # later instead of re-scraped.
    append_raw_gz(announced, {
        "harvested_at": harvested_at, "slug": slug, "pageProps": props,
    })

    bw.append_jsonl(LISTINGS_LOG, listing_record(bounty, len(subs), harvested_at))

    out = submissions_log(announced)
    winners = 0
    for sub in subs:
        rec = flatten(bounty, sub, harvested_at)
        bw.append_jsonl(out, rec)
        if rec["is_winner"]:
            winners += 1
    return len(subs), winners, build_id


def main():
    limit = MAX_PER_RUN
    for i, a in enumerate(sys.argv):
        if a == "--max" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    DATA.mkdir(exist_ok=True)
    harvested_at = datetime.now(timezone.utc).isoformat()
    done = load_harvested()
    log(f"harvest starting — {len(done)} listings already harvested")

    try:
        build_id = scrape_build_id()
        log(f"buildId = {build_id}")
    except Exception as e:
        record_failure("buildid", "-", e)
        log("SUMMARY: could not read buildId — nothing harvested")
        return 1
    time.sleep(SLEEP)

    found = enumerate_completed()
    if found is None:
        log("SUMMARY: enumeration failed on the first page — nothing harvested")
        return 1

    todo = [s for s in found if s not in done]
    # Newest announcements first: recent winners say more about what wins now.
    todo.sort(key=lambda s: found[s].get("winnersAnnouncedAt") or "", reverse=True)
    todo = todo[:limit]

    n_listings = n_subs = n_winners = 0
    for slug in todo:
        try:
            subs, wins, build_id = harvest_one(slug, build_id, harvested_at)
        except Exception as e:
            record_failure("submissions", slug, e)   # not marked done; retried
            time.sleep(SLEEP)
            continue
        done.add(slug)
        n_listings += 1
        n_subs += subs
        n_winners += wins
        save_harvested(done)        # flush as we go; a killed run keeps progress
        time.sleep(SLEEP)

    remaining = len(found) - len(done)
    log(f"SUMMARY: {len(found)} completed listings checked, "
        f"{n_listings} newly harvested, {n_winners} winners recorded "
        f"({n_subs} submissions total, {remaining} listings still to backfill)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
