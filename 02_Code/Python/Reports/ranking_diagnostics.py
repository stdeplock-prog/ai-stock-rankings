"""Ranking Diagnostics — explain *why* current top-ranked names rank
highly, flag suspicious top ranks, and run diagnostic-only alternate
weighting sensitivity analyses.

This is a read-only, live-generated site report — distinct from any
planning workbook. It does NOT change scoring formulas. It only re-reads
existing fields from the published rankings/watchlist artifacts and the
sibling diagnostic reports, then renders an explanation layer on top.

Inputs (read-only, no network):
  - data/rankings.json
  - data/watchlist_rankings.json
  - data/reports/benchmark_review.json
  - data/reports/scoring_parity_review.json
  - data/reports/low_risk_drift_review.json
  - data/reports/data_quality_audit.json

Outputs:
  - data/reports/ranking_diagnostics.json   (machine-readable)
  - reports/ranking-diagnostics.html        (human-readable)

Verdict levels: OK / WARN / FAIL.
  * OK   — no severe suspicious ranks; data quality OK.
  * WARN — suspicious ranks present, sector crowding, or technical-only
           top names. Action items surfaced for manual review.
  * FAIL — top leaders driven by missing/stale data, or a critical
           upstream data-quality FAIL.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"
HTML_REPORTS_DIR = REPO_ROOT / "reports"

RANKINGS_FILE = DATA_DIR / "rankings.json"
WATCHLIST_FILE = DATA_DIR / "watchlist_rankings.json"
BENCHMARK_REVIEW_FILE = DATA_REPORTS_DIR / "benchmark_review.json"
SCORING_PARITY_FILE = DATA_REPORTS_DIR / "scoring_parity_review.json"
LOW_RISK_DRIFT_FILE = DATA_REPORTS_DIR / "low_risk_drift_review.json"
DATA_QUALITY_FILE = DATA_REPORTS_DIR / "data_quality_audit.json"

JSON_OUTPUT = DATA_REPORTS_DIR / "ranking_diagnostics.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "ranking-diagnostics.html"
TASKS_FILE = DATA_DIR / "tasks.json"

LEVEL_RANK = {"OK": 0, "WARN": 1, "FAIL": 2}

# Component-score thresholds. Scores live on a 0-10 scale.
HIGH_THRESHOLD = 8.0      # "primary driver" cutoff
WEAK_THRESHOLD = 5.0      # "weak spot" cutoff
NEG_MOV_TRIGGER = -1.0    # "negative MOV" trigger (in % of last close, see below)

# Sector-crowding thresholds against the top-10 cohort.
SECTOR_CROWD_WARN_PCT = 0.30
SECTOR_CROWD_FAIL_PCT = 0.50


# ---------- IO ----------


def _load_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return None


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _worst(a: str, b: str) -> str:
    return a if LEVEL_RANK[a] >= LEVEL_RANK[b] else b


def _is_numeric(v) -> bool:
    return isinstance(v, (int, float)) and not (isinstance(v, float) and v != v)


def _safe_num(v):
    return float(v) if _is_numeric(v) else None


# ---------- MOV (recent close-window momentum proxy) ----------


def compute_mov_pct(closes) -> float | None:
    """Recent-window momentum proxy: percent change from first to last
    close in the row's `closes` list. Returns None if insufficient data.

    The published row schemas don't include an explicit "MOV" score, but
    every row that has price history carries a `closes` list. Using the
    full window lets us spot deteriorating leaders without inventing new
    fields. Result is a fraction (e.g. 0.05 = +5%).
    """
    if not isinstance(closes, list) or len(closes) < 2:
        return None
    first, last = closes[0], closes[-1]
    if not (_is_numeric(first) and _is_numeric(last)) or float(first) == 0.0:
        return None
    return round((float(last) - float(first)) / float(first), 4)


# ---------- Driver / weak-spot detection ----------


COMPONENT_FIELDS = ("fundamental", "technical", "sentiment", "low_risk", "swing_score")
COMPONENT_LABELS = {
    "fundamental": "FUND",
    "technical": "TECH",
    "sentiment": "SENT",
    "low_risk": "LOW_RISK",
    "swing_score": "SWING",
}


def detect_drivers(row: dict) -> list:
    """Return component labels that are strong (>= HIGH_THRESHOLD) on this row.

    Order: highest score first; ties broken by component order.
    """
    items = []
    for f in COMPONENT_FIELDS:
        v = _safe_num(row.get(f))
        if v is not None and v >= HIGH_THRESHOLD:
            items.append((COMPONENT_LABELS[f], v))
    items.sort(key=lambda x: (-x[1],))
    return [name for name, _ in items]


def detect_weak_spots(row: dict, mov_pct: float | None) -> list:
    """Return short human-readable weak-spot tags for a top-ranked row.

    These are not formula errors — they are annotations explaining why a
    top rank may be fragile. Empty list means "no weak spots flagged".
    """
    spots: list = []
    for f in COMPONENT_FIELDS:
        v = _safe_num(row.get(f))
        if v is None:
            spots.append(f"missing {COMPONENT_LABELS[f]}")
        elif v <= WEAK_THRESHOLD:
            spots.append(f"low {COMPONENT_LABELS[f]} ({v:.1f})")
    if mov_pct is not None and mov_pct * 100.0 <= NEG_MOV_TRIGGER:
        spots.append(f"negative MOV ({mov_pct * 100.0:+.1f}%)")
    basis = row.get("ai_score_basis")
    if basis == "supp_technical_only":
        spots.append("technical-only basis (SUPP)")
    return spots


# ---------- Top-leader explanations ----------


def _market_cap_text(row: dict) -> str:
    v = row.get("market_cap")
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:,.0f}"
    return str(v)


def _source_group(row: dict) -> str | None:
    """For watchlist rows, classify into 'main_pipeline' / 'supplemental_*'.
    Main rankings rows return None (the classification only matters when
    the source population is heterogeneous).
    """
    ds = row.get("data_source")
    if not ds:
        return None
    return str(ds)


def _explain_row(row: dict, include_source: bool = False) -> dict:
    mov_pct = compute_mov_pct(row.get("closes"))
    drivers = detect_drivers(row)
    weak = detect_weak_spots(row, mov_pct)
    out = {
        "rank": row.get("rank"),
        "ticker": row.get("ticker"),
        "company": row.get("company"),
        "sector": row.get("sector"),
        "industry": row.get("industry"),
        "ai_score": _safe_num(row.get("ai_score")),
        "fundamental": _safe_num(row.get("fundamental")),
        "technical": _safe_num(row.get("technical")),
        "sentiment": _safe_num(row.get("sentiment")),
        "low_risk": _safe_num(row.get("low_risk")),
        "swing_score": _safe_num(row.get("swing_score")),
        "mov_pct": mov_pct,
        "market_cap": _market_cap_text(row),
        "ai_score_basis": row.get("ai_score_basis"),
        "primary_drivers": drivers,
        "weak_spots": weak,
    }
    if include_source:
        out["source_group"] = _source_group(row)
    return out


def explain_top(rows: list, n: int, include_source: bool = False) -> list:
    """Return per-row explanations for the first `n` rows of a ranked list.

    Rows are assumed to already be sorted by rank (the JSON artifacts are).
    """
    return [_explain_row(r, include_source=include_source) for r in (rows or [])[:n]]


# ---------- Sector crowding ----------


def sector_crowding(rows: list) -> dict:
    """Return sector counts and the dominant sector share for a cohort.

    Used against the top-10 cohort to flag concentration risk. A WARN at
    >=30% of the cohort, FAIL at >=50%, since both indicate the leader
    table is being driven by a single theme rather than broad strength.
    """
    sectors = [r.get("sector") or "Unknown" for r in (rows or [])]
    n = len(sectors) or 1
    counts = Counter(sectors)
    top_sector, top_count = counts.most_common(1)[0] if counts else ("—", 0)
    share = top_count / n
    if share >= SECTOR_CROWD_FAIL_PCT:
        status = "FAIL"
    elif share >= SECTOR_CROWD_WARN_PCT:
        status = "WARN"
    else:
        status = "OK"
    return {
        "status": status,
        "n": n,
        "top_sector": top_sector,
        "top_count": top_count,
        "top_share": round(share, 4),
        "distribution": dict(counts),
    }


# ---------- Suspicious rank detector ----------


def detect_suspicious(top_rows_explained: list, *, group_label: str) -> list:
    """Return suspicious-rank findings from an already-explained top list.

    A row is suspicious if it appears in the leader board and one of the
    following is true:
      * any component is missing (None)
      * the row is technical-only (basis == supp_technical_only)
      * any component score is at or below WEAK_THRESHOLD
      * the recent MOV is at or below NEG_MOV_TRIGGER
    """
    findings: list = []
    for ex in top_rows_explained:
        reasons: list = []
        if ex.get("ai_score_basis") == "supp_technical_only":
            reasons.append("technical-only basis")
        for f in COMPONENT_FIELDS:
            if ex.get(f) is None:
                reasons.append(f"missing {COMPONENT_LABELS[f]}")
        for f in COMPONENT_FIELDS:
            v = ex.get(f)
            if v is not None and v <= WEAK_THRESHOLD:
                reasons.append(f"weak {COMPONENT_LABELS[f]} ({v:.1f})")
        mov = ex.get("mov_pct")
        if mov is not None and mov * 100.0 <= NEG_MOV_TRIGGER:
            reasons.append(f"negative MOV ({mov * 100.0:+.1f}%)")
        if reasons:
            findings.append({
                "group": group_label,
                "rank": ex.get("rank"),
                "ticker": ex.get("ticker"),
                "company": ex.get("company"),
                "ai_score": ex.get("ai_score"),
                "reasons": reasons,
            })
    return findings


# ---------- Alternate weighting (diagnostic only) ----------


WEIGHT_SCHEMES = {
    # Recoverable approximation of the production composite. Equal weight
    # across the four named components is a transparent baseline that we
    # can compare against without altering production scoring code.
    "balanced_baseline": {
        "fundamental": 0.25,
        "technical": 0.25,
        "sentiment": 0.25,
        "low_risk": 0.25,
        "swing_score": 0.0,
    },
    # Momentum / technical tilt: TECH and SWING heavier.
    "momentum_tilt": {
        "fundamental": 0.10,
        "technical": 0.50,
        "sentiment": 0.10,
        "low_risk": 0.10,
        "swing_score": 0.20,
    },
    # Fundamentals / quality tilt: FUND and LOW_RISK heavier.
    "quality_tilt": {
        "fundamental": 0.50,
        "technical": 0.15,
        "sentiment": 0.10,
        "low_risk": 0.20,
        "swing_score": 0.05,
    },
}


def alt_weight_score(row: dict, weights: dict) -> float | None:
    """Compute one alternate weighted score from existing fields.

    Strict policy: if any component with a non-zero weight is missing,
    the alt score is None for that row. This keeps the diagnostic clean
    by NOT silently treating missing fundamentals as zero — that would
    bias the comparison toward technical-only rows.
    """
    total = 0.0
    used_weight = 0.0
    for f, w in weights.items():
        if w == 0:
            continue
        v = _safe_num(row.get(f))
        if v is None:
            return None
        total += v * w
        used_weight += w
    if used_weight <= 0:
        return None
    return round(total / used_weight, 4)


def alt_rerank(rows: list, weights: dict) -> list:
    """Return [(ticker, alt_score, alt_rank, current_rank)] sorted by alt rank."""
    scored: list = []
    for r in rows or []:
        alt = alt_weight_score(r, weights)
        scored.append({
            "ticker": r.get("ticker"),
            "company": r.get("company"),
            "alt_score": alt,
            "current_rank": r.get("rank"),
            "ai_score": _safe_num(r.get("ai_score")),
        })
    # Rank by alt_score desc; rows with None alt_score sink to the bottom
    # but are kept so the table shows them as "no alt rank (missing inputs)".
    has_alt = [x for x in scored if x["alt_score"] is not None]
    no_alt = [x for x in scored if x["alt_score"] is None]
    has_alt.sort(key=lambda x: (-x["alt_score"], x.get("ticker") or ""))
    for i, x in enumerate(has_alt, start=1):
        x["alt_rank"] = i
    for x in no_alt:
        x["alt_rank"] = None
    return has_alt + no_alt


def _delta(current_rank, alt_rank) -> int | None:
    if current_rank is None or alt_rank is None:
        return None
    return int(current_rank) - int(alt_rank)  # positive = riser under alt


def alt_sensitivity(rows: list, top_n: int, weights_by_scheme: dict) -> dict:
    """Run each weighting scheme over `rows`, then for each scheme return
    the biggest risers/fallers within the top-N current cohort PLUS new
    entrants/exits to the top-N alt cohort.
    """
    results: dict = {}
    current_top_tickers = {r.get("ticker") for r in (rows or [])[:top_n]}
    for scheme, weights in weights_by_scheme.items():
        ranked = alt_rerank(rows, weights)
        # Rank deltas for every row that has both a current and alt rank.
        deltas = []
        for x in ranked:
            d = _delta(x["current_rank"], x["alt_rank"])
            if d is not None:
                deltas.append({
                    "ticker": x["ticker"],
                    "company": x["company"],
                    "current_rank": x["current_rank"],
                    "alt_rank": x["alt_rank"],
                    "alt_score": x["alt_score"],
                    "ai_score": x["ai_score"],
                    "delta": d,
                })
        # Risers within the top-N current cohort: rows whose current rank
        # is in [1..top_n] and whose alt rank improves OR worsens vs current.
        top_cohort_deltas = [
            d for d in deltas
            if d["current_rank"] is not None and d["current_rank"] <= top_n
        ]
        risers = sorted(top_cohort_deltas, key=lambda d: d["delta"])[:5]
        fallers = sorted(top_cohort_deltas, key=lambda d: -d["delta"])[:5]
        # New entrants: tickers in alt top-N that were NOT in current top-N.
        alt_top = [x for x in ranked if x["alt_rank"] is not None and x["alt_rank"] <= top_n]
        new_entrants = [x for x in alt_top if x["ticker"] not in current_top_tickers]
        # Exits: tickers in current top-N that drop OUT of alt top-N.
        alt_top_tickers = {x["ticker"] for x in alt_top}
        exits = [
            d for d in top_cohort_deltas
            if d["ticker"] not in alt_top_tickers
        ]
        results[scheme] = {
            "weights": weights,
            "top_n": top_n,
            "risers": risers,
            "fallers": fallers,
            "new_entrants_top_n": [
                {"ticker": x["ticker"], "company": x["company"],
                 "alt_rank": x["alt_rank"], "current_rank": x["current_rank"],
                 "alt_score": x["alt_score"], "ai_score": x["ai_score"]}
                for x in new_entrants
            ],
            "exits_top_n": exits,
            "rows_with_alt_score": sum(1 for x in ranked if x["alt_score"] is not None),
            "rows_total": len(ranked),
        }
    return results


# ---------- Benchmark context ----------


def benchmark_context(bench: dict | None) -> dict:
    """Pull just enough from benchmark_review.json to give the reader a
    sense of how leader buckets have been performing so far. Always wraps
    a 'limited forward history' caveat — the snapshot scaffold is young.
    """
    if not isinstance(bench, dict):
        return {
            "available": False,
            "note": "benchmark_review.json not found; skipping forward context.",
        }
    snap = bench.get("snapshot_summary") or {}
    horizons = snap.get("horizons") or {}
    out: dict = {
        "available": True,
        "snapshots_total": snap.get("snapshots_total"),
        "as_of": bench.get("as_of_rankings") or bench.get("as_of_watchlist"),
        "horizons": {},
    }
    for hz_key, hz in horizons.items():
        completed = hz.get("completed") or 0
        bk_summary: dict = {}
        for bk_name, bk in (hz.get("buckets") or {}).items():
            bk_summary[bk_name] = {
                "snapshots": bk.get("snapshots"),
                "wins": bk.get("wins"),
                "losses": bk.get("losses"),
                "avg_mean_return": bk.get("avg_mean_return"),
            }
        out["horizons"][hz_key] = {
            "completed": completed,
            "buckets": bk_summary,
        }
    out["caveat"] = (
        "Forward-performance snapshots are still accumulating; small sample "
        "sizes mean these averages are directional context, not statistically "
        "meaningful evidence. Do not adjust scoring weights on this alone."
    )
    return out


# ---------- Recommendations ----------


def build_recommendations(
    *,
    top_main: list,
    top_watchlist: list,
    suspicious: list,
    crowding_main: dict,
    crowding_wl: dict,
    alt_main: dict,
    dq_overall: str,
) -> list:
    out: list = []
    sus_tickers = sorted({(s["group"], s["ticker"]) for s in suspicious})
    if sus_tickers:
        sample = ", ".join(f"{t}({g})" for g, t in sus_tickers[:8])
        out.append(
            f"Manually review the {len(sus_tickers)} suspicious top-ranked names "
            f"(sample: {sample}). Each was flagged for missing/weak components, "
            f"negative recent MOV, or technical-only basis."
        )
    if crowding_main.get("status") in ("WARN", "FAIL"):
        out.append(
            f"Main top-10 sector concentration: "
            f"{crowding_main.get('top_sector')} = "
            f"{int(crowding_main.get('top_share', 0) * 100)}%. Consider whether "
            f"the leader board is reflecting broad strength or a single theme."
        )
    if crowding_wl.get("status") in ("WARN", "FAIL"):
        out.append(
            f"Watchlist top-10 sector concentration: "
            f"{crowding_wl.get('top_sector')} = "
            f"{int(crowding_wl.get('top_share', 0) * 100)}%."
        )
    # Compare schemes: if every top-10 main ranker drops out under the
    # quality_tilt OR momentum_tilt alternate, the production composite
    # may be over-relying on the de-emphasized component.
    for scheme, label in (
        ("quality_tilt", "fundamentals/quality"),
        ("momentum_tilt", "momentum/technical"),
    ):
        info = (alt_main or {}).get(scheme) or {}
        exits = info.get("exits_top_n") or []
        if len(exits) >= max(4, info.get("top_n", 10) // 2):
            out.append(
                f"Under the diagnostic {scheme} weighting, "
                f"{len(exits)} of the current top-{info.get('top_n')} drop out — "
                f"production composite may be under-weighting {label} signals. "
                f"Diagnostic only; do not change formulas without forward-test data."
            )
    if dq_overall == "FAIL":
        out.append(
            "Upstream data_quality_audit overall = FAIL. Treat top-leader "
            "explanations cautiously until that is resolved."
        )
    if not out:
        out.append(
            "No suspicious top ranks; no severe sector crowding; alternate "
            "weighting reshuffles are within tolerance. Top leaders look "
            "explainable as-is."
        )
    out.append(
        "Do NOT change scoring weights based on this report alone — forward "
        "performance history is still limited. Use these diagnostics to pick "
        "names for manual review, not to retune the engine."
    )
    return out


# ---------- Top-level build ----------


def build_report(
    rankings: dict | None,
    watchlist: dict | None,
    *,
    bench: dict | None = None,
    parity: dict | None = None,
    drift: dict | None = None,
    dq: dict | None = None,
) -> dict:
    main_rows = (rankings or {}).get("rows") or []
    wl_rows = (watchlist or {}).get("rows") or []

    main_top10 = explain_top(main_rows, 10)
    main_top25 = explain_top(main_rows, 25)
    wl_top10 = explain_top(wl_rows, 10, include_source=True)
    wl_top25 = explain_top(wl_rows, 25, include_source=True)

    suspicious = (
        detect_suspicious(main_top10, group_label="main_top10")
        + detect_suspicious(wl_top10, group_label="watchlist_top10")
    )

    crowding_main = sector_crowding(main_rows[:10])
    crowding_wl = sector_crowding(wl_rows[:10])

    alt_main = alt_sensitivity(main_rows, 25, WEIGHT_SCHEMES)
    alt_wl = alt_sensitivity(wl_rows, 25, WEIGHT_SCHEMES)

    bench_ctx = benchmark_context(bench)

    dq_overall = (dq or {}).get("overall") or "OK"

    recs = build_recommendations(
        top_main=main_top10,
        top_watchlist=wl_top10,
        suspicious=suspicious,
        crowding_main=crowding_main,
        crowding_wl=crowding_wl,
        alt_main=alt_main,
        dq_overall=dq_overall,
    )

    # Overall verdict.
    overall = "OK"
    if suspicious:
        overall = _worst(overall, "WARN")
    if crowding_main.get("status") in ("WARN", "FAIL"):
        overall = _worst(overall, crowding_main["status"])
    if crowding_wl.get("status") in ("WARN", "FAIL"):
        overall = _worst(overall, crowding_wl["status"])
    # Technical-only top names in main (not just watchlist) is a stronger
    # signal — escalate to WARN at minimum.
    main_tech_only = [
        ex for ex in main_top10
        if ex.get("ai_score_basis") == "supp_technical_only"
    ]
    if main_tech_only:
        overall = _worst(overall, "WARN")
    # Critical-data FAIL conditions: leaders dominated by missing-data
    # rows, or upstream data_quality_audit hard FAIL.
    missing_data_share = (
        sum(1 for ex in main_top10 if any(ex.get(f) is None for f in COMPONENT_FIELDS))
        / max(len(main_top10), 1)
    )
    if missing_data_share >= 0.5:
        overall = _worst(overall, "FAIL")
    if dq_overall == "FAIL":
        overall = _worst(overall, "FAIL")
    if not main_rows:
        overall = _worst(overall, "FAIL")

    return {
        "generated_at": _now_utc_iso(),
        "overall": overall,
        "inputs": {
            "rankings_present": rankings is not None,
            "watchlist_present": watchlist is not None,
            "benchmark_review_present": bench is not None,
            "scoring_parity_present": parity is not None,
            "low_risk_drift_present": drift is not None,
            "data_quality_audit_present": dq is not None,
            "rankings_as_of": (rankings or {}).get("as_of"),
            "watchlist_as_of": (watchlist or {}).get("as_of"),
            "rankings_open_date": (rankings or {}).get("open_date"),
        },
        "thresholds": {
            "high_component": HIGH_THRESHOLD,
            "weak_component": WEAK_THRESHOLD,
            "negative_mov_pct": NEG_MOV_TRIGGER,
            "sector_crowd_warn_pct": SECTOR_CROWD_WARN_PCT,
            "sector_crowd_fail_pct": SECTOR_CROWD_FAIL_PCT,
        },
        "leaders": {
            "main_top10": main_top10,
            "main_top25": main_top25,
            "watchlist_top10": wl_top10,
            "watchlist_top25": wl_top25,
        },
        "sector_crowding": {
            "main_top10": crowding_main,
            "watchlist_top10": crowding_wl,
        },
        "suspicious_ranks": suspicious,
        "alternate_weighting": {
            "diagnostic_only": True,
            "schemes": WEIGHT_SCHEMES,
            "main_top25": alt_main,
            "watchlist_top25": alt_wl,
        },
        "benchmark_context": bench_ctx,
        "upstream": {
            "data_quality_audit_overall": dq_overall,
            "scoring_parity_overall": (parity or {}).get("overall"),
            "low_risk_drift_verdict": (
                ((drift or {}).get("verdict") or {}).get("verdict")
                if isinstance(drift, dict) else None
            ),
        },
        "recommendations": recs,
    }


# ---------- HTML rendering ----------


_LEVEL_COLOR = {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}


def _fmt_score(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.1f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{float(v) * 100.0:+.1f}%"


def _render_leader_table(rows: list, *, include_source: bool) -> str:
    if not rows:
        return "<p>(no rows)</p>"
    headers = [
        "#", "Ticker", "Company", "Sector", "AI", "FUND", "TECH", "SENT",
        "LOW_RISK", "SWING", "MOV", "MktCap",
    ]
    if include_source:
        headers.append("Source")
    headers += ["Drivers", "Weak spots"]
    parts = ["<table><thead><tr>"]
    for h in headers:
        cls = ' class="num"' if h in ("AI", "FUND", "TECH", "SENT", "LOW_RISK", "SWING", "MOV", "#") else ""
        parts.append(f'<th{cls}>{escape(h)}</th>')
    parts.append("</tr></thead><tbody>")
    for ex in rows:
        cells = [
            f'<td class="num">{escape(str(ex.get("rank") or "—"))}</td>',
            f'<td>{escape(str(ex.get("ticker") or "—"))}</td>',
            f'<td>{escape(str(ex.get("company") or "—"))}</td>',
            f'<td>{escape(str(ex.get("sector") or "—"))}</td>',
            f'<td class="num">{_fmt_score(ex.get("ai_score"))}</td>',
            f'<td class="num">{_fmt_score(ex.get("fundamental"))}</td>',
            f'<td class="num">{_fmt_score(ex.get("technical"))}</td>',
            f'<td class="num">{_fmt_score(ex.get("sentiment"))}</td>',
            f'<td class="num">{_fmt_score(ex.get("low_risk"))}</td>',
            f'<td class="num">{_fmt_score(ex.get("swing_score"))}</td>',
            f'<td class="num">{_fmt_pct(ex.get("mov_pct"))}</td>',
            f'<td>{escape(str(ex.get("market_cap") or "—"))}</td>',
        ]
        if include_source:
            cells.append(f'<td>{escape(str(ex.get("source_group") or "—"))}</td>')
        drivers = ", ".join(ex.get("primary_drivers") or []) or "—"
        weak = "; ".join(ex.get("weak_spots") or []) or "—"
        cells.append(f'<td>{escape(drivers)}</td>')
        cells.append(f'<td>{escape(weak)}</td>')
        parts.append("<tr>" + "".join(cells) + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _render_suspicious(suspicious: list) -> str:
    if not suspicious:
        return "<p>None — top leaders look explainable on the available components.</p>"
    parts = [
        '<table><thead><tr>'
        '<th>Group</th><th class="num">#</th><th>Ticker</th><th>Company</th>'
        '<th class="num">AI</th><th>Reasons</th></tr></thead><tbody>'
    ]
    for s in suspicious:
        parts.append(
            "<tr>"
            f'<td>{escape(str(s.get("group") or "—"))}</td>'
            f'<td class="num">{escape(str(s.get("rank") or "—"))}</td>'
            f'<td>{escape(str(s.get("ticker") or "—"))}</td>'
            f'<td>{escape(str(s.get("company") or "—"))}</td>'
            f'<td class="num">{_fmt_score(s.get("ai_score"))}</td>'
            f'<td>{escape("; ".join(s.get("reasons") or []))}</td>'
            "</tr>"
        )
    parts.append("</tbody></table>")
    return "".join(parts)


def _render_alt_scheme(scheme: str, info: dict) -> str:
    weights = info.get("weights") or {}
    top_n = info.get("top_n")
    risers = info.get("risers") or []
    fallers = info.get("fallers") or []
    new_entrants = info.get("new_entrants_top_n") or []
    exits = info.get("exits_top_n") or []
    rows_alt = info.get("rows_with_alt_score")
    rows_total = info.get("rows_total")

    weights_str = ", ".join(
        f"{COMPONENT_LABELS.get(k, k)}={v}" for k, v in weights.items() if v
    )
    parts = [
        f'<h3>{escape(scheme)}</h3>',
        f'<p class="meta">Weights: {escape(weights_str)} &middot; '
        f'Rows scoreable under scheme: {rows_alt}/{rows_total} &middot; '
        f'Cohort: top {top_n}</p>',
    ]

    def _table(title: str, items: list, cols: list) -> str:
        if not items:
            return f"<p><em>{escape(title)}:</em> none.</p>"
        head = "".join(f'<th class="num">{escape(c)}</th>' if c in ("Cur", "Alt", "Δ", "AltScore", "AI") else f'<th>{escape(c)}</th>' for c in cols)
        body = []
        for x in items:
            body.append(
                "<tr>"
                f'<td>{escape(str(x.get("ticker") or "—"))}</td>'
                f'<td>{escape(str(x.get("company") or "—"))}</td>'
                f'<td class="num">{escape(str(x.get("current_rank") or "—"))}</td>'
                f'<td class="num">{escape(str(x.get("alt_rank") or "—"))}</td>'
                f'<td class="num">{escape(str(x.get("delta") if x.get("delta") is not None else "—"))}</td>'
                f'<td class="num">{_fmt_score(x.get("alt_score"))}</td>'
                f'<td class="num">{_fmt_score(x.get("ai_score"))}</td>'
                "</tr>"
            )
        return (
            f'<p style="margin:8px 0 4px"><strong>{escape(title)}</strong></p>'
            f'<table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table>'
        )

    cols = ["Ticker", "Company", "Cur", "Alt", "Δ", "AltScore", "AI"]
    parts.append(_table(f"Top risers (current top-{top_n})", risers, cols))
    parts.append(_table(f"Top fallers (current top-{top_n})", fallers, cols))
    # New entrants don't have a current_rank in cohort sense; reuse same shape.
    parts.append(_table(f"New entrants into alt top-{top_n}", new_entrants, cols))
    parts.append(_table(f"Exits from current top-{top_n}", exits, cols))
    return "".join(parts)


def _render_html(report: dict) -> str:
    overall = report["overall"]
    overall_color = _LEVEL_COLOR.get(overall, "#666")
    parts: list = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Ranking Diagnostics</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:1180px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
h1{{margin:0 0 4px}} h2{{margin:18px 0 8px;font-size:18px}}
h3{{margin:14px 0 6px;font-size:15px}}
.meta{{color:#666;font-size:13px;margin-bottom:14px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;
       font-weight:600;font-size:12px;letter-spacing:.5px;background:{overall_color}}}
.section{{border:1px solid #e3e3e3;border-radius:8px;padding:14px 16px;margin:14px 0}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:6px}}
th,td{{text-align:left;padding:5px 7px;border-bottom:1px solid #eee;vertical-align:top}}
.OK{{color:#3c8c3c;font-weight:600}}
.WARN{{color:#b88a00;font-weight:600}}
.FAIL{{color:#c0392b;font-weight:600}}
.recs li{{margin:4px 0}}
.back{{font-size:13px}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.note{{background:#fff8e1;border:1px solid #f0d27a;padding:8px 10px;
       border-radius:6px;margin:8px 0;font-size:13px}}
.diag{{background:#eef5ff;border:1px solid #c8d8ee;padding:8px 10px;
        border-radius:6px;margin:8px 0;font-size:13px}}
details{{margin:8px 0}} summary{{cursor:pointer;font-weight:600;font-size:14px}}
.kv pre{{background:#f7f7f7;padding:8px;border-radius:4px;overflow-x:auto;
        font-size:12px}}
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>Ranking Diagnostics</h1>
<p class="meta">Generated {escape(report["generated_at"])} &middot; Overall: <span class="badge">{escape(overall)}</span></p>
""")

    inp = report["inputs"]
    parts.append(
        '<div class="section"><h2>Inputs</h2><table>'
        '<tr><th>Source</th><th>Present</th><th>as_of / verdict</th></tr>'
        f'<tr><td>data/rankings.json</td><td>{inp["rankings_present"]}</td>'
        f'<td>{escape(str(inp.get("rankings_as_of") or "—"))}</td></tr>'
        f'<tr><td>data/watchlist_rankings.json</td><td>{inp["watchlist_present"]}</td>'
        f'<td>{escape(str(inp.get("watchlist_as_of") or "—"))}</td></tr>'
        f'<tr><td>data/reports/benchmark_review.json</td><td>{inp["benchmark_review_present"]}</td><td>—</td></tr>'
        f'<tr><td>data/reports/scoring_parity_review.json</td><td>{inp["scoring_parity_present"]}</td>'
        f'<td>{escape(str(report["upstream"].get("scoring_parity_overall") or "—"))}</td></tr>'
        f'<tr><td>data/reports/low_risk_drift_review.json</td><td>{inp["low_risk_drift_present"]}</td>'
        f'<td>{escape(str(report["upstream"].get("low_risk_drift_verdict") or "—"))}</td></tr>'
        f'<tr><td>data/reports/data_quality_audit.json</td><td>{inp["data_quality_audit_present"]}</td>'
        f'<td>{escape(str(report["upstream"].get("data_quality_audit_overall") or "—"))}</td></tr>'
        '</table></div>'
    )

    # Sector crowding.
    parts.append('<div class="section"><h2>Sector crowding (top 10)</h2>')
    for label, info in report["sector_crowding"].items():
        st = info.get("status", "OK")
        parts.append(
            f'<p><strong>{escape(label)}:</strong> '
            f'<span class="{st}">{st}</span> &mdash; top sector '
            f'<em>{escape(str(info.get("top_sector") or "—"))}</em>: '
            f'{info.get("top_count", 0)}/{info.get("n", 0)} '
            f'({int((info.get("top_share") or 0) * 100)}%).</p>'
        )
        dist_str = ", ".join(
            f"{k}={v}" for k, v in (info.get("distribution") or {}).items()
        )
        if dist_str:
            parts.append(f'<p class="meta">Distribution: {escape(dist_str)}</p>')
    parts.append('</div>')

    # Top leaders.
    parts.append('<div class="section"><h2>Top leaders &mdash; main rankings</h2>')
    parts.append('<h3>Main top 10</h3>')
    parts.append(_render_leader_table(report["leaders"]["main_top10"], include_source=False))
    parts.append('<details><summary>Main top 25 (expand)</summary>')
    parts.append(_render_leader_table(report["leaders"]["main_top25"], include_source=False))
    parts.append('</details></div>')

    parts.append('<div class="section"><h2>Top leaders &mdash; watchlist</h2>')
    parts.append('<h3>Watchlist top 10</h3>')
    parts.append(_render_leader_table(report["leaders"]["watchlist_top10"], include_source=True))
    parts.append('<details><summary>Watchlist top 25 (expand)</summary>')
    parts.append(_render_leader_table(report["leaders"]["watchlist_top25"], include_source=True))
    parts.append('</details></div>')

    # Suspicious.
    parts.append('<div class="section"><h2>Suspicious top ranks</h2>')
    parts.append(
        '<p class="meta">A row is flagged when a top-10 leader has any '
        'missing/weak component, a technical-only basis (SUPP), or a '
        f'recent MOV at or below {NEG_MOV_TRIGGER:+.1f}%. These are not '
        'errors — they are annotations to drive manual review.</p>'
    )
    parts.append(_render_suspicious(report["suspicious_ranks"]))
    parts.append('</div>')

    # Alternate weighting.
    parts.append('<div class="section"><h2>Alternate weighting sensitivity</h2>')
    parts.append(
        '<p class="diag"><strong>Diagnostic only.</strong> These weighting '
        'schemes are not used in production scoring. Each scheme is computed '
        'from existing component fields; rows with any missing component '
        'under a non-zero weight are excluded so technical-only rows do not '
        'silently inflate.</p>'
    )
    alt_main = report["alternate_weighting"]["main_top25"]
    alt_wl = report["alternate_weighting"]["watchlist_top25"]
    parts.append('<h3 style="margin-top:8px">Main rankings (cohort: current top 25)</h3>')
    for scheme, info in alt_main.items():
        parts.append(_render_alt_scheme(scheme, info))
    parts.append('<h3 style="margin-top:14px">Watchlist (cohort: current top 25)</h3>')
    for scheme, info in alt_wl.items():
        parts.append(_render_alt_scheme(scheme, info))
    parts.append('</div>')

    # Benchmark context.
    bench = report.get("benchmark_context") or {}
    parts.append('<div class="section"><h2>Benchmark context</h2>')
    if not bench.get("available"):
        parts.append(f'<p>{escape(bench.get("note") or "—")}</p>')
    else:
        parts.append(
            f'<p class="meta">Snapshots accumulated: '
            f'{escape(str(bench.get("snapshots_total") or 0))} &middot; '
            f'as_of {escape(str(bench.get("as_of") or "—"))}</p>'
        )
        parts.append('<table><thead><tr>'
                     '<th>Horizon</th><th>Bucket</th>'
                     '<th class="num">Snapshots</th>'
                     '<th class="num">Wins</th>'
                     '<th class="num">Losses</th>'
                     '<th class="num">Avg mean return</th></tr></thead><tbody>')
        for hz_key, hz in (bench.get("horizons") or {}).items():
            for bk_name, bk in (hz.get("buckets") or {}).items():
                avg_ret = bk.get("avg_mean_return")
                avg_str = f"{avg_ret:+.4f}" if isinstance(avg_ret, (int, float)) else "—"
                parts.append(
                    "<tr>"
                    f'<td>{escape(str(hz_key))}</td>'
                    f'<td>{escape(str(bk_name))}</td>'
                    f'<td class="num">{escape(str(bk.get("snapshots") or 0))}</td>'
                    f'<td class="num">{escape(str(bk.get("wins") or 0))}</td>'
                    f'<td class="num">{escape(str(bk.get("losses") or 0))}</td>'
                    f'<td class="num">{escape(avg_str)}</td>'
                    "</tr>"
                )
        parts.append('</tbody></table>')
        parts.append(f'<p class="note">{escape(bench.get("caveat") or "")}</p>')
    parts.append('</div>')

    # Recommendations.
    parts.append('<div class="section"><h2>Recommendations / action items</h2><ol class="recs">')
    for r in report["recommendations"]:
        parts.append(f'<li>{escape(r)}</li>')
    parts.append('</ol></div>')

    parts.append('</body></html>')
    return "\n".join(parts)


# ---------- main ----------


def _maybe_update_task(report: dict) -> None:
    """Best-effort tasks.json stamp. Silently no-ops if helper or task row
    is missing. We DO NOT add a new task row from here — task list shape
    is owned by the dashboard maintainer; we only stamp if a row exists.
    """
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import _tasks_meta  # type: ignore  # noqa: WPS433
    except Exception:
        return
    overall = report.get("overall", "OK")
    status_label = {"OK": "ok", "WARN": "warn", "FAIL": "fail"}.get(overall, "ok")
    sus = report.get("suspicious_ranks") or []
    summary = (
        f"Overall {overall}. "
        f"Suspicious top ranks: {len(sus)}. "
        f"Main top sector: "
        f"{report['sector_crowding']['main_top10'].get('top_sector')} "
        f"{int((report['sector_crowding']['main_top10'].get('top_share') or 0) * 100)}%."
    )
    try:
        _tasks_meta.update_task(
            TASKS_FILE,
            task_id="ranking-diagnostics",
            status=status_label,
            summary=summary,
            report_url="./reports/ranking-diagnostics.html",
        )
    except Exception:
        return


def main() -> int:
    rankings = _load_json(RANKINGS_FILE)
    watchlist = _load_json(WATCHLIST_FILE)
    bench = _load_json(BENCHMARK_REVIEW_FILE)
    parity = _load_json(SCORING_PARITY_FILE)
    drift = _load_json(LOW_RISK_DRIFT_FILE)
    dq = _load_json(DATA_QUALITY_FILE)

    rankings = rankings if isinstance(rankings, dict) else None
    watchlist = watchlist if isinstance(watchlist, dict) else None
    bench = bench if isinstance(bench, dict) else None
    parity = parity if isinstance(parity, dict) else None
    drift = drift if isinstance(drift, dict) else None
    dq = dq if isinstance(dq, dict) else None

    report = build_report(
        rankings, watchlist,
        bench=bench, parity=parity, drift=drift, dq=dq,
    )

    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUTPUT.write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8"
    )
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")

    _maybe_update_task(report)

    sus_n = len(report.get("suspicious_ranks") or [])
    print(
        f"[ranking_diagnostics] overall={report['overall']} "
        f"suspicious={sus_n} -> {JSON_OUTPUT}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
