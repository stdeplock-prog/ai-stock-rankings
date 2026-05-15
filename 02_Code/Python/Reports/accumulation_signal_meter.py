"""Accumulation Signal Meter v1 (diagnostic).

A lightweight, deterministic, per-ticker "accumulation" score built on
metrics already produced by Pine Go/No-Go diagnostic. Aims to surface
names that are quietly being accumulated (rising MFI, in-zone RSI, near
20d high, healthy rel-vol, strong bar, close near upper band but not
overextended) without changing any production scoring.

This report is **diagnostic only**: it does not feed the live `ai_score`
or rank. It produces a top/bottom list and the overlap with Pine's
clean-go names and the activity-adjusted top-25 so the reviewer can spot
where multiple diagnostics agree.

Inputs (read-only, no network):
  - data/reports/pine_go_no_go_diagnostic.json   (required)
  - data/rankings.json                            (optional, for sector/rank)
  - data/reports/activity_adjusted_review.json    (optional, for overlap)

Outputs:
  - data/reports/accumulation_signal_meter.json
  - reports/accumulation-signal-meter.html
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"
HTML_REPORTS_DIR = REPO_ROOT / "reports"

PINE_FILE = DATA_REPORTS_DIR / "pine_go_no_go_diagnostic.json"
RANKINGS_FILE = DATA_DIR / "rankings.json"
ACTIVITY_FILE = DATA_REPORTS_DIR / "activity_adjusted_review.json"

JSON_OUT = DATA_REPORTS_DIR / "accumulation_signal_meter.json"
HTML_OUT = HTML_REPORTS_DIR / "accumulation-signal-meter.html"
TASKS_FILE = DATA_DIR / "tasks.json"
TASK_ID = "accumulation-signal-meter"
REPORT_URL = "./reports/accumulation-signal-meter.html"

# Component weights (sum to 1.0 over *available* components — missing
# components are dropped and remaining weights renormalized so the score
# never penalizes a ticker just for missing one input).
COMPONENT_WEIGHTS = {
    "relvol": 0.22,
    "mfi": 0.18,
    "bar_strength": 0.15,
    "near_20d_high": 0.15,
    "rsi_zone": 0.15,
    "close_location": 0.15,
}

# Thresholds — picked so each component returns a value in [0, 1].
# All bounded; no constant references a sector or specific ticker.
RELVOL_FLOOR = 0.8   # below this -> 0
RELVOL_FULL = 1.5    # at/above -> 1.0; linear in between
MFI_FLOOR = 50.0
MFI_FULL = 80.0
RSI_LOW = 55.0
RSI_HIGH = 70.0
RSI_BUFFER = 5.0     # taper inside last 5 pts of either edge


def _safe_float(v, default=None):
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _ramp(v, lo, hi):
    """Linear ramp from 0 at `lo` to 1 at `hi`. Clamped."""
    if v is None or hi == lo:
        return None
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def relvol_component(rel_vol_20d):
    v = _safe_float(rel_vol_20d)
    return _ramp(v, RELVOL_FLOOR, RELVOL_FULL)


def mfi_component(mfi14):
    v = _safe_float(mfi14)
    return _ramp(v, MFI_FLOOR, MFI_FULL)


def rsi_zone_component(rsi14):
    """Reward RSI inside the accumulation zone [RSI_LOW, RSI_HIGH].
    Tapered at the edges so RSI just inside the band scores < 1.0."""
    v = _safe_float(rsi14)
    if v is None:
        return None
    if v < RSI_LOW - RSI_BUFFER or v > RSI_HIGH + RSI_BUFFER:
        return 0.0
    if v < RSI_LOW:
        return max(0.0, (v - (RSI_LOW - RSI_BUFFER)) / RSI_BUFFER) * 0.5
    if v > RSI_HIGH:
        return max(0.0, ((RSI_HIGH + RSI_BUFFER) - v) / RSI_BUFFER) * 0.5
    # Centered: full credit at midpoint, taper toward edges to keep extremes < 1
    mid = (RSI_LOW + RSI_HIGH) / 2.0
    half = (RSI_HIGH - RSI_LOW) / 2.0
    return max(0.0, 1.0 - abs(v - mid) / max(half, 1e-9) * 0.2)


def close_location_component(last_close, sma20, bb_upper):
    """Where in the upper half of the BB is the close? 0 at SMA20, 1 at
    BB upper. Above BB upper -> penalty (mild overextension)."""
    lc = _safe_float(last_close)
    s = _safe_float(sma20)
    bu = _safe_float(bb_upper)
    if lc is None or s is None or bu is None or bu <= s:
        return None
    span = bu - s
    pos = (lc - s) / span
    if pos < 0:
        return 0.0
    if pos <= 1.0:
        return pos
    # Above BB upper -> taper to 0 over one extra band width.
    over = pos - 1.0
    return max(0.0, 1.0 - over)


def bool_component(value):
    if value is None:
        return None
    return 1.0 if bool(value) else 0.0


def compute_components(entry):
    """Return dict of component_name -> score in [0,1] or None if missing."""
    metrics = (entry or {}).get("metrics") or {}
    gates = (entry or {}).get("gates") or {}
    return {
        "relvol": relvol_component(metrics.get("rel_vol_20d")),
        "mfi": mfi_component(metrics.get("mfi14")),
        "bar_strength": bool_component(gates.get("bar_strength")),
        "near_20d_high": bool_component(gates.get("near_20d_high")),
        "rsi_zone": rsi_zone_component(metrics.get("rsi14")),
        "close_location": close_location_component(
            metrics.get("last_close"),
            metrics.get("sma20"),
            metrics.get("bb_upper_20"),
        ),
    }


def score_components(components):
    """Weighted average across *available* components; renormalize weights
    over what was provided so missing components do not penalize.
    Returns (score_0to10, n_components, missing_components)."""
    available = {k: v for k, v in components.items() if v is not None}
    missing = [k for k, v in components.items() if v is None]
    if not available:
        return 0.0, 0, missing
    total_weight = sum(COMPONENT_WEIGHTS[k] for k in available)
    if total_weight <= 0:
        return 0.0, 0, missing
    weighted = sum(COMPONENT_WEIGHTS[k] * v for k, v in available.items())
    score = (weighted / total_weight) * 10.0
    return round(score, 2), len(available), missing


def _verdict(rows):
    if not rows:
        return "FAIL", "no Pine per-ticker rows available"
    n_strong = sum(1 for r in rows if r["score"] >= 7.0)
    n_weak = sum(1 for r in rows if r["score"] < 3.0)
    if n_strong == 0:
        return "INFO", f"no strong accumulation signals (>=7); {n_weak} weak (<3)"
    return "OK", f"{n_strong} strong accumulation signals; {n_weak} weak (<3)"


# ---------- payload assembly ----------


def build_rows(pine_data, rankings_data):
    per_ticker = (pine_data or {}).get("per_ticker") or []
    rank_lookup = {}
    sector_lookup = {}
    if rankings_data:
        for r in rankings_data.get("rows") or []:
            t = r.get("ticker")
            if not t:
                continue
            rank_lookup[t] = r.get("rank")
            sector_lookup[t] = r.get("sector")

    rows = []
    for entry in per_ticker:
        if not isinstance(entry, dict):
            continue
        ticker = entry.get("ticker")
        if not ticker:
            continue
        if not entry.get("evaluated", True):
            # Skip ohlcv-missing / insufficient-bars rows — they would all
            # score 0 and clutter the report.
            continue
        components = compute_components(entry)
        score, n_components, missing = score_components(components)
        rows.append({
            "ticker": ticker,
            "company": entry.get("company"),
            "sector": sector_lookup.get(ticker) or entry.get("sector"),
            "ai_rank": rank_lookup.get(ticker),
            "score": score,
            "components": {k: (round(v, 3) if v is not None else None)
                           for k, v in components.items()},
            "n_components_present": n_components,
            "missing_components": missing,
            "pine_go_norm": entry.get("go_no_go_score_normalized"),
            "blockers": list(entry.get("blockers") or []),
        })
    rows.sort(key=lambda r: (-r["score"], r["ticker"]))
    return rows


def build_overlaps(rows, pine_data, activity_data):
    top_n = 25
    top_accum = {r["ticker"] for r in rows[:top_n]}
    overlaps = {"top_n": top_n}

    pine_clean = []
    if isinstance(pine_data, dict):
        highlights = pine_data.get("highlights") or {}
        pine_clean = [
            e.get("ticker") for e in (highlights.get("cleanest_go_main") or [])
            if isinstance(e, dict) and e.get("ticker")
        ]
    pine_clean_set = set(pine_clean)
    overlaps["pine_clean_go"] = {
        "n_pine_clean_go": len(pine_clean_set),
        "overlap_with_top_accum": sorted(top_accum & pine_clean_set),
    }

    activity_top = []
    if isinstance(activity_data, dict):
        rows_a = activity_data.get("rows") or []
        # rows are pre-sorted by activity_rank ascending
        activity_top = [r.get("ticker") for r in rows_a[:top_n] if r.get("ticker")]
    activity_top_set = set(activity_top)
    overlaps["activity_top_25"] = {
        "n": len(activity_top_set),
        "overlap_with_top_accum": sorted(top_accum & activity_top_set),
    }
    return overlaps


# ---------- HTML ----------


STYLE = (
    "body{font-family:-apple-system,Helvetica,Arial,sans-serif;"
    "margin:24px;color:#1f2937;max-width:1180px;}"
    "h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:18px 0 6px}"
    "h3{font-size:14px;margin:12px 0 4px}"
    "table{border-collapse:collapse;font-size:12px;margin-top:8px;width:100%}"
    "th,td{border:1px solid #e5e7eb;padding:4px 8px;text-align:right;}"
    "th{background:#f3f4f6}td.t,th.t{text-align:left}"
    ".muted{color:#6b7280}.bar{display:inline-block;height:8px;background:#10b981;"
    "border-radius:4px;vertical-align:middle;margin-right:6px}"
    ".meter{display:inline-block;width:90px;height:8px;background:#e5e7eb;"
    "border-radius:4px;overflow:hidden;vertical-align:middle;margin-right:6px}"
    ".meter > span{display:block;height:100%;background:#10b981}"
    ".weak .meter > span{background:#ef4444}"
    ".mid .meter > span{background:#f59e0b}"
    ".caveat{background:#fff6e0;border:1px solid #f0d49a;color:#8a5a00;"
    "padding:8px 10px;border-radius:6px;margin:10px 0;font-size:13px}"
    ".back{font-size:13px;margin-bottom:10px}"
)


def _meter_html(score):
    pct = max(0, min(100, int(round(score * 10))))
    cls = "weak" if score < 3 else ("mid" if score < 7 else "")
    return (
        f"<span class='{cls}'><span class='meter'>"
        f"<span style='width:{pct}%'></span></span>"
        f"<b>{score:.2f}</b></span>"
    )


def _render_table(rows, *, n=25, weak=False):
    if not rows:
        return "<p class=muted>None.</p>"
    out = [
        "<table><thead><tr>"
        "<th>#</th><th class=t>Ticker</th><th class=t>Sector</th>"
        "<th>AI rank</th><th>Score</th>"
        "<th>rv</th><th>mfi</th><th>bar</th><th>20dH</th><th>rsi</th><th>loc</th>"
        "<th>Pine</th><th class=t>Missing</th>"
        "</tr></thead><tbody>"
    ]
    for i, r in enumerate(rows[:n], 1):
        c = r["components"] or {}
        def _cell(v):
            return "—" if v is None else f"{v:.2f}"
        out.append(
            f"<tr><td>{i}</td>"
            f"<td class=t><b>{escape(str(r['ticker'] or ''))}</b></td>"
            f"<td class=t>{escape(str(r.get('sector') or ''))}</td>"
            f"<td>{r.get('ai_rank') if r.get('ai_rank') is not None else '—'}</td>"
            f"<td class=t>{_meter_html(r['score'])}</td>"
            f"<td>{_cell(c.get('relvol'))}</td>"
            f"<td>{_cell(c.get('mfi'))}</td>"
            f"<td>{_cell(c.get('bar_strength'))}</td>"
            f"<td>{_cell(c.get('near_20d_high'))}</td>"
            f"<td>{_cell(c.get('rsi_zone'))}</td>"
            f"<td>{_cell(c.get('close_location'))}</td>"
            f"<td>{r.get('pine_go_norm') if r.get('pine_go_norm') is not None else '—'}</td>"
            f"<td class=t>{escape(','.join(r.get('missing_components') or []))}</td>"
            "</tr>"
        )
    out.append("</tbody></table>")
    return "".join(out)


def render_html(payload):
    rows = payload.get("rows") or []
    overlaps = payload.get("overlaps") or {}
    top_n = (overlaps.get("top_n") or 25)
    strong = rows[:top_n]
    weak = [r for r in reversed(rows) if r["score"] < 3.0][:top_n]
    overlap_pine = overlaps.get("pine_clean_go", {})
    overlap_activity = overlaps.get("activity_top_25", {})
    parts = [
        f"<!doctype html><meta charset=utf-8><title>Accumulation Signal Meter</title>"
        f"<style>{STYLE}</style>",
        "<p class='back'><a href='../index.html'>&larr; Back to dashboard</a></p>",
        "<h1>Accumulation Signal Meter (v1)</h1>",
        f"<p class=muted>Generated {escape(payload.get('generated_at',''))} "
        f"&middot; verdict <b>{escape(payload.get('verdict',''))}</b> "
        f"&middot; {escape(payload.get('note',''))}.</p>",
        "<div class='caveat'><b>Diagnostic only.</b> This report does not "
        "modify the live <code>ai_score</code> or rank. Use it as a "
        "second-opinion filter alongside Pine clean-go and activity-adjusted.</div>",
        f"<h2>Top {top_n} accumulation candidates</h2>",
        _render_table(strong, n=top_n),
        f"<h2>Weak accumulation ({len(weak)} with score &lt; 3.0)</h2>",
        _render_table(weak, n=top_n, weak=True),
        "<h2>Overlap with other diagnostics</h2>",
        "<h3>Pine cleanest-go (main board)</h3>",
        (f"<p class=muted>{len(overlap_pine.get('overlap_with_top_accum') or [])} "
         f"of top-{top_n} accumulation overlap with "
         f"{overlap_pine.get('n_pine_clean_go', 0)} Pine clean-go names: "
         + ", ".join(escape(t) for t in (overlap_pine.get("overlap_with_top_accum") or [])) + ".</p>"),
        "<h3>Activity-adjusted top 25</h3>",
        (f"<p class=muted>{len(overlap_activity.get('overlap_with_top_accum') or [])} "
         f"of top-{top_n} accumulation overlap with activity-adjusted top 25: "
         + ", ".join(escape(t) for t in (overlap_activity.get("overlap_with_top_accum") or [])) + ".</p>"),
        "<h2>Components</h2>",
        "<ul>"
        "<li><b>relvol</b>: rel_vol_20d ramped from "
        f"{RELVOL_FLOOR} to {RELVOL_FULL}.</li>"
        f"<li><b>mfi</b>: MFI14 ramped from {MFI_FLOOR} to {MFI_FULL}.</li>"
        "<li><b>bar_strength</b>: Pine bar_strength gate.</li>"
        "<li><b>near_20d_high</b>: Pine near_20d_high gate.</li>"
        f"<li><b>rsi_zone</b>: in-zone preference around "
        f"[{RSI_LOW},{RSI_HIGH}].</li>"
        "<li><b>close_location</b>: where last close sits between SMA20 and "
        "BB upper; mild taper above BB upper.</li>"
        "</ul>",
    ]
    return "".join(parts)


# ---------- tasks.json ----------


def _stamp_task(payload):
    if not TASKS_FILE.exists():
        return
    try:
        from _tasks_meta import update_task  # type: ignore
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        try:
            from _tasks_meta import update_task  # type: ignore
        except ImportError:
            return
    verdict = (payload.get("verdict") or "INFO").upper()
    status = "FAIL" if verdict == "FAIL" else ("warn" if verdict == "WARN" else "OK")
    update_task(TASKS_FILE, TASK_ID,
                status=status,
                summary=payload.get("note", ""),
                report_url=REPORT_URL)


def _ensure_task_row():
    if not TASKS_FILE.exists():
        return
    try:
        data = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return
    if any(isinstance(t, dict) and t.get("id") == TASK_ID for t in tasks):
        return
    tasks.append({
        "id": TASK_ID,
        "name": "Accumulation Signal Meter",
        "schedule": "Every refresh (08:45/12:30/15:35 CT, weekdays)",
        "last_run": "—",
        "next_run": "—",
        "status": "Not Run",
        "summary": "—",
        "report_url": REPORT_URL,
    })
    TASKS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------- entry ----------


def main():
    if not PINE_FILE.exists():
        print(f"missing pine file: {PINE_FILE}", file=sys.stderr)
        return 1
    pine_data = json.loads(PINE_FILE.read_text())
    rankings_data = None
    if RANKINGS_FILE.exists():
        try:
            rankings_data = json.loads(RANKINGS_FILE.read_text())
        except Exception as exc:
            print(f"warning: could not load rankings: {exc}", file=sys.stderr)
    activity_data = None
    if ACTIVITY_FILE.exists():
        try:
            activity_data = json.loads(ACTIVITY_FILE.read_text())
        except Exception as exc:
            print(f"warning: could not load activity-adjusted: {exc}",
                  file=sys.stderr)

    rows = build_rows(pine_data, rankings_data)
    verdict, note = _verdict(rows)
    overlaps = build_overlaps(rows, pine_data, activity_data)
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "verdict": verdict,
        "note": note,
        "overall": verdict,
        "summary": (
            f"n={len(rows)} top@7={sum(1 for r in rows if r['score']>=7)} "
            f"weak@3={sum(1 for r in rows if r['score']<3)}"
        ),
        "constants": {
            "component_weights": COMPONENT_WEIGHTS,
            "relvol_floor": RELVOL_FLOOR,
            "relvol_full": RELVOL_FULL,
            "mfi_floor": MFI_FLOOR,
            "mfi_full": MFI_FULL,
            "rsi_low": RSI_LOW,
            "rsi_high": RSI_HIGH,
            "rsi_buffer": RSI_BUFFER,
        },
        "overlaps": overlaps,
        "rows": rows,
    }

    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(payload, indent=2))
    HTML_OUT.write_text(render_html(payload))
    _ensure_task_row()
    _stamp_task(payload)
    print(f"wrote {JSON_OUT}")
    print(f"wrote {HTML_OUT}")
    print(f"verdict: {verdict} - {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
