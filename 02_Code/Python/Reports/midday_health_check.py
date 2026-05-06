"""Midday Health Check — operational rollup that summarizes the current
state of the rankings pipeline by digesting the artifacts produced by the
other reports.

This is the dashboard's at-a-glance "is everything OK right now?"
indicator. Unlike the morning-only reports, this runs on every proceeded
slot so the task table reflects the *current* operational state rather
than this morning's snapshot.

Inputs (read-only):
  - data/rankings.json
  - data/watchlist_rankings.json
  - data/reports/data_quality_audit.json
  - data/reports/schedule_reliability.json
  - data/reports/benchmark_review.json
  - data/reports/scoring_parity_review.json

Outputs:
  - data/reports/midday_health_check.json
  - reports/midday-health-check.html
  - data/tasks.json row id=midday-health-check stamped with current run

Status logic (worst wins, with a few specific overrides):
  * FAIL when:
      - rankings.json missing or unparseable
      - rankings as_of stale on a weekday (>24h)
      - data-quality audit overall=FAIL on rankings or tasks sections
  * WARN when:
      - data-quality WARN, OR
      - schedule_reliability FAIL but driven by recovered/manual coverage
        (today's slots have hits, last_run.event=workflow_dispatch or
        recent days have missing slots while today is satisfied), OR
      - watchlist SUPP coverage degraded, OR
      - scoring parity FAIL/WARN
  * OK otherwise.

Tolerance: any missing input file is itself a finding (degraded WARN),
not an exception. The script never raises out of build_report; the JSON
report always lands.
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
BENCHMARK_FILE = DATA_REPORTS_DIR / "benchmark_review.json"
PARITY_FILE = DATA_REPORTS_DIR / "scoring_parity_review.json"

JSON_OUTPUT = DATA_REPORTS_DIR / "midday_health_check.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "midday-health-check.html"
TASKS_FILE = DATA_DIR / "tasks.json"
REPORT_URL = "./reports/midday-health-check.html"

LEVEL_RANK = {"OK": 0, "WARN": 1, "FAIL": 2}

# Freshness thresholds (hours) for live rankings.json.
FRESH_WARN_HOURS_WEEKDAY = 6.0
FRESH_FAIL_HOURS_WEEKDAY = 24.0
FRESH_WARN_HOURS_WEEKEND = 72.0
FRESH_FAIL_HOURS_WEEKEND = 168.0

# Time window in which we say "today's data is live": same Chicago
# calendar date.


# ----------------- helpers -----------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_chicago(dt: datetime) -> datetime:
    """Cheap CDT/CST approximation matching the rest of the codebase."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    chi_utc = dt.astimezone(timezone.utc)
    offset_h = -5 if 3 <= chi_utc.month <= 10 else -6
    return chi_utc.astimezone(timezone(timedelta(hours=offset_h)))


def _parse_as_of(s: str | None) -> datetime | None:
    """Parse 'YYYY-MM-DD HH:MM AM/PM CDT|CST' to UTC datetime."""
    if not s or not isinstance(s, str):
        return None
    m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{1,2}:\d{2} (?:AM|PM)) (CDT|CST)$", s.strip())
    if not m:
        return None
    naive = datetime.strptime(m.group(1), "%Y-%m-%d %I:%M %p")
    offset_hours = -5 if m.group(2) == "CDT" else -6
    return naive.replace(tzinfo=timezone(timedelta(hours=offset_hours))).astimezone(timezone.utc)


def _short_time(s: str | None) -> str:
    """Convert 'YYYY-MM-DD HH:MM AM/PM CDT|CST' into a brief 'HH:MM AM CT'."""
    if not s:
        return "—"
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{1,2}:\d{2} (?:AM|PM)) (CDT|CST)$", s.strip())
    if not m:
        return s
    return f"{m.group(2)} CT"


def _worst(a: str, b: str) -> str:
    return a if LEVEL_RANK[a] >= LEVEL_RANK[b] else b


def _load_json(path: Path) -> dict | None:
    """Return parsed JSON or None on any error."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


# ----------------- analyzers -----------------


def analyze_data_freshness(rankings: dict | None) -> dict:
    """Did the live rankings.json land recently?

    On weekdays, > FRESH_FAIL_HOURS_WEEKDAY old is a FAIL — the table on
    the dashboard will be misleading. WARN starts at
    FRESH_WARN_HOURS_WEEKDAY so a stale midday slot is visible without
    failing the rollup outright.
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
    # Today-live: as_of's chicago calendar date matches today
    as_of_chi = _to_chicago(as_of_dt)
    today_live = as_of_chi.date() == chi_now.date()
    section["metrics"]["today_live"] = today_live
    section["checks"].append({
        "name": "today_live",
        "status": "OK" if (today_live or is_weekend) else "WARN",
        "message": f"data {'is' if today_live else 'is not'} from today (Chicago)",
    })
    section["status"] = max((c["status"] for c in section["checks"]),
                            key=lambda s: LEVEL_RANK[s])
    return section


def analyze_data_quality(dq: dict | None) -> dict:
    """Roll up the data_quality_audit.json. WARN on missing input."""
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
    section["metrics"]["generated_at"] = dq.get("generated_at")
    sections = dq.get("sections") or {}
    by_section: dict[str, str] = {}
    rankings_status = "OK"
    tasks_status = "OK"
    for key, sec in sections.items():
        if not isinstance(sec, dict):
            continue
        # Promote the worst non-OK check into the section status
        sec_status = "OK"
        for c in sec.get("checks", []):
            sec_status = _worst(sec_status, (c.get("status") or "OK").upper())
        by_section[key] = sec_status
        if key == "rankings":
            rankings_status = sec_status
        elif key == "tasks":
            tasks_status = sec_status
    section["metrics"]["by_section"] = by_section

    # Critical-section rule: rankings or tasks FAIL is itself a FAIL signal
    # to the overall health rollup, regardless of the audit's own overall.
    critical_fail = rankings_status == "FAIL" or tasks_status == "FAIL"
    section["metrics"]["critical_section_fail"] = critical_fail
    section["checks"].append({
        "name": "data_quality_overall",
        "status": overall,
        "message": f"overall={overall}; sections=" + ", ".join(
            f"{k}:{v}" for k, v in by_section.items()) or f"overall={overall}",
    })
    if critical_fail:
        section["checks"].append({
            "name": "critical_sections",
            "status": "FAIL",
            "message": (
                f"rankings={rankings_status}, tasks={tasks_status}: "
                "critical section failure"),
        })
    section["status"] = max((c["status"] for c in section["checks"]),
                            key=lambda s: LEVEL_RANK[s])
    return section


def analyze_schedule_reliability(sr_rep: dict | None) -> dict:
    """Roll up schedule_reliability.json with recovery awareness.

    The schedule_reliability report itself now exposes both `overall`
    (raw, history-aware) and `overall_effective` (current operational
    state — FAIL downgraded to WARN when today's slot is satisfied via
    watchdog/manual rescue and live data is fresh). This function prefers
    the report's own `overall_effective` when present and falls back to
    the inline recovery heuristic for older JSONs that pre-date that
    field.
    """
    section = {"checks": [], "metrics": {}, "status": "WARN"}
    if not isinstance(sr_rep, dict):
        section["checks"].append({
            "name": "schedule_reliability_present",
            "status": "WARN",
            "message": "schedule_reliability.json missing",
        })
        return section
    overall = (sr_rep.get("overall") or "OK").upper()
    raw = overall
    section["metrics"]["overall_raw"] = raw

    sections = sr_rep.get("sections") or {}
    cal = (sections.get("calendar") or {}).get("metrics", {}).get("calendar") or {}
    rows = cal.get("rows") or []
    missing_count = cal.get("missing_count", 0)
    section["metrics"]["missing_count"] = missing_count
    section["metrics"]["lookback_days"] = cal.get("lookback_days")

    # Today's coverage: any rows for today's chicago_date with no missing slots
    chi_today = _to_chicago(_now_utc()).date().strftime("%Y-%m-%d")
    today_row = next((r for r in rows if r.get("date") == chi_today), None)
    today_missing = list((today_row or {}).get("missing") or [])
    section["metrics"]["today_missing"] = today_missing
    section["metrics"]["today_satisfied"] = today_row is not None and not today_missing

    # Recency / last_run
    rec = sections.get("recency") or {}
    last_run = (rec.get("metrics") or {}).get("last_run") or {}
    section["metrics"]["last_run_event"] = last_run.get("event_name")
    section["metrics"]["last_run_slot"] = last_run.get("slot")
    section["metrics"]["last_run_ts_chicago"] = last_run.get("ts_chicago")

    # Prefer the schedule report's own effective overall when it provides
    # one. Falls back to the legacy heuristic for backward compatibility.
    report_effective = sr_rep.get("overall_effective")
    if isinstance(report_effective, str) and report_effective.upper() in LEVEL_RANK:
        effective = report_effective.upper()
        recovered = raw == "FAIL" and effective != "FAIL"
        if effective == "OK":
            msg = "schedule reliability OK"
        elif effective == "WARN" and raw == "FAIL":
            msg = (f"schedule reliability FAIL/recovered: today satisfied, "
                   f"{missing_count} missing in lookback")
        elif effective == "WARN":
            msg = "schedule reliability WARN"
        else:
            msg = (f"schedule reliability FAIL: today_missing={today_missing}, "
                   f"history_missing={missing_count}")
    else:
        recovered = (
            raw == "FAIL"
            and section["metrics"]["today_satisfied"]
            and (last_run.get("event_name") == "workflow_dispatch"
                 or missing_count > 0)  # historical misses only
        )
        if raw == "OK":
            effective = "OK"
            msg = "schedule reliability OK"
        elif raw == "WARN":
            effective = "WARN"
            msg = "schedule reliability WARN"
        elif recovered:
            effective = "WARN"
            msg = (f"schedule reliability FAIL/recovered: today satisfied, "
                   f"{missing_count} missing in lookback")
        else:
            effective = "FAIL"
            msg = (f"schedule reliability FAIL: today_missing={today_missing}, "
                   f"history_missing={missing_count}")

    section["metrics"]["recovered"] = recovered
    section["metrics"]["overall_effective"] = effective
    section["checks"].append({
        "name": "schedule_reliability",
        "status": effective,
        "message": msg,
    })
    section["status"] = effective
    return section


def analyze_watchlist(watchlist: dict | None) -> dict:
    """SUPP coverage and yfinance cache stats from watchlist source_meta."""
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(watchlist, dict):
        section["checks"].append({
            "name": "watchlist_present",
            "status": "WARN",
            "message": "watchlist_rankings.json missing",
        })
        section["status"] = "WARN"
        return section
    sm = watchlist.get("source_meta") or {}
    supp_summary = sm.get("supp_summary") or {}
    yf_cache = sm.get("yfinance_info_cache") or {}
    section["metrics"]["supp_summary"] = supp_summary
    section["metrics"]["yfinance_info_cache"] = yf_cache
    section["metrics"]["unavailable_count"] = sm.get("unavailable_count", 0)
    section["metrics"]["scored"] = sm.get("scored")

    supp_total = supp_summary.get("total") or 0
    full = supp_summary.get("full_fundamentals") or 0
    price_only = supp_summary.get("price_only") or 0
    technical_only = supp_summary.get("technical_only") or 0

    if supp_total == 0:
        section["checks"].append({
            "name": "supp_coverage",
            "status": "WARN",
            "message": "no SUPP rows scored in watchlist",
        })
    else:
        # Coverage warning if more than half of SUPP is degraded
        degraded = price_only + technical_only
        degraded_pct = (degraded / supp_total) if supp_total else 0
        section["metrics"]["supp_degraded_pct"] = round(degraded_pct, 3)
        if degraded_pct >= 0.5:
            section["checks"].append({
                "name": "supp_coverage",
                "status": "WARN",
                "message": (f"SUPP degraded {degraded}/{supp_total} "
                            f"({degraded_pct:.0%}); full={full}"),
            })
        else:
            section["checks"].append({
                "name": "supp_coverage",
                "status": "OK",
                "message": (f"SUPP {full}/{supp_total} full fundamentals; "
                            f"price_only={price_only} tech_only={technical_only}"),
            })

    if yf_cache:
        misses = yf_cache.get("cache_miss", 0)
        hits = yf_cache.get("cache_hit_fresh", 0)
        section["checks"].append({
            "name": "yfinance_cache",
            "status": "OK",
            "message": f"yfinance cache hits={hits} misses={misses}",
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]),
        key=lambda s: LEVEL_RANK[s], default="OK")
    return section


def analyze_benchmark(bench: dict | None) -> dict:
    """Snapshot availability and forward-result presence."""
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(bench, dict):
        section["checks"].append({
            "name": "benchmark_present",
            "status": "WARN",
            "message": "benchmark_review.json missing",
        })
        section["status"] = "WARN"
        return section
    snaps = bench.get("snapshots_kept") or 0
    summary = bench.get("snapshot_summary") or {}
    horizons = summary.get("horizons") or {}
    section["metrics"]["snapshots_kept"] = snaps
    section["metrics"]["horizons"] = list(horizons.keys())
    completed_any = any(
        (h.get("completed") or 0) > 0 for h in horizons.values()
        if isinstance(h, dict)
    )
    section["metrics"]["any_horizon_completed"] = completed_any
    findings = bench.get("findings") or []
    findings_warn = sum(1 for f in findings if (f.get("status") or "").upper() == "WARN")
    findings_fail = sum(1 for f in findings if (f.get("status") or "").upper() == "FAIL")
    section["metrics"]["findings_warn"] = findings_warn
    section["metrics"]["findings_fail"] = findings_fail

    if snaps == 0:
        section["checks"].append({
            "name": "benchmark_snapshots",
            "status": "WARN",
            "message": "no snapshots present yet",
        })
    elif not completed_any:
        section["checks"].append({
            "name": "benchmark_snapshots",
            "status": "OK",
            "message": f"{snaps} snapshot(s) kept; horizons pending forward returns",
        })
    else:
        section["checks"].append({
            "name": "benchmark_snapshots",
            "status": "OK",
            "message": f"{snaps} snapshot(s); ≥1 horizon has forward results",
        })
    if findings_fail:
        section["checks"].append({
            "name": "benchmark_findings",
            "status": "WARN",  # benchmark findings are advisory; never FAIL the rollup
            "message": f"{findings_fail} FAIL finding(s) in benchmark review",
        })
    section["status"] = max(
        (c["status"] for c in section["checks"]),
        key=lambda s: LEVEL_RANK[s], default="OK")
    return section


def analyze_parity(par: dict | None) -> dict:
    """Scoring parity overall and key blockers.

    When the parity report flags `low_risk_bias_known=True` and the only
    cross-group FAIL fields are the known selection-bias rows
    (already demoted to WARN inside parity), the rollup message reads as
    "low_risk drift explained by selection bias" rather than "parity
    blocker fields=['low_risk']" — the underlying drift number is preserved
    in the parity report itself.
    """
    section = {"checks": [], "metrics": {}, "status": "OK"}
    if not isinstance(par, dict):
        section["checks"].append({
            "name": "parity_present",
            "status": "WARN",
            "message": "scoring_parity_review.json missing",
        })
        section["status"] = "WARN"
        return section
    overall = (par.get("overall") or "OK").upper()
    section["metrics"]["overall"] = overall
    cgp = par.get("cross_group_parity") or {}
    section["metrics"]["cross_group_parity_status"] = (cgp.get("status") or "OK").upper()
    by_field = cgp.get("by_field") or {}
    blockers = [
        k for k, v in by_field.items()
        if isinstance(v, dict) and (v.get("status") or "").upper() == "FAIL"
    ]
    # Fields whose FAIL has been demoted to WARN under known_bias=True (these
    # are explained, not active blockers — surface separately).
    known_bias_fields = [
        k for k, v in by_field.items()
        if isinstance(v, dict) and v.get("known_bias")
    ]
    section["metrics"]["fail_fields"] = blockers
    section["metrics"]["known_bias_fields"] = known_bias_fields
    section["metrics"]["low_risk_bias_known"] = bool(
        par.get("low_risk_bias_known")
    )

    # Parity FAIL is treated as WARN by the health rollup — it's an advisory,
    # not a stop-the-line condition (most parity FAILs are by-design SUPP gaps).
    if overall == "FAIL":
        if blockers:
            msg = f"parity overall=FAIL; blocker fields={blockers}"
        elif known_bias_fields:
            msg = (f"parity overall=FAIL; only remaining drift is "
                   f"{known_bias_fields} explained by selection bias")
        else:
            msg = "parity overall=FAIL"
        section["checks"].append({
            "name": "scoring_parity",
            "status": "WARN",
            "message": msg,
        })
    elif overall == "WARN":
        if known_bias_fields and not blockers:
            msg = (f"parity overall=WARN; "
                   f"low_risk drift explained by selection bias "
                   f"({known_bias_fields})")
        else:
            msg = "parity overall=WARN"
        section["checks"].append({
            "name": "scoring_parity",
            "status": "WARN",
            "message": msg,
        })
    else:
        section["checks"].append({
            "name": "scoring_parity",
            "status": "OK",
            "message": "parity overall=OK",
        })
    section["status"] = section["checks"][0]["status"]
    return section


# ----------------- rollup -----------------


def compute_overall(sections: dict) -> str:
    """Combine section statuses with the documented health logic.

    Freshness FAIL or DQ critical-section FAIL forces FAIL outright.
    Otherwise, the worst section status (after each section's own
    downgrades, e.g. recovered schedule -> WARN) wins.
    """
    fresh = sections["data_freshness"]["status"]
    dq = sections["data_quality"]
    if fresh == "FAIL":
        return "FAIL"
    if dq.get("metrics", {}).get("critical_section_fail"):
        return "FAIL"
    overall = "OK"
    for sec in sections.values():
        overall = _worst(overall, sec.get("status", "OK"))
    return overall


def collect_action_items(sections: dict) -> list[str]:
    """Top warnings/failures across sections, deduped, in priority order."""
    items: list[str] = []
    for sec_name, sec in sections.items():
        for c in sec.get("checks", []):
            level = (c.get("status") or "").upper()
            if level in ("WARN", "FAIL"):
                items.append(f"[{level}] {sec_name}.{c.get('name')}: {c.get('message')}")
    # Stable: FAILs first, then WARNs, otherwise input order
    items.sort(key=lambda s: 0 if s.startswith("[FAIL]") else 1)
    return items


def build_summary(report: dict) -> str:
    """Concise one-liner safe for the tasks table.

    Example: 'Fresh 12:32 PM CT · Data quality OK · Watchlist OK · Schedule FAIL/recovered · Benchmark active'
    """
    s = report["sections"]
    fresh = s["data_freshness"]
    fresh_metrics = fresh.get("metrics", {})
    as_of = fresh_metrics.get("as_of")
    fresh_label = _short_time(as_of) if fresh["status"] != "FAIL" else "STALE"
    dq_status = s["data_quality"]["metrics"].get("overall", "?")
    wl = s["watchlist"]["status"]
    sched = s["schedule_reliability"]
    sched_raw = sched["metrics"].get("overall_raw", "?")
    sched_eff = sched["metrics"].get("overall_effective", sched_raw)
    if sched_raw == "FAIL" and sched_eff != "FAIL":
        sched_label = "FAIL/recovered"
    else:
        sched_label = sched_eff
    bench = s["benchmark"]
    bench_label = "active" if bench["metrics"].get("snapshots_kept", 0) > 0 else "missing"
    parts = [
        f"Fresh {fresh_label}",
        f"Data quality {dq_status}",
        f"Watchlist {wl}",
        f"Schedule {sched_label}",
        f"Benchmark {bench_label}",
    ]
    return " · ".join(parts)


# ----------------- output -----------------


def _render_html(report: dict) -> str:
    overall = report["overall"]
    color = {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}[overall]
    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Midday Health Check</title>
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
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>Midday Health Check</h1>
<p class="meta">Generated {escape(report.get("generated_at_chicago",""))} &middot; Overall: <span class="badge">{escape(overall)}</span></p>
<div class="summary"><strong>Summary:</strong> {escape(report.get("summary",""))}</div>
""")

    actions = report.get("action_items") or []
    if actions:
        parts.append('<div class="section"><h2>Action Items</h2><ul class="action">')
        for a in actions:
            parts.append(f"<li>{escape(a)}</li>")
        parts.append('</ul></div>')

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
                + '</pre></div>'
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
        parts.append('</tbody></table></div>')

    parts.append('</body></html>')
    return "\n".join(parts)


# ----------------- core -----------------


def build_report() -> dict:
    rankings = _load_json(RANKINGS_FILE)
    watchlist = _load_json(WATCHLIST_FILE)
    dq = _load_json(DATA_QUALITY_FILE)
    sr_rep = _load_json(SCHEDULE_RELIABILITY_FILE)
    bench = _load_json(BENCHMARK_FILE)
    par = _load_json(PARITY_FILE)

    sections = {
        "data_freshness": analyze_data_freshness(rankings),
        "data_quality": analyze_data_quality(dq),
        "schedule_reliability": analyze_schedule_reliability(sr_rep),
        "watchlist": analyze_watchlist(watchlist),
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
            "benchmark_present": bench is not None,
            "parity_present": par is not None,
        },
    }
    report["action_items"] = collect_action_items(sections)
    report["summary"] = build_summary(report)
    return report


def _stamp_task(report: dict) -> None:
    """Update tasks.json row id=midday-health-check."""
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
    summary = report.get("summary") or "Midday health check"
    changed = False
    for row in tasks:
        if isinstance(row, dict) and row.get("id") == "midday-health-check":
            row["last_run"] = last_run
            row["status"] = status
            row["summary"] = summary
            row["report_url"] = REPORT_URL
            # next_run intentionally left as "—" — runs every refresh, so
            # there is no single "next" slot worth advertising here.
            row["next_run"] = "—"
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
    print(f"[midday_health_check] overall={report['overall']} -> {JSON_OUTPUT}")
    print(f"[midday_health_check] summary: {report['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
