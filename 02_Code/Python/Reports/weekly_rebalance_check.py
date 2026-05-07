"""Weekly Rebalance Check — decision-support rollup that helps the user
review adds/removes, conviction changes, sector concentration, benchmark
performance, and watchlist candidates.

Companion to Close Recap. Re-generated on every proceeded slot so the
dashboard always has a fresh weekly view, but the task row advertises a
weekly cadence (Friday close) since that is when the report is most
actionable. Read-only — does NOT touch scoring formulas.

Inputs:
  - data/rankings.json
  - data/watchlist_rankings.json
  - data/reports/benchmark_review.json (optional)
  - data/reports/benchmark_snapshots.jsonl (optional, weekly history)
  - data/reports/ranking_diagnostics.json (optional)
  - data/reports/scoring_parity_review.json (optional)
  - data/reports/data_quality_audit.json (optional)
  - data/reports/schedule_reliability.json (optional)
  - data/reports/low_risk_drift_review.json (optional)

Outputs:
  - data/reports/weekly_rebalance_check.json
  - reports/weekly-rebalance-check.html
  - data/tasks.json row id=weekly-rebalance stamped on each run.

Status logic:
  * FAIL when:
      - rankings.json missing/unparseable/stale
      - data quality audit critical section (rankings/tasks) FAIL
  * WARN when:
      - benchmark history < ~5 trading days (limited compare)
      - schedule reliability rescued/degraded
      - sector crowding >=40% in main or watchlist top25
      - diagnostics flags suspicious top ranks
      - benchmark bucket lagging (avg_mean_return negative on completed
        horizons)
  * OK otherwise.
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
BENCHMARK_FILE = DATA_REPORTS_DIR / "benchmark_review.json"
BENCHMARK_SNAPSHOTS_FILE = DATA_REPORTS_DIR / "benchmark_snapshots.jsonl"
DIAGNOSTICS_FILE = DATA_REPORTS_DIR / "ranking_diagnostics.json"
PARITY_FILE = DATA_REPORTS_DIR / "scoring_parity_review.json"
DATA_QUALITY_FILE = DATA_REPORTS_DIR / "data_quality_audit.json"
SCHEDULE_RELIABILITY_FILE = DATA_REPORTS_DIR / "schedule_reliability.json"
LOW_RISK_DRIFT_FILE = DATA_REPORTS_DIR / "low_risk_drift_review.json"

JSON_OUTPUT = DATA_REPORTS_DIR / "weekly_rebalance_check.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "weekly-rebalance-check.html"
TASKS_FILE = DATA_DIR / "tasks.json"
REPORT_URL = "./reports/weekly-rebalance-check.html"
TASK_ID_PRIMARY = "weekly-rebalance"
TASK_ID_FALLBACK = "weekly-rebalance-check"

LEVEL_RANK = {"OK": 0, "WARN": 1, "FAIL": 2}

FRESH_WARN_HOURS_WEEKDAY = 6.0
FRESH_FAIL_HOURS_WEEKDAY = 24.0
FRESH_WARN_HOURS_WEEKEND = 72.0
FRESH_FAIL_HOURS_WEEKEND = 168.0

TOP_N = 25
SECTOR_CONCENTRATION_WARN = 0.40
HISTORY_WARN_DAYS = 5  # need ~5 trading days for a weekly compare
WEEKLY_LOOKBACK_DAYS = 7
TOP_CONVICTION_DELTAS = 5


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


def _worst(a: str, b: str) -> str:
    return a if LEVEL_RANK[a] >= LEVEL_RANK[b] else b


def _load_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_snapshots(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
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
                if isinstance(rec, dict) and rec.get("as_of_date"):
                    out.append(rec)
    except OSError:
        return []
    out.sort(key=lambda r: r.get("as_of_date") or "")
    return out


def _bucket_members(snapshot: dict | None, bucket_key: str) -> list[dict]:
    if not isinstance(snapshot, dict):
        return []
    bucket = ((snapshot.get("buckets") or {}).get(bucket_key) or {})
    members = bucket.get("members") or []
    return [m for m in members if isinstance(m, dict) and m.get("ticker")]


def _ticker_set(members: list[dict]) -> set[str]:
    return {m["ticker"] for m in members if m.get("ticker")}


def _ticker_score_map(members: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for m in members:
        t = m.get("ticker")
        s = m.get("ai_score")
        if t and isinstance(s, (int, float)):
            out[t] = float(s)
    return out


# ----------------- analyzers -----------------


def analyze_freshness(rankings: dict | None, watchlist: dict | None) -> dict:
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(rankings, dict):
        section["checks"].append({
            "name": "rankings_present", "status": "FAIL",
            "message": "rankings.json missing or unparseable",
        })
        section["status"] = "FAIL"
        return section
    as_of = rankings.get("as_of")
    section["metrics"]["rankings_as_of"] = as_of
    section["metrics"]["watchlist_as_of"] = (
        watchlist.get("as_of") if isinstance(watchlist, dict) else None
    )
    section["metrics"]["open_date"] = rankings.get("open_date")
    section["metrics"]["row_count"] = len(rankings.get("rows") or [])
    now_utc = _now_utc()
    chi_now = _to_chicago(now_utc)
    is_weekend = chi_now.weekday() >= 5
    section["metrics"]["is_weekend"] = is_weekend
    as_of_dt = _parse_as_of(as_of)
    if as_of_dt is None:
        section["checks"].append({
            "name": "rankings_freshness", "status": "FAIL",
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
        "name": "rankings_freshness", "status": level,
        "message": f"as_of {as_of} (age {age_h:.1f}h, weekend={is_weekend})",
    })
    section["status"] = level
    return section


def analyze_data_quality(dq: dict | None) -> dict:
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(dq, dict):
        section["checks"].append({
            "name": "data_quality_present", "status": "OK",
            "message": "data_quality_audit.json not present (informational)",
        })
        return section
    overall = (dq.get("overall") or "OK").upper()
    section["metrics"]["overall"] = overall
    sections = dq.get("sections") or {}
    rankings_status = "OK"
    tasks_status = "OK"
    for key, sec in sections.items():
        if not isinstance(sec, dict):
            continue
        sec_status = "OK"
        for c in sec.get("checks", []):
            sec_status = _worst(sec_status, (c.get("status") or "OK").upper())
        if key == "rankings":
            rankings_status = sec_status
        elif key == "tasks":
            tasks_status = sec_status
    critical_fail = rankings_status == "FAIL" or tasks_status == "FAIL"
    section["metrics"]["critical_section_fail"] = critical_fail
    section["checks"].append({
        "name": "data_quality_overall", "status": overall,
        "message": f"overall={overall}; rankings={rankings_status}, tasks={tasks_status}",
    })
    if critical_fail:
        section["checks"].append({
            "name": "critical_sections", "status": "FAIL",
            "message": "rankings/tasks data quality FAIL",
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]), key=lambda s: LEVEL_RANK[s])
    return section


def analyze_schedule(sr_rep: dict | None) -> dict:
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(sr_rep, dict):
        section["checks"].append({
            "name": "schedule_reliability_present", "status": "OK",
            "message": "schedule_reliability.json not present (informational)",
        })
        return section
    raw = (sr_rep.get("overall") or "OK").upper()
    eff = (sr_rep.get("overall_effective") or raw).upper()
    section["metrics"]["overall_raw"] = raw
    section["metrics"]["overall_effective"] = eff
    if eff in ("WARN", "FAIL"):
        section["checks"].append({
            "name": "schedule_effective", "status": "WARN",
            "message": f"schedule effective={eff} (raw={raw})",
        })
    elif raw != "OK":
        section["checks"].append({
            "name": "schedule_recovered", "status": "WARN",
            "message": f"schedule raw={raw} but recovered (effective={eff})",
        })
    else:
        section["checks"].append({
            "name": "schedule_ok", "status": "OK",
            "message": "schedule effective=OK",
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]), key=lambda s: LEVEL_RANK[s])
    return section


def _pick_top(rows: list[dict], n: int = TOP_N) -> list[dict]:
    return [
        {
            "rank": r.get("rank"),
            "ticker": r.get("ticker"),
            "company": r.get("company"),
            "ai_score": r.get("ai_score"),
            "sector": r.get("sector"),
            "change": r.get("change"),
            "fundamental": r.get("fundamental"),
            "technical": r.get("technical"),
            "sentiment": r.get("sentiment"),
            "low_risk": r.get("low_risk"),
            "data_source": r.get("data_source"),
        }
        for r in rows[:n] if isinstance(r, dict)
    ]


def _sector_concentration(top: list[dict]) -> dict:
    sectors = [t.get("sector") for t in top if t.get("sector")]
    if not sectors:
        return {}
    counts = Counter(sectors)
    top_sec, top_count = counts.most_common(1)[0]
    pct = top_count / len(sectors)
    return {
        "top_sector": top_sec,
        "top_count": top_count,
        "of": len(sectors),
        "pct": round(pct, 3),
        "warn": pct >= SECTOR_CONCENTRATION_WARN,
        "by_sector": dict(counts.most_common()),
    }


def analyze_leaderboards(rankings: dict | None, watchlist: dict | None) -> dict:
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if isinstance(rankings, dict):
        rk_top = _pick_top(rankings.get("rows") or [], TOP_N)
        section["metrics"]["main_top25"] = rk_top
        section["metrics"]["main_sector_concentration"] = _sector_concentration(rk_top)
    else:
        section["metrics"]["main_top25"] = []
        section["metrics"]["main_sector_concentration"] = {}
    if isinstance(watchlist, dict):
        wl_rows = watchlist.get("rows") or []
        wl_top = _pick_top(wl_rows, TOP_N)
        section["metrics"]["watchlist_top25"] = wl_top
        section["metrics"]["watchlist_sector_concentration"] = _sector_concentration(wl_top)
        # SUPP top25 — supplemental_yfinance subset re-ranked by AI score
        supp_rows = [
            r for r in wl_rows
            if isinstance(r, dict) and r.get("data_source") == "supplemental_yfinance"
        ]
        supp_rows_sorted = sorted(
            supp_rows,
            key=lambda r: r.get("ai_score") if isinstance(r.get("ai_score"), (int, float)) else -1,
            reverse=True,
        )
        section["metrics"]["supp_top25"] = _pick_top(supp_rows_sorted, TOP_N)
    else:
        section["metrics"]["watchlist_top25"] = []
        section["metrics"]["watchlist_sector_concentration"] = {}
        section["metrics"]["supp_top25"] = []

    main_sc = section["metrics"]["main_sector_concentration"]
    if main_sc.get("warn"):
        section["checks"].append({
            "name": "main_sector_concentration", "status": "WARN",
            "message": (f"main_top25 dominated by {main_sc['top_sector']} "
                        f"{main_sc['top_count']}/{main_sc['of']} "
                        f"({main_sc['pct']:.0%})"),
        })
    wl_sc = section["metrics"]["watchlist_sector_concentration"]
    if wl_sc.get("warn"):
        section["checks"].append({
            "name": "watchlist_sector_concentration", "status": "WARN",
            "message": (f"watchlist_top25 dominated by {wl_sc['top_sector']} "
                        f"{wl_sc['top_count']}/{wl_sc['of']} "
                        f"({wl_sc['pct']:.0%})"),
        })
    if not section["checks"]:
        section["checks"].append({
            "name": "leaderboards", "status": "OK",
            "message": "leaderboards loaded; no sector crowding warnings",
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]), key=lambda s: LEVEL_RANK[s])
    return section


def analyze_change_review(
    rankings: dict | None,
    watchlist: dict | None,
    snapshots: list[dict],
) -> dict:
    """Compare current top buckets to the earliest snapshot in the last
    ~5-7 trading days, when enough history exists. When insufficient
    history, surface a limitation rather than failing.
    """
    section = {"checks": [], "metrics": {}, "status": "OK"}
    today_chi = _to_chicago(_now_utc()).date()
    cutoff = today_chi - timedelta(days=WEEKLY_LOOKBACK_DAYS)
    in_window = [
        s for s in snapshots
        if (d := s.get("as_of_date")) and d >= cutoff.strftime("%Y-%m-%d")
        and d < today_chi.strftime("%Y-%m-%d")
    ]
    section["metrics"]["snapshots_total"] = len(snapshots)
    section["metrics"]["snapshots_in_window"] = len(in_window)
    section["metrics"]["history_warn_threshold_days"] = HISTORY_WARN_DAYS
    if not in_window:
        section["metrics"]["limitation"] = (
            "no snapshots in the last "
            f"{WEEKLY_LOOKBACK_DAYS} days — change review unavailable"
        )
        section["checks"].append({
            "name": "history_window", "status": "WARN",
            "message": section["metrics"]["limitation"],
        })
        section["status"] = "WARN"
        return section
    earliest = in_window[0]
    section["metrics"]["compared_against_date"] = earliest.get("as_of_date")
    section["metrics"]["compared_against_captured_at"] = earliest.get("captured_at")

    # Build deltas for each bucket present in both snapshots and current.
    bucket_targets = {
        "main_top10": (rankings.get("rows") if isinstance(rankings, dict) else []) or [],
        "watchlist_top10": (watchlist.get("rows") if isinstance(watchlist, dict) else []) or [],
        "supp_top10": [
            r for r in ((watchlist.get("rows") if isinstance(watchlist, dict) else []) or [])
            if isinstance(r, dict) and r.get("data_source") == "supplemental_yfinance"
        ],
    }
    deltas: dict = {}
    for bucket_key, rows in bucket_targets.items():
        prior_members = _bucket_members(earliest, bucket_key)
        prior_tickers = _ticker_set(prior_members)
        prior_scores = _ticker_score_map(prior_members)
        # Take current top10 (snapshots use top10 baseline)
        current = [r for r in rows if isinstance(r, dict) and r.get("ticker")][:10]
        current_tickers = {r["ticker"] for r in current if r.get("ticker")}
        current_scores = {
            r["ticker"]: float(r["ai_score"])
            for r in current
            if r.get("ticker") and isinstance(r.get("ai_score"), (int, float))
        }
        new_entries = [
            {"ticker": r["ticker"], "rank": r.get("rank"),
             "ai_score": r.get("ai_score"), "sector": r.get("sector")}
            for r in current
            if r.get("ticker") and r["ticker"] not in prior_tickers
        ]
        exited = sorted(prior_tickers - current_tickers)
        score_changes = []
        for tic, cur in current_scores.items():
            if tic in prior_scores:
                delta = round(cur - prior_scores[tic], 4)
                score_changes.append({
                    "ticker": tic, "prior": prior_scores[tic],
                    "current": cur, "delta": delta,
                })
        score_changes.sort(key=lambda x: abs(x["delta"]), reverse=True)
        deltas[bucket_key] = {
            "new_entries": new_entries,
            "exited": exited,
            "score_changes": score_changes[:TOP_CONVICTION_DELTAS],
            "prior_size": len(prior_tickers),
            "current_size": len(current_tickers),
        }
    section["metrics"]["deltas"] = deltas
    if len(snapshots) < HISTORY_WARN_DAYS:
        section["metrics"]["limited_history"] = True
        section["checks"].append({
            "name": "history_window", "status": "WARN",
            "message": (f"only {len(snapshots)} snapshot(s) total; weekly "
                        f"compare uses oldest snapshot in last "
                        f"{WEEKLY_LOOKBACK_DAYS}d (limited)"),
        })
    else:
        section["metrics"]["limited_history"] = False
        section["checks"].append({
            "name": "history_window", "status": "OK",
            "message": (f"comparing to {earliest.get('as_of_date')} "
                        f"({len(snapshots)} snapshots total)"),
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]), key=lambda s: LEVEL_RANK[s])
    return section


def analyze_benchmark_context(bench: dict | None) -> dict:
    """Forward-return summary by bucket/horizon. Flag lagging buckets."""
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(bench, dict):
        section["checks"].append({
            "name": "benchmark_present", "status": "WARN",
            "message": "benchmark_review.json not present — no forward-return context",
        })
        section["status"] = "WARN"
        return section
    summary = bench.get("snapshot_summary") or {}
    horizons = summary.get("horizons") or {}
    section["metrics"]["snapshots_total"] = summary.get("snapshots_total") or 0
    by_horizon: dict[str, dict] = {}
    laggers: list[dict] = []
    leaders: list[dict] = []
    for h_key, h_data in horizons.items():
        if not isinstance(h_data, dict):
            continue
        completed = h_data.get("completed") or 0
        buckets = h_data.get("buckets") or {}
        h_entry = {"completed": completed, "buckets": {}}
        for b_key, b_data in buckets.items():
            if not isinstance(b_data, dict):
                continue
            mean_ret = b_data.get("avg_mean_return")
            if isinstance(mean_ret, (int, float)):
                h_entry["buckets"][b_key] = {
                    "snapshots": b_data.get("snapshots"),
                    "mean_return": round(float(mean_ret), 4),
                    "wins": b_data.get("wins"),
                    "losses": b_data.get("losses"),
                }
                rec = {"horizon": h_key, "bucket": b_key,
                       "mean_return": round(float(mean_ret), 4)}
                if mean_ret < 0:
                    laggers.append(rec)
                else:
                    leaders.append(rec)
        by_horizon[h_key] = h_entry
    section["metrics"]["by_horizon"] = by_horizon
    section["metrics"]["laggers"] = laggers
    section["metrics"]["leaders"] = leaders
    bc = bench.get("benchmark_compare") or {}
    section["metrics"]["spy_return_21d"] = bc.get("spy_return_21d")
    section["metrics"]["main_top25_window_return"] = bc.get("main_top25_mean_window_return")
    section["metrics"]["watchlist_top25_window_return"] = bc.get("watchlist_top25_mean_window_return")

    if section["metrics"]["snapshots_total"] < HISTORY_WARN_DAYS:
        section["checks"].append({
            "name": "limited_history", "status": "WARN",
            "message": (f"only {section['metrics']['snapshots_total']} snapshot(s) "
                        "in benchmark history — context limited"),
        })
    if laggers:
        section["checks"].append({
            "name": "lagging_buckets", "status": "WARN",
            "message": (f"{len(laggers)} bucket/horizon pairs with negative "
                        f"avg_mean_return"),
        })
    else:
        section["checks"].append({
            "name": "lagging_buckets", "status": "OK",
            "message": "no buckets show negative forward returns on completed horizons",
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]), key=lambda s: LEVEL_RANK[s])
    return section


def analyze_diagnostics(diag: dict | None) -> dict:
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
            "ticker": s.get("ticker"), "group": s.get("group"),
            "rank": s.get("rank"), "reasons": s.get("reasons"),
        }
        for s in suspicious[:8] if isinstance(s, dict)
    ]
    if suspicious:
        section["checks"].append({
            "name": "suspicious_ranks", "status": "WARN",
            "message": f"{len(suspicious)} suspicious top rank(s) flagged",
        })
    else:
        section["checks"].append({
            "name": "suspicious_ranks", "status": "OK",
            "message": "no suspicious top ranks",
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]), key=lambda s: LEVEL_RANK[s])
    return section


def _component_score(row: dict) -> float | None:
    parts = []
    for k in ("fundamental", "technical", "sentiment"):
        v = row.get(k)
        if isinstance(v, (int, float)):
            parts.append(float(v))
    return sum(parts) / len(parts) if parts else None


def _row_strength(row: dict) -> tuple[float, float]:
    ai = row.get("ai_score") if isinstance(row.get("ai_score"), (int, float)) else 0.0
    comp = _component_score(row) or 0.0
    return float(ai), comp


def build_candidates(
    rankings: dict | None,
    watchlist: dict | None,
    change_review: dict,
    diagnostics: dict | None,
) -> dict:
    """Build review-only candidate add/trim lists. Heuristic, not advice."""
    out: dict = {
        "candidate_adds": [],
        "candidate_trims": [],
        "disclaimer": (
            "Review candidates only — heuristic; not financial advice. "
            "Adds favor new entrants and high-component watchlist names; "
            "trims surface exits, suspicious ranks, weak components, "
            "and sector-crowded names."
        ),
    }
    deltas = (change_review.get("metrics", {}) or {}).get("deltas") or {}
    seen_add: set[str] = set()
    seen_trim: set[str] = set()

    # Adds: new entrants in main_top10
    for entry in (deltas.get("main_top10") or {}).get("new_entries") or []:
        tic = entry.get("ticker")
        if not tic or tic in seen_add:
            continue
        out["candidate_adds"].append({
            "ticker": tic, "reason": "new entrant — main top10",
            "ai_score": entry.get("ai_score"), "sector": entry.get("sector"),
        })
        seen_add.add(tic)
    # Adds: new entrants in watchlist_top10
    for entry in (deltas.get("watchlist_top10") or {}).get("new_entries") or []:
        tic = entry.get("ticker")
        if not tic or tic in seen_add:
            continue
        out["candidate_adds"].append({
            "ticker": tic, "reason": "new entrant — watchlist top10",
            "ai_score": entry.get("ai_score"), "sector": entry.get("sector"),
        })
        seen_add.add(tic)
    # Adds: high AI+TECH watchlist names not yet in main top25
    main_tickers = {
        r.get("ticker") for r in ((rankings.get("rows") if isinstance(rankings, dict) else []) or [])[:25]
        if isinstance(r, dict)
    }
    if isinstance(watchlist, dict):
        wl_rows = watchlist.get("rows") or []
        for r in wl_rows[:25]:
            if not isinstance(r, dict):
                continue
            tic = r.get("ticker")
            if not tic or tic in seen_add or tic in main_tickers:
                continue
            ai = r.get("ai_score")
            tech = r.get("technical")
            if (isinstance(ai, (int, float)) and isinstance(tech, (int, float))
                    and ai >= 7.5 and tech >= 7.5):
                out["candidate_adds"].append({
                    "ticker": tic,
                    "reason": f"watchlist top25 with AI {ai} TECH {tech}",
                    "ai_score": ai, "sector": r.get("sector"),
                })
                seen_add.add(tic)
                if len(out["candidate_adds"]) >= 15:
                    break

    # Trims: exits from main_top10
    for tic in (deltas.get("main_top10") or {}).get("exited") or []:
        if tic and tic not in seen_trim:
            out["candidate_trims"].append({
                "ticker": tic, "reason": "exited main top10 vs prior snapshot",
            })
            seen_trim.add(tic)
    # Trims: suspicious ranks
    if isinstance(diagnostics, dict):
        for s in (diagnostics.get("suspicious_ranks") or [])[:10]:
            if not isinstance(s, dict):
                continue
            tic = s.get("ticker")
            if not tic or tic in seen_trim:
                continue
            reasons = s.get("reasons") or []
            reason_str = "; ".join(reasons) if isinstance(reasons, list) else str(reasons)
            out["candidate_trims"].append({
                "ticker": tic,
                "reason": (f"diagnostics flagged ({s.get('group')} #"
                           f"{s.get('rank')}): {reason_str}"),
            })
            seen_trim.add(tic)
    # Trims: weak components in main top25 (low_risk OR sentiment < 4)
    if isinstance(rankings, dict):
        for r in (rankings.get("rows") or [])[:25]:
            if not isinstance(r, dict):
                continue
            tic = r.get("ticker")
            if not tic or tic in seen_trim:
                continue
            lr = r.get("low_risk")
            sent = r.get("sentiment")
            weak = []
            if isinstance(lr, (int, float)) and lr < 4:
                weak.append(f"LOW_RISK {lr}")
            if isinstance(sent, (int, float)) and sent < 4:
                weak.append(f"SENT {sent}")
            if weak:
                out["candidate_trims"].append({
                    "ticker": tic,
                    "reason": (f"weak components in main top25: "
                               + ", ".join(weak)),
                })
                seen_trim.add(tic)
    return out


# ----------------- rollup -----------------


def compute_overall(sections: dict) -> str:
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


def collect_action_items(sections: dict, candidates: dict) -> list[str]:
    items: list[str] = []
    for sec_name, sec in sections.items():
        for c in sec.get("checks", []):
            level = (c.get("status") or "").upper()
            if level not in ("WARN", "FAIL"):
                continue
            items.append(f"[{level}] {sec_name}.{c.get('name')}: {c.get('message')}")
    cr = sections.get("change_review", {}).get("metrics", {}) or {}
    deltas = cr.get("deltas") or {}
    for bk, payload in deltas.items():
        new_entries = payload.get("new_entries") or []
        exited = payload.get("exited") or []
        if new_entries:
            tickers = ", ".join(e["ticker"] for e in new_entries[:5])
            items.append(f"[INFO] {bk} new entries: {tickers}")
        if exited:
            items.append(f"[INFO] {bk} exited: {', '.join(exited[:5])}")
    if candidates.get("candidate_adds"):
        names = ", ".join(c["ticker"] for c in candidates["candidate_adds"][:5])
        items.append(f"[INFO] candidate adds (review): {names}")
    if candidates.get("candidate_trims"):
        names = ", ".join(c["ticker"] for c in candidates["candidate_trims"][:5])
        items.append(f"[INFO] candidate trims (review): {names}")

    def _rank(line: str) -> int:
        if line.startswith("[FAIL]"):
            return 0
        if line.startswith("[WARN]"):
            return 1
        return 2
    items.sort(key=_rank)
    return items[:10]


def build_summary(report: dict) -> str:
    s = report["sections"]
    overall = report["overall"]
    cr = s.get("change_review", {}).get("metrics", {}) or {}
    snaps = cr.get("snapshots_total") or 0
    deltas = cr.get("deltas") or {}
    main_d = deltas.get("main_top10") or {}
    new_main = len(main_d.get("new_entries") or [])
    exit_main = len(main_d.get("exited") or [])
    lb = s.get("leaderboards", {}).get("metrics", {}) or {}
    main_sc = lb.get("main_sector_concentration") or {}
    sc_label = "—"
    if main_sc.get("top_sector"):
        sc_label = f"{main_sc['top_sector']} {main_sc.get('pct', 0):.0%}"
    bc = s.get("benchmark_context", {}).get("metrics", {}) or {}
    bench_lag = len(bc.get("laggers") or [])
    cand = report.get("candidates") or {}
    n_adds = len(cand.get("candidate_adds") or [])
    n_trims = len(cand.get("candidate_trims") or [])
    parts = [
        f"Overall {overall}",
        f"snaps={snaps}",
        f"main top10 Δ +{new_main}/-{exit_main}",
        f"sector {sc_label}",
        f"bench lag={bench_lag}",
        f"adds={n_adds} trims={n_trims}",
    ]
    return " · ".join(parts)


# ----------------- output -----------------


def _render_html(report: dict) -> str:
    overall = report["overall"]
    color = {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}[overall]
    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Weekly Rebalance Check</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:1080px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
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
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
@media(max-width:760px){{.cols{{grid-template-columns:1fr}}}}
.disclaimer{{font-style:italic;color:#777;font-size:12px;margin-top:6px}}
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>Weekly Rebalance Check</h1>
<p class="meta">Generated {escape(report.get("generated_at_chicago",""))} &middot; Overall: <span class="badge">{escape(overall)}</span></p>
<div class="summary"><strong>Summary:</strong> {escape(report.get("summary",""))}</div>
""")

    actions = report.get("action_items") or []
    if actions:
        parts.append('<div class="section"><h2>Action Items</h2><ul class="action">')
        for a in actions:
            parts.append(f"<li>{escape(a)}</li>")
        parts.append("</ul></div>")

    # Candidate adds / trims
    cand = report.get("candidates") or {}
    parts.append('<div class="section"><h2>Review Candidates</h2>')
    parts.append(f'<p class="disclaimer">{escape(cand.get("disclaimer",""))}</p>')
    parts.append('<div class="cols">')
    for label, key in (("Candidate adds", "candidate_adds"),
                       ("Candidate trims / reviews", "candidate_trims")):
        rows = cand.get(key) or []
        parts.append(f"<div><h3>{escape(label)}</h3>")
        if rows:
            parts.append("<table><thead><tr><th>Ticker</th><th>Reason</th>"
                         "</tr></thead><tbody>")
            for r in rows:
                parts.append(
                    f"<tr><td><strong>{escape(str(r.get('ticker') or ''))}"
                    f"</strong></td>"
                    f"<td>{escape(str(r.get('reason') or ''))}</td></tr>"
                )
            parts.append("</tbody></table>")
        else:
            parts.append("<p class='kv'>—</p>")
        parts.append("</div>")
    parts.append("</div></div>")

    # Change review
    cr = report["sections"].get("change_review", {}).get("metrics", {}) or {}
    deltas = cr.get("deltas") or {}
    if deltas:
        parts.append('<div class="section"><h2>Weekly Change Review</h2>')
        cmp_date = cr.get("compared_against_date")
        snaps_total = cr.get("snapshots_total")
        snaps_in_window = cr.get("snapshots_in_window")
        parts.append(
            f"<p class='kv'><strong>Compared against:</strong> {escape(str(cmp_date or '—'))}"
            f" · snapshots total {snaps_total} · in {WEEKLY_LOOKBACK_DAYS}-day window {snaps_in_window}</p>"
        )
        if cr.get("limited_history"):
            parts.append("<p class='kv'><span class='WARN'>WARN</span> limited "
                         f"snapshot history (&lt;{HISTORY_WARN_DAYS} snapshots).</p>")
        for bucket, payload in deltas.items():
            parts.append(f"<h3>{escape(bucket)}</h3>")
            new_entries = payload.get("new_entries") or []
            exited = payload.get("exited") or []
            score_changes = payload.get("score_changes") or []
            if new_entries:
                parts.append("<p class='kv'><strong>New entries:</strong> "
                             + escape(", ".join(e["ticker"] for e in new_entries))
                             + "</p>")
            else:
                parts.append("<p class='kv'><strong>New entries:</strong> none</p>")
            if exited:
                parts.append("<p class='kv'><strong>Exited:</strong> "
                             + escape(", ".join(exited)) + "</p>")
            else:
                parts.append("<p class='kv'><strong>Exited:</strong> none</p>")
            if score_changes:
                parts.append("<table><thead><tr><th>Ticker</th><th>Prior</th>"
                             "<th>Current</th><th>Δ</th></tr></thead><tbody>")
                for sc in score_changes:
                    parts.append(
                        f"<tr><td><strong>{escape(str(sc.get('ticker') or ''))}"
                        f"</strong></td>"
                        f"<td>{escape(str(sc.get('prior')))}</td>"
                        f"<td>{escape(str(sc.get('current')))}</td>"
                        f"<td>{escape(str(sc.get('delta')))}</td></tr>"
                    )
                parts.append("</tbody></table>")
        parts.append("</div>")
    elif cr.get("limitation"):
        parts.append(
            f"<div class='section'><h2>Weekly Change Review</h2>"
            f"<p class='kv'><span class='WARN'>WARN</span> "
            f"{escape(str(cr['limitation']))}</p></div>"
        )

    # Benchmark context
    bc = report["sections"].get("benchmark_context", {}).get("metrics", {}) or {}
    parts.append('<div class="section"><h2>Benchmark Performance Context</h2>')
    snaps_t = bc.get("snapshots_total")
    parts.append(f"<p class='kv'><strong>Snapshots in benchmark history:</strong> {snaps_t}</p>")
    by_h = bc.get("by_horizon") or {}
    if by_h:
        parts.append("<table><thead><tr><th>Horizon</th><th>Bucket</th>"
                     "<th>Snapshots</th><th>Mean Return</th><th>W/L</th>"
                     "</tr></thead><tbody>")
        for h, h_data in by_h.items():
            for b, b_data in (h_data.get("buckets") or {}).items():
                parts.append(
                    f"<tr><td>{escape(str(h))}</td>"
                    f"<td>{escape(str(b))}</td>"
                    f"<td>{escape(str(b_data.get('snapshots')))}</td>"
                    f"<td>{escape(str(b_data.get('mean_return')))}</td>"
                    f"<td>{escape(str(b_data.get('wins')))}/{escape(str(b_data.get('losses')))}</td></tr>"
                )
        parts.append("</tbody></table>")
    else:
        parts.append("<p class='kv'>No completed forward-return horizons yet.</p>")
    parts.append("</div>")

    # Leaderboards
    lb = report["sections"].get("leaderboards", {}).get("metrics", {}) or {}
    for label, key in (("Main Top 25", "main_top25"),
                       ("Watchlist Top 25", "watchlist_top25"),
                       ("SUPP Top 25", "supp_top25")):
        rows = lb.get(key) or []
        parts.append(f'<div class="section"><h2>{escape(label)}</h2>')
        if rows:
            parts.append("<table><thead><tr><th>#</th><th>Ticker</th>"
                         "<th>Company</th><th>AI</th><th>FUND</th><th>TECH</th>"
                         "<th>SENT</th><th>Sector</th></tr></thead><tbody>")
            for r in rows:
                parts.append(
                    f"<tr><td>{escape(str(r.get('rank') or ''))}</td>"
                    f"<td><strong>{escape(str(r.get('ticker') or ''))}</strong></td>"
                    f"<td>{escape(str(r.get('company') or ''))}</td>"
                    f"<td>{escape(str(r.get('ai_score') or ''))}</td>"
                    f"<td>{escape(str(r.get('fundamental') or ''))}</td>"
                    f"<td>{escape(str(r.get('technical') or ''))}</td>"
                    f"<td>{escape(str(r.get('sentiment') or ''))}</td>"
                    f"<td>{escape(str(r.get('sector') or ''))}</td></tr>"
                )
            parts.append("</tbody></table>")
        else:
            parts.append("<p class='kv'>—</p>")
        parts.append("</div>")

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
    bench = _load_json(BENCHMARK_FILE)
    diag = _load_json(DIAGNOSTICS_FILE)
    par = _load_json(PARITY_FILE)
    dq = _load_json(DATA_QUALITY_FILE)
    sr_rep = _load_json(SCHEDULE_RELIABILITY_FILE)
    snapshots = _load_snapshots(BENCHMARK_SNAPSHOTS_FILE)

    sections = {
        "freshness": analyze_freshness(rankings, watchlist),
        "data_quality": analyze_data_quality(dq),
        "schedule": analyze_schedule(sr_rep),
        "leaderboards": analyze_leaderboards(rankings, watchlist),
        "change_review": analyze_change_review(rankings, watchlist, snapshots),
        "benchmark_context": analyze_benchmark_context(bench),
        "diagnostics": analyze_diagnostics(diag),
    }
    candidates = build_candidates(rankings, watchlist, sections["change_review"], diag)
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
        "candidates": candidates,
        "inputs": {
            "rankings_present": rankings is not None,
            "watchlist_present": watchlist is not None,
            "benchmark_present": bench is not None,
            "diagnostics_present": diag is not None,
            "parity_present": par is not None,
            "data_quality_present": dq is not None,
            "schedule_reliability_present": sr_rep is not None,
            "snapshot_count": len(snapshots),
        },
    }
    report["action_items"] = collect_action_items(sections, candidates)
    report["summary"] = build_summary(report)
    return report


def _stamp_task(report: dict) -> None:
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
    summary = report.get("summary") or "Weekly rebalance check"
    target_id = None
    for row in tasks:
        if isinstance(row, dict) and row.get("id") in (TASK_ID_PRIMARY, TASK_ID_FALLBACK):
            target_id = row.get("id")
            row["last_run"] = last_run
            row["status"] = status
            row["summary"] = summary
            row["report_url"] = REPORT_URL
            row["next_run"] = "—"
            row["schedule"] = "Fridays 4:00 PM CT / generated each refresh"
            if not row.get("name"):
                row["name"] = "Weekly Rebalance Check"
            break
    if target_id is not None:
        TASKS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report()
    JSON_OUTPUT.write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")
    _stamp_task(report)
    print(f"[weekly_rebalance_check] overall={report['overall']} -> {JSON_OUTPUT}")
    print(f"[weekly_rebalance_check] summary: {report['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
