# tasks.json schema

`data/tasks.json` is consumed by `index.html` (`renderTasks()`). Each entry in
the `tasks` array is rendered as one row in the Background Tasks table.

## Required-ish fields

| JSON field    | Aliases the UI also accepts            | Rendered as |
|---------------|----------------------------------------|-------------|
| `name`        | `title`, `task`                        | task name |
| `schedule`    | `cron`, `frequency`                    | schedule cell |
| `last_run`    | `lastrun`, `lastRun`, `last_run_ct`    | last-run cell |
| `next_run`    | `nextrun`, `nextRun`, `next_run_ct`    | next-run cell |
| `status`      | `state`                                | status pill (`OK` → green; contains `warn` → amber; `Not Run`/`pending`/`planned`/`n/a`/`—` → neutral gray; else red error) |
| `summary`     | `result`, `message`, `detail`          | summary cell text |

## Optional: result link

If you populate any of these fields with an `http(s)://` URL or a
same-origin relative path (`./reports/...`, `/reports/...`,
`reports/...`, `./data/...`), the frontend appends a "View report →"
hyperlink to the summary cell. This is the hook for linking out to a
Perplexity task result, a Slack message, a GitHub Action run, or a
generated report file in this repo:

- `link`
- `url`
- `href`
- `result_url`
- `report_url`

Only one is needed. The link must be `http(s)://` OR a same-origin
relative path beginning with `./`, `/`, `reports/`, or `data/`. Other
schemes (e.g. `javascript:`, `data:`) are ignored for safety. Example:

```json
{
  "id": "close-recap",
  "name": "Close Recap",
  "schedule": "Weekdays 3:30 PM CT",
  "last_run": "2026-04-28 03:30 PM CDT",
  "next_run": "2026-04-29 03:30 PM CDT",
  "status": "OK",
  "summary": "Top movers: NVDA +3, TSLA -2.",
  "result_url": "https://www.perplexity.ai/search/<task-id>"
}
```

## Status values

The frontend matches `status` case-insensitively:

- `OK` / `success` → green pill
- anything containing `warn` → amber pill
- `Not Run`, `pending`, `planned`, `n/a`, or `—` → neutral gray pill (use
  this for tasks that are listed but not yet wired up, so they don't
  visually resemble a passing or alerting task)
- anything else → red error pill

## Report cadence

The two report-generating tasks currently driven by
`.github/workflows/update-rankings.yml` are:

- **Market Risk Monitor** (`reports/market-risk-monitor.html`)
- **Options & Earnings Watchlist** (`reports/options-watchlist.html`)

Both are regenerated only on the **first scheduled rankings refresh of
the day** (08:45 America/Chicago, weekdays — the active CDT or CST
twin) and on manual `workflow_dispatch` runs. The midday (12:30 CT) and
near-close (15:35 CT) rankings refreshes leave the existing report
files untouched, so the reports reflect the morning snapshot until the
next morning (or a manual dispatch).

## Populating today

There is no automation in this repo that fetches a Perplexity (or other
agent) result URL. To surface real task output:

- **Manual**: edit `data/tasks.json` and commit. The site picks it up on
  the next Pages deploy.
- **Future automation**: a workflow step would need to call whatever
  service produced the task result (Perplexity, an internal agent runner,
  etc.) and write the URL into the matching task's `result_url` field
  before the existing commit-and-push step in
  `.github/workflows/update-rankings.yml`.
