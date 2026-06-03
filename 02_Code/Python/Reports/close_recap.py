"""Close Recap — end-of-day rollup that summarizes where the rankings
landed at/near close. Companion to Market Open Scan and Midday Health
Check; intended to fire on the 3:35 PM CT close refresh, but cheap and
read-only enough to run on every proceeded slot so the dashboard always
reflects the most recent close-state snapshot.

Inputs (read-only):
  - data/rankings.json
  - data/watchlist_rankings.json
  - data/reports/data_quality_audit.json
  - data/reports/schedule_reliability.json
  - data/reports/market_risk_monitor.json (optional)
  - data/reports/benchmark_review.json (optional)
  - data/reports/scoring_parity_review.json (optional)
  - data/reports/ranking_diagnostics.json (optional)
  - data/reports/market_open_scan.json (optional, for top10 baseline)
  - data/reports/benchmark_snapshots.jsonl (optional, prior trading day)

Outputs:
  - data/reports/close_recap.json
  - reports/close-recap.html
  - data/tasks.json row id=close-recap stamped on each run.

Status logic:
  * FAIL when:
      - rankings.json missing/unparseable/stale (>24h on weekday)
      - data quality audit overall=FAIL on rankings/tasks sections
  * WARN when:
      - schedule reliability rescued/manual or WARN
      - market risk monitor alert is active
      - ranking diagnostics overall=FAIL/WARN
      - sector concentration high (>=40% of top10 in one sector)
      - watchlist SUPP coverage degraded or unavailable count > 0
      - data quality WARN
  * OK otherwise.

Like the morning briefing the report never raises out of build_report;
missing optional inputs become advisory findings rather than exceptions.
Scoring formulas are NOT touched — this is purely an observability
rollup.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"
HTML_REPORTS_DIR = REPO_ROOT / "reports"

RANKINGS_FILE = DATA_DIR / "rankings.json"
WATCHLIST_FILE = DATA_DIR / "watchlist_rankings.json"
DATA_QUALITY_FILE = DATA_REPORTS_DIR / "data_quality_audit.json"
SCHEDULE_RELIABILITY_FILE = DATA_REPORTS_DIR / "schedule_reliability.json"
MARKET_RISK_FILE = DATA_REPORTS_DIR / "market_risk_monitor.json"
BENCHMARK_FILE = DATA_REPORTS_DIR / "benchmark_review.json"
PARITY_FILE = DATA_REPORTS_DIR / "scoring_parity_review.json"
DIAGNOSTICS_FILE = DATA_REPORTS_DIR / "ranking_diagnostics.json"
MARKET_OPEN_SCAN_FILE = DATA_REPORTS_DIR / "market_open_scan.json"
BENCHMARK_SNAPSHOTS_FILE = DATA_REPORTS_DIR / "benchmark_snapshots.jsonl"

JSON_OUTPUT = DATA_REPORTS_DIR / "close_recap.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "close-recap.html"
TASKS_FILE = DATA_DIR / "tasks.json"
REPORT_URL = "./reports/close-recap.html"

LEVEL_RANK = {"OK": 0, "WARN": 1, "FAIL": 2}

FRESH_WARN_HOURS_WEEKDAY = 6.0
FRESH_FAIL_HOURS_WEEKDAY = 24.0
FRESH_WARN_HOURS_WEEKEND = 72.0
FRESH_FAIL_HOURS_WEEKEND = 168.0

TOP_N = 10
TOP_MOVERS = 5

# Sector concentration threshold for "Top10 dominated by sector X" warning.
SECTOR_CONCENTRATION_WARN = 0.40


# ----------------- helpers -----------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_chicago(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    chi_utc = dt.astimezone(timezone.utc)
    offset_h = -5 if 3 <= chi_utc.month <= 10 else -6
    return chi_utc.astimezone(timezone(timedelta(hours=offset_h)))


def _parse_as_of(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{1,2}:\d{2} (?:AM|PM)) (CDT|CST)$", s.strip())
    if not m:
        return None
    naive = datetime.strptime(m.group(1), "%Y-%m-%d %I:%M %p")
    offset_hours = -5 if m.group(2) == "CDT" else -6
    return naive.replace(tzinfo=timezone(timedelta(hours=offset_hours))).astimezone(timezone.utc)


def _short_time(s: str | None) -> str:
    if not s:
        return "—"
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{1,2}:\d{2} (?:AM|PM)) (CDT|CST)$", s.strip())
    if not m:
        return s
    return f"{m.group(2)} CT"


def _worst(a: str, b: str) -> str:
    return a if LEVEL_RANK[a] >= LEVEL_RANK[b] else b


def _load_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_prior_snapshot(path: Path, today_chi: str) -> dict | None:
    """Return the most recent benchmark snapshot strictly before today."""
    if not path.exists():
        return None
    latest: dict | None = None
    latest_date: str | None = None
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                d = rec.get("as_of_date")
                if not isinstance(d, str) or d >= today_chi:
                    continue
                if latest_date is None or d > latest_date:
                    latest_date = d
                    latest = rec
    except OSError:
        return None
    return latest


def _safe_change(row: dict) -> int | None:
    v = row.get("change")
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


# ----------------- analyzers -----------------


def analyze_freshness(rankings: dict | None) -> dict:
    """Did the close-refresh data land?

    The morning briefing's freshness logic is reused here; what changes
    is how the result is described in the rollup summary (Live/Stale at
    close vs. Live at open). The thresholds match: a >24h gap on a
    weekday is FAIL because the dashboard would be misleading.
    """
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(rankings, dict):
        section["checks"].append({
            "name": "rankings_present",
            "status": "FAIL",
            "message": "rankings.json missing or unparseable",
        })
        section["status"] = "FAIL"
        return section
    as_of = rankings.get("as_of")
    open_date = rankings.get("open_date")
    section["metrics"]["as_of"] = as_of
    section["metrics"]["open_date"] = open_date
    section["metrics"]["row_count"] = len(rankings.get("rows") or [])
    section["metrics"]["is_open_run"] = bool(rankings.get("is_open_run"))
    section["metrics"]["universe"] = rankings.get("universe")

    now_utc = _now_utc()
    chi_now = _to_chicago(now_utc)
    is_weekend = chi_now.weekday() >= 5
    section["metrics"]["is_weekend"] = is_weekend
    section["metrics"]["chicago_now"] = chi_now.strftime("%Y-%m-%d %H:%M")

    as_of_dt = _parse_as_of(as_of)
    if as_of_dt is None:
        section["checks"].append({
            "name": "rankings_freshness",
            "status": "FAIL",
            "message": f"could not parse as_of: {as_of!r}",
        })
        section["status"] = "FAIL"
        return section
    age_h = (now_utc - as_of_dt).total_seconds() / 3600.0
    section["metrics"]["rankings_age_hours"] = round(age_h, 2)

    warn_h = FRESH_WARN_HOURS_WEEKEND if is_weekend else FRESH_WARN_HOURS_WEEKDAY
    fail_h = FRESH_FAIL_HOURS_WEEKEND if is_weekend else FRESH_FAIL_HOURS_WEEKDAY
    if age_h >= fail_h:
        level = "FAIL"
    elif age_h >= warn_h:
        level = "WARN"
    else:
        level = "OK"
    section["checks"].append({
        "name": "rankings_freshness",
        "status": level,
        "message": f"as_of {as_of} (age {age_h:.1f}h, weekend={is_weekend})",
    })
    as_of_chi = _to_chicago(as_of_dt)
    today_live = as_of_chi.date() == chi_now.date()
    section["metrics"]["today_live"] = today_live
    # Identify whether the latest run is in the close window. Close slot
    # is satisfied by a commit at or after 15:35 Chicago.
    section["metrics"]["close_slot_satisfied"] = (
        today_live and as_of_chi.hour >= 15 and (
            as_of_chi.hour > 15 or as_of_chi.minute >= 35)
    )
    section["checks"].append({
        "name": "today_live",
        # On a weekday, today_live is required for OK; weekend tolerates.
        "status": "OK" if (today_live or is_weekend) else "WARN",
        "message": f"data {'is' if today_live else 'is not'} from today (Chicago)",
    })
    section["status"] = max(
        (c["status"] for c in section["checks"]), key=lambda s: LEVEL_RANK[s])
    return section


def analyze_run_source(sr_rep: dict | None) -> dict:
    """Classify how the latest run got here (schedule/manual/recovered)."""
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(sr_rep, dict):
        section["checks"].append({
            "name": "schedule_reliability_present",
            "status": "WARN",
            "message": "schedule_reliability.json missing — run source unknown",
        })
        section["status"] = "WARN"
        return section

    raw = (sr_rep.get("overall") or "OK").upper()
    eff = (sr_rep.get("overall_effective") or raw).upper()
    section["metrics"]["overall_raw"] = raw
    section["metrics"]["overall_effective"] = eff

    sections = sr_rep.get("sections") or {}
    rec = (sections.get("recency") or {}).get("metrics", {}).get("last_run") or {}
    last_event = rec.get("event_name")
    last_slot = rec.get("slot")
    last_ts = rec.get("ts_chicago")
    section["metrics"]["last_run_event"] = last_event
    section["metrics"]["last_run_slot"] = last_slot
    section["metrics"]["last_run_ts_chicago"] = last_ts

    cal = (sections.get("calendar") or {}).get("metrics", {}).get("calendar") or {}
    rows = cal.get("rows") or []
    chi_today = _to_chicago(_now_utc()).date().strftime("%Y-%m-%d")
    today_row = next((r for r in rows if r.get("date") == chi_today), None)
    today_missing = list((today_row or {}).get("missing") or [])
    section["metrics"]["today_missing"] = today_missing
    today_satisfied = today_row is not None and not today_missing
    section["metrics"]["today_satisfied"] = today_satisfied

    missing_count = cal.get("missing_count", 0) or 0
    # Prefer the report's own recovered flag (freshness + current-slot
    # coverage basis); fall back to today_satisfied for older JSONs, which
    # undercounts recoveries when a delayed delivery is credited to a
    # neighbouring slot.
    report_recovered = bool((sr_rep.get("effective") or {}).get("recovered"))
    if (raw == "FAIL" and eff != "FAIL"
            and (report_recovered or (today_satisfied and missing_count > 0))):
        source = "recovered"
    elif last_event == "workflow_dispatch":
        source = "manual"
    elif raw == "OK":
        source = "schedule"
    elif eff in ("WARN", "FAIL"):
        source = "schedule_degraded"
    else:
        source = "schedule"
    section["metrics"]["source"] = source

    # Whether today's close slot was specifically satisfied (by either
    # the schedule or a manual rescue). We can't tell from raw schedule
    # data which slot the last commit covered, but the calendar row
    # exposes per-slot misses.
    close_missing = "close" in today_missing
    section["metrics"]["close_slot_missing"] = close_missing

    if source == "manual":
        msg = f"latest run: manual workflow_dispatch ({last_ts or '—'})"
        status = "WARN"
    elif source == "recovered":
        msg = (f"latest run: rescued slot ({last_ts or '—'}), "
               f"history shows {missing_count} missing slot(s)")
        status = "WARN"
    elif source == "schedule_degraded":
        msg = (f"schedule degraded: today_missing={today_missing}, "
               f"history_missing={missing_count}")
        status = eff if eff in LEVEL_RANK else "WARN"
    else:
        msg = f"latest run: scheduled ({last_ts or '—'})"
        status = "OK"
    if close_missing and source not in ("manual", "recovered"):
        # If the close slot specifically is unmet today (and we can't say
        # the latest was a rescue), surface that as a WARN even when the
        # broader schedule view is OK — close recap consumers care about
        # the close slot more than the morning slot.
        msg += " · close slot still missing today"
        status = _worst(status, "WARN")
    section["checks"].append({"name": "run_source", "status": status, "message": msg})
    section["status"] = status
    return section


def analyze_data_quality(dq: dict | None) -> dict:
    """Roll up data_quality_audit.json (same logic as the morning scan)."""
    section = {"checks": [], "metrics": {}, "status": "WARN"}
    if not isinstance(dq, dict):
        section["checks"].append({
            "name": "data_quality_present",
            "status": "WARN",
            "message": "data_quality_audit.json missing — run not yet generated",
        })
        return section
    overall = (dq.get("overall") or "OK").upper()
    section["metrics"]["overall"] = overall
    sections = dq.get("sections") or {}
    by_section: dict[str, str] = {}
    rankings_status = "OK"
    tasks_status = "OK"
    for key, sec in sections.items():
        if not isinstance(sec, dict):
            continue
        sec_status = "OK"
        for c in sec.get("checks", []):
            sec_status = _worst(sec_status, (c.get("status") or "OK").upper())
        by_section[key] = sec_status
        if key == "rankings":
            rankings_status = sec_status
        elif key == "tasks":
            tasks_status = sec_status
    section["metrics"]["by_section"] = by_section
    critical_fail = rankings_status == "FAIL" or tasks_status == "FAIL"
    section["metrics"]["critical_section_fail"] = critical_fail
    section["checks"].append({
        "name": "data_quality_overall",
        "status": overall,
        "message": (f"overall={overall}; sections="
                    + ", ".join(f"{k}:{v}" for k, v in by_section.items())),
    })
    if critical_fail:
        section["checks"].append({
            "name": "critical_sections",
            "status": "FAIL",
            "message": (f"rankings={rankings_status}, tasks={tasks_status}: "
                        "critical section failure"),
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]), key=lambda s: LEVEL_RANK[s])
    return section


def _prior_bucket_tickers(snapshot: dict | None, bucket_key: str) -> set[str]:
    if not isinstance(snapshot, dict):
        return set()
    bucket = ((snapshot.get("buckets") or {}).get(bucket_key) or {})
    members = bucket.get("members") or []
    out: set[str] = set()
    for m in members:
        if isinstance(m, dict) and m.get("ticker"):
            out.add(m["ticker"])
    return out


def _open_top_tickers(open_scan: dict | None) -> set[str]:
    """Pull this-morning's top10 tickers from the market_open_scan report,
    so the close recap can flag intraday additions/exits without needing
    its own snapshot store."""
    if not isinstance(open_scan, dict):
        return set()
    rc = (open_scan.get("sections") or {}).get("rankings_changes") or {}
    top10 = (rc.get("metrics") or {}).get("top_10") or []
    return {r.get("ticker") for r in top10 if isinstance(r, dict) and r.get("ticker")}


def extract_rankings_movers(
    rankings: dict | None,
    prior_top_tickers: set[str] | None = None,
    prior_as_of_date: str | None = None,
    open_top_tickers: set[str] | None = None,
) -> dict:
    """Pull top10, MOV gainers/losers, plus new/exited entries vs the
    prior trading day's top10 *and* (when available) intraday entries
    that joined the top10 since this morning's open scan.

    Records with non-numeric or null `change` are kept out of the
    gainer/loser lists. Sector concentration is computed on the top10.
    """
    out: dict = {
        "top_10": [],
        "top_gainers": [],
        "top_losers": [],
        "new_top10_entries": [],
        "exited_top10_entries": [],
        "intraday_new_entries": [],
        "prior_top10_compared_against": prior_as_of_date,
        "mov_summary": {},
        "sector_concentration": {},
        "limitation": (
            "MOV-based gainers/losers; new/exited entries vs prior "
            "benchmark snapshot; intraday entries vs market_open_scan."
        ),
    }
    if not isinstance(rankings, dict):
        return out
    rows = rankings.get("rows") or []
    if not isinstance(rows, list):
        return out

    out["top_10"] = [
        {
            "rank": r.get("rank"),
            "ticker": r.get("ticker"),
            "company": r.get("company"),
            "ai_score": r.get("ai_score"),
            "change": _safe_change(r),
            "sector": r.get("sector"),
        }
        for r in rows[:TOP_N] if isinstance(r, dict)
    ]
    cur_top_tickers = {t["ticker"] for t in out["top_10"] if t.get("ticker")}

    if prior_top_tickers is not None:
        out["new_top10_entries"] = [
            {
                "ticker": t["ticker"],
                "rank": t["rank"],
                "ai_score": t.get("ai_score"),
                "sector": t.get("sector"),
            }
            for t in out["top_10"]
            if t.get("ticker") and t["ticker"] not in prior_top_tickers
        ]
        # Tickers that were in the prior top10 but are no longer in the
        # current top10 — useful for understanding what dropped through
        # the close.
        out["exited_top10_entries"] = sorted(
            list(prior_top_tickers - cur_top_tickers)
        )

    if open_top_tickers:
        out["intraday_new_entries"] = [
            {
                "ticker": t["ticker"],
                "rank": t["rank"],
                "ai_score": t.get("ai_score"),
                "sector": t.get("sector"),
            }
            for t in out["top_10"]
            if t.get("ticker") and t["ticker"] not in open_top_tickers
        ]

    with_change = [
        (r, _safe_change(r)) for r in rows
        if isinstance(r, dict) and _safe_change(r) is not None
    ]
    pos = sum(1 for _, c in with_change if c > 0)
    neg = sum(1 for _, c in with_change if c < 0)
    zero = sum(1 for _, c in with_change if c == 0)
    out["mov_summary"] = {
        "with_change": len(with_change),
        "positive": pos,
        "negative": neg,
        "zero": zero,
    }

    gainers = sorted(with_change, key=lambda t: t[1], reverse=True)[:TOP_MOVERS]
    losers = sorted(with_change, key=lambda t: t[1])[:TOP_MOVERS]
    gainers = [t for t in gainers if t[1] > 0]
    losers = [t for t in losers if t[1] < 0]
    out["top_gainers"] = [
        {
            "rank": r.get("rank"), "ticker": r.get("ticker"),
            "company": r.get("company"), "change": c,
            "ai_score": r.get("ai_score"), "sector": r.get("sector"),
        } for r, c in gainers
    ]
    out["top_losers"] = [
        {
            "rank": r.get("rank"), "ticker": r.get("ticker"),
            "company": r.get("company"), "change": c,
            "ai_score": r.get("ai_score"), "sector": r.get("sector"),
        } for r, c in losers
    ]

    # Sector concentration on the top10
    sectors = [t.get("sector") for t in out["top_10"] if t.get("sector")]
    if sectors:
        counts = Counter(sectors)
        top_sec, top_count = counts.most_common(1)[0]
        pct = top_count / len(sectors)
        out["sector_concentration"] = {
            "top_sector": top_sec,
            "top_count": top_count,
            "of": len(sectors),
            "pct": round(pct, 3),
            "warn": pct >= SECTOR_CONCENTRATION_WARN,
        }
    return out


def analyze_rankings_recap(
    rankings: dict | None,
    prior_snapshot: dict | None = None,
    open_scan: dict | None = None,
) -> dict:
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(rankings, dict):
        section["checks"].append({
            "name": "rankings_present", "status": "WARN",
            "message": "rankings.json missing — no leader board to recap",
        })
        section["status"] = "WARN"
        return section
    prior_tickers = _prior_bucket_tickers(prior_snapshot, "main_top10")
    prior_date = prior_snapshot.get("as_of_date") if isinstance(prior_snapshot, dict) else None
    open_tickers = _open_top_tickers(open_scan)
    section["metrics"].update(
        extract_rankings_movers(
            rankings,
            prior_top_tickers=prior_tickers if prior_tickers else None,
            prior_as_of_date=prior_date,
            open_top_tickers=open_tickers if open_tickers else None,
        )
    )
    rows = rankings.get("rows") or []
    section["checks"].append({
        "name": "top_n",
        "status": "OK" if rows else "WARN",
        "message": f"{min(len(rows), TOP_N)} top names extracted (rows={len(rows)})",
    })
    new_entries = section["metrics"].get("new_top10_entries") or []
    if prior_tickers and new_entries:
        names = ", ".join(e["ticker"] for e in new_entries)
        section["checks"].append({
            "name": "new_top10_entries", "status": "OK",
            "message": f"new top10 vs {prior_date}: {names}",
        })
    elif prior_tickers:
        section["checks"].append({
            "name": "new_top10_entries", "status": "OK",
            "message": f"top10 unchanged vs {prior_date}",
        })
    sc = section["metrics"].get("sector_concentration") or {}
    if sc.get("warn"):
        section["checks"].append({
            "name": "sector_concentration", "status": "WARN",
            "message": (f"top10 dominated by {sc['top_sector']} "
                        f"({sc['top_count']}/{sc['of']} = {sc['pct']:.0%})"),
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]),
        key=lambda s: LEVEL_RANK[s], default="OK")
    return section


def analyze_watchlist(
    watchlist: dict | None,
    prior_snapshot: dict | None = None,
) -> dict:
    """Watchlist top, movers, SUPP coverage, plus new top10 entries vs
    prior trading day when a benchmark snapshot is available.

    Same shape as the morning briefing's analyzer — duplicated rather
    than imported so the two reports remain independently runnable
    even if one of them is removed or refactored.
    """
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(watchlist, dict):
        section["checks"].append({
            "name": "watchlist_present", "status": "WARN",
            "message": "watchlist_rankings.json missing",
        })
        section["status"] = "WARN"
        return section
    rows = watchlist.get("rows") or []
    section["metrics"]["row_count"] = len(rows)

    top = [
        {
            "rank": r.get("rank"), "ticker": r.get("ticker"),
            "company": r.get("company"), "ai_score": r.get("ai_score"),
            "change": _safe_change(r), "sector": r.get("sector"),
            "data_source": r.get("data_source"),
        }
        for r in rows[:TOP_N] if isinstance(r, dict)
    ]
    section["metrics"]["top_10"] = top

    prior_wl = _prior_bucket_tickers(prior_snapshot, "watchlist_top10")
    prior_date = prior_snapshot.get("as_of_date") if isinstance(prior_snapshot, dict) else None
    section["metrics"]["prior_top10_compared_against"] = prior_date
    if prior_wl:
        section["metrics"]["new_top10_entries"] = [
            {"ticker": t["ticker"], "rank": t["rank"], "ai_score": t.get("ai_score")}
            for t in top if t.get("ticker") and t["ticker"] not in prior_wl
        ]
    else:
        section["metrics"]["new_top10_entries"] = []

    with_change = [
        (r, _safe_change(r)) for r in rows
        if isinstance(r, dict) and _safe_change(r) is not None
    ]
    gainers = sorted(with_change, key=lambda t: t[1], reverse=True)[:TOP_MOVERS]
    losers = sorted(with_change, key=lambda t: t[1])[:TOP_MOVERS]
    gainers = [t for t in gainers if t[1] > 0]
    losers = [t for t in losers if t[1] < 0]
    section["metrics"]["top_gainers"] = [
        {"ticker": r.get("ticker"), "company": r.get("company"), "change": c,
         "ai_score": r.get("ai_score")}
        for r, c in gainers
    ]
    section["metrics"]["top_losers"] = [
        {"ticker": r.get("ticker"), "company": r.get("company"), "change": c,
         "ai_score": r.get("ai_score")}
        for r, c in losers
    ]

    sm = watchlist.get("source_meta") or {}
    supp_summary = sm.get("supp_summary") or {}
    section["metrics"]["supp_summary"] = supp_summary
    unavailable_count = sm.get("unavailable_count", 0) or 0
    section["metrics"]["unavailable_count"] = unavailable_count
    unavailable = sm.get("unavailable") or []
    if isinstance(unavailable, list):
        section["metrics"]["unavailable_tickers"] = [
            (u.get("ticker") if isinstance(u, dict) else u) for u in unavailable[:10]
        ]

    supp_total = supp_summary.get("total") or 0
    full = supp_summary.get("full_fundamentals") or 0
    price_only = supp_summary.get("price_only") or 0
    technical_only = supp_summary.get("technical_only") or 0
    if supp_total == 0:
        section["checks"].append({
            "name": "supp_coverage", "status": "WARN",
            "message": "no SUPP rows scored in watchlist",
        })
    else:
        degraded = price_only + technical_only
        degraded_pct = (degraded / supp_total) if supp_total else 0
        section["metrics"]["supp_degraded_pct"] = round(degraded_pct, 3)
        if degraded_pct >= 0.5:
            section["checks"].append({
                "name": "supp_coverage", "status": "WARN",
                "message": (f"SUPP degraded {degraded}/{supp_total} "
                            f"({degraded_pct:.0%}); full={full}"),
            })
        else:
            section["checks"].append({
                "name": "supp_coverage", "status": "OK",
                "message": (f"SUPP {full}/{supp_total} full fundamentals; "
                            f"price_only={price_only} tech_only={technical_only}"),
            })

    if unavailable_count > 0:
        section["checks"].append({
            "name": "watchlist_unavailable",
            "status": "WARN",
            "message": (
                f"{unavailable_count} watchlist ticker(s) unavailable: "
                + ", ".join(section["metrics"].get("unavailable_tickers") or [])
            ),
        })

    yf = sm.get("yfinance_info_cache") or {}
    if yf:
        hits = yf.get("cache_hit_fresh", 0)
        misses = yf.get("cache_miss", 0)
        section["checks"].append({
            "name": "yfinance_cache", "status": "OK",
            "message": f"yfinance cache hits={hits} misses={misses}",
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]),
        key=lambda s: LEVEL_RANK[s], default="OK")
    return section


def analyze_market_risk(mrm: dict | None) -> dict:
    """Roll up market_risk_monitor.json. Alert -> WARN, no alert -> OK."""
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(mrm, dict):
        section["checks"].append({
            "name": "market_risk_present", "status": "OK",
            "message": "market_risk_monitor.json not present (informational)",
        })
        return section
    section["metrics"]["generated_at"] = mrm.get("generated_at")
    indicators = mrm.get("indicators") or {}
    generals = indicators.get("generals_fail") or {}
    below = generals.get("below_count")
    available = generals.get("available_count")
    threshold = generals.get("threshold")
    alert = bool(generals.get("alert"))
    section["metrics"]["generals_fail"] = {
        "below_count": below, "available_count": available,
        "threshold": threshold, "alert": alert,
        "below_tickers": [
            r.get("ticker") for r in generals.get("rows") or []
            if isinstance(r, dict) and r.get("below")
        ],
    }
    if alert:
        below_tickers = section["metrics"]["generals_fail"]["below_tickers"]
        section["checks"].append({
            "name": "generals_fail", "status": "WARN",
            "message": (f"Generals Fail {below}/{available} below 200DMA "
                        f"(threshold {threshold}): "
                        f"{', '.join(below_tickers) or '—'}"),
        })
    elif available is not None:
        section["checks"].append({
            "name": "generals_fail", "status": "OK",
            "message": (f"Generals Fail {below}/{available} below 200DMA "
                        f"(threshold {threshold})"),
        })
    pending = [
        k for k, v in indicators.items()
        if isinstance(v, dict) and (v.get("status") in ("source_needed", "unavailable"))
    ]
    if pending:
        section["metrics"]["pending_indicators"] = pending
        section["checks"].append({
            "name": "indicator_coverage", "status": "OK",
            "message": f"indicators pending source: {', '.join(pending)}",
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]),
        key=lambda s: LEVEL_RANK[s], default="OK")
    return section


def analyze_benchmark(bench: dict | None) -> dict:
    """Forward-tracking presence and (when available) bucket performance."""
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(bench, dict):
        section["checks"].append({
            "name": "benchmark_present", "status": "OK",
            "message": "benchmark_review.json not present (informational)",
        })
        return section
    snaps = bench.get("snapshots_kept") or 0
    summary = bench.get("snapshot_summary") or {}
    horizons = summary.get("horizons") or {}
    completed_any = any(
        (h.get("completed") or 0) > 0 for h in horizons.values()
        if isinstance(h, dict)
    )
    section["metrics"]["snapshots_kept"] = snaps
    section["metrics"]["any_horizon_completed"] = completed_any
    # Top bucket performance, when present
    bucket_perf = bench.get("bucket_performance") or {}
    if isinstance(bucket_perf, dict):
        section["metrics"]["bucket_performance"] = {
            k: v for k, v in bucket_perf.items()
            if isinstance(v, (dict, list, str, int, float))
        }
    findings = bench.get("findings") or []
    findings_warn = sum(1 for f in findings if (f.get("status") or "").upper() == "WARN")
    section["metrics"]["findings_warn"] = findings_warn
    if snaps == 0:
        section["checks"].append({
            "name": "benchmark_snapshots", "status": "OK",
            "message": "no benchmark snapshots present yet",
        })
    elif not completed_any:
        section["checks"].append({
            "name": "benchmark_snapshots", "status": "OK",
            "message": f"{snaps} snapshot(s) kept; horizons pending forward returns",
        })
    else:
        section["checks"].append({
            "name": "benchmark_snapshots", "status": "OK",
            "message": f"{snaps} snapshot(s); ≥1 horizon has forward results",
        })
    if findings_warn:
        section["checks"].append({
            "name": "benchmark_findings", "status": "OK",
            "message": f"{findings_warn} WARN finding(s) in benchmark review (advisory)",
        })
    return section


def analyze_diagnostics(diag: dict | None) -> dict:
    """Roll up ranking_diagnostics: suspicious top ranks, sector
    concentration, and overall verdict.

    Diagnostics WARN/FAIL is a WARN signal at the recap level — the
    recap is a status board, not a stop-the-line alarm; we still want
    the user to see that diagnostics flagged something.
    """
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(diag, dict):
        section["checks"].append({
            "name": "diagnostics_present", "status": "OK",
            "message": "ranking_diagnostics.json not present (informational)",
        })
        return section
    overall = (diag.get("overall") or "OK").upper()
    section["metrics"]["overall"] = overall
    suspicious = diag.get("suspicious_ranks") or []
    section["metrics"]["suspicious_count"] = len(suspicious)
    section["metrics"]["suspicious_top"] = [
        {
            "ticker": s.get("ticker"),
            "group": s.get("group"),
            "rank": s.get("rank"),
            "reasons": s.get("reasons"),
        }
        for s in suspicious[:5] if isinstance(s, dict)
    ]
    sc = diag.get("sector_crowding") or {}
    section["metrics"]["sector_crowding"] = sc
    if overall in ("WARN", "FAIL"):
        section["checks"].append({
            "name": "diagnostics_overall",
            "status": "WARN",  # advisory
            "message": (f"diagnostics overall={overall}; "
                        f"{len(suspicious)} suspicious top rank(s)"),
        })
    else:
        section["checks"].append({
            "name": "diagnostics_overall", "status": "OK",
            "message": "diagnostics overall=OK",
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]),
        key=lambda s: LEVEL_RANK[s], default="OK")
    return section


def analyze_parity(par: dict | None) -> dict:
    """Compact parity rollup. FAIL is treated as advisory WARN at the
    recap level, since parity FAILs are usually by-design SUPP gaps."""
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(par, dict):
        section["checks"].append({
            "name": "parity_present", "status": "OK",
            "message": "scoring_parity_review.json not present (informational)",
        })
        return section
    overall = (par.get("overall") or "OK").upper()
    section["metrics"]["overall"] = overall
    section["metrics"]["low_risk_bias_known"] = bool(par.get("low_risk_bias_known"))
    cgp = par.get("cross_group_parity") or {}
    section["metrics"]["cross_group_parity_status"] = (cgp.get("status") or "OK").upper()
    if overall in ("WARN", "FAIL"):
        section["checks"].append({
            "name": "scoring_parity", "status": "OK",  # advisory
            "message": f"parity overall={overall} (advisory)",
        })
    else:
        section["checks"].append({
            "name": "scoring_parity", "status": "OK",
            "message": "parity overall=OK",
        })
    return section


# ----------------- rollup -----------------


def compute_overall(sections: dict) -> str:
    """Combine section statuses with the documented close-recap rules."""
    fresh = sections["freshness"]["status"]
    if fresh == "FAIL":
        return "FAIL"
    dq = sections["data_quality"]
    if dq.get("metrics", {}).get("critical_section_fail"):
        return "FAIL"
    overall = "OK"
    for sec in sections.values():
        overall = _worst(overall, sec.get("status", "OK"))
    return overall


_OPERATIONAL_SECTIONS = {"freshness", "run_source", "data_quality"}


def collect_action_items(sections: dict) -> list[str]:
    """Split into Operational and Market/Rankings buckets, capped.

    Mirrors the morning briefing's action-item logic so the two reports
    feel consistent — operational lines first, then market signals,
    then a few [INFO] highlights drawn from the close-state metrics.
    """
    operational: list[str] = []
    market: list[str] = []

    for sec_name, sec in sections.items():
        is_op = sec_name in _OPERATIONAL_SECTIONS
        for c in sec.get("checks", []):
            level = (c.get("status") or "").upper()
            if level not in ("WARN", "FAIL"):
                continue
            line = f"[{level}] {sec_name}.{c.get('name')}: {c.get('message')}"
            (operational if is_op else market).append(line)

    rc = sections.get("rankings_recap", {}).get("metrics", {}) or {}
    new_entries = rc.get("new_top10_entries") or []
    if new_entries:
        names = ", ".join(e["ticker"] for e in new_entries[:5])
        prior_dt = rc.get("prior_top10_compared_against") or "prior"
        market.append(f"[INFO] New top10 vs {prior_dt}: {names}")
    exited = rc.get("exited_top10_entries") or []
    if exited:
        prior_dt = rc.get("prior_top10_compared_against") or "prior"
        market.append(
            f"[INFO] Exited top10 since {prior_dt}: {', '.join(exited[:5])}"
        )
    intraday = rc.get("intraday_new_entries") or []
    if intraday:
        names = ", ".join(e["ticker"] for e in intraday[:5])
        market.append(f"[INFO] Intraday top10 entries since open: {names}")

    gainers = rc.get("top_gainers") or []
    losers = rc.get("top_losers") or []
    if gainers:
        top = gainers[0]
        market.append(
            f"[INFO] Top MOV gainer {top.get('ticker')} "
            f"(change={top.get('change')}, rank={top.get('rank')})"
        )
    if losers:
        top = losers[0]
        market.append(
            f"[INFO] Top MOV loser {top.get('ticker')} "
            f"(change={top.get('change')}, rank={top.get('rank')})"
        )

    diag = sections.get("diagnostics", {}).get("metrics", {}) or {}
    susp_top = diag.get("suspicious_top") or []
    if susp_top:
        names = ", ".join(s["ticker"] for s in susp_top[:3] if s.get("ticker"))
        if names:
            market.append(f"[INFO] Diagnostics flagged: {names}")

    def _rank(line: str) -> int:
        if line.startswith("[FAIL]"):
            return 0
        if line.startswith("[WARN]"):
            return 1
        return 2

    operational.sort(key=_rank)
    market.sort(key=_rank)
    return (operational[:3] + market[:5])[:7]


def build_summary(report: dict) -> str:
    """One-liner safe for the tasks table."""
    s = report["sections"]
    fresh = s["freshness"]
    fresh_metrics = fresh.get("metrics", {})
    as_of = fresh_metrics.get("as_of")
    today_live = fresh_metrics.get("today_live")
    close_satisfied = fresh_metrics.get("close_slot_satisfied")
    if fresh["status"] == "FAIL":
        fresh_label = "STALE"
    elif close_satisfied:
        fresh_label = f"Close {_short_time(as_of)}"
    elif today_live:
        fresh_label = f"Live {_short_time(as_of)}"
    else:
        fresh_label = f"Stale {_short_time(as_of)}"

    rs = s["run_source"].get("metrics", {})
    src = rs.get("source", "schedule")
    src_label = {
        "schedule": "scheduled",
        "manual": "manual",
        "recovered": "rescued",
        "schedule_degraded": "schedule degraded",
    }.get(src, src)

    dq_status = s["data_quality"]["metrics"].get("overall", "?")

    rc = s["rankings_recap"]["metrics"]
    top_g = rc.get("top_gainers") or []
    g_label = top_g[0]["ticker"] if top_g else "—"
    top_l = rc.get("top_losers") or []
    l_label = top_l[0]["ticker"] if top_l else "—"
    new_top = rc.get("new_top10_entries") or []
    new_label = (
        f"{len(new_top)} new top10 ({new_top[0]['ticker']}…)"
        if new_top else "Top10 stable"
    )

    mr = s.get("market_risk", {}).get("metrics", {}) or {}
    gen = mr.get("generals_fail") or {}
    if gen.get("alert"):
        below = gen.get("below_count")
        avail = gen.get("available_count")
        risk_label = f"Risk alert {below}/{avail}<200DMA"
    elif gen.get("available_count") is not None:
        risk_label = "Risk OK"
    else:
        risk_label = "Risk —"

    parts = [
        fresh_label,
        f"Source {src_label}",
        f"DQ {dq_status}",
        f"↑{g_label} ↓{l_label}",
        new_label,
        risk_label,
    ]
    return " · ".join(parts)


# ----------------- output -----------------


def _render_html(report: dict) -> str:
    overall = report["overall"]
    color = {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}[overall]
    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Close Recap</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:980px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
h1{{margin:0 0 4px}} .meta{{color:#666;font-size:13px;margin-bottom:18px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;
       font-weight:600;font-size:12px;letter-spacing:.5px;background:{color}}}
.section{{border:1px solid #e3e3e3;border-radius:8px;padding:14px 16px;margin:14px 0}}
.section h2{{margin:0 0 10px;font-size:18px;display:flex;justify-content:space-between;align-items:center}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
.OK{{color:#3c8c3c;font-weight:600}}
.WARN{{color:#b88a00;font-weight:600}}
.FAIL{{color:#c0392b;font-weight:600}}
.kv{{font-size:13px;color:#444}} .kv pre{{background:#f8f8f8;padding:8px;border-radius:4px;overflow:auto}}
.summary{{font-size:14px;background:#f6f8fa;padding:10px 12px;border-radius:6px;margin-top:8px}}
.action li{{margin:4px 0}}
.back{{font-size:13px}}
.movers{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
@media(max-width:720px){{.movers{{grid-template-columns:1fr}}}}
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>Close Recap</h1>
<p class="meta">Generated {escape(report.get("generated_at_chicago",""))} &middot; Overall: <span class="badge">{escape(overall)}</span></p>
<div class="summary"><strong>Recap:</strong> {escape(report.get("summary",""))}</div>
""")

    actions = report.get("action_items") or []
    if actions:
        op_prefixes = tuple(f"[{lvl}] {sec}." for lvl in ("FAIL", "WARN")
                            for sec in _OPERATIONAL_SECTIONS)
        op_lines = [a for a in actions if a.startswith(op_prefixes)]
        mkt_lines = [a for a in actions if a not in op_lines]
        parts.append('<div class="section"><h2>Action Items</h2>')
        if op_lines:
            parts.append('<h3>Operational</h3><ul class="action">')
            for a in op_lines:
                parts.append(f"<li>{escape(a)}</li>")
            parts.append("</ul>")
        if mkt_lines:
            parts.append('<h3>Market &amp; Rankings</h3><ul class="action">')
            for a in mkt_lines:
                parts.append(f"<li>{escape(a)}</li>")
            parts.append("</ul>")
        parts.append("</div>")

    # Compact risk monitor summary
    mr = report["sections"].get("market_risk", {}).get("metrics", {}) or {}
    gen = mr.get("generals_fail") or {}
    if gen.get("available_count") is not None:
        below = gen.get("below_count") or 0
        avail = gen.get("available_count") or 0
        threshold = gen.get("threshold")
        alert_cls = "WARN" if gen.get("alert") else "OK"
        below_tickers = gen.get("below_tickers") or []
        below_str = ", ".join(below_tickers) if below_tickers else "none"
        pending = mr.get("pending_indicators") or []
        pending_str = (f" · pending: {', '.join(pending)}" if pending else "")
        parts.append(
            '<div class="section"><h2>Risk Monitor '
            f'<span class="{alert_cls}">{alert_cls}</span></h2>'
            f'<p class="kv">Generals Fail: <strong>{below}/{avail}</strong>'
            f' below 200DMA (threshold {threshold}); below: {escape(below_str)}'
            f'{escape(pending_str)}.</p></div>'
        )

    # Rankings recap block
    rc = report["sections"].get("rankings_recap", {}).get("metrics", {}) or {}
    parts.append('<div class="section"><h2>Main Rankings <span class="OK">RECAP</span></h2>')
    new_entries = rc.get("new_top10_entries") or []
    exited = rc.get("exited_top10_entries") or []
    intraday = rc.get("intraday_new_entries") or []
    prior_dt = rc.get("prior_top10_compared_against")
    if prior_dt:
        if new_entries:
            names = ", ".join(e["ticker"] for e in new_entries)
            parts.append(
                f"<p class='kv'><strong>New top10 vs {escape(prior_dt)}:</strong> "
                f"{escape(names)}</p>"
            )
        else:
            parts.append(
                f"<p class='kv'><strong>Top10 unchanged vs {escape(prior_dt)}.</strong></p>"
            )
        if exited:
            parts.append(
                f"<p class='kv'><strong>Exited top10 since {escape(prior_dt)}:</strong> "
                f"{escape(', '.join(exited))}</p>"
            )
    if intraday:
        names = ", ".join(e["ticker"] for e in intraday)
        parts.append(
            f"<p class='kv'><strong>Intraday top10 additions:</strong> "
            f"{escape(names)}</p>"
        )
    sc = rc.get("sector_concentration") or {}
    if sc.get("of"):
        cls = "WARN" if sc.get("warn") else "OK"
        parts.append(
            f"<p class='kv'><strong>Sector concentration:</strong> "
            f"<span class='{cls}'>{cls}</span> "
            f"{escape(sc.get('top_sector') or '')} "
            f"{sc.get('top_count')}/{sc.get('of')} "
            f"({sc.get('pct'):.0%})</p>"
        )
    top10 = rc.get("top_10") or []
    if top10:
        parts.append("<h3>Top 10</h3><table><thead><tr>"
                     "<th>#</th><th>Ticker</th><th>Company</th>"
                     "<th>AI</th><th>Δ</th><th>Sector</th></tr></thead><tbody>")
        for r in top10:
            parts.append(
                f"<tr><td>{escape(str(r.get('rank') or ''))}</td>"
                f"<td><strong>{escape(str(r.get('ticker') or ''))}</strong></td>"
                f"<td>{escape(str(r.get('company') or ''))}</td>"
                f"<td>{escape(str(r.get('ai_score') or ''))}</td>"
                f"<td>{escape(str(r.get('change') if r.get('change') is not None else ''))}</td>"
                f"<td>{escape(str(r.get('sector') or ''))}</td></tr>"
            )
        parts.append("</tbody></table>")
    parts.append('<div class="movers">')
    for label, key in (("Top MOV Gainers", "top_gainers"),
                       ("Top MOV Losers", "top_losers")):
        rows = rc.get(key) or []
        parts.append(f"<div><h3>{label}</h3>")
        if rows:
            parts.append("<table><thead><tr><th>#</th><th>Ticker</th>"
                         "<th>Δ</th><th>AI</th></tr></thead><tbody>")
            for r in rows:
                parts.append(
                    f"<tr><td>{escape(str(r.get('rank') or ''))}</td>"
                    f"<td><strong>{escape(str(r.get('ticker') or ''))}</strong></td>"
                    f"<td>{escape(str(r.get('change')))}</td>"
                    f"<td>{escape(str(r.get('ai_score') or ''))}</td></tr>"
                )
            parts.append("</tbody></table>")
        else:
            parts.append("<p class='kv'>No movers in this slot.</p>")
        parts.append("</div>")
    parts.append("</div>")
    if rc.get("limitation"):
        parts.append(f"<p class='kv'><em>{escape(rc['limitation'])}</em></p>")
    parts.append("</div>")

    # Watchlist block
    wl = report["sections"].get("watchlist", {}).get("metrics", {}) or {}
    parts.append('<div class="section"><h2>Watchlist</h2>')
    wl_top = wl.get("top_10") or []
    if wl_top:
        parts.append("<h3>Top 10</h3><table><thead><tr>"
                     "<th>#</th><th>Ticker</th><th>Company</th>"
                     "<th>AI</th><th>Δ</th><th>Source</th></tr></thead><tbody>")
        for r in wl_top:
            parts.append(
                f"<tr><td>{escape(str(r.get('rank') or ''))}</td>"
                f"<td><strong>{escape(str(r.get('ticker') or ''))}</strong></td>"
                f"<td>{escape(str(r.get('company') or ''))}</td>"
                f"<td>{escape(str(r.get('ai_score') or ''))}</td>"
                f"<td>{escape(str(r.get('change') if r.get('change') is not None else ''))}</td>"
                f"<td>{escape(str(r.get('data_source') or ''))}</td></tr>"
            )
        parts.append("</tbody></table>")
    parts.append('<div class="movers">')
    for label, key in (("Watchlist gainers", "top_gainers"),
                       ("Watchlist laggards", "top_losers")):
        rows = wl.get(key) or []
        parts.append(f"<div><h3>{label}</h3>")
        if rows:
            parts.append("<table><thead><tr><th>Ticker</th>"
                         "<th>AI</th><th>Δ</th></tr></thead><tbody>")
            for r in rows:
                parts.append(
                    f"<tr><td><strong>{escape(str(r.get('ticker') or ''))}"
                    f"</strong></td>"
                    f"<td>{escape(str(r.get('ai_score') or ''))}</td>"
                    f"<td>{escape(str(r.get('change') if r.get('change') is not None else ''))}</td></tr>"
                )
            parts.append("</tbody></table>")
        else:
            parts.append("<p class='kv'>—</p>")
        parts.append("</div>")
    parts.append("</div></div>")

    # Diagnostics block (optional)
    diag = report["sections"].get("diagnostics", {}).get("metrics", {}) or {}
    susp_top = diag.get("suspicious_top") or []
    if susp_top:
        parts.append('<div class="section"><h2>Suspicious Top Ranks</h2>'
                     '<table><thead><tr><th>Group</th><th>#</th><th>Ticker</th>'
                     '<th>Reasons</th></tr></thead><tbody>')
        for s in susp_top:
            reasons = s.get("reasons") or []
            parts.append(
                f"<tr><td>{escape(str(s.get('group') or ''))}</td>"
                f"<td>{escape(str(s.get('rank') or ''))}</td>"
                f"<td><strong>{escape(str(s.get('ticker') or ''))}</strong></td>"
                f"<td>{escape('; '.join(reasons) if isinstance(reasons, list) else str(reasons))}</td></tr>"
            )
        parts.append("</tbody></table></div>")

    # Render every section's checks/metrics for transparency.
    for key, sec in report["sections"].items():
        st = (sec.get("status") or "OK").upper()
        parts.append(
            f'<div class="section"><h2>{escape(key.replace("_", " ").title())}'
            f'<span class="{escape(st)}">{escape(st)}</span></h2>'
        )
        if sec.get("metrics"):
            parts.append(
                '<div class="kv"><strong>Metrics:</strong><pre>'
                + escape(json.dumps(sec["metrics"], indent=2, default=str))
                + "</pre></div>"
            )
        parts.append(
            '<table><thead><tr><th style="width:32%">Check</th>'
            '<th style="width:10%">Status</th><th>Detail</th></tr></thead><tbody>'
        )
        for c in sec.get("checks", []):
            cs = (c.get("status") or "").upper()
            parts.append(
                f'<tr><td>{escape(c.get("name",""))}</td>'
                f'<td class="{escape(cs)}">{escape(cs)}</td>'
                f'<td>{escape(c.get("message",""))}</td></tr>'
            )
        parts.append("</tbody></table></div>")

    parts.append("</body></html>")
    return "\n".join(parts)


# ----------------- core -----------------


def build_report() -> dict:
    rankings = _load_json(RANKINGS_FILE)
    watchlist = _load_json(WATCHLIST_FILE)
    dq = _load_json(DATA_QUALITY_FILE)
    sr_rep = _load_json(SCHEDULE_RELIABILITY_FILE)
    mrm = _load_json(MARKET_RISK_FILE)
    bench = _load_json(BENCHMARK_FILE)
    par = _load_json(PARITY_FILE)
    diag = _load_json(DIAGNOSTICS_FILE)
    open_scan = _load_json(MARKET_OPEN_SCAN_FILE)

    today_chi = _to_chicago(_now_utc()).date().strftime("%Y-%m-%d")
    prior_snapshot = _load_prior_snapshot(BENCHMARK_SNAPSHOTS_FILE, today_chi)

    sections = {
        "freshness": analyze_freshness(rankings),
        "run_source": analyze_run_source(sr_rep),
        "data_quality": analyze_data_quality(dq),
        "rankings_recap": analyze_rankings_recap(rankings, prior_snapshot, open_scan),
        "watchlist": analyze_watchlist(watchlist, prior_snapshot),
        "market_risk": analyze_market_risk(mrm),
        "diagnostics": analyze_diagnostics(diag),
        "benchmark": analyze_benchmark(bench),
        "parity": analyze_parity(par),
    }
    overall = compute_overall(sections)
    now_utc = _now_utc()
    chi_now = _to_chicago(now_utc)
    chi_label = "CDT" if chi_now.utcoffset().total_seconds() == -5 * 3600 else "CST"
    chi_str = chi_now.strftime("%Y-%m-%d %I:%M %p ") + chi_label

    report = {
        "generated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_chicago": chi_str,
        "overall": overall,
        "sections": sections,
        "inputs": {
            "rankings_present": rankings is not None,
            "watchlist_present": watchlist is not None,
            "data_quality_present": dq is not None,
            "schedule_reliability_present": sr_rep is not None,
            "market_risk_present": mrm is not None,
            "benchmark_present": bench is not None,
            "parity_present": par is not None,
            "diagnostics_present": diag is not None,
            "market_open_scan_present": open_scan is not None,
            "prior_snapshot_present": prior_snapshot is not None,
        },
    }
    report["action_items"] = collect_action_items(sections)
    report["summary"] = build_summary(report)
    return report


def _stamp_task(report: dict) -> None:
    """Update tasks.json row id=close-recap in place."""
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
    overall = report["overall"]
    status = ("OK" if overall == "OK" else
              ("warn" if overall == "WARN" else "fail"))
    last_run = report.get("generated_at_chicago") or "—"
    summary = report.get("summary") or "Close recap"
    changed = False
    for row in tasks:
        if isinstance(row, dict) and row.get("id") == "close-recap":
            row["last_run"] = last_run
            row["status"] = status
            row["summary"] = summary
            row["report_url"] = REPORT_URL
            # The recap re-runs every proceeded slot but is most useful
            # at/after the close refresh. No single "next slot" worth
            # advertising.
            row["next_run"] = "—"
            row["schedule"] = "Weekdays 3:35 PM CT / close refresh"
            changed = True
            break
    if changed:
        TASKS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report()
    JSON_OUTPUT.write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")
    _stamp_task(report)
    print(f"[close_recap] overall={report['overall']} -> {JSON_OUTPUT}")
    print(f"[close_recap] summary: {report['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
