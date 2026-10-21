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

# Regions I am known to be eligible for, so the alert states it plainly
# instead of telling me to go and check. "Balkan" is here because Superteam
# Balkan confirmed on 2026-10-14 that their chapter covers Greece. Every
# other region keeps the check-if-you-qualify warning.
ELIGIBLE_REGIONS = {"GLOBAL", "BALKAN"}

# BOUNTY: everyone submits, the sponsor picks 1-5 winners. 25 is where the
# measured win rate falls off a cliff: 18.1% at 11-25 vs 9.6% at 26-50.
MAX_SUBMISSIONS = 25

# PROJECT: you apply, they pick exactly ONE person and pay them. Measured on
# 122 harvested projects: every single one had exactly 1 winner, so the odds
# are 1/applications and far worse than a bounty (0.98% base vs 6.30%). The
# cliff is at 10/11 — 14.7% at 6-10 collapsing to 5.5% at 11-25 — so this is
# 10, NOT the bounty's 25.
MAX_APPLICATIONS = 10

# Skip listings whose deadline is sooner than this — not enough time to work.
MIN_HOURS_LEFT = 24

# Historical win rate by how many entries a listing ended up with, from
# harvest.py's dataset. Measured SEPARATELY per type: a bounty's bands say
# nothing about a project's odds, and neither applies to a grant.
WIN_RATE_SOURCE = ("bounties: 6,782 winners / 107,677 submissions · "
                   "projects: 122 winners / 12,417 applications · "
                   "measured 2026-10-21")
BOUNTY_BANDS = (
    (0, 10, 35.4),
    (11, 25, 18.1),
    (26, 50, 9.6),
    (51, 100, 6.6),
    (101, 250, 6.2),
    (251, None, 2.4),
)
PROJECT_BANDS = (
    (0, 5, 25.0),
    (6, 10, 14.7),
    (11, 25, 5.5),
    (26, 50, 2.8),
    (51, None, 0.6),
)
BASE_WIN_RATE = {"bounty": 6.30, "project": 0.98}

# GRANT: not a competition. Rolling applications judged individually, no
# deadline, no submission count, and it can be paused. There is no win-rate
# data for grants and none is invented — the alert shows no odds at all.
GRANT_RESCAN_HOURS = 24

# The list endpoint discriminates by `tab`, NOT by `type`. `?type=...` is
# silently ignored, which is why it always looked like an open-bounty list.
LIST_TABS = (("bounties", "bounty"), ("projects", "project"))
LIST_PARAMS = {
    "context": "all", "category": "All", "status": "open",
    "sortBy": "Date", "order": "asc", "region": "", "sponsor": "",
}

# Skills are NOT filtered on. Content appears in 86.7% of winning listings
# and 84.7% of losing ones — a 1.02 lift, i.e. no signal. It is shown in the
# alert because it is worth reading, just not worth rejecting on.

POLL_MINUTES = 15

# ----------------------------------------------------------------------------

BASE = "https://superteam.fun"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "seen.json"
GRANTS_STATE = HERE / "grants_state.json"
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


def fetch_list(tab="bounties"):
    """Return the raw listing array for one tab, or None if it failed.

    `tab` is the real discriminator: "bounties" or "projects".
    """
    try:
        r = requests.get(
            f"{BASE}/api/listings",
            params={**LIST_PARAMS, "tab": tab},
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


def fetch_grants():
    """Return the grants array, or None. The list payload has a completely
    different shape from listings: no id, no deadline, no region, no
    isPaused — those live only in the per-grant detail."""
    try:
        r = requests.get(f"{BASE}/api/grants",
                         params={"context": "all", "category": "All",
                                 "region": "", "sponsor": ""},
                         headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            log(f"WARN: unexpected grants shape: {type(data)}")
            return None
        return data
    except Exception as e:
        log(f"ERROR fetching grants: {e}")
        return None


def fetch_grant(slug):
    """Return one grant's detail, or None."""
    try:
        r = requests.get(f"{BASE}/api/grants/{slug}", headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"WARN: grant fetch failed for {slug}: {e}")
        return None


def load_grants_state():
    if GRANTS_STATE.exists():
        try:
            return json.loads(GRANTS_STATE.read_text())
        except Exception as e:
            log(f"WARN: could not read grants_state.json ({e})")
    return {"last_scan": None, "alerted": []}


def save_grants_state(state):
    tmp = GRANTS_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=0, sort_keys=True))
    tmp.replace(GRANTS_STATE)


def parse_deadline(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def passes_list_filter(item, now, kind="bounty"):
    """Cheap checks using only the list payload. Returns (ok, reason).

    The entry-count cap depends on the kind: a project picks one person, so
    25 applications would be 4% odds, not a bounty's 18%.
    """
    cap = MAX_APPLICATIONS if kind == "project" else MAX_SUBMISSIONS
    noun = "applications" if kind == "project" else "submissions"

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
    if subs > cap:
        return False, f"{subs} {noun} (cap {cap})"

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


def band_for(subs, kind="bounty"):
    """(label, historical win rate) for an entry count, per listing kind.

    Grants get nothing: there is no grant win-rate data, and printing a
    bounty's number on a grant would be a lie.
    """
    table = {"bounty": BOUNTY_BANDS, "project": PROJECT_BANDS}.get(kind)
    if not table:
        return "?", None
    for lo, hi, rate in table:
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
    """The region, stated loudly. A region I am known to be eligible for says
    so; anything else is flagged, because eligibility is then something I
    have to check myself."""
    if unverified:
        return "📍 <b>REGION UNKNOWN</b> — details fetch failed, check it yourself"
    if not region:
        return "📍 <b>REGION NOT STATED</b> — check the listing"
    up = region.upper()
    if up == "GLOBAL":
        return "🌍 <b>GLOBAL</b> — open to everyone"
    if up in ELIGIBLE_REGIONS:
        return (f"✅ <b>{html.escape(up)}</b> — eligible, their chapter "
                f"covers Greece")
    return f"📍 <b>{html.escape(up)} ONLY</b> — region-locked, check if you qualify"


def competition_lines(subs, kind="bounty"):
    """The odds, stated up front, using the band table for THIS kind."""
    noun = "applications" if kind == "project" else "submissions"
    label, rate = band_for(subs, kind)
    if rate is None:
        return f"👥 <b>{subs}</b> {noun}"

    headline = (f"👥 <b>{subs} {noun} — band {label}, "
                f"historically {rate:.1f}% win rate</b>")
    top = PROJECT_BANDS[0] if kind == "project" else BOUNTY_BANDS[0]
    top_label = f"{top[0]}-{top[1]}"
    if label == top_label:
        return ("🔥🔥🔥 <b>TOP BAND</b> 🔥🔥🔥\n"
                f"{headline}\n"
                f"   ↳ best odds there are — worth dropping other work for")
    if rate >= 10:
        return f"🔥 {headline}"
    return headline


def format_alert(item, detail, region, unverified, skills=None, kind="bounty"):
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

    if kind == "project":
        caveat = ("<i>A project pays ONE person — these odds are 1/applications, "
                  "not a bounty's. The count will grow.</i>")
    else:
        caveat = ("<i>Band is the count right now — it will fill up, and the "
                  "odds drop as it does.</i>")

    # Titles and sponsor names are free text written by sponsors: a stray <
    # or & makes Telegram reject the whole HTML message with a 400.
    return (
        f"<b>[{kind.upper()}]</b> <b>{html.escape(str(title))}</b>\n"
        f"{region_line(region, unverified)}\n"
        f"{competition_lines(subs, kind)}\n"
        f"💰 {reward} {html.escape(str(token))}  ·  ⏳ {days} days left{places}"
        f"{skill_line}\n"
        f"🏢 {html.escape(str(sponsor))}\n"
        f"{caveat}\n\n"
        f"{BASE}/earn/listing/{slug}"
    )


def format_grant_alert(g):
    """A grant is not a competition, so this shows no odds at all.

    Every value is escaped: it all comes from the API.
    """
    esc = lambda v: html.escape(str(v if v is not None else "?"))
    skills = [s.get("skills") for s in (g.get("skills") or []) if s.get("skills")]
    skill_line = f"\n🧠 {esc(', '.join(str(s) for s in skills))}" if skills else ""

    lo, hi = g.get("minReward"), g.get("maxReward")
    amount = f"up to {esc(hi)}" if not lo else f"{esc(lo)}–{esc(hi)}"
    apps = g.get("totalApplications")
    approved = g.get("approvedAmountTotal")
    approved_line = ""
    if approved:
        approved_line = f"\n💵 {approved:,.0f} {esc(g.get('token'))} approved to date"

    return (
        f"<b>[GRANT]</b> <b>{esc(g.get('title'))}</b>\n"
        f"{region_line((g.get('region') or '').strip(), False)}\n"
        f"💰 {amount} {esc(g.get('token'))}\n"
        f"📨 {esc(apps)} applications so far  ·  ⏱ typically "
        f"{esc(g.get('avgResponseTime'))} to hear back{approved_line}"
        f"{skill_line}\n"
        f"🏢 {esc((g.get('sponsor') or {}).get('name'))}\n"
        f"<i>Rolling applications, judged individually — no deadline and no "
        f"competing field, so no win-rate band applies.</i>\n\n"
        f"{BASE}/earn/grants/{g.get('slug')}"
    )


def scan_grants(now):
    """Grants, at most once every GRANT_RESCAN_HOURS. Returns (alerts, ok).

    Grants are rolling and have no deadline, so there is nothing to miss by
    checking daily. That matters because isPaused lives only in the detail,
    and 45 of 47 grants are paused — a rejected grant is re-checked next
    time rather than being marked seen, since a pause can be lifted.
    """
    state = load_grants_state()
    last = parse_deadline(state.get("last_scan"))
    if last and (now - last).total_seconds() < GRANT_RESCAN_HOURS * 3600:
        hrs = (now - last).total_seconds() / 3600
        log(f"grants: checked {hrs:.1f}h ago, next in "
            f"{GRANT_RESCAN_HOURS - hrs:.1f}h")
        return 0, True

    grants = fetch_grants()
    if grants is None:
        log("grants scan FAILED — list unavailable")
        return 0, False

    alerted = set(state.get("alerted") or [])
    log(f"grants: {len(grants)} listed, {len(alerted)} already alerted")

    new = 0
    for g in grants:
        slug = g.get("slug")
        if not slug or slug in alerted:
            continue
        detail = fetch_grant(slug)
        time.sleep(1)  # be polite
        if detail is None:
            continue  # not recorded as alerted, so it is retried next time

        if detail.get("isPaused"):
            log(f"  skip grant {slug}: paused")
            continue
        if detail.get("isArchived") or not detail.get("isPublished"):
            log(f"  skip grant {slug}: archived/unpublished")
            continue

        if send_telegram(format_grant_alert(detail)):
            alerted.add(slug)
            new += 1
            append_jsonl(ALERT_LOG, {"at": now.isoformat(), "kind": "grant",
                                     "slug": slug,
                                     "region": detail.get("region"),
                                     "verified": True})
            state["alerted"] = sorted(alerted)
            save_grants_state(state)      # flush as we go
            log(f"  ALERT grant {slug} ({detail.get('region')})")

    state["alerted"] = sorted(alerted)
    state["last_scan"] = now.isoformat()
    save_grants_state(state)
    return new, True


def run_once(seen):
    """Returns (seen, new_alerts, ok). ok=False means do not trust this scan."""
    now = datetime.now(timezone.utc)
    ok_all = True

    # Collect across both tabs before sending anything, so the alerts can go
    # out in a deliberate order.
    matches = []
    for tab, kind in LIST_TABS:
        items = fetch_list(tab)
        if items is None:
            log(f"{tab} scan FAILED — list unavailable. Not updating state.")
            ok_all = False
            continue

        append_raw({"fetched_at": now.isoformat(), "tab": tab,
                    "count": len(items), "items": items})

        if not check_schema(items):
            ok_all = False
            continue

        shortlist = [it for it in items if passes_list_filter(it, now, kind)[0]]
        log(f"{tab}: {len(items)} listings → {len(shortlist)} pass the cheap filter")

        for item in shortlist:
            lid = item.get("id")
            if not lid or lid in seen:
                continue

            slug = item.get("slug", "")
            detail = fetch_details(slug) if slug else None
            time.sleep(1)  # be polite

            if detail is None:
                # Could not verify. Alert anyway, clearly flagged.
                matches.append((item, None, "", True, [], kind))
                continue

            region, skills = listing_facts(detail)
            matches.append((item, detail, region, False, skills, kind))

    # Most-crowded first, so the least-crowded one is the LAST message to
    # arrive — in a chat the newest message is the one sitting in front of you.
    matches.sort(key=lambda m: submission_count(m[0]), reverse=True)

    new_alerts = 0
    for item, detail, region, unverified, skills, kind in matches:
        lid = item.get("id")
        slug = item.get("slug", "")
        text = format_alert(item, detail, region, unverified=unverified,
                            skills=skills, kind=kind)
        if send_telegram(text):
            seen.add(lid)
            new_alerts += 1
            append_jsonl(ALERT_LOG, {"at": now.isoformat(), "id": lid,
                                     "kind": kind, "slug": slug,
                                     "region": region,
                                     "subs": submission_count(item),
                                     "verified": not unverified})
            save_seen(seen)  # flush now: a killed run must not re-alert
            log(f"  ALERT [{kind}] {slug} ({region or 'region?'}, "
                f"{submission_count(item)} entries)")

    save_seen(seen)

    g_new, g_ok = scan_grants(now)
    return seen, new_alerts + g_new, ok_all and g_ok


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
        now = datetime.now(timezone.utc)
        n = 0
        for tab, kind in LIST_TABS:
            for item in fetch_list(tab) or []:
                ok, _ = passes_list_filter(item, now, kind)
                if ok and item.get("id"):
                    seen.add(item["id"])
                    n += 1
        save_seen(seen)
        state = load_grants_state()
        grants = fetch_grants() or []
        state["alerted"] = sorted({g["slug"] for g in grants if g.get("slug")}
                                  | set(state.get("alerted") or []))
        state["last_scan"] = now.isoformat()
        save_grants_state(state)
        log(f"primed: {n} listings and {len(grants)} grants marked as seen. "
            f"Run normally now.")
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
