"""Shared helpers for updating data/tasks.json after a daily report regenerates.

The dashboard renders tasks.json verbatim, so each report owner is responsible
for stamping its own row (last_run / next_run / status / summary) on every
successful regeneration. This keeps the table honest instead of frozen at
hand-edited values.

next_run is always the next weekday at 08:45 America/Chicago (matching the
morning slot in .github/workflows/update-rankings.yml). Weekends roll forward
to Monday.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Morning slot wall-clock used in update-rankings.yml.
MORNING_SLOT_HOUR = 8
MORNING_SLOT_MIN = 45


def _central_now() -> datetime:
    """Return current time in America/Chicago without depending on tz packages.
    Mirrors the offset logic in export_to_json.py: CDT (Mar-Oct) else CST."""
    utc_now = datetime.now(timezone.utc)
    offset = timedelta(hours=-5) if 3 <= utc_now.month <= 10 else timedelta(hours=-6)
    return utc_now + offset


def _central_label(dt: datetime) -> str:
    return "CDT" if 3 <= dt.month <= 10 else "CST"


def _fmt_central(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %I:%M %p") + " " + _central_label(dt)


def _next_weekday_morning(now_ct: datetime) -> datetime:
    """Next 08:45 CT slot strictly after `now_ct`, skipping Sat/Sun."""
    candidate = now_ct.replace(
        hour=MORNING_SLOT_HOUR, minute=MORNING_SLOT_MIN, second=0, microsecond=0
    )
    if candidate <= now_ct:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=Sat, 6=Sun
        candidate += timedelta(days=1)
    return candidate


def update_task(
    tasks_file: Path,
    task_id: str,
    status: str,
    summary: str,
    report_url: str | None = None,
) -> None:
    """Stamp the row matching `task_id` with current run metadata.

    Silently no-ops if the file is missing or unparseable: the dashboard will
    keep showing the previous values rather than blowing up the report run.
    Non-wired rows (status='Not Run') are left untouched.
    """
    try:
        text = tasks_file.read_text(encoding="utf-8")
        data = json.loads(text)
    except (FileNotFoundError, json.JSONDecodeError):
        return

    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return

    now_ct = _central_now()
    last_run = _fmt_central(now_ct)
    next_run = _fmt_central(_next_weekday_morning(now_ct))

    for row in tasks:
        if not isinstance(row, dict):
            continue
        if row.get("id") == task_id:
            row["last_run"] = last_run
            row["next_run"] = next_run
            row["status"] = status
            row["summary"] = summary
            if report_url is not None:
                row["report_url"] = report_url
            break

    tasks_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
