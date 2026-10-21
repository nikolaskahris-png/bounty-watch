#!/usr/bin/env python3
"""
make_stats.py — turn the private harvest into public aggregate statistics.

The harvested rows hold third-party usernames and the personal links people
submitted, so they live in a separate private repo. This script is the only
bridge between the two, and it lives in the PUBLIC repo so the method can be
audited: you can read exactly what is published and what is not.

WHAT IT PUBLISHES
  * win rate per entry-count band, per listing type, with the counts behind
    each rate so the sample size is visible
  * totals: listings, entries, winners

WHAT IT REFUSES TO PUBLISH
  * usernames, display names, user ids
  * submitted links, submission ids or URLs
  * anything per-submission or per-person at all

Every output number is a count or a ratio over a group. A band is suppressed
entirely if fewer than MIN_BAND_ENTRIES rows fall in it, so no row can be
singled out by elimination.

Usage:
    HARVEST_DATA_DIR=../bounty-watch-data python make_stats.py
    python make_stats.py --data ../bounty-watch-data --out .
"""

import glob
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# A band thinner than this is reported as a count only, with no rate: a rate
# over a handful of rows is noise, and a very thin group is also the kind of
# thing from which an individual can be inferred.
MIN_BAND_ENTRIES = 25

BANDS = {
    "bounty": [(0, 10), (11, 25), (26, 50), (51, 100), (101, 250), (251, None)],
    "project": [(0, 10), (11, 25), (26, 50), (51, None)],
}

# Fields that must never reach the output. Asserted, not assumed.
FORBIDDEN = ("author_username", "author_name", "author_id", "submitted_link",
             "submission_id", "submission_url", "text", "listing_slug",
             "listing_title", "sponsor")


def band_label(lo, hi):
    return f"{lo}-{hi}" if hi is not None else f"{lo}+"


def load(data_root: Path):
    listings = {}
    lf = data_root / "data" / "listings.jsonl"
    if not lf.exists():
        sys.exit(f"no listings.jsonl under {data_root / 'data'} — "
                 f"point --data at the private data repo")
    for line in lf.open(encoding="utf-8"):
        if line.strip():
            r = json.loads(line)
            listings[r["listing_slug"]] = r

    rows = []
    for f in sorted(glob.glob(str(data_root / "data" / "submissions-*.jsonl"))):
        for line in open(f, encoding="utf-8"):
            if line.strip():
                s = json.loads(line)
                meta = listings.get(s["listing_slug"]) or {}
                # Carry forward ONLY the three aggregate keys we need. The
                # personal fields are dropped here, at the boundary, so they
                # cannot leak into anything downstream.
                rows.append((meta.get("listing_type"),
                             meta.get("total_submissions") or 0,
                             bool(s.get("is_winner"))))
    return listings, rows


def compute(listings, rows):
    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "min_band_entries": MIN_BAND_ENTRIES,
        "totals": {},
        "bands": {},
    }
    for kind in ("bounty", "project"):
        krows = [r for r in rows if r[0] == kind]
        if not krows:
            continue
        winners = sum(1 for r in krows if r[2])
        n_listings = sum(1 for m in listings.values()
                         if m.get("listing_type") == kind)
        out["totals"][kind] = {
            "listings": n_listings,
            "entries": len(krows),
            "winners": winners,
            "base_win_rate_pct": round(winners / len(krows) * 100, 2),
        }
        bands = []
        for lo, hi in BANDS[kind]:
            sel = [r for r in krows if lo <= r[1] <= (hi if hi is not None else 10**9)]
            w = sum(1 for r in sel if r[2])
            row = {"band": band_label(lo, hi), "entries": len(sel), "winners": w}
            # Thin bands get counts but no headline rate.
            row["win_rate_pct"] = (round(w / len(sel) * 100, 1)
                                   if len(sel) >= MIN_BAND_ENTRIES else None)
            bands.append(row)
        out["bands"][kind] = bands
    return out


def assert_no_personal_data(stats):
    """Fail loudly rather than publish something personal by accident."""
    blob = json.dumps(stats)
    for f in FORBIDDEN:
        if f in blob:
            sys.exit(f"REFUSING TO WRITE: field '{f}' reached the output")
    # Every leaf must be a number or a short label, never free text.
    for kind, bands in stats["bands"].items():
        for b in bands:
            if not isinstance(b["entries"], int):
                sys.exit("REFUSING TO WRITE: non-numeric aggregate")


def markdown(stats):
    L = [
        "# Aggregate win-rate statistics",
        "",
        f"Generated from the private harvest by [`make_stats.py`](make_stats.py) "
        f"on **{stats['generated_at']}**.",
        "",
        "Only aggregates are published here: counts and ratios over groups. The "
        "underlying rows contain third-party usernames and the links people "
        "submitted, so they are kept in a separate private repository and are "
        "never committed to this one. A band with fewer than "
        f"{stats['min_band_entries']} entries is shown as a count with no rate.",
        "",
    ]
    for kind in ("bounty", "project"):
        if kind not in stats["bands"]:
            continue
        t = stats["totals"][kind]
        L += [
            f"## {kind.capitalize()}",
            "",
            f"{t['listings']:,} listings · {t['entries']:,} entries · "
            f"{t['winners']:,} winners · base rate **{t['base_win_rate_pct']}%**",
            "",
            "| Entries in listing | Entries | Winners | Win rate |",
            "| --- | --- | --- | --- |",
        ]
        for b in stats["bands"][kind]:
            rate = f"**{b['win_rate_pct']}%**" if b["win_rate_pct"] is not None \
                   else f"_n<{stats['min_band_entries']}_"
            L.append(f"| {b['band']} | {b['entries']:,} | {b['winners']:,} | {rate} |")
        L.append("")
    L += [
        "A bounty picks up to 5 winners; a project picks exactly one. Their "
        "rates are therefore computed separately and are not comparable. "
        "Grants are absent on purpose: they are not a competition, so no "
        "win rate exists for them.",
        "",
    ]
    return "\n".join(L)


def main():
    data = os.environ.get("HARVEST_DATA_DIR")
    out = "."
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--data" and i + 1 < len(argv):
            data = argv[i + 1]
        if a == "--out" and i + 1 < len(argv):
            out = argv[i + 1]
    if not data:
        sys.exit("need --data <private repo path> or HARVEST_DATA_DIR")

    listings, rows = load(Path(data))
    stats = compute(listings, rows)
    assert_no_personal_data(stats)

    outp = Path(out)
    (outp / "stats.json").write_text(
        json.dumps(stats, indent=1) + "\n", encoding="utf-8", newline="\n")
    (outp / "STATS.md").write_text(markdown(stats), encoding="utf-8", newline="\n")

    print(f"read {len(rows):,} private rows from {data}")
    for kind, t in stats["totals"].items():
        print(f"  {kind}: {t['listings']:,} listings, {t['entries']:,} entries, "
              f"{t['winners']:,} winners, base {t['base_win_rate_pct']}%")
    print(f"wrote {outp/'STATS.md'} and {outp/'stats.json'} — aggregates only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
