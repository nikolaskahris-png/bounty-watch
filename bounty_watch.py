#!/usr/bin/env python3
"""
bounty_watch.py — Superteam Earn bounty scanner with Telegram alerts.

Polls the public Superteam Earn API, keeps only listings that match your
filters, and sends you a Telegram message the first time each one appears.

Design notes (learned from the data, not assumed):
  * The `status` field is ALWAYS "OPEN", even for listings whose winners were
    announced months ago. It is useless as a filter. We use
    `isWinnersAnnounced == False` AND `deadline > now` instead.
  * The list endpoint does NOT return `region` or `skills`. Those live only in
    /api/listings/details/{slug}. So we fetch the list, shortlist, then fetch
    details once per NEW listing only.
  * Every raw payload is appended to raw_log.jsonl before any filtering, so a
    bug in the filter can be recomputed away instead of losing history.
  * If a details fetch fails we record the listing as UNVERIFIED and alert
    anyway, flagged. "I could not check it" is never silently "it's fine".

Credentials come from the environment (TG_TOKEN, TG_CHAT) so they never live
in this file. Locally, a .env file beside this script is read automatically.
"""

import html
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ----------------------------------------------------------------------------
# CONFIG — filters you may want to tune
# ----------------------------------------------------------------------------

# Region is NOT a filter. Country-locked listings are the least crowded ones,
# so they are worth seeing and judging by hand — the alert flags the region
# instead of hiding the listing.

# Skip anything with more competition than this already. 25 is where the
# measured win rate falls off a cliff: 21.9% at 11-25 vs 9.7% at 26-50.
MAX_SUBMISSIONS = 25

# Skip listings whose deadline is sooner than this — not enough time to work.
MIN_HOURS_LEFT = 24

# Historical win rate by how many submissions a listing ended up with, from
# harvest.py's dataset. Shown in the alert so the odds are visible up front.
WIN_RATE_SOURCE = "1,848 winners across 317 listings, measured 2026-10-13"
WIN_RATE_BANDS = (
    (0, 10, 38.2),
    (11, 25, 21.9),
    (26, 50, 9.7),
    (51, 100, 8.0),
    (101, 250, 4.1),
    (251, None, 2.1),
)
BASE_WIN_RATE = 4.6

# Skills are NOT filtered on. Content appears in 86.7% of winning listings
# and 84.7% of losing ones — a 1.02 lift, i.e. no signal. It is shown in the
# alert because it is worth reading, just not worth rejecting on.

POLL_MINUTES = 15

# ----------------------------------------------------------------------------

BASE = "https://superteam.fun"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "seen.json"
RAW_LOG = HERE / "raw_log.jsonl"
ALERT_LOG = HERE / "alerts.jsonl"
ENV_FILE = HERE / ".env"

HEADERS = {"User-Agent": "bounty-watch/1.0"}

# A Greek-locale Windows console is cp1253 and cannot print the arrows and
# emoji in these messages — it raises UnicodeEncodeError mid-scan. Linux
# runners are UTF-8 already, so this only matters when you run it locally.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_env_file():
    """Read KEY=VALUE lines from .env beside this script, if present.

    Environment variables that are already set always win, so GitHub Actions
    secrets can never be overridden by a stale local file.
    """
    if not ENV_FILE.exists():
        return
    try:
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("\"").strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception as e:
        log(f"WARN: could not read .env ({e}); relying on real env vars")


load_env_file()

TELEGRAM_TOKEN = os.environ.get("TG_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TG_CHAT", "").strip()

# How many lines of raw_log.jsonl to keep. 0 disables the log entirely — that
# is what CI uses, since every run starts from a clean checkout and the file
# would be thrown away with the runner anyway.
RAW_LOG_KEEP = int(os.environ.get("RAW_LOG_KEEP") or 50)


def load_seen():
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception as e:
            log(f"WARN: could not read state file ({e}); starting empty")
            return set()
    return set()


def save_seen(seen):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen), indent=0))
    tmp.replace(STATE_FILE)


def append_jsonl(path, obj):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def append_raw(obj):
    """Append to raw_log.jsonl, keeping only the last RAW_LOG_KEEP lines."""
    if RAW_LOG_KEEP <= 0:
        return
    append_jsonl(RAW_LOG, obj)
    try:
        lines = RAW_LOG.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > RAW_LOG_KEEP:
            RAW_LOG.write_text("".join(lines[-RAW_LOG_KEEP:]), encoding="utf-8")
    except Exception as e:
        log(f"WARN: could not trim raw log ({e})")


def fetch_list():
    """Return the raw listing array, or None if the request failed."""
    try:
        r = requests.get(
            f"{BASE}/api/listings",
            params={"type": "bounty"},
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            log(f"WARN: unexpected list shape: {type(data)}")
            return None
        return data
    except Exception as e:
        log(f"ERROR fetching list: {e}")
        return None


def check_schema(items):
    """Confirm the fields the filters depend on still exist.

    If Superteam renames or drops them, every listing would quietly fail the
    filter and no alert would ever fire again. Better to go red loudly.
    """
    if not items:
        return True
    needed = ("isWinnersAnnounced", "deadline")
    missing = [k for k in needed if not any(k in it for it in items)]
    if missing:
        log(f"ERROR: API schema changed — no listing carries {missing}. "
            f"The filters cannot be trusted, so this is not a clean scan.")
        return False
    return True


def fetch_details(slug):
    """Return the detail object, or None if it could not be fetched."""
    try:
        r = requests.get(
            f"{BASE}/api/listings/details/{slug}", headers=HEADERS, timeout=30
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"WARN: details fetch failed for {slug}: {e}")
        return None


def parse_deadline(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def passes_list_filter(item, now):
    """Cheap checks using only the list payload. Returns (ok, reason)."""
    if item.get("isWinnersAnnounced"):
        return False, "winners announced"

    dl = parse_deadline(item.get("deadline"))
    if dl is None:
        return False, "no parseable deadline"

    hours_left = (dl - now).total_seconds() / 3600
    if hours_left <= 0:
        return False, "deadline passed"
    if hours_left < MIN_HOURS_LEFT:
        return False, f"only {hours_left:.1f}h left"

    subs = (item.get("_count") or {}).get("Submission", 0)
    if subs > MAX_SUBMISSIONS:
        return False, f"{subs} submissions"

    return True, "ok"


def submission_count(item):
    return (item.get("_count") or {}).get("Submission", 0)


def listing_facts(detail):
    """Pull the reportable facts out of the detail payload.

    Nothing here rejects a listing any more: region and skills are both
    things to read in the alert, not things to filter on.
    """
    region = (detail.get("region") or "").strip()
    skills = [s.get("skills") for s in (detail.get("skills") or []) if s.get("skills")]
    return region, skills


def band_for(subs):
    """(label, historical win rate) for a submission count."""
    for lo, hi, rate in WIN_RATE_BANDS:
        if subs >= lo and (hi is None or subs <= hi):
            return (f"{lo}-{hi}" if hi is not None else f"{lo}+"), rate
    return "?", None


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram not configured (TG_TOKEN / TG_CHAT) — printing instead:")
        print(text)
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=30,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log(f"ERROR sending telegram: {e}")
        return False


def region_line(region, unverified):
    """The region, stated loudly. Anything other than Global is flagged,
    because eligibility is then something you have to check yourself."""
    if unverified:
        return "📍 <b>REGION UNKNOWN</b> — details fetch failed, check it yourself"
    if not region:
        return "📍 <b>REGION NOT STATED</b> — check the listing"
    if region.upper() == "GLOBAL":
        return "🌍 <b>GLOBAL</b> — open to everyone"
    return f"📍 <b>{html.escape(region.upper())} ONLY</b> — region-locked, check if you qualify"


def competition_lines(subs):
    """The odds, stated up front. Two lines: the band, then the caveat."""
    label, rate = band_for(subs)
    if rate is None:
        return f"👥 <b>{subs}</b> submissions"
    headline = (f"👥 <b>{subs} submissions — band {label}, "
                f"historically {rate:.1f}% win rate</b>")
    if label == "0-10":
        # The band worth dropping other work for: 8x the 4.6% base rate.
        return ("🔥🔥🔥 <b>TOP BAND</b> 🔥🔥🔥\n"
                f"{headline}\n"
                f"   ↳ best odds there are — worth dropping other work for")
    if label == "11-25":
        return f"🔥 {headline}"
    return headline


def format_alert(item, detail, region, unverified, skills=None):
    subs = submission_count(item)
    dl = parse_deadline(item.get("deadline"))
    now = datetime.now(timezone.utc)
    days = (dl - now).days if dl else "?"

    reward = item.get("rewardAmount")
    token = item.get("token", "")
    sponsor = (item.get("sponsor") or {}).get("name", "?")
    slug = item.get("slug", "")
    title = item.get("title") or "(no title)"

    places = ""
    if detail:
        rewards = detail.get("rewards") or {}
        if rewards:
            vals = [f"{v}" for _, v in sorted(rewards.items(), key=lambda kv: str(kv[0]))]
            places = f"\n🏆 {len(vals)} places: {' / '.join(vals)}"

    skill_line = ""
    if skills:
        skill_line = f"\n🧠 {html.escape(', '.join(str(s) for s in skills))}"

    # Titles and sponsor names are free text written by sponsors: a stray <
    # or & makes Telegram reject the whole HTML message with a 400.
    return (
        f"<b>{html.escape(str(title))}</b>\n"
        f"{region_line(region, unverified)}\n"
        f"{competition_lines(subs)}\n"
        f"💰 {reward} {html.escape(str(token))}  ·  ⏳ {days} days left{places}"
        f"{skill_line}\n"
        f"🏢 {html.escape(str(sponsor))}\n"
        f"<i>Band is the count right now — it will fill up, and the odds "
        f"drop as it does.</i>\n\n"
        f"{BASE}/earn/listing/{slug}"
    )


def run_once(seen):
    """Returns (seen, new_alerts, ok). ok=False means do not trust this scan."""
    now = datetime.now(timezone.utc)
    items = fetch_list()

    if items is None:
        log("scan FAILED — list unavailable. Not updating state.")
        return seen, 0, False

    append_raw({"fetched_at": now.isoformat(), "count": len(items), "items": items})

    if not check_schema(items):
        return seen, 0, False

    shortlist = []
    for item in items:
        ok, reason = passes_list_filter(item, now)
        if ok:
            shortlist.append(item)

    log(f"{len(items)} listings → {len(shortlist)} pass the cheap filter")

    # Collect everything that passes before sending anything, so the alerts
    # can go out in a deliberate order.
    matches = []
    for item in shortlist:
        lid = item.get("id")
        if not lid or lid in seen:
            continue

        slug = item.get("slug", "")
        detail = fetch_details(slug) if slug else None
        time.sleep(1)  # be polite

        if detail is None:
            # Could not verify. Alert anyway, clearly flagged.
            matches.append((item, None, "", True, []))
            continue

        region, skills = listing_facts(detail)
        matches.append((item, detail, region, False, skills))

    # Most-crowded first, so the least-crowded bounty is the LAST message to
    # arrive — in a chat the newest message is the one sitting in front of you.
    matches.sort(key=lambda m: submission_count(m[0]), reverse=True)

    new_alerts = 0
    for item, detail, region, unverified, skills in matches:
        lid = item.get("id")
        slug = item.get("slug", "")
        text = format_alert(item, detail, region, unverified=unverified,
                            skills=skills)
        if send_telegram(text):
            seen.add(lid)
            new_alerts += 1
            append_jsonl(ALERT_LOG, {"at": now.isoformat(), "id": lid,
                                     "slug": slug, "region": region,
                                     "subs": submission_count(item),
                                     "verified": not unverified})
            save_seen(seen)  # flush now: a killed run must not re-alert
            log(f"  ALERT {slug} ({region or 'region?'}, "
                f"{submission_count(item)} subs)")

    save_seen(seen)
    return seen, new_alerts, True


def main():
    once = "--once" in sys.argv
    prime = "--prime" in sys.argv

    seen = load_seen()
    log(f"started — {len(seen)} listings already seen")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("WARN: TG_TOKEN / TG_CHAT not set — alerts will print, not send")

    if prime:
        # Mark everything currently matching as seen, without alerting.
        # Run this the first time so you don't get flooded.
        items = fetch_list() or []
        now = datetime.now(timezone.utc)
        n = 0
        for item in items:
            ok, _ = passes_list_filter(item, now)
            if ok and item.get("id"):
                seen.add(item["id"])
                n += 1
        save_seen(seen)
        log(f"primed: {n} current listings marked as seen. Run normally now.")
        return 0

    while True:
        seen, n, ok = run_once(seen)
        log(f"scan done — {n} new alert(s)")
        if once:
            # A non-zero exit turns the Actions run red, which is the only
            # notification channel left when the API itself breaks.
            return 0 if ok else 1
        time.sleep(POLL_MINUTES * 60)


if __name__ == "__main__":
    sys.exit(main())
