---
description: Sync Google Calendar OOO into config.json and regenerate the PR dashboard
allowed-tools: Bash, Edit, Read, ToolSearch, mcp__claude_ai_Google_Calendar__list_events
---

Refresh the PR-Maxxer dashboard end to end: pull native Out-of-office days from Google
Calendar, then regenerate the GitHub PR stats. All commands run from the project root via
`$CLAUDE_PROJECT_DIR` (the repo containing `generate.py`); `${CLAUDE_PROJECT_DIR:-.}`
falls back to the current directory if that variable isn't set.

## Step 0 — Ensure config.json exists

`config.json` is gitignored (it holds personal OOO data). If it's missing, seed it from
the committed starter before doing anything else:

```bash
cd "${CLAUDE_PROJECT_DIR:-.}" && [ -f config.json ] || cp config.example.json config.json
```

## Step 1 — Compute the dashboard window

Read `config.json` and compute the date window the dashboard covers, respecting
`monthsBack` and `timezone`. Run:

```bash
cd "${CLAUDE_PROJECT_DIR:-.}" && python3 - <<'PY'
import json
from datetime import datetime, date
from zoneinfo import ZoneInfo
c = json.load(open("config.json"))
tz = ZoneInfo(c.get("timezone", "America/New_York"))
mb = int(c.get("monthsBack", 3))
today = datetime.now(tz).date()
idx = today.year * 12 + (today.month - 1) - (mb - 1)
start = date(idx // 12, idx % 12 + 1, 1)
print(start.isoformat(), today.isoformat(), str(tz))
PY
```

Use the printed `rangeStart`, `today`, and `timezone` below.

## Step 2 — Pull native OOO events

Load the connector tool if needed (`ToolSearch` →
`select:mcp__claude_ai_Google_Calendar__list_events`), then call
`mcp__claude_ai_Google_Calendar__list_events` with:
- `eventTypeFilter: ["outOfOffice"]` (native OOO only — do not keyword-match)
- `startTime`: rangeStart at `00:00:00` (a day earlier is fine)
- `endTime`: the day after `today` at `00:00:00`
- `timeZone`: the config timezone
- `orderBy: "startTime"`, `pageSize: 100` (follow `nextPageToken` if present)

If the call returns an auth message instead of events, stop and tell the user to run
`/mcp`, select **claude.ai Google Calendar**, then re-run this command.

## Step 3 — Expand to weekday dates

For each OOO event, treat the all-day `end` as **exclusive** (Google convention). Expand
`[start, end)` into individual dates in the config timezone, keeping **weekdays only**
(skip Sat/Sun — already non-work-days). If two events cover the same day, keep the first
one's title. Build a sorted array of `{ "date": "YYYY-MM-DD", "title": <event summary> }`.

## Step 4 — Write `calendarOoo` (preserve everything else)

Overwrite **only** `config.calendarOoo`; leave `vacations` and all other keys untouched.
Use python so formatting/key order stays stable:

```bash
cd "${CLAUDE_PROJECT_DIR:-.}" && python3 - <<'PY'
import json
ooo = [ ... ]  # the array you built in Step 3
c = json.load(open("config.json"))
c["calendarOoo"] = ooo
json.dump(c, open("config.json", "w"), indent=2)
open("config.json", "a").write("\n")
PY
```

## Step 5 — Regenerate the dashboard

```bash
cd "${CLAUDE_PROJECT_DIR:-.}" && uv run generate.py
```

## Step 6 — Report

Summarize for the user:
- OOO days synced (count + dates), and the new total/per-month PRs-per-work-day.
- The Claude spend line from the script output (total $ and $/PR).
- Flag any synced event that looks like a working day rather than time off (e.g. an
  "Offsite" / "Summit" titled OOO block) so they can decide whether to keep it — they can
  remove it by hand from `config.calendarOoo` and re-run, or just tell you to drop it.

Notes:
- `generate.py` is `gh`-only and never touches the calendar — this command is the only
  path that writes `calendarOoo`.
- A re-run fully replaces `calendarOoo`, so OOO events deleted/changed on the calendar
  self-correct; manually-added `vacations` are never disturbed.
