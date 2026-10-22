#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["holidays"]
# ///
"""Generate a static HTML dashboard of merged GitHub PRs and Linear issues.

Pulls merged-PR data via the `gh` CLI and (optionally) Linear issues assigned to you
via the Linear GraphQL API, groups both by local date, computes work-day stats, and
injects a JSON blob into template.html to produce index.html.

Usage:
    python3 generate.py

Requires:
    - `gh` CLI, authenticated (`gh auth status`)
    - the `holidays` package (`pip install -r requirements.txt`)
    - optional: LINEAR_API_KEY env var (a Linear personal API key) for Linear counts
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
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
LINEAR_API_URL = "https://api.linear.app/graphql"


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


def linear_query(api_key, query, variables):
    """POST a GraphQL query to Linear, returning `data`. Exit on errors."""
    req = urllib.request.Request(
        LINEAR_API_URL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json", "Authorization": api_key},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Linear API request failed ({e.code}): {e.read().decode()[:500]}\n"
                 "Is LINEAR_API_KEY a valid personal API key?")
    if body.get("errors"):
        sys.exit(f"Linear API returned errors: {json.dumps(body['errors'])[:500]}")
    return body["data"]


LINEAR_ISSUES_QUERY = """
query($since: DateTimeOrDuration!, $after: String) {
  viewer {
    assignedIssues(first: 50, after: $after, filter: { updatedAt: { gte: $since } }) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id identifier title url completedAt
        history(first: 50) {
          pageInfo { hasNextPage endCursor }
          nodes { createdAt toState { name } }
        }
      }
    }
  }
}
"""

LINEAR_HISTORY_QUERY = """
query($id: String!, $after: String) {
  issue(id: $id) {
    history(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { createdAt toState { name } }
    }
  }
}
"""


def fetch_linear_issues(api_key, range_start):
    """Fetch issues assigned to the viewer updated since range_start, with full history.

    Any state transition bumps `updatedAt`, so this covers every issue completed or
    moved to validation within the range.
    """
    issues = []
    after = None
    while True:
        data = linear_query(api_key, LINEAR_ISSUES_QUERY,
                            {"since": range_start.isoformat(), "after": after})
        conn = data["viewer"]["assignedIssues"]
        for issue in conn["nodes"]:
            history = issue["history"]
            entries = list(history["nodes"])
            h_after = history["pageInfo"]["endCursor"]
            h_more = history["pageInfo"]["hasNextPage"]
            while h_more:
                h = linear_query(api_key, LINEAR_HISTORY_QUERY,
                                 {"id": issue["id"], "after": h_after})["issue"]["history"]
                entries.extend(h["nodes"])
                h_after = h["pageInfo"]["endCursor"]
                h_more = h["pageInfo"]["hasNextPage"]
            issue["history"] = entries
            issues.append(issue)
        if not conn["pageInfo"]["hasNextPage"]:
            return issues
        after = conn["pageInfo"]["endCursor"]


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

    def to_local_date(ts):
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz).date()

    # Linear issues assigned to you, bucketed per local date into two independent
    # counts: "done" (completedAt) and "validation" (moved into the validation state).
    # An issue can appear in both, on the same or different days.
    linear_config = config.get("linear", {})
    linear_key = os.environ.get("LINEAR_API_KEY") or linear_config.get("apiKey")
    validation_state = linear_config.get("validationState", "In Validation").casefold()
    linear_by_date = {}  # "YYYY-MM-DD" -> {"done": [...], "validation": [...]}
    if linear_key:
        for issue in fetch_linear_issues(linear_key, range_start):
            ref = {"identifier": issue["identifier"], "title": issue["title"],
                   "url": issue["url"]}
            hits = set()
            if issue.get("completedAt"):
                hits.add(("done", to_local_date(issue["completedAt"])))
            for h in issue["history"]:
                to_state = (h.get("toState") or {}).get("name", "")
                if to_state.casefold() == validation_state:
                    hits.add(("validation", to_local_date(h["createdAt"])))
            for kind, local_date in hits:
                if range_start <= local_date <= range_end:
                    day = linear_by_date.setdefault(
                        local_date.isoformat(), {"done": [], "validation": []})
                    day[kind].append(ref)
        for day in linear_by_date.values():
            for refs in day.values():
                refs.sort(key=lambda r: r["identifier"])
    else:
        print("LINEAR_API_KEY not set; skipping Linear counts.")

    def linear_count(day_iso, kind):
        return len(linear_by_date.get(day_iso, {}).get(kind, []))

    # Build the Brazilian-holiday set across the years the range touches.
    years = list(range(range_start.year, range_end.year + 1))
    br_holidays = holidays_lib.Brazil(years=years)
    holidays_in_range = {}

    def is_work_day(d):
        if d.weekday() >= 5:  # Sat/Sun
            return False
        if d in br_holidays:
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
        if d in br_holidays:
            holidays_in_range[d.isoformat()] = br_holidays.get(d)
        if is_work_day(d):
            overall_work_days += 1
            by_month[mk]["workDays"] += 1
        d += timedelta(days=1)

    def per_work_day(total, work_days):
        return round(total / work_days, 2) if work_days else 0.0

    def window_stats(w_start, w_end):
        work_days = total = done = validation = 0
        d = w_start
        while d <= w_end:
            key = d.isoformat()
            if is_work_day(d):
                work_days += 1
            total += len(prs_by_date.get(key, []))
            done += linear_count(key, "done")
            validation += linear_count(key, "validation")
            d += timedelta(days=1)
        return {
            "totalPRs": total,
            "workDays": work_days,
            "prsPerWorkDay": per_work_day(total, work_days),
            "linearDone": done,
            "linearDonePerWorkDay": per_work_day(done, work_days),
            "linearValidation": validation,
            "linearValidationPerWorkDay": per_work_day(validation, work_days),
        }

    stats = {
        "overall": window_stats(range_start, range_end),
        "last7": window_stats(max(range_start, range_end - timedelta(days=6)), range_end),
        "last30": window_stats(max(range_start, range_end - timedelta(days=29)), range_end),
        "byMonth": {},
    }
    for mk in by_month:
        m_start = date(int(mk[:4]), int(mk[5:]), 1)
        m_end = min(range_end, add_months(m_start, 1) - timedelta(days=1))
        stats["byMonth"][mk] = window_stats(m_start, m_end)

    # Daily series of the trailing 7-day window, starting once a full window fits in
    # range. Rates are None when the window has no work days (rendered as a gap).
    rolling7 = []
    d = range_start + timedelta(days=6)
    while d <= range_end:
        w = window_stats(d - timedelta(days=6), d)
        has_work = w["workDays"] > 0
        rolling7.append({
            "date": d.isoformat(),
            "workDays": w["workDays"],
            "prs": w["prsPerWorkDay"] if has_work else None,
            "done": w["linearDonePerWorkDay"] if has_work else None,
            "validation": w["linearValidationPerWorkDay"] if has_work else None,
        })
        d += timedelta(days=1)
    stats["rolling7"] = rolling7
    total_prs = stats["overall"]["totalPRs"]

    data = {
        "login": login,
        "generatedAt": datetime.now(tz).isoformat(timespec="seconds"),
        "timezone": str(tz),
        "rangeStart": range_start.isoformat(),
        "rangeEnd": range_end.isoformat(),
        "prsByDate": prs_by_date,
        "linearEnabled": bool(linear_key),
        "linearByDate": linear_by_date,
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
    if linear_key:
        o = stats["overall"]
        print(f"Linear: {o['linearDone']} done ({o['linearDonePerWorkDay']}/work day), "
              f"{o['linearValidation']} moved to validation "
              f"({o['linearValidationPerWorkDay']}/work day).")


if __name__ == "__main__":
    main()
