"""Schedule Reliability Monitor — evaluates whether the three weekday
slots (morning / midday / close) actually delivered on recent trading
days, and how often backups/manual recoveries were needed.

The pipeline used to silently drop scheduled runs (e.g. the morning slot
on 2026-05-04 when GitHub Actions delivered nothing between Sat 06:21Z
and Mon 16:54Z and the user had to manually dispatch). The data quality
audit checks freshness of the *current* run but not the historical slot
delivery pattern. This report fills that gap by reading the slot-history
artifact written by the workflow (and falling back to git history of
data/rankings.json) so we can answer:

  * Did each expected weekday slot occur on each recent trading day?
  * Is the live data fresh enough for the slot/time we are now in?
  * Did the latest update come from `schedule` or `workflow_dispatch`?
  * How many recent firings were skipped as off-target / stale?
  * Are there obvious duplicates (slot appearing more than once / day)?

Inputs (all read-only):
  - data/reports/workflow_runs.jsonl  (preferred; written by workflow)
  - data/rankings.json                (current freshness & as_of)
  - git log on data/rankings.json     (fallback when JSONL missing)

Outputs:
  - data/reports/schedule_reliability.json
  - reports/schedule-reliability.html

Status levels per check and overall: OK / WARN / FAIL. Tolerant of
missing inputs — a missing JSONL is itself a finding (degraded WARN),
not an exception.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"
HTML_REPORTS_DIR = REPO_ROOT / "reports"

RANKINGS_FILE = DATA_DIR / "rankings.json"
RUNS_JSONL = DATA_REPORTS_DIR / "workflow_runs.jsonl"
JSON_OUTPUT = DATA_REPORTS_DIR / "schedule_reliability.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "schedule-reliability.html"
TASKS_FILE = DATA_DIR / "tasks.json"

LEVEL_RANK = {"OK": 0, "WARN": 1, "FAIL": 2}

# Slot definitions in America/Chicago. Mirror the workflow gate's MIN/MAX
# windows so any analysis matches what the pipeline actually accepts.
SLOT_WINDOWS = {
    "morning": ("08:45", "12:00"),
    "midday":  ("12:30", "15:00"),
    "close":   ("15:35", "18:30"),
}
SLOT_ORDER = ["morning", "midday", "close"]

# How many recent trading days to evaluate. Five weekdays = one trading week.
LOOKBACK_TRADING_DAYS = 5

# Cap on JSONL entries we retain; keeps the file bounded.
MAX_RUN_HISTORY = 500
HISTORY_DAYS = 90


# ----------------- helpers -----------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_chicago(dt: datetime) -> datetime:
    """Convert an aware UTC datetime to America/Chicago using a naive
    seasonal offset (CDT in months 3-10, CST otherwise). Matches the
    rest of the codebase's cheap CDT/CST approximation."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    chi = dt.astimezone(timezone.utc)
    offset_h = -5 if 3 <= chi.month <= 10 else -6
    return chi.astimezone(timezone(timedelta(hours=offset_h)))


def _parse_as_of(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{1,2}:\d{2} (?:AM|PM)) (CDT|CST)$", s.strip())
    if not m:
        return None
    naive = datetime.strptime(m.group(1), "%Y-%m-%d %I:%M %p")
    offset_hours = -5 if m.group(2) == "CDT" else -6
    return naive.replace(tzinfo=timezone(timedelta(hours=offset_hours))).astimezone(timezone.utc)


def _check(name: str, level: str, message: str, data: dict | None = None) -> dict:
    return {
        "name": name,
        "status": level,
        "message": message,
        "data": data or {},
    }


def _worst(level_a: str, level_b: str) -> str:
    return level_a if LEVEL_RANK[level_a] >= LEVEL_RANK[level_b] else level_b


def _rollup(checks: list[dict]) -> str:
    level = "OK"
    for c in checks:
        level = _worst(level, c.get("status", "OK"))
    return level


def _slot_for_chicago_hm(hm: str) -> str | None:
    """Return the slot whose window contains hm (HH:MM string)."""
    for slot, (lo, hi) in SLOT_WINDOWS.items():
        if lo <= hm <= hi:
            return slot
    return None


def _trading_days(today_chi: date, n: int) -> list[date]:
    """Return the most recent N weekday dates ending on (and including)
    today_chi if it's a weekday, else stepping back until n weekdays found."""
    out: list[date] = []
    d = today_chi
    while len(out) < n:
        if d.weekday() < 5:  # 0=Mon..4=Fri
            out.append(d)
        d -= timedelta(days=1)
    return out


# ----------------- inputs -----------------


def load_runs_jsonl() -> list[dict]:
    if not RUNS_JSONL.exists():
        return []
    out: list[dict] = []
    try:
        with RUNS_JSONL.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def load_runs_from_git() -> list[dict]:
    """Fallback when JSONL missing: derive run records from git log of
    data/rankings.json. Less rich (no skip records, no slot id from gate)
    but enough to populate the slot-occurrence calendar."""
    try:
        cp = subprocess.run(
            ["git", "log", "--format=%ct%x00%H", "--", "data/rankings.json"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if cp.returncode != 0:
        return []
    out: list[dict] = []
    for line in cp.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\x00", 1)
        if len(parts) != 2:
            continue
        try:
            ts = int(parts[0])
        except ValueError:
            continue
        sha = parts[1].strip()
        utc = datetime.fromtimestamp(ts, tz=timezone.utc)
        chi = _to_chicago(utc)
        slot = _slot_for_chicago_hm(chi.strftime("%H:%M")) or "unknown"
        out.append({
            "ts_utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ts_chicago": chi.strftime("%Y-%m-%d %H:%M"),
            "chicago_date": chi.strftime("%Y-%m-%d"),
            "event_name": "git",
            "slot": slot,
            "proceeded": True,
            "commit_sha": sha,
            "source": "git_log_fallback",
        })
    return out


def load_rankings() -> dict | None:
    try:
        with RANKINGS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ----------------- core analysis -----------------


def _bucket_runs_by_day(runs: list[dict]) -> dict[str, list[dict]]:
    """Group proceed=True runs by Chicago calendar date string."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        if not r.get("proceeded", False) and not r.get("commit_sha"):
            # Skip records (proceed=false, no commit) don't satisfy a slot.
            continue
        # Day either explicit on the record or derived from ts_chicago.
        day = r.get("chicago_date")
        if not day:
            ts = r.get("ts_chicago") or ""
            if len(ts) >= 10:
                day = ts[:10]
        if not day:
            continue
        by_day[day].append(r)
    return by_day


def analyze_slot_calendar(runs: list[dict], today_chi: date) -> dict:
    """For each of the last LOOKBACK_TRADING_DAYS weekdays, ask whether
    each slot saw at least one proceed=True run, and how many duplicates."""
    by_day = _bucket_runs_by_day(runs)
    days = _trading_days(today_chi, LOOKBACK_TRADING_DAYS)
    days.sort()  # oldest first
    rows: list[dict] = []
    missing_count = 0
    duplicate_count = 0
    for d in days:
        ds = d.strftime("%Y-%m-%d")
        day_runs = by_day.get(ds, [])
        slot_hits: dict[str, int] = {s: 0 for s in SLOT_ORDER}
        for r in day_runs:
            slot = r.get("slot")
            # Manual dispatches don't carry a slot id, but they *do*
            # satisfy whichever slot's window their wall-clock landed in.
            # Infer from ts_chicago so the calendar credits a manual
            # run rather than reporting the slot as missing.
            if slot in (None, "", "manual", "unknown"):
                ts_chi = r.get("ts_chicago") or ""
                hm = ts_chi[-5:] if len(ts_chi) >= 5 else ""
                inferred = _slot_for_chicago_hm(hm) if hm else None
                if inferred:
                    slot = inferred
            if slot in slot_hits:
                slot_hits[slot] += 1
        row = {"date": ds, "slot_hits": slot_hits, "missing": [], "duplicate": []}
        for slot in SLOT_ORDER:
            cnt = slot_hits[slot]
            if cnt == 0:
                # Don't count today's still-future slots as missing.
                if d == today_chi:
                    lo, _ = SLOT_WINDOWS[slot]
                    now_hm = datetime.now(
                        timezone(timedelta(hours=-5 if 3 <= d.month <= 10 else -6))
                    ).strftime("%H:%M")
                    if now_hm < lo:
                        continue
                row["missing"].append(slot)
                missing_count += 1
            elif cnt > 1:
                row["duplicate"].append(slot)
                duplicate_count += 1
        rows.append(row)
    return {
        "rows": rows,
        "missing_count": missing_count,
        "duplicate_count": duplicate_count,
        "lookback_days": LOOKBACK_TRADING_DAYS,
    }


def analyze_recency(runs: list[dict], rankings: dict | None) -> dict:
    """Assess freshness against the *expected* slot for now. If the live
    rankings.json's as_of is older than 24h on a weekday, FAIL."""
    out = {"checks": [], "metrics": {}}
    now_utc = _now_utc()
    chi_now = _to_chicago(now_utc)
    chi_hm = chi_now.strftime("%H:%M")
    chi_dow = chi_now.weekday()
    is_weekend = chi_dow >= 5
    out["metrics"]["chicago_now"] = chi_now.strftime("%Y-%m-%d %H:%M")
    out["metrics"]["is_weekend"] = is_weekend

    expected = _slot_for_chicago_hm(chi_hm)
    out["metrics"]["expected_slot_now"] = expected

    as_of_str = rankings.get("as_of") if isinstance(rankings, dict) else None
    as_of_dt = _parse_as_of(as_of_str)
    out["metrics"]["rankings_as_of"] = as_of_str

    if as_of_dt is None:
        out["checks"].append(_check(
            "rankings_freshness", "FAIL",
            f"could not parse rankings as_of: {as_of_str!r}"))
    else:
        age_h = (now_utc - as_of_dt).total_seconds() / 3600.0
        out["metrics"]["rankings_age_hours"] = round(age_h, 2)
        warn_h = 72.0 if is_weekend else 6.0
        fail_h = 168.0 if is_weekend else 24.0
        msg = f"as_of {as_of_str} (age {age_h:.1f}h)"
        if age_h >= fail_h:
            out["checks"].append(_check("rankings_freshness", "FAIL", msg))
        elif age_h >= warn_h:
            out["checks"].append(_check("rankings_freshness", "WARN", msg))
        else:
            out["checks"].append(_check("rankings_freshness", "OK", msg))

    # Slot match: did the most recent proceeding run cover the expected slot
    # for today? (only meaningful on weekdays past the morning window start)
    proceeded = [r for r in runs if r.get("proceeded", False) or r.get("commit_sha")]
    proceeded.sort(key=lambda r: r.get("ts_utc") or "", reverse=True)
    last = proceeded[0] if proceeded else None
    out["metrics"]["last_run"] = last
    if last:
        out["checks"].append(_check(
            "last_run_event", "OK",
            f"latest run: event={last.get('event_name')} slot={last.get('slot')} "
            f"at {last.get('ts_chicago')}"))
    else:
        out["checks"].append(_check(
            "last_run_event", "WARN",
            "no run history available — JSONL missing and git log empty"))
    return out


def analyze_skip_pattern(runs: list[dict]) -> dict:
    """Count non-proceed events in the last lookback window (off-target /
    stale / weekend). High counts on weekdays in the morning slot are a
    signal that GitHub delivery is degraded again."""
    out = {"checks": [], "metrics": {}}
    cutoff = _now_utc() - timedelta(days=14)
    skip_reasons: Counter = Counter()
    proceed_count = 0
    skip_count = 0
    for r in runs:
        ts = r.get("ts_utc") or ""
        try:
            t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if t < cutoff:
            continue
        if r.get("proceeded"):
            proceed_count += 1
        else:
            skip_count += 1
            reason = r.get("skip_reason") or r.get("slot") or "unknown"
            skip_reasons[reason] += 1
    out["metrics"]["proceed_14d"] = proceed_count
    out["metrics"]["skip_14d"] = skip_count
    out["metrics"]["skip_reasons_14d"] = dict(skip_reasons)
    # Excessive skips during weekday morning would WARN — the workflow's
    # idempotency means most backups SHOULD skip; the warning bar is high.
    if skip_count > 200:
        out["checks"].append(_check(
            "skip_volume", "WARN",
            f"{skip_count} skip records in 14d (idempotent backups expected, but >200 is unusual)"))
    else:
        out["checks"].append(_check(
            "skip_volume", "OK",
            f"{skip_count} skip records in 14d ({proceed_count} proceeded)"))
    return out


def analyze_event_mix(runs: list[dict]) -> dict:
    """Did recent proceed=True runs come from schedule or manual dispatch?
    Heavy reliance on workflow_dispatch is itself a warning signal."""
    out = {"checks": [], "metrics": {}}
    cutoff = _now_utc() - timedelta(days=14)
    schedule_n = 0
    dispatch_n = 0
    other_n = 0
    for r in runs:
        if not (r.get("proceeded") or r.get("commit_sha")):
            continue
        ts = r.get("ts_utc") or ""
        try:
            t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if t < cutoff:
            continue
        ev = (r.get("event_name") or "").lower()
        if ev == "schedule":
            schedule_n += 1
        elif ev == "workflow_dispatch":
            dispatch_n += 1
        else:
            other_n += 1
    total = schedule_n + dispatch_n + other_n
    out["metrics"]["proceed_14d_by_event"] = {
        "schedule": schedule_n,
        "workflow_dispatch": dispatch_n,
        "other": other_n,
    }
    if total == 0:
        out["checks"].append(_check(
            "event_mix", "WARN",
            "no proceed records in 14d — cannot distinguish schedule vs manual"))
    elif dispatch_n > schedule_n and dispatch_n >= 3:
        out["checks"].append(_check(
            "event_mix", "WARN",
            f"workflow_dispatch ({dispatch_n}) dominates schedule ({schedule_n}) — "
            f"GitHub Actions schedule delivery may be degraded"))
    else:
        out["checks"].append(_check(
            "event_mix", "OK",
            f"schedule={schedule_n} dispatch={dispatch_n} other={other_n}"))
    return out


# ----------------- output -----------------


def _build_overall(sections: dict) -> str:
    level = "OK"
    for sec in sections.values():
        level = _worst(level, sec.get("status", "OK"))
    return level


# How fresh "today" must be for an effective downgrade. Mirrors the
# weekday WARN threshold used in analyze_recency.
EFFECTIVE_FRESH_HOURS_WEEKDAY = 6.0
EFFECTIVE_FRESH_HOURS_WEEKEND = 72.0


def _current_expected_slot(chi_now: datetime) -> str | None:
    """The slot the pipeline is expected to have covered by `chi_now`.

    Returns the slot whose window currently contains now; if now sits in a
    gap between slots (or after the close window) it returns the most recent
    slot whose start time has already passed today. Before the morning
    window opens, or on weekends, there is no expected slot yet -> None.

    This is the basis for "did the current/most-recent expected slot get a
    refresh?" — distinct from "did every slot get a dedicated firing?", which
    a single delayed delivery can never satisfy.
    """
    if chi_now.weekday() >= 5:
        return None
    hm = chi_now.strftime("%H:%M")
    inside = _slot_for_chicago_hm(hm)
    if inside:
        return inside
    # Not inside any window: pick the latest slot whose MIN_HM has passed.
    passed = [s for s in SLOT_ORDER if SLOT_WINDOWS[s][0] <= hm]
    return passed[-1] if passed else None


def compute_effective_overall(raw: str, sections: dict) -> dict:
    """Decide the *current operational* overall vs the raw history rollup.

    Raw `overall` reflects the worst section status — it folds in two
    *diagnostic* signals that do not mean the pipeline is broken right now:
      * historical slot misses on prior weekdays, and
      * slot-coverage bookkeeping gaps (GitHub Actions delivers a slot's
        cron late, the run lands in a neighbouring slot's window, and
        slot-level idempotency then skips the dedicated firing — so the
        calendar credits one logical slot and flags the other "missing"
        even though the day actually refreshed).

    `overall_effective` answers a different question: "is the pipeline
    healthy right now?" An *active* FAIL only happens when:
      * the live data is stale for the current time, OR
      * no successful refresh has covered the current/most-recent expected
        slot (and we cannot establish freshness another way).

    When the live data is fresh AND the current expected slot has been
    covered, a raw FAIL downgrades to WARN (recovered) — historical and
    bookkeeping misses stay visible in the raw rollup for transparency but
    no longer masquerade as an outage.

    Returns a dict with `effective`, `recovered`, `today_satisfied`,
    `today_missing`, `current_slot`, `current_slot_covered`,
    `rankings_age_hours`, `rankings_fresh`, `last_run_event`,
    `missing_count`, and a short `reason` string.
    """
    cal = (sections.get("calendar") or {}).get("metrics", {}).get("calendar") or {}
    rows = cal.get("rows") or []
    missing_count = cal.get("missing_count", 0)

    chi_now = _to_chicago(_now_utc())
    chi_today_str = chi_now.date().strftime("%Y-%m-%d")
    today_row = next((r for r in rows if r.get("date") == chi_today_str), None)
    today_missing = list((today_row or {}).get("missing") or [])
    today_satisfied = today_row is not None and not today_missing

    rec = sections.get("recency") or {}
    rec_metrics = rec.get("metrics") or {}
    last_run = rec_metrics.get("last_run") or {}
    age_h = rec_metrics.get("rankings_age_hours")
    is_weekend = bool(rec_metrics.get("is_weekend"))
    fresh_warn = (EFFECTIVE_FRESH_HOURS_WEEKEND if is_weekend
                  else EFFECTIVE_FRESH_HOURS_WEEKDAY)
    is_fresh = isinstance(age_h, (int, float)) and age_h < fresh_warn

    # Is the current/most-recent expected slot covered? Two independent
    # signals satisfy this:
    #   1. Fresh data: a fresh as_of means a refresh just landed, regardless
    #      of which logical slot id the gate stamped on it. This is the key
    #      fix for delayed deliveries that get credited to a neighbouring slot.
    #   2. The latest proceeding run's Chicago timestamp is on today's date
    #      and at/after the current expected slot's start minute.
    current_slot = _current_expected_slot(chi_now)
    current_slot_covered = False
    if current_slot is None:
        # No slot is expected yet (pre-morning / weekend): nothing to cover.
        current_slot_covered = is_fresh
    else:
        if is_fresh:
            current_slot_covered = True
        else:
            last_ts_chi = (last_run.get("ts_chicago") or "")
            last_date = last_ts_chi[:10]
            last_hm = last_ts_chi[-5:] if len(last_ts_chi) >= 5 else ""
            min_hm = SLOT_WINDOWS[current_slot][0]
            current_slot_covered = (
                last_date == chi_today_str and bool(last_hm) and last_hm >= min_hm
            )

    has_any_refresh = bool(last_run) or isinstance(age_h, (int, float))

    out = {
        "effective": raw,
        "recovered": False,
        "today_satisfied": today_satisfied,
        "today_missing": today_missing,
        "current_slot": current_slot,
        "current_slot_covered": current_slot_covered,
        "rankings_age_hours": age_h,
        "rankings_fresh": is_fresh,
        "last_run_event": last_run.get("event_name"),
        "missing_count": missing_count,
        "reason": "",
    }

    if raw == "OK":
        out["reason"] = "no historical misses; pipeline healthy"
        return out
    if raw == "WARN":
        out["reason"] = (
            f"diagnostic slot gaps only (missing={missing_count}); "
            f"live data {'fresh' if is_fresh else 'check freshness'}"
        )
        return out

    # raw == "FAIL"
    if not is_fresh:
        out["reason"] = (
            f"live data stale (age {age_h}h >= {fresh_warn}h) — active failure"
        )
        return out
    if not has_any_refresh:
        out["reason"] = "no successful refresh on record — active failure"
        return out
    if current_slot_covered:
        out["effective"] = "WARN"
        out["recovered"] = True
        out["reason"] = (
            f"live data fresh (age {age_h}h < {fresh_warn}h) and current "
            f"expected slot ({current_slot or 'none'}) covered; "
            f"{missing_count} historical/bookkeeping slot gap(s) in lookback "
            f"are diagnostic, not an outage"
        )
        return out
    out["reason"] = (
        f"current expected slot ({current_slot}) not yet covered today; "
        f"{missing_count} slot gap(s) in lookback — active failure"
    )
    return out


def _render_html(report: dict) -> str:
    raw = report.get("overall_raw") or report["overall"]
    effective = report.get("overall_effective") or raw
    color_eff = {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}[effective]
    color_raw = {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}[raw]
    eff_meta = report.get("effective") or {}
    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Schedule Reliability</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:980px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
h1{{margin:0 0 4px}} .meta{{color:#666;font-size:13px;margin-bottom:18px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;
       font-weight:600;font-size:12px;letter-spacing:.5px;background:{color_eff}}}
.badge-raw{{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;
       font-weight:600;font-size:12px;letter-spacing:.5px;background:{color_raw};margin-left:6px}}
.section{{border:1px solid #e3e3e3;border-radius:8px;padding:14px 16px;margin:14px 0}}
.section h2{{margin:0 0 10px;font-size:18px;display:flex;justify-content:space-between;align-items:center}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
.OK{{color:#3c8c3c;font-weight:600}}
.WARN{{color:#b88a00;font-weight:600}}
.FAIL{{color:#c0392b;font-weight:600}}
.kv{{font-size:13px;color:#444}} .kv pre{{background:#f8f8f8;padding:8px;border-radius:4px;overflow:auto}}
.calendar td.hit{{background:#e6f4ea;color:#2e7d32}}
.calendar td.miss{{background:#fdecea;color:#c0392b;font-weight:600}}
.calendar td.dup{{background:#fff3cd;color:#8a6d3b}}
.calendar td.future{{background:#f3f3f3;color:#999}}
.back{{font-size:13px}}
.banner{{background:#fff8e1;border:1px solid #f0d27a;padding:10px 12px;
        border-radius:6px;margin:0 0 12px;font-size:13px}}
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>Schedule Reliability</h1>
<p class="meta">Generated {escape(report["generated_at"])} &middot; Effective: <span class="badge">{effective}</span> &middot; Raw history: <span class="badge-raw">{raw}</span></p>
""")
    if eff_meta.get("recovered"):
        parts.append(
            '<p class="banner"><strong>Fresh — slot gaps are diagnostic.</strong> '
            + escape(str(eff_meta.get("reason") or ""))
            + '. Effective state is WARN; raw history remains FAIL because the '
              'missed/late slot bookkeeping is preserved for transparency. The '
              'live data is current.</p>'
        )
    elif raw == "FAIL" and effective == "FAIL":
        parts.append(
            '<p class="banner" style="background:#fdecea;border-color:#e6a8a0;color:#7a2018">'
            '<strong>Active failure.</strong> '
            + escape(str(eff_meta.get("reason") or ""))
            + '</p>'
        )

    # Slot calendar (visual)
    cal = report["sections"].get("calendar", {})
    cal_data = cal.get("metrics", {}).get("calendar") or {}
    cal_rows = cal_data.get("rows") or []
    if cal_rows:
        parts.append('<div class="section calendar"><h2>Recent Slot Delivery</h2>')
        parts.append('<table><thead><tr><th>Date</th>')
        for s in SLOT_ORDER:
            parts.append(f'<th>{s}</th>')
        parts.append('</tr></thead><tbody>')
        for r in cal_rows:
            parts.append(f'<tr><td>{escape(r["date"])}</td>')
            for s in SLOT_ORDER:
                cnt = r["slot_hits"].get(s, 0)
                if cnt == 0 and s not in r.get("missing", []):
                    cls, label = "future", "—"
                elif cnt == 0:
                    cls, label = "miss", "missing"
                elif cnt > 1:
                    cls, label = "dup", f"{cnt}× dup"
                else:
                    cls, label = "hit", "✓"
                parts.append(f'<td class="{cls}">{label}</td>')
            parts.append('</tr>')
        parts.append('</tbody></table></div>')

    for key, sec in report["sections"].items():
        st = sec.get("status", "OK")
        parts.append(f'<div class="section"><h2>{escape(key.replace("_", " ").title())}'
                     f'<span class="{st}">{st}</span></h2>')
        if sec.get("metrics"):
            parts.append('<div class="kv"><strong>Metrics:</strong><pre>'
                         + escape(json.dumps(sec["metrics"], indent=2, default=str))
                         + '</pre></div>')
        parts.append('<table><thead><tr><th style="width:32%">Check</th>'
                     '<th style="width:10%">Status</th><th>Detail</th></tr></thead><tbody>')
        for c in sec.get("checks", []):
            parts.append(
                f'<tr><td>{escape(c.get("name",""))}</td>'
                f'<td class="{escape(c.get("status",""))}">{escape(c.get("status",""))}</td>'
                f'<td>{escape(c.get("message",""))}</td></tr>'
            )
        parts.append('</tbody></table></div>')

    parts.append('</body></html>')
    return "\n".join(parts)


def _stamp_task_if_present(report: dict) -> None:
    if not TASKS_FILE.exists():
        return
    try:
        with TASKS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return
    changed = False
    for row in tasks:
        if isinstance(row, dict) and row.get("id") == "schedule-reliability":
            raw = report.get("overall_raw") or report["overall"]
            effective = report.get("overall_effective") or raw
            row["last_run"] = report.get("generated_at_chicago") or row.get("last_run") or "—"
            row["status"] = ("OK" if effective == "OK"
                             else ("warn" if effective == "WARN" else "fail"))
            cal = report.get("sections", {}).get("calendar", {}).get("metrics", {}).get("calendar", {})
            missing = cal.get("missing_count")
            if raw == "FAIL" and effective != "FAIL":
                row["summary"] = (
                    f"Reliability: {effective} — live data fresh; "
                    f"{missing} diagnostic slot gap(s) last "
                    f"{cal.get('lookback_days', LOOKBACK_TRADING_DAYS)}d (recovered)"
                )
            else:
                row["summary"] = (
                    f"Reliability: {effective}; missing slots last "
                    f"{cal.get('lookback_days', LOOKBACK_TRADING_DAYS)}d = {missing}"
                )
            row["report_url"] = "./reports/schedule-reliability.html"
            changed = True
    if changed:
        TASKS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def build_report() -> dict:
    runs = load_runs_jsonl()
    used_fallback = False
    used_supplement = False
    if not runs:
        runs = load_runs_from_git()
        used_fallback = True
    elif len(runs) < LOOKBACK_TRADING_DAYS * 2:
        # JSONL is freshly bootstrapped (first ~few days after deploy);
        # supplement with git-log entries from days NOT already covered
        # so the calendar isn't blanket-missing while history fills in.
        covered_days = {r.get("chicago_date") for r in runs if r.get("chicago_date")}
        for g in load_runs_from_git():
            if g.get("chicago_date") not in covered_days:
                runs.append(g)
        used_supplement = True

    rankings = load_rankings()
    now_utc = _now_utc()
    chi_now = _to_chicago(now_utc)
    today_chi = chi_now.date()

    # Calendar section
    cal = analyze_slot_calendar(runs, today_chi)
    cal_section: dict = {"checks": [], "metrics": {"calendar": cal}}
    # Missing-slot threshold logic. A single missing slot WARNs (could be
    # GH delay or genuine miss); 3+ missing in 5 days is a FAIL.
    if cal["missing_count"] >= 3:
        cal_section["checks"].append(_check(
            "missing_slots", "FAIL",
            f"{cal['missing_count']} expected slot(s) missing in last "
            f"{LOOKBACK_TRADING_DAYS} weekdays (diagnostic — see effective "
            f"status; a delayed delivery credited to a neighbouring slot "
            f"shows here as a gap even when the day refreshed)"))
    elif cal["missing_count"] >= 1:
        cal_section["checks"].append(_check(
            "missing_slots", "WARN",
            f"{cal['missing_count']} expected slot(s) missing in last "
            f"{LOOKBACK_TRADING_DAYS} weekdays (diagnostic — see effective status)"))
    else:
        cal_section["checks"].append(_check(
            "missing_slots", "OK",
            f"all expected slots present in last {LOOKBACK_TRADING_DAYS} weekdays"))
    if cal["duplicate_count"] >= 1:
        cal_section["checks"].append(_check(
            "duplicate_slots", "WARN",
            f"{cal['duplicate_count']} day-slot pair(s) had multiple proceed events — "
            f"slot-level idempotency may be misfiring"))
    else:
        cal_section["checks"].append(_check(
            "duplicate_slots", "OK", "no duplicate slot proceeds detected"))
    if used_fallback:
        cal_section["checks"].append(_check(
            "history_source", "WARN",
            "workflow_runs.jsonl missing — calendar derived from git log fallback"))
    elif used_supplement:
        cal_section["checks"].append(_check(
            "history_source", "OK",
            f"jsonl has {sum(1 for r in runs if r.get('source') != 'git_log_fallback')} records "
            f"(supplemented with git log for older days while history fills in)"))
    else:
        cal_section["checks"].append(_check(
            "history_source", "OK",
            f"history loaded from {RUNS_JSONL.name} ({len(runs)} records)"))
    cal_section["status"] = _rollup(cal_section["checks"])

    # Recency section
    rec = analyze_recency(runs, rankings)
    rec["status"] = _rollup(rec["checks"])

    # Skip pattern
    skip = analyze_skip_pattern(runs)
    skip["status"] = _rollup(skip["checks"])

    # Event mix
    mix = analyze_event_mix(runs)
    mix["status"] = _rollup(mix["checks"])

    sections = {
        "calendar": cal_section,
        "recency": rec,
        "skip_pattern": skip,
        "event_mix": mix,
    }

    chi_str = chi_now.strftime("%Y-%m-%d %I:%M %p ") + ("CDT" if chi_now.utcoffset().total_seconds() == -5 * 3600 else "CST")
    raw_overall = _build_overall(sections)
    eff = compute_effective_overall(raw_overall, sections)
    report = {
        "generated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_chicago": chi_str,
        "overall": raw_overall,
        "overall_raw": raw_overall,
        "overall_effective": eff["effective"],
        "effective": eff,
        "sections": sections,
        "history_records": len(runs),
        "history_source": "git_log_fallback" if used_fallback else "jsonl",
    }
    return report


def _record_run_from_env() -> int:
    """CLI mode used by the workflow to append a single run record.
    All fields read from env vars so the workflow yaml stays simple.
    Required: SR_EVENT_NAME, SR_SLOT, SR_PROCEEDED.
    Optional: SR_AS_OF, SR_OPEN_DATE, SR_RUN_URL, SR_COMMIT_SHA, SR_SKIP_REASON,
              SR_DURATION_SECONDS.
    """
    proceeded_raw = os.environ.get("SR_PROCEEDED", "false").strip().lower()
    proceeded = proceeded_raw in ("1", "true", "yes", "on")
    now_utc = _now_utc()
    chi = _to_chicago(now_utc)
    record: dict = {
        "ts_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_chicago": chi.strftime("%Y-%m-%d %H:%M"),
        "chicago_date": chi.strftime("%Y-%m-%d"),
        "event_name": os.environ.get("SR_EVENT_NAME", "") or "unknown",
        "slot": os.environ.get("SR_SLOT", "") or "unknown",
        "proceeded": proceeded,
    }
    for env_key, json_key in (
        ("SR_AS_OF", "as_of"),
        ("SR_OPEN_DATE", "open_date"),
        ("SR_RUN_URL", "run_url"),
        ("SR_COMMIT_SHA", "commit_sha"),
        ("SR_SKIP_REASON", "skip_reason"),
    ):
        v = os.environ.get(env_key, "").strip()
        if v:
            record[json_key] = v
    dur = os.environ.get("SR_DURATION_SECONDS", "").strip()
    if dur:
        try:
            record["duration_seconds"] = int(float(dur))
        except ValueError:
            pass
    append_run_record(record)
    print(f"[schedule_reliability] appended run record: slot={record['slot']} "
          f"event={record['event_name']} proceeded={proceeded}")
    return 0


def main() -> int:
    if "--record-run" in sys.argv:
        return _record_run_from_env()
    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report()
    JSON_OUTPUT.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")
    _stamp_task_if_present(report)
    print(f"[schedule_reliability] overall={report['overall']} -> {JSON_OUTPUT}")
    return 0


# ----------------- JSONL bounded writer (used by workflow) -----------------


def append_run_record(record: dict, *, max_records: int = MAX_RUN_HISTORY,
                      max_age_days: int = HISTORY_DAYS) -> None:
    """Append a single run record to RUNS_JSONL, then trim to bounds.
    Caller passes a fully-formed record dict with at minimum:
      ts_utc, ts_chicago, chicago_date, event_name, slot, proceeded.
    """
    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_runs_jsonl()
    existing.append(record)
    # Trim by age first, then cap to max_records.
    cutoff = _now_utc() - timedelta(days=max_age_days)

    def _ok(r: dict) -> bool:
        ts = r.get("ts_utc") or ""
        try:
            t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return True  # keep records with unparsable timestamps; better than dropping
        return t >= cutoff

    existing = [r for r in existing if _ok(r)]
    if len(existing) > max_records:
        existing = existing[-max_records:]
    with RUNS_JSONL.open("w", encoding="utf-8") as f:
        for r in existing:
            f.write(json.dumps(r, default=str) + "\n")


if __name__ == "__main__":
    sys.exit(main())
