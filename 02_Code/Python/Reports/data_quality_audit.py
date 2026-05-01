"""Data Quality Audit — summarizes freshness, completeness, and health of
the pipeline's published artifacts.

Inputs (read-only):
  - data/rankings.json
  - data/watchlist_rankings.json
  - data/tasks.json
  - data/reports/market_risk_monitor.json (optional)

Outputs:
  - data/reports/data_quality_audit.json (machine-readable; structured findings)
  - reports/data-quality-audit.html      (human-readable summary)

Status levels per check and overall: OK / WARN / FAIL. The script is
deliberately tolerant of missing inputs — a missing artifact is itself a
finding, not an exception.
"""

from __future__ import annotations

import json
import os
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
TASKS_FILE = DATA_DIR / "tasks.json"
MARKET_RISK_FILE = DATA_REPORTS_DIR / "market_risk_monitor.json"

JSON_OUTPUT = DATA_REPORTS_DIR / "data_quality_audit.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "data-quality-audit.html"

SCORE_FIELDS = (
    "ai_score",
    "fundamental",
    "technical",
    "sentiment",
    "low_risk",
    "swing_score",
)

LEVEL_RANK = {"OK": 0, "WARN": 1, "FAIL": 2}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_as_of(s: str | None) -> datetime | None:
    """Parse an as_of like '2026-05-01 11:06 AM CDT' to an aware UTC datetime."""
    if not s or not isinstance(s, str):
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{1,2}:\d{2} (?:AM|PM)) (CDT|CST)$", s.strip())
    if not m:
        return None
    naive = datetime.strptime(m.group(1), "%Y-%m-%d %I:%M %p")
    offset_hours = -5 if m.group(2) == "CDT" else -6
    return naive.replace(tzinfo=timezone(timedelta(hours=offset_hours))).astimezone(timezone.utc)


def _is_weekend_utc(dt: datetime) -> bool:
    # Approx by Chicago calendar
    chi = dt.astimezone(timezone(timedelta(hours=-5)))
    return chi.weekday() >= 5


def _load_json(path: Path) -> dict | list | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None


def _is_missing(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        return s == "" or s == "—" or s.lower() in ("nan", "none", "n/a")
    return False


def _worst(level_a: str, level_b: str) -> str:
    return level_a if LEVEL_RANK[level_a] >= LEVEL_RANK[level_b] else level_b


def _check(name: str, level: str, message: str, data: dict | None = None) -> dict:
    return {
        "name": name,
        "status": level,
        "message": message,
        "data": data or {},
    }


# ---------- Audits ----------


def audit_rankings(payload: dict | None) -> dict:
    section: dict = {"present": payload is not None, "checks": [], "metrics": {}}
    if payload is None:
        section["checks"].append(_check("rankings_present", "FAIL", "data/rankings.json missing or unreadable"))
        section["status"] = "FAIL"
        return section

    rows = payload.get("rows") or []
    as_of = payload.get("as_of")
    open_date = payload.get("open_date")
    is_open_run = bool(payload.get("is_open_run"))
    universe = payload.get("universe")

    section["metrics"]["as_of"] = as_of
    section["metrics"]["open_date"] = open_date
    section["metrics"]["is_open_run"] = is_open_run
    section["metrics"]["universe"] = universe
    section["metrics"]["row_count"] = len(rows)

    # Row count
    if len(rows) == 100:
        section["checks"].append(_check("row_count", "OK", f"{len(rows)} rows"))
    else:
        section["checks"].append(
            _check("row_count", "WARN", f"expected 100 rows, found {len(rows)}")
        )

    # Freshness
    section["checks"].append(_freshness_check("rankings_freshness", as_of))

    # Missing market_cap / sector / industry
    miss_mcap = sum(1 for r in rows if _is_missing(r.get("market_cap")))
    miss_sector = sum(1 for r in rows if _is_missing(r.get("sector")))
    miss_industry = sum(1 for r in rows if _is_missing(r.get("industry")))
    section["metrics"]["missing_market_cap"] = miss_mcap
    section["metrics"]["missing_sector"] = miss_sector
    section["metrics"]["missing_industry"] = miss_industry
    section["checks"].append(_threshold_check(
        "missing_market_cap", miss_mcap, len(rows), warn_pct=0.05, fail_pct=0.20))
    section["checks"].append(_threshold_check(
        "missing_sector", miss_sector, len(rows), warn_pct=0.20, fail_pct=0.60))
    section["checks"].append(_threshold_check(
        "missing_industry", miss_industry, len(rows), warn_pct=0.10, fail_pct=0.40))

    # Closes / sparkline completeness
    closes_lens = [len(r.get("closes") or []) for r in rows]
    len_dist = Counter(closes_lens)
    short_spark = sum(1 for n in closes_lens if n < 5)
    section["metrics"]["closes_length_distribution"] = dict(len_dist)
    section["metrics"]["closes_short_count"] = short_spark
    section["checks"].append(_threshold_check(
        "sparkline_short", short_spark, len(rows), warn_pct=0.05, fail_pct=0.25,
        suffix="rows with closes length < 5"))

    # MOV distribution
    mov_pos = mov_neg = mov_zero = mov_missing = 0
    for r in rows:
        ch = r.get("change")
        if ch is None:
            mov_missing += 1
        elif isinstance(ch, (int, float)):
            if ch > 0:
                mov_pos += 1
            elif ch < 0:
                mov_neg += 1
            else:
                mov_zero += 1
        else:
            mov_missing += 1
    section["metrics"]["mov_distribution"] = {
        "positive": mov_pos,
        "negative": mov_neg,
        "zero": mov_zero,
        "missing": mov_missing,
    }
    if mov_missing >= len(rows) and len(rows):
        section["checks"].append(_check(
            "mov_distribution", "WARN", "all rows missing MOV (change) — prior-run snapshot may be missing"))
    else:
        section["checks"].append(_check(
            "mov_distribution", "OK",
            f"+{mov_pos} / -{mov_neg} / 0={mov_zero} / missing={mov_missing}"))

    # Score completeness
    score_missing = {f: 0 for f in SCORE_FIELDS}
    for r in rows:
        for f in SCORE_FIELDS:
            v = r.get(f)
            if v is None or (isinstance(v, float) and v != v):  # NaN
                score_missing[f] += 1
    section["metrics"]["score_missing"] = score_missing
    for f, cnt in score_missing.items():
        section["checks"].append(_threshold_check(
            f"score_missing_{f}", cnt, len(rows), warn_pct=0.05, fail_pct=0.25))

    section["status"] = _rollup(section["checks"])
    return section


def audit_watchlist(payload: dict | None) -> dict:
    section: dict = {"present": payload is not None, "checks": [], "metrics": {}}
    if payload is None:
        section["checks"].append(_check("watchlist_present", "WARN", "data/watchlist_rankings.json missing or unreadable"))
        section["status"] = "WARN"
        return section

    rows = payload.get("rows") or []
    as_of = payload.get("as_of")
    unavailable = payload.get("unavailable") or []
    source_meta = payload.get("source_meta") or {}

    section["metrics"]["as_of"] = as_of
    section["metrics"]["row_count"] = len(rows)
    section["metrics"]["unavailable_count"] = len(unavailable)
    section["metrics"]["unavailable_sample"] = [
        {k: u.get(k) for k in ("input", "source", "reason")} for u in unavailable[:10]
    ]

    # Freshness
    section["checks"].append(_freshness_check("watchlist_freshness", as_of))

    # Source breakdown
    src_counter: Counter = Counter()
    ds_counter: Counter = Counter()
    for r in rows:
        src_counter[r.get("source") or "unknown"] += 1
        ds_counter[r.get("data_source") or "unknown"] += 1
    section["metrics"]["by_source_label"] = dict(src_counter)
    section["metrics"]["by_data_source"] = dict(ds_counter)

    # SUPP summary (from source_meta.supp_summary if present)
    supp = source_meta.get("supp_summary") or {}
    section["metrics"]["supp_summary"] = supp
    section["metrics"]["supp_by_kind"] = source_meta.get("supp_by_kind") or {}
    section["metrics"]["supp_by_enrichment"] = source_meta.get("supp_by_enrichment") or {}

    # SUPP full-fundamentals proportion check
    supp_total = supp.get("total")
    supp_full = supp.get("full_fundamentals")
    if isinstance(supp_total, int) and supp_total > 0 and isinstance(supp_full, int):
        ratio = supp_full / supp_total
        if ratio >= 0.70:
            section["checks"].append(_check(
                "supp_full_fundamentals", "OK",
                f"{supp_full}/{supp_total} ({ratio:.0%}) full fundamentals"))
        elif ratio >= 0.40:
            section["checks"].append(_check(
                "supp_full_fundamentals", "WARN",
                f"only {supp_full}/{supp_total} ({ratio:.0%}) full fundamentals — yfinance enrichment degraded"))
        else:
            section["checks"].append(_check(
                "supp_full_fundamentals", "FAIL",
                f"only {supp_full}/{supp_total} ({ratio:.0%}) full fundamentals — yfinance enrichment broken"))
    else:
        section["checks"].append(_check(
            "supp_full_fundamentals", "OK", "no SUPP fundamentals to evaluate"))

    # Unavailable spike — relative to combined_unique if known
    combined_unique = source_meta.get("combined_unique")
    if isinstance(combined_unique, int) and combined_unique > 0:
        ratio = len(unavailable) / combined_unique
        section["metrics"]["unavailable_ratio"] = round(ratio, 4)
        if ratio >= 0.10:
            section["checks"].append(_check(
                "unavailable_spike", "FAIL",
                f"{len(unavailable)}/{combined_unique} unavailable ({ratio:.0%}) — fetch path likely broken"))
        elif ratio >= 0.03:
            section["checks"].append(_check(
                "unavailable_spike", "WARN",
                f"{len(unavailable)}/{combined_unique} unavailable ({ratio:.0%})"))
        else:
            section["checks"].append(_check(
                "unavailable_spike", "OK",
                f"{len(unavailable)}/{combined_unique} unavailable ({ratio:.0%})"))
    else:
        section["checks"].append(_check(
            "unavailable_count", "OK", f"{len(unavailable)} unavailable"))

    # Missing fundamentals by data_source (only meaningful for supplemental rows)
    miss_mcap_supp = miss_sector_supp = miss_fund_supp = 0
    supp_rows = 0
    for r in rows:
        if r.get("data_source", "").startswith("supplemental"):
            supp_rows += 1
            if _is_missing(r.get("market_cap")):
                miss_mcap_supp += 1
            if _is_missing(r.get("sector")):
                miss_sector_supp += 1
            if r.get("fundamental") in (None, ""):
                miss_fund_supp += 1
    section["metrics"]["supp_rows_inspected"] = supp_rows
    section["metrics"]["supp_missing_market_cap"] = miss_mcap_supp
    section["metrics"]["supp_missing_sector"] = miss_sector_supp
    section["metrics"]["supp_missing_fundamental"] = miss_fund_supp
    if supp_rows:
        section["checks"].append(_threshold_check(
            "supp_missing_market_cap", miss_mcap_supp, supp_rows, warn_pct=0.20, fail_pct=0.50))
        section["checks"].append(_threshold_check(
            "supp_missing_fundamental", miss_fund_supp, supp_rows, warn_pct=0.30, fail_pct=0.60))

    # Watchlist row-count drop sentinel: too small a watchlist is suspect
    if len(rows) < 30:
        section["checks"].append(_check(
            "watchlist_row_count", "WARN",
            f"watchlist row count is unusually small ({len(rows)})"))

    section["status"] = _rollup(section["checks"])
    return section


def audit_tasks(payload: dict | list | None, rankings_as_of: datetime | None) -> dict:
    section: dict = {"present": payload is not None, "checks": [], "metrics": {}}
    if payload is None:
        section["checks"].append(_check("tasks_present", "WARN", "data/tasks.json missing or unreadable"))
        section["status"] = "WARN"
        return section

    tasks = payload.get("tasks") if isinstance(payload, dict) else payload
    if not isinstance(tasks, list):
        section["checks"].append(_check("tasks_shape", "FAIL", "tasks.json has unexpected shape"))
        section["status"] = "FAIL"
        return section

    section["metrics"]["task_count"] = len(tasks)
    status_counter: Counter = Counter()
    not_run_ids: list[str] = []
    stale_report_ids: list[str] = []
    summary_rows: list[dict] = []
    for t in tasks:
        st = (t.get("status") or "").strip()
        status_counter[st or "unknown"] += 1
        if st.lower() == "not run":
            not_run_ids.append(t.get("id") or t.get("name") or "?")

        # Stale-report detection: if task has report_url AND a parseable last_run
        # AND that last_run is older than rankings as_of by more than 24h, flag.
        if rankings_as_of and t.get("report_url"):
            last_run_dt = _parse_as_of(t.get("last_run"))
            if last_run_dt and (rankings_as_of - last_run_dt) > timedelta(hours=24):
                stale_report_ids.append(t.get("id") or t.get("name") or "?")

        summary_rows.append({
            "id": t.get("id"),
            "name": t.get("name"),
            "status": st,
            "last_run": t.get("last_run"),
            "next_run": t.get("next_run"),
            "has_report": bool(t.get("report_url")),
        })

    section["metrics"]["status_counts"] = dict(status_counter)
    section["metrics"]["not_run_count"] = len(not_run_ids)
    section["metrics"]["not_run_ids"] = not_run_ids
    section["metrics"]["stale_report_ids"] = stale_report_ids
    section["metrics"]["tasks"] = summary_rows

    # Not Run is informational while tasks are still being wired up. WARN only
    # if all tasks are Not Run (would indicate the tasks file got reset).
    if status_counter.get("Not Run", 0) >= len(tasks) and len(tasks) > 0:
        section["checks"].append(_check(
            "tasks_all_not_run", "FAIL",
            "every task is Not Run — tasks.json may have been reset"))
    else:
        section["checks"].append(_check(
            "tasks_status", "OK",
            f"{status_counter.get('Not Run', 0)}/{len(tasks)} Not Run"))

    if stale_report_ids:
        section["checks"].append(_check(
            "stale_report_metadata", "WARN",
            f"report task(s) with last_run >24h before rankings as_of: {', '.join(stale_report_ids)}",
            {"ids": stale_report_ids}))
    else:
        section["checks"].append(_check(
            "stale_report_metadata", "OK", "no stale report metadata"))

    section["status"] = _rollup(section["checks"])
    return section


# ---------- Generic helpers ----------


def _rollup(checks: list[dict]) -> str:
    level = "OK"
    for c in checks:
        level = _worst(level, c.get("status", "OK"))
    return level


def _threshold_check(name: str, count: int, total: int, *, warn_pct: float, fail_pct: float, suffix: str = "") -> dict:
    if total <= 0:
        return _check(name, "OK", f"{count} (no rows to evaluate)")
    pct = count / total
    msg_suffix = f" ({suffix})" if suffix else ""
    msg = f"{count}/{total} ({pct:.0%}){msg_suffix}"
    if pct >= fail_pct:
        return _check(name, "FAIL", msg)
    if pct >= warn_pct:
        return _check(name, "WARN", msg)
    return _check(name, "OK", msg)


def _freshness_check(name: str, as_of_str: str | None) -> dict:
    dt = _parse_as_of(as_of_str)
    if dt is None:
        return _check(name, "WARN", f"unparseable as_of: {as_of_str!r}")
    age = _now_utc() - dt
    age_hours = age.total_seconds() / 3600.0
    weekend = _is_weekend_utc(_now_utc())
    warn_h = 72.0 if weekend else 6.0
    fail_h = 168.0 if weekend else 24.0
    msg = f"as_of {as_of_str} (age {age_hours:.1f}h{'; weekend' if weekend else ''})"
    if age_hours >= fail_h:
        return _check(name, "FAIL", msg)
    if age_hours >= warn_h:
        return _check(name, "WARN", msg)
    return _check(name, "OK", msg)


# ---------- Output ----------


def _build_overall(sections: dict) -> str:
    level = "OK"
    for sec in sections.values():
        level = _worst(level, sec.get("status", "OK"))
    return level


def _render_html(report: dict) -> str:
    overall = report["overall"]
    color = {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}[overall]
    generated = report["generated_at"]
    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Data Quality Audit</title>
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
.kv{{font-size:13px;color:#444}} .kv code{{background:#f3f3f3;padding:1px 4px;border-radius:3px}}
.back{{font-size:13px}}
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>Data Quality Audit</h1>
<p class="meta">Generated {escape(generated)} &middot; Overall: <span class="badge">{overall}</span></p>
""")

    for key, sec in report["sections"].items():
        st = sec.get("status", "OK")
        st_class = st
        parts.append(f'<div class="section"><h2>{escape(key.replace("_", " ").title())}'
                     f'<span class="{st_class}">{st}</span></h2>')

        # Metrics
        if sec.get("metrics"):
            parts.append('<div class="kv"><strong>Metrics:</strong><pre>'
                         + escape(json.dumps(sec["metrics"], indent=2, default=str))
                         + '</pre></div>')

        # Checks
        parts.append('<table><thead><tr><th style="width:32%">Check</th>'
                     '<th style="width:10%">Status</th><th>Detail</th></tr></thead><tbody>')
        for c in sec.get("checks", []):
            parts.append(
                f'<tr><td>{escape(c.get("name",""))}</td>'
                f'<td class="{escape(c.get("status",""))}">{escape(c.get("status",""))}</td>'
                f'<td>{escape(c.get("message",""))}</td></tr>'
            )
        parts.append("</tbody></table></div>")

    parts.append("</body></html>")
    return "\n".join(parts)


def main() -> int:
    rankings = _load_json(RANKINGS_FILE)
    watchlist = _load_json(WATCHLIST_FILE)
    tasks = _load_json(TASKS_FILE)

    rankings_as_of = _parse_as_of(rankings.get("as_of") if isinstance(rankings, dict) else None)

    sections = {
        "rankings": audit_rankings(rankings if isinstance(rankings, dict) else None),
        "watchlist": audit_watchlist(watchlist if isinstance(watchlist, dict) else None),
        "tasks": audit_tasks(tasks, rankings_as_of),
    }

    report = {
        "generated_at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "overall": _build_overall(sections),
        "sections": sections,
    }

    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUTPUT.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")

    # Also stamp the data-quality-audit task in tasks.json if such a row exists.
    _stamp_task_if_present(report)

    print(f"[data_quality_audit] overall={report['overall']} -> {JSON_OUTPUT}")
    return 0


def _stamp_task_if_present(report: dict) -> None:
    """If tasks.json already has a row with id 'data-quality-audit', stamp it.
    We do not invent the row here; whoever decides to surface this on the
    dashboard owns adding the row to tasks.json.
    """
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
        if not isinstance(row, dict):
            continue
        if row.get("id") == "data-quality-audit":
            ranks = report["sections"].get("rankings", {}).get("metrics", {})
            row["last_run"] = ranks.get("as_of") or row.get("last_run") or "—"
            overall = report["overall"]
            row["status"] = "OK" if overall == "OK" else ("warn" if overall == "WARN" else "fail")
            row["summary"] = _short_summary(report)
            row["report_url"] = "./reports/data-quality-audit.html"
            changed = True
    if changed:
        TASKS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _short_summary(report: dict) -> str:
    parts = []
    for key, sec in report["sections"].items():
        parts.append(f"{key}={sec.get('status','?')}")
    return "Audit: " + ", ".join(parts)


if __name__ == "__main__":
    sys.exit(main())
