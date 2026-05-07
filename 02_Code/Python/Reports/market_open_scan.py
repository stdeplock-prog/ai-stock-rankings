"""Market Open Scan — morning briefing that runs on every proceeded slot
and answers four questions for the user:

  1. Is today's morning data live, and how did we get here (schedule
     primary / rescued / manual)?
  2. What changed at the top of the rankings (top 10, MOV gainers/losers)?
  3. What does the watchlist look like (top names, top SUPP, movers,
     SUPP coverage)?
  4. What context should the user keep in mind (market risk, benchmark
     forward tracking)?

Inputs (read-only):
  - data/rankings.json
  - data/watchlist_rankings.json
  - data/reports/data_quality_audit.json
  - data/reports/schedule_reliability.json
  - data/reports/market_risk_monitor.json (optional)
  - data/reports/benchmark_review.json (optional)

Outputs:
  - data/reports/market_open_scan.json
  - reports/market-open-scan.html
  - data/tasks.json row id=market-open-scan stamped on each run.

Status logic:
  * FAIL when:
      - rankings.json missing, unparseable, or stale (>24h on weekday)
      - today's data is not live on a weekday
      - data quality audit overall=FAIL on rankings/tasks sections
  * WARN when:
      - schedule reliability rescued (FAIL but recovered/manual) or WARN
      - market risk monitor alert is active
      - data quality WARN
  * OK otherwise.

The report never raises out of build_report; missing optional inputs
become advisory findings rather than exceptions so the briefing always
lands.
"""

from __future__ import annotations

import json
import re
import sys
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
BENCHMARK_SNAPSHOTS_FILE = DATA_REPORTS_DIR / "benchmark_snapshots.jsonl"

JSON_OUTPUT = DATA_REPORTS_DIR / "market_open_scan.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "market-open-scan.html"
TASKS_FILE = DATA_DIR / "tasks.json"
REPORT_URL = "./reports/market-open-scan.html"

LEVEL_RANK = {"OK": 0, "WARN": 1, "FAIL": 2}

# Weekday freshness thresholds (hours).
FRESH_WARN_HOURS_WEEKDAY = 6.0
FRESH_FAIL_HOURS_WEEKDAY = 24.0
FRESH_WARN_HOURS_WEEKEND = 72.0
FRESH_FAIL_HOURS_WEEKEND = 168.0

TOP_N = 10
TOP_MOVERS = 5


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
    """Read the most recent benchmark snapshot strictly before today.

    Used to detect *new top10 entries* relative to the prior trading day.
    Returns the snapshot dict (with `buckets`) or None if unavailable.
    JSONL is small (one line per day), so a single linear scan is fine.
    """
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
    """Coerce a rankings row's `change` field to int, treating non-numeric
    or missing values as None so they sort to the bottom."""
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
    """Did today's morning data land? Same logic as midday_health_check
    but framed as a morning briefing."""
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
    section["checks"].append({
        "name": "today_live",
        "status": "OK" if (today_live or is_weekend) else "FAIL",
        "message": f"data {'is' if today_live else 'is not'} from today (Chicago)",
    })
    section["status"] = max(
        (c["status"] for c in section["checks"]), key=lambda s: LEVEL_RANK[s])
    return section


def analyze_run_source(sr_rep: dict | None) -> dict:
    """How did today's run get here? Pulled from schedule_reliability.json.

    We surface the source label (schedule/manual/rescued) so the morning
    briefing makes the rescue path visible, even when the dashboard's
    other reports stay green.
    """
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

    # Classify the source. The schedule_reliability "effective" rollup
    # already encodes the rescue logic — we just translate it into a
    # one-line label the morning briefing can use. "recovered" takes
    # priority over "manual" when the recent history actually had missing
    # slots: a workflow_dispatch that filled an unmet morning slot is
    # more usefully described as a rescue than as a generic manual run.
    missing_count = cal.get("missing_count", 0) or 0
    if raw == "FAIL" and eff != "FAIL" and today_satisfied and missing_count > 0:
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

    if source == "manual":
        msg = f"latest run: manual workflow_dispatch ({last_ts or '—'})"
        status = "WARN"  # manual is a notable signal even if data is fine
    elif source == "recovered":
        msg = (f"latest run: rescued morning slot ({last_ts or '—'}), "
               f"history shows {cal.get('missing_count', 0)} missing slot(s)")
        status = "WARN"
    elif source == "schedule_degraded":
        msg = (f"schedule degraded: today_missing={today_missing}, "
               f"history_missing={cal.get('missing_count', 0)}")
        status = eff if eff in LEVEL_RANK else "WARN"
    else:
        msg = f"latest run: scheduled ({last_ts or '—'})"
        status = "OK"
    section["checks"].append({"name": "run_source", "status": status, "message": msg})
    section["status"] = status
    return section


def analyze_data_quality(dq: dict | None) -> dict:
    """Roll up data_quality_audit.json for the morning briefing."""
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


def extract_rankings_movers(
    rankings: dict | None,
    prior_top_tickers: set[str] | None = None,
    prior_as_of_date: str | None = None,
) -> dict:
    """Pull top 10, top MOV gainers/losers, and (when a prior snapshot is
    available) the set of *new* top10 entries vs the prior trading day.

    `change` is the daily MOV (positions moved). When a prior snapshot is
    not available we still surface MOV gainers/losers but note that the
    "new entries" list is empty rather than fabricating rank-delta data.
    Records with non-numeric or null `change` are kept out of the
    gainer/loser lists.
    """
    out: dict = {
        "top_10": [],
        "top_gainers": [],
        "top_losers": [],
        "new_top10_entries": [],
        "prior_top10_compared_against": prior_as_of_date,
        "mov_summary": {},
        "limitation": (
            "MOV-based gainers/losers; new-entry detection uses prior "
            "benchmark snapshot when available."
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
    # Filter out 0-change rows from gainer/loser lists (they're not movers).
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
    return out


def _prior_bucket_tickers(snapshot: dict | None, bucket_key: str) -> set[str]:
    """Pull the set of tickers from `bucket_key` in a benchmark snapshot,
    tolerating missing/empty fields. Used to compute new top10 entries.
    """
    if not isinstance(snapshot, dict):
        return set()
    bucket = ((snapshot.get("buckets") or {}).get(bucket_key) or {})
    members = bucket.get("members") or []
    out: set[str] = set()
    for m in members:
        if isinstance(m, dict) and m.get("ticker"):
            out.add(m["ticker"])
    return out


def analyze_rankings_changes(
    rankings: dict | None,
    prior_snapshot: dict | None = None,
) -> dict:
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(rankings, dict):
        section["checks"].append({
            "name": "rankings_present", "status": "WARN",
            "message": "rankings.json missing — no leader board to scan",
        })
        section["status"] = "WARN"
        return section
    prior_tickers = _prior_bucket_tickers(prior_snapshot, "main_top10")
    prior_date = prior_snapshot.get("as_of_date") if isinstance(prior_snapshot, dict) else None
    section["metrics"].update(
        extract_rankings_movers(
            rankings,
            prior_top_tickers=prior_tickers if prior_tickers else None,
            prior_as_of_date=prior_date,
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
    section["status"] = max(
        (c["status"] for c in section["checks"]),
        key=lambda s: LEVEL_RANK[s], default="OK")
    return section


def analyze_watchlist(
    watchlist: dict | None,
    prior_snapshot: dict | None = None,
) -> dict:
    """Top watchlist names, top SUPP, watchlist movers, SUPP coverage,
    plus new top10 entries vs prior trading day when a benchmark snapshot
    is available."""
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

    supp_rows = [
        r for r in rows
        if isinstance(r, dict) and r.get("data_source") == "supplemental_yfinance"
    ]
    section["metrics"]["supp_top"] = [
        {
            "ticker": r.get("ticker"), "company": r.get("company"),
            "ai_score": r.get("ai_score"), "change": _safe_change(r),
            "sector": r.get("sector"),
        }
        for r in supp_rows[:TOP_MOVERS]
    ]

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
    section["metrics"]["unavailable_count"] = sm.get("unavailable_count", 0)

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
    """Roll up market_risk_monitor.json. Alert -> WARN."""
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
    else:
        section["checks"].append({
            "name": "generals_fail", "status": "OK",
            "message": (f"Generals Fail {below}/{available} below 200DMA "
                        f"(threshold {threshold})"),
        })
    # Surface source-needed indicators concisely so the briefing reflects
    # known gaps without flagging them as alerts.
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
    """Forward-tracking presence; advisory only."""
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


# ----------------- rollup -----------------


def compute_overall(sections: dict) -> str:
    """Combine section statuses with the documented morning-scan rules."""
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


def collect_action_items(sections: dict, overall: str) -> list[str]:
    """Build a focused list of items the user should review this morning.

    Output is split into two buckets:
      * operational  — schedule source / recovery / data-quality issues
      * market       — ranking, watchlist and risk-monitor signals

    Each bucket caps so a single noisy area can't crowd out the other,
    and the final list is bounded to keep the briefing scannable.
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

    rc = sections.get("rankings_changes", {}).get("metrics", {})
    new_entries = rc.get("new_top10_entries") or []
    if new_entries:
        names = ", ".join(e["ticker"] for e in new_entries[:5])
        prior_dt = rc.get("prior_top10_compared_against") or "prior"
        market.append(
            f"[INFO] New top10 entries vs {prior_dt}: {names}"
        )

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

    wl = sections.get("watchlist", {}).get("metrics", {})
    wl_new = wl.get("new_top10_entries") or []
    if wl_new:
        names = ", ".join(e["ticker"] for e in wl_new[:5])
        market.append(f"[INFO] New watchlist top10: {names}")
    wl_top = wl.get("top_10") or []
    if wl_top:
        sectors = [r.get("sector") for r in wl_top if r.get("sector")]
        if sectors:
            from collections import Counter
            top_sector, top_count = Counter(sectors).most_common(1)[0]
            if top_count >= max(3, len(sectors) // 2):
                market.append(
                    f"[INFO] Watchlist concentration: "
                    f"{top_count}/{len(sectors)} top names in {top_sector}"
                )

    def _rank(line: str) -> int:
        if line.startswith("[FAIL]"):
            return 0
        if line.startswith("[WARN]"):
            return 1
        return 2

    operational.sort(key=_rank)
    market.sort(key=_rank)
    # Cap each bucket so a noisy operational state can't drown out market
    # signals (and vice versa); cap the total at 7 lines for scannability.
    return (operational[:3] + market[:5])[:7]


def collect_strengths_risks(sections: dict) -> dict:
    """Top 3 strengths and risks to review for the morning. Strengths are
    the highest-conviction names (top AI score in main top10). Risks come
    from MOV losers, market_risk generals_fail tickers, and SUPP-degraded
    counts when high.
    """
    strengths: list[str] = []
    risks: list[str] = []

    rc = sections.get("rankings_changes", {}).get("metrics", {}) or {}
    top10 = rc.get("top_10") or []
    for r in top10:
        if len(strengths) >= 3:
            break
        ai = r.get("ai_score")
        if ai is None:
            continue
        strengths.append(
            f"{r.get('ticker')} (AI {ai}, rank {r.get('rank')}, "
            f"{r.get('sector') or '—'})"
        )

    losers = rc.get("top_losers") or []
    for r in losers[:3]:
        risks.append(
            f"{r.get('ticker')} MOV {r.get('change')} "
            f"(rank {r.get('rank')}, {r.get('sector') or '—'})"
        )

    mr = sections.get("market_risk", {}).get("metrics", {}) or {}
    gen = mr.get("generals_fail") or {}
    if gen.get("alert"):
        below = gen.get("below_tickers") or []
        if below and len(risks) < 3:
            risks.append(
                f"Generals Fail alert: {', '.join(below[:5])} below 200DMA"
            )

    wl = sections.get("watchlist", {}).get("metrics", {}) or {}
    supp = wl.get("supp_summary") or {}
    total = supp.get("total") or 0
    if total:
        degraded = (supp.get("price_only") or 0) + (supp.get("technical_only") or 0)
        if degraded / total >= 0.5 and len(risks) < 3:
            risks.append(
                f"SUPP coverage degraded: {degraded}/{total} thin rows"
            )

    return {"strengths": strengths[:3], "risks": risks[:3]}


def build_summary(report: dict) -> str:
    s = report["sections"]
    fresh = s["freshness"]
    fresh_metrics = fresh.get("metrics", {})
    as_of = fresh_metrics.get("as_of")
    today_live = fresh_metrics.get("today_live")
    if fresh["status"] == "FAIL":
        fresh_label = "STALE"
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

    rc = s["rankings_changes"]["metrics"]
    top_g = rc.get("top_gainers") or []
    g_label = top_g[0]["ticker"] if top_g else "—"
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
        f"Top gainer {g_label}",
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
<html><head><meta charset="utf-8"><title>Market Open Scan</title>
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
<h1>Market Open Scan</h1>
<p class="meta">Generated {escape(report.get("generated_at_chicago",""))} &middot; Overall: <span class="badge">{escape(overall)}</span></p>
<div class="summary"><strong>Briefing:</strong> {escape(report.get("summary",""))}</div>
""")

    actions = report.get("action_items") or []
    if actions:
        # Split visually into operational vs market lines so the user can
        # tell schedule/recovery noise from rankings/risk action items at
        # a glance.
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

    sr = report.get("strengths_risks") or {}
    strengths = sr.get("strengths") or []
    risks = sr.get("risks") or []
    if strengths or risks:
        parts.append(
            '<div class="section"><h2>Top Strengths &amp; Risks</h2>'
            '<div class="movers">'
        )
        parts.append('<div><h3>Top Strengths</h3>')
        if strengths:
            parts.append('<ul class="action">')
            for s_line in strengths:
                parts.append(f"<li>{escape(s_line)}</li>")
            parts.append("</ul>")
        else:
            parts.append("<p class='kv'>—</p>")
        parts.append('</div><div><h3>Top Risks</h3>')
        if risks:
            parts.append('<ul class="action">')
            for r_line in risks:
                parts.append(f"<li>{escape(r_line)}</li>")
            parts.append("</ul>")
        else:
            parts.append("<p class='kv'>—</p>")
        parts.append("</div></div></div>")

    # Compact risk monitor summary so the user doesn't have to scroll the
    # raw metrics block to know whether any market-wide alarms are active.
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

    # Rankings changes block: top 10 + gainers/losers tables.
    rc = report["sections"].get("rankings_changes", {}).get("metrics", {}) or {}
    parts.append('<div class="section"><h2>Main Rankings <span class="OK">SCAN</span></h2>')
    new_entries = rc.get("new_top10_entries") or []
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
    for label, key in (("Top SUPP names", "supp_top"),
                       ("Watchlist movers", "top_gainers")):
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

    today_chi = _to_chicago(_now_utc()).date().strftime("%Y-%m-%d")
    prior_snapshot = _load_prior_snapshot(BENCHMARK_SNAPSHOTS_FILE, today_chi)

    sections = {
        "freshness": analyze_freshness(rankings),
        "run_source": analyze_run_source(sr_rep),
        "data_quality": analyze_data_quality(dq),
        "rankings_changes": analyze_rankings_changes(rankings, prior_snapshot),
        "watchlist": analyze_watchlist(watchlist, prior_snapshot),
        "market_risk": analyze_market_risk(mrm),
        "benchmark": analyze_benchmark(bench),
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
        },
    }
    report["action_items"] = collect_action_items(sections, overall)
    report["strengths_risks"] = collect_strengths_risks(sections)
    report["summary"] = build_summary(report)
    return report


def _stamp_task(report: dict) -> None:
    """Update tasks.json row id=market-open-scan in place."""
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
    summary = report.get("summary") or "Market open scan"
    changed = False
    for row in tasks:
        if isinstance(row, dict) and row.get("id") == "market-open-scan":
            row["last_run"] = last_run
            row["status"] = status
            row["summary"] = summary
            row["report_url"] = REPORT_URL
            # Runs on every proceeded slot like the midday rollup, so a
            # specific "next slot" doesn't add information here.
            row["next_run"] = "—"
            row["schedule"] = "Weekdays 8:45 AM CT / rescue"
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
    print(f"[market_open_scan] overall={report['overall']} -> {JSON_OUTPUT}")
    print(f"[market_open_scan] summary: {report['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
