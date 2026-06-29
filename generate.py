#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["holidays"]
# ///
"""Generate a static HTML dashboard of merged GitHub PRs.

Pulls merged-PR data via the `gh` CLI, groups it by local merge date, computes
work-day stats, and injects a JSON blob into template.html to produce index.html.

Usage:
    python3 generate.py

Requires:
    - `gh` CLI, authenticated (`gh auth status`)
    - the `holidays` package (`pip install -r requirements.txt`)
"""

import json
import subprocess
import sys
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import holidays as holidays_lib
except ImportError:
    sys.exit(
        "Missing dependency 'holidays'.\n"
        "Run this script with uv so deps are handled automatically:\n"
        "    uv run generate.py\n"
        "(or: ./generate.py — the shebang invokes uv)"
    )

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
EXAMPLE_CONFIG_PATH = HERE / "config.example.json"
TEMPLATE_PATH = HERE / "template.html"
OUTPUT_PATH = HERE / "index.html"


def run(cmd):
    """Run a command, returning stdout. Exit with a friendly error on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(
            f"Command failed: {' '.join(cmd)}\n{result.stderr.strip()}\n"
            "Is `gh` installed and authenticated? Check `gh auth status`."
        )
    return result.stdout


def resolve_login(config):
    """Resolve the literal GitHub login for building browser search URLs."""
    try:
        login = run(["gh", "api", "user", "--jq", ".login"]).strip()
        if login:
            return login
    except SystemExit:
        pass
    return config.get("username", "")


def first_of_month(d):
    return d.replace(day=1)


def add_months(d, months):
    """Shift a date by N months, landing on the first of the resulting month."""
    month_index = (d.year * 12 + (d.month - 1)) + months
    year, month = divmod(month_index, 12)
    return date(year, month + 1, 1)


def month_key(d):
    return f"{d.year:04d}-{d.month:02d}"


def fetch_prs(range_start):
    """Fetch merged PRs authored by the current user since range_start."""
    out = run([
        "gh", "search", "prs",
        "--author=@me",
        "--merged",
        f"merged:>={range_start.isoformat()}",
        "--json", "number,title,repository,closedAt,url",
        "--limit", "1000",
    ])
    return json.loads(out)


def main():
    # config.json is gitignored (it holds personal vacation/OOO data). On a fresh
    # checkout, seed it from the committed starter so the script runs out of the box.
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(EXAMPLE_CONFIG_PATH.read_text())
        print(f"Created {CONFIG_PATH.name} from {EXAMPLE_CONFIG_PATH.name}.")

    config = json.loads(CONFIG_PATH.read_text())
    tz = ZoneInfo(config.get("timezone", "America/New_York"))
    months_back = int(config.get("monthsBack", 3))

    # Merge the two time-off sources into one date -> label map. `vacations` is the
    # hand-edited list; `calendarOoo` is owned by the Google Calendar connector refresh.
    # Calendar entries win on overlap (their title is more descriptive).
    timeoff = {d: "Vacation" for d in config.get("vacations", [])}
    for entry in config.get("calendarOoo", []):
        timeoff[entry["date"]] = entry.get("title") or "Out of office"

    login = resolve_login(config)

    today_local = datetime.now(tz).date()
    range_start = add_months(first_of_month(today_local), -(months_back - 1))
    range_end = today_local

    raw_prs = fetch_prs(range_start)

    exclude_owners = set(config.get("excludeOwners", []))
    if exclude_owners:
        raw_prs = [
            pr for pr in raw_prs
            if pr["repository"]["nameWithOwner"].split("/")[0] not in exclude_owners
        ]

    # Group PRs by local merge date (closedAt is the merge time for merged PRs).
    prs_by_date = {}
    for pr in raw_prs:
        closed_at = pr.get("closedAt")
        if not closed_at:
            continue
        utc_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        local_date = utc_dt.astimezone(tz).date()
        if local_date < range_start or local_date > range_end:
            continue
        key = local_date.isoformat()
        prs_by_date.setdefault(key, []).append({
            "number": pr["number"],
            "title": pr["title"],
            "repo": pr["repository"]["nameWithOwner"],
            "url": pr["url"],
        })

    # Sort each day's PRs by number for stable output.
    for prs in prs_by_date.values():
        prs.sort(key=lambda p: p["number"])

    # Build the US-holiday set across the years the range touches.
    years = list(range(range_start.year, range_end.year + 1))
    us_holidays = holidays_lib.UnitedStates(years=years)
    holidays_in_range = {}

    def is_work_day(d):
        if d.weekday() >= 5:  # Sat/Sun
            return False
        if d in us_holidays:
            return False
        if d.isoformat() in timeoff:
            return False
        return True

    # Walk every date in range: tally work days and per-month stats.
    overall_work_days = 0
    by_month = {}  # "YYYY-MM" -> {"workDays": int}
    d = range_start
    while d <= range_end:
        mk = month_key(d)
        by_month.setdefault(mk, {"workDays": 0})
        if d in us_holidays:
            holidays_in_range[d.isoformat()] = us_holidays.get(d)
        if is_work_day(d):
            overall_work_days += 1
            by_month[mk]["workDays"] += 1
        d += timedelta(days=1)

    # PR counts per month.
    pr_count_by_month = {}
    for key, prs in prs_by_date.items():
        mk = key[:7]
        pr_count_by_month[mk] = pr_count_by_month.get(mk, 0) + len(prs)

    def per_work_day(total, work_days):
        return round(total / work_days, 2) if work_days else 0.0

    def window_stats(w_start, w_end):
        work_days = 0
        d = w_start
        while d <= w_end:
            if is_work_day(d):
                work_days += 1
            d += timedelta(days=1)
        total = sum(
            len(prs_by_date.get((w_start + timedelta(days=i)).isoformat(), []))
            for i in range((w_end - w_start).days + 1)
        )
        return {
            "totalPRs": total,
            "workDays": work_days,
            "prsPerWorkDay": per_work_day(total, work_days),
        }

    total_prs = sum(len(v) for v in prs_by_date.values())
    stats = {
        "overall": {
            "totalPRs": total_prs,
            "workDays": overall_work_days,
            "prsPerWorkDay": per_work_day(total_prs, overall_work_days),
        },
        "last7": window_stats(max(range_start, range_end - timedelta(days=6)), range_end),
        "last30": window_stats(max(range_start, range_end - timedelta(days=29)), range_end),
        "byMonth": {},
    }
    for mk, info in by_month.items():
        total = pr_count_by_month.get(mk, 0)
        stats["byMonth"][mk] = {
            "totalPRs": total,
            "workDays": info["workDays"],
            "prsPerWorkDay": per_work_day(total, info["workDays"]),
        }

    data = {
        "login": login,
        "generatedAt": datetime.now(tz).isoformat(timespec="seconds"),
        "timezone": str(tz),
        "rangeStart": range_start.isoformat(),
        "rangeEnd": range_end.isoformat(),
        "prsByDate": prs_by_date,
        "holidays": holidays_in_range,
        "vacations": {d: timeoff[d] for d in sorted(timeoff)},
        "stats": stats,
    }

    template = TEMPLATE_PATH.read_text()
    rendered = template.replace("__DATA__", json.dumps(data))
    OUTPUT_PATH.write_text(rendered)

    print(
        f"Wrote {OUTPUT_PATH.name}: {total_prs} PRs across {overall_work_days} "
        f"work days = {stats['overall']['prsPerWorkDay']}/work day "
        f"({range_start} → {range_end}, login: {login or 'unknown'})."
    )


if __name__ == "__main__":
    main()
