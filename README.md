# PR-Maxxer

A static HTML dashboard showing the GitHub PRs you've merged over the last few
months — a calendar view with per-day merge counts (each day links to the matching
GitHub search) plus PRs-merged-per-work-day stats.

<img width="1736" height="1508" alt="CleanShot 2026-06-01 at 16 28 18@2x" src="https://github.com/user-attachments/assets/b11241a4-5d7b-4f2b-a762-7cd218e0c22e" />


## Requirements

- [`gh`](https://cli.github.com/) CLI, authenticated — check with `gh auth status`
- [`uv`](https://docs.astral.sh/uv/) — runs the script and handles Python deps for you

## Usage

```sh
uv run generate.py      # or: ./generate.py
open index.html
```

`generate.py` carries its dependency (`holidays`) inline via PEP 723 metadata, so
`uv run` installs it automatically in an ephemeral environment — no venv or
`pip install` needed. Re-run the script any time to refresh the data.

## Configuration — `config.json`

`config.json` is **gitignored** because it holds personal vacation/OOO data, so the repo
ships a starter at `config.example.json`. On first run, `generate.py` (and `/refresh`)
auto-creates `config.json` by copying the example — or copy it yourself:

```sh
cp config.example.json config.json   # then edit username, etc.
```

```json
{
  "username": "your-github-username",
  "monthsBack": 3,
  "timezone": "America/New_York",
  "vacations": [],
  "calendarOoo": []
}
```

- **username** — GitHub login (used for the GitHub-search links). The script also
  auto-detects your login via `gh api user`, so the placeholder is usually fine.
- **monthsBack** — how many calendar months back to include (and page across).
- **timezone** — IANA tz name. Merge timestamps (UTC) are grouped by their local
  date in this zone, so day boundaries match your expectation.
- **vacations** — hand-edited list of `YYYY-MM-DD` dates to exclude from the work-day
  count (treated just like holidays). Add days here and re-run.
- **calendarOoo** — list of `{ "date": "YYYY-MM-DD", "title": "..." }` entries, **managed
  by the Google Calendar sync** (see below). Don't hand-edit; a sync overwrites it.

## Vacation / out-of-office days

Two sources feed the work-day count, both excluded just like holidays:

1. **Manual** — add dates to `vacations` in `config.json` and re-run.
2. **Google Calendar OOO** — in Claude Code, run the project command **`/refresh`**. It
   reads your native *Out of office* events via the Google Calendar connector, expands
   multi-day blocks into individual weekdays, rewrites the `calendarOoo` array, and
   regenerates the dashboard in one shot. Your manual `vacations` are left untouched.
   (The `generate.py` script itself stays `gh`-only and needs no calendar credentials —
   `/refresh` is the single entry point that updates both OOO and PR stats.)

   > Note: a sync writes your OOO event titles into `config.json`. If those are sensitive,
   > keep `config.json` out of any shared repo (or gitignore it).

## How stats work

- **Work days** = weekdays (Mon–Fri) minus Brazilian national holidays (auto-computed via
  the `holidays` package) minus your time-off (`vacations` + `calendarOoo`).
- **PRs per work day** = total merged PRs in the range ÷ number of work days.
  Weekend/holiday merges still count toward the numerator.

## Notes

- The output `index.html` is fully self-contained (data + JS embedded) — open it
  directly, no server required.
- For a merged PR, GitHub reports the merge time as the PR's `closedAt`, which is
  what the script groups on.
