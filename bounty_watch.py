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

# Which regions you can actually enter.
ALLOWED_REGIONS = {"GLOBAL", "GREECE"}

# Skip anything with more competition than this already.
MAX_SUBMISSIONS = 40

# Skip listings whose deadline is sooner than this — not enough time to work.
MIN_HOURS_LEFT = 24

# Skills that mean "content bounty" — these get filtered out.
# (Based on the listings reviewed: content bounties ran 90-677 submissions.)
CONTENT_ONLY_SKILLS = {"CONTENT", "DESIGN", "GROWTH", "COMMUNITY"}

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


def passes_detail_filter(detail):
    """Checks needing the detail payload. Returns (ok, reason, region)."""
    region = (detail.get("region") or "").strip()
    if region.upper() not in ALLOWED_REGIONS:
        return False, f"region={region}", region

    skills = detail.get("skills") or []
    names = {(s.get("skills") or "").upper() for s in skills}
    if names and names.issubset(CONTENT_ONLY_SKILLS):
        return False, f"content-only skills {sorted(names)}", region

    return True, "ok", region


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


def format_alert(item, detail, region, unverified):
    subs = (item.get("_count") or {}).get("Submission", 0)
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

    flag = "\n⚠️ <b>UNVERIFIED</b> — could not fetch details, check region yourself" if unverified else ""

    # Titles and sponsor names are free text written by sponsors: a stray <
    # or & makes Telegram reject the whole HTML message with a 400.
    return (
        f"<b>{html.escape(str(title))}</b>\n"
        f"💰 {reward} {html.escape(str(token))}  ·  👥 {subs} submissions{places}\n"
        f"🌍 {html.escape(region) or '?'}  ·  ⏳ {days} days left\n"
        f"🏢 {html.escape(str(sponsor))}{flag}\n\n"
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

    new_alerts = 0
    for item in shortlist:
        lid = item.get("id")
        if not lid or lid in seen:
            continue

        slug = item.get("slug", "")
        detail = fetch_details(slug) if slug else None
        time.sleep(1)  # be polite

        if detail is None:
            # Could not verify. Alert anyway, clearly flagged.
            text = format_alert(item, None, "", unverified=True)
            if send_telegram(text):
                seen.add(lid)
                new_alerts += 1
                append_jsonl(ALERT_LOG, {"at": now.isoformat(), "id": lid,
                                         "slug": slug, "verified": False})
                save_seen(seen)  # flush now: a killed run must not re-alert
            continue

        ok, reason, region = passes_detail_filter(detail)
        if not ok:
            seen.add(lid)  # checked and rejected — don't re-check every cycle
            log(f"  skip {slug}: {reason}")
            continue

        text = format_alert(item, detail, region, unverified=False)
        if send_telegram(text):
            seen.add(lid)
            new_alerts += 1
            append_jsonl(ALERT_LOG, {"at": now.isoformat(), "id": lid,
                                     "slug": slug, "region": region,
                                     "verified": True})
            save_seen(seen)  # flush now: a killed run must not re-alert
            log(f"  ALERT {slug} ({region})")

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
