"""External Benchmark Review — multi-source disagreement queue.

Reads pre-normalized seed captures from data/external_benchmarks/*.json
(seeded by data/external_benchmarks/build_seed.py from the static raw
captures in raw/), joins them against the published rankings/watchlist,
and emits an agreement summary and an actionable disagreement queue.

Outputs:
  - data/reports/external_benchmark_review.json
  - data/reports/disagreement_queue.json
  - reports/external-benchmark-review.html

This report is **diagnostic only**. It does NOT alter production scores.
The seeds are static (one capture, 2026-05-07) so the report can
regenerate every workflow run without new scraping.

Source / internal-score pairing (per the benchmark plan):

  TradingView overall      <-> internal TECH (and SWING context)
  Fidelity ESS             <-> internal AI / FUND / SENT (composite)
  E*TRADE LSEG bullish     <-> internal AI / FUND / SENT (composite)
  Zacks rank (bullish)     <-> internal AI / FUND
  MarketBeat consensus     <-> internal SENT / AI
  MarketBeat upside %      <-> flag target divergence vs internal AI

Internal scores 0..10 are mapped to a comparable 1..5 scale via /2.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from statistics import mean, median

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
SEED_DIR = DATA_DIR / "external_benchmarks"
DATA_REPORTS_DIR = DATA_DIR / "reports"
HTML_REPORTS_DIR = REPO_ROOT / "reports"

RANKINGS_FILE = DATA_DIR / "rankings.json"
WATCHLIST_FILE = DATA_DIR / "watchlist_rankings.json"

JSON_OUTPUT = DATA_REPORTS_DIR / "external_benchmark_review.json"
DISAGREEMENT_OUTPUT = DATA_REPORTS_DIR / "disagreement_queue.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "external-benchmark-review.html"

# Per-source comparison config: which internal score field to compare
# against on a 1..5 scale, and which external normalized field to read.
SOURCE_CONFIG = {
    "tradingview": {
        "external_field": "overall_1to5",
        "internal_fields": ["technical", "swing_score"],
        "primary_internal": "technical",
        "label_field": "tv_overall_label",
    },
    "fidelity": {
        "external_field": "ess_1to5",
        "internal_fields": ["ai_score", "fundamental", "sentiment"],
        "primary_internal": "ai_score",
        "label_field": "fidelity_label",
    },
    "etrade": {
        "external_field": "lseg_bullish_1to5",
        "internal_fields": ["ai_score", "fundamental", "sentiment"],
        "primary_internal": "ai_score",
        "label_field": "tipranks_consensus",
    },
    "zacks": {
        "external_field": "zacks_rank_bullish_1to5",
        "internal_fields": ["ai_score", "fundamental"],
        "primary_internal": "ai_score",
        "label_field": "zacks_rank_label",
    },
    "marketbeat": {
        "external_field": "consensus_1to5",
        "internal_fields": ["sentiment", "ai_score"],
        "primary_internal": "sentiment",
        "label_field": "consensus_rating",
    },
}

# Disagreement / confirmation thresholds (1..5 scale).
STRONG_DISAGREE_GAP = 1.5  # at least one source disagrees by ~1.5 points
SEVERE_DISAGREE_GAP = 2.5
MIN_SOURCES_FOR_QUEUE = 1  # any single source can flag
MIN_SOURCES_FOR_CONFIRM = 2  # need >= 2 agreeing sources to "confirm"
AGREE_GAP_THRESHOLD = 0.5  # |gap| <= 0.5 counts as agree on that source

# Direction buckets for "agreement direction":
#   bearish:  1..2.49
#   neutral:  2.5..3.49
#   bullish:  3.5..5
DIR_BEARISH = "bearish"
DIR_NEUTRAL = "neutral"
DIR_BULLISH = "bullish"


# ---------- Helpers ----------


def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None


def _round(v, n: int = 3):
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return round(float(v), n)


def to_1to5(score_0to10) -> float | None:
    if not isinstance(score_0to10, (int, float)):
        return None
    if isinstance(score_0to10, float) and (math.isnan(score_0to10) or math.isinf(score_0to10)):
        return None
    return round(float(score_0to10) / 2, 3)


def direction_of(score_1to5) -> str | None:
    if not isinstance(score_1to5, (int, float)):
        return None
    if score_1to5 < 2.5:
        return DIR_BEARISH
    if score_1to5 < 3.5:
        return DIR_NEUTRAL
    return DIR_BULLISH


# ---------- Internal index ----------


def build_internal_index(rankings: dict | None, watchlist: dict | None) -> dict[str, dict]:
    """Return {ticker: {ai_score, fundamental, technical, sentiment,
    low_risk, swing_score, sector, source_dataset}} merged from main +
    watchlist. Watchlist overwrites only when main is missing.
    """
    out: dict[str, dict] = {}

    def collect(payload, source):
        if not isinstance(payload, dict):
            return
        for r in payload.get("rows") or []:
            t = r.get("ticker")
            if not t:
                continue
            entry = out.get(t) or {"source_dataset": source}
            for f in ("ai_score", "fundamental", "technical", "sentiment",
                      "low_risk", "swing_score"):
                v = r.get(f)
                if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
                    entry.setdefault(f, float(v))
            for f in ("sector", "industry", "company"):
                v = r.get(f)
                if v and not entry.get(f):
                    entry[f] = v
            out[t] = entry

    collect(rankings, "main")
    collect(watchlist, "watchlist")
    return out


# ---------- Per-source comparison ----------


def compare_row(source: str, ext_row: dict, internal: dict) -> dict | None:
    """Compute the comparison block for one (source, ticker) pair.

    Returns None if the external row isn't covered or the ticker isn't in
    the internal index — those are tracked separately as coverage gaps.
    """
    if not ext_row.get("covered"):
        return None
    cfg = SOURCE_CONFIG[source]
    ext_val = (ext_row.get("normalized") or {}).get(cfg["external_field"])
    if not isinstance(ext_val, (int, float)):
        return None
    primary_field = cfg["primary_internal"]
    int_score_0to10 = internal.get(primary_field)
    int_val = to_1to5(int_score_0to10)
    if int_val is None:
        return None

    gap = round(ext_val - int_val, 3)
    abs_gap = round(abs(gap), 3)
    ext_dir = direction_of(ext_val)
    int_dir = direction_of(int_val)
    direction_agrees = (ext_dir == int_dir) if (ext_dir and int_dir) else None

    # Multi-field context comparisons (informational only).
    extras: dict = {}
    for f in cfg["internal_fields"]:
        v = to_1to5(internal.get(f))
        if v is not None:
            extras[f] = v

    return {
        "source": source,
        "external_value": ext_val,
        "external_label": (ext_row.get("raw") or {}).get(cfg["label_field"]),
        "primary_internal_field": primary_field,
        "internal_value_1to5": int_val,
        "internal_value_0to10": int_score_0to10,
        "extras_1to5": extras,
        "gap": gap,
        "abs_gap": abs_gap,
        "external_direction": ext_dir,
        "internal_direction": int_dir,
        "direction_agrees": direction_agrees,
    }


def severity_for_gap(abs_gap: float) -> str:
    if abs_gap >= SEVERE_DISAGREE_GAP:
        return "severe"
    if abs_gap >= STRONG_DISAGREE_GAP:
        return "strong"
    return "moderate"


# ---------- Per-source agreement metrics ----------


def per_source_metrics(source: str, comparisons: list[dict],
                       seed_rows: list[dict]) -> dict:
    """Aggregate metrics for one source's comparison list."""
    seed_total = len(seed_rows)
    covered = sum(1 for r in seed_rows if r.get("covered"))
    compared = len(comparisons)
    if not comparisons:
        return {
            "source": source,
            "seed_total": seed_total,
            "covered": covered,
            "compared": compared,
            "direction_agreement_rate": None,
            "mean_gap": None,
            "median_gap": None,
            "mean_abs_gap": None,
            "strong_agreements": 0,
            "strong_disagreements": 0,
        }
    gaps = [c["gap"] for c in comparisons]
    abs_gaps = [c["abs_gap"] for c in comparisons]
    dir_agrees = [c["direction_agrees"] for c in comparisons
                  if c["direction_agrees"] is not None]
    strong_agree = sum(1 for c in comparisons
                       if c["direction_agrees"] and c["abs_gap"] <= AGREE_GAP_THRESHOLD)
    strong_disagree = sum(1 for c in comparisons
                          if c["abs_gap"] >= STRONG_DISAGREE_GAP)
    return {
        "source": source,
        "seed_total": seed_total,
        "covered": covered,
        "compared": compared,
        "direction_agreement_rate": _round(sum(dir_agrees) / len(dir_agrees), 4) if dir_agrees else None,
        "mean_gap": _round(mean(gaps)),
        "median_gap": _round(median(gaps)),
        "mean_abs_gap": _round(mean(abs_gaps)),
        "strong_agreements": strong_agree,
        "strong_disagreements": strong_disagree,
    }


# ---------- Disagreement queue ----------


def build_queue_entries(per_ticker_comparisons: dict[str, list[dict]],
                         marketbeat_targets: dict[str, dict],
                         internal_index: dict[str, dict]) -> list[dict]:
    """Yield one queue entry per ticker that has at least one
    strong/severe disagreement on any source.

    Each entry summarizes ALL sources that diverge (severity strong+),
    confidence (#sources flagging), reason summary, internal score,
    and a notes field for future manual review.
    """
    entries: list[dict] = []
    for ticker, comps in per_ticker_comparisons.items():
        flagged = [c for c in comps if c["abs_gap"] >= STRONG_DISAGREE_GAP]
        # Also surface MarketBeat target divergence even when consensus
        # itself agrees with our score: a -50% upside from analysts is
        # actionable signal.
        mb_target = marketbeat_targets.get(ticker)
        target_divergence = None
        if mb_target and isinstance(mb_target.get("upside_pct"), (int, float)):
            up = mb_target["upside_pct"]
            if up <= -20.0 or up >= 30.0:
                target_divergence = {
                    "upside_pct": up,
                    "price_target": mb_target.get("price_target"),
                    "kind": "negative" if up < 0 else "positive",
                }
        if not flagged and not target_divergence:
            continue

        internal = internal_index.get(ticker, {})
        ai = internal.get("ai_score")
        ai_dir = direction_of(to_1to5(ai))
        # Choose the most severe gap as the headline.
        if flagged:
            headline = max(flagged, key=lambda c: c["abs_gap"])
            severity = severity_for_gap(headline["abs_gap"])
            reason = (f"{headline['source']} {headline['external_direction']} "
                      f"(={headline['external_value']}) vs internal "
                      f"{headline['primary_internal_field']} {headline['internal_direction']} "
                      f"(={headline['internal_value_1to5']}); gap "
                      f"{headline['gap']:+.2f}")
        else:
            headline = None
            severity = "moderate"
            reason = (f"marketbeat price target {target_divergence['upside_pct']:+.1f}% "
                      f"vs current price (target divergence)")

        sources_flagging = sorted({c["source"] for c in flagged})
        if target_divergence and "marketbeat" not in sources_flagging:
            sources_flagging.append("marketbeat:target")

        entries.append({
            "ticker": ticker,
            "sector": internal.get("sector"),
            "internal_ai_score_0to10": ai,
            "internal_ai_direction": ai_dir,
            "headline_source": headline["source"] if headline else "marketbeat:target",
            "headline_severity": severity,
            "headline_gap": headline["gap"] if headline else None,
            "reason": reason,
            "confidence_n_sources": len(sources_flagging),
            "sources_flagging": sources_flagging,
            "external_signals": [
                {
                    "source": c["source"],
                    "external_value": c["external_value"],
                    "external_label": c["external_label"],
                    "internal_value_1to5": c["internal_value_1to5"],
                    "primary_internal_field": c["primary_internal_field"],
                    "gap": c["gap"],
                    "severity": severity_for_gap(c["abs_gap"]),
                    "direction_agrees": c["direction_agrees"],
                }
                for c in sorted(comps, key=lambda x: -x["abs_gap"])
            ],
            "marketbeat_target": target_divergence,
            "reviewed": False,
            "notes": "",
        })

    # Sort by (severity rank, confidence_n_sources desc, headline abs_gap desc)
    severity_rank = {"severe": 0, "strong": 1, "moderate": 2}
    entries.sort(key=lambda e: (
        severity_rank.get(e["headline_severity"], 9),
        -e["confidence_n_sources"],
        -abs(e["headline_gap"] or 0),
    ))
    return entries


# ---------- Confirmations ----------


def build_confirmations(per_ticker_comparisons: dict[str, list[dict]],
                         internal_index: dict[str, dict]) -> list[dict]:
    """Tickers where at least MIN_SOURCES_FOR_CONFIRM sources agree on
    direction with internal AI/primary score AND |gap|<=AGREE_GAP_THRESHOLD.
    """
    out: list[dict] = []
    for ticker, comps in per_ticker_comparisons.items():
        agreeing = [c for c in comps
                    if c["direction_agrees"] and c["abs_gap"] <= AGREE_GAP_THRESHOLD]
        if len(agreeing) < MIN_SOURCES_FOR_CONFIRM:
            continue
        internal = internal_index.get(ticker, {})
        out.append({
            "ticker": ticker,
            "sector": internal.get("sector"),
            "internal_ai_score_0to10": internal.get("ai_score"),
            "internal_ai_direction": direction_of(to_1to5(internal.get("ai_score"))),
            "n_confirming_sources": len(agreeing),
            "confirming_sources": sorted({c["source"] for c in agreeing}),
            "mean_external_1to5": _round(mean(c["external_value"] for c in agreeing)),
        })
    out.sort(key=lambda e: (-e["n_confirming_sources"], e["ticker"]))
    return out


# ---------- Top-level assembly ----------


def load_seeds() -> dict[str, dict]:
    """Load every {source}_{date}.json under SEED_DIR. The latest date
    wins per source. Returns {source: payload}."""
    by_source: dict[str, tuple[str, dict]] = {}
    if not SEED_DIR.exists():
        return {}
    for path in sorted(SEED_DIR.glob("*.json")):
        if path.name.startswith("seed_capture_") or path.name == "MEMORY.md":
            continue
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        source = payload.get("source")
        date = payload.get("as_of_date") or ""
        if not source or source not in SOURCE_CONFIG:
            continue
        prev = by_source.get(source)
        if prev is None or date > prev[0]:
            by_source[source] = (date, payload)
    return {s: p for s, (_d, p) in by_source.items()}


def build_report(rankings: dict | None, watchlist: dict | None,
                 seeds: dict[str, dict]) -> tuple[dict, list[dict]]:
    internal_index = build_internal_index(rankings, watchlist)

    per_source_comparisons: dict[str, list[dict]] = defaultdict(list)
    per_ticker_comparisons: dict[str, list[dict]] = defaultdict(list)
    coverage: dict[str, dict] = {}
    marketbeat_targets: dict[str, dict] = {}

    for source, payload in seeds.items():
        seed_rows = payload.get("rows") or []
        not_in_internal: list[str] = []
        for row in seed_rows:
            ticker = row.get("ticker")
            if not ticker:
                continue
            internal = internal_index.get(ticker)
            if internal is None:
                if row.get("covered"):
                    not_in_internal.append(ticker)
                continue
            comp = compare_row(source, row, internal)
            if comp is None:
                continue
            comp["ticker"] = ticker
            per_source_comparisons[source].append(comp)
            per_ticker_comparisons[ticker].append(comp)
            # Capture MarketBeat target context independent of consensus comparison.
            if source == "marketbeat":
                norm = row.get("normalized") or {}
                raw = row.get("raw") or {}
                if isinstance(norm.get("upside_pct"), (int, float)):
                    marketbeat_targets[ticker] = {
                        "upside_pct": norm["upside_pct"],
                        "price_target": raw.get("price_target"),
                    }
        coverage[source] = {
            "as_of_date": payload.get("as_of_date"),
            "seed_total": len(seed_rows),
            "covered": sum(1 for r in seed_rows if r.get("covered")),
            "covered_and_internal": len(per_source_comparisons[source]),
            "covered_but_not_internal": sorted(set(not_in_internal)),
        }

    # Also catch MarketBeat-only target divergence on tickers where
    # consensus comparison succeeded but we never recorded a target.
    for source, payload in seeds.items():
        if source != "marketbeat":
            continue
        for row in payload.get("rows") or []:
            ticker = row.get("ticker")
            if ticker in marketbeat_targets:
                continue
            norm = row.get("normalized") or {}
            raw = row.get("raw") or {}
            if isinstance(norm.get("upside_pct"), (int, float)):
                marketbeat_targets[ticker] = {
                    "upside_pct": norm["upside_pct"],
                    "price_target": raw.get("price_target"),
                }

    metrics_by_source = {
        s: per_source_metrics(s, per_source_comparisons[s], seeds[s].get("rows") or [])
        for s in seeds
    }

    queue = build_queue_entries(per_ticker_comparisons, marketbeat_targets,
                                internal_index)
    confirmations = build_confirmations(per_ticker_comparisons, internal_index)

    overall = _overall_status(metrics_by_source, queue)

    report = {
        "generated_at": _now_utc_str(),
        "as_of_rankings": (rankings or {}).get("as_of"),
        "as_of_watchlist": (watchlist or {}).get("as_of"),
        "seed_dates": {s: p.get("as_of_date") for s, p in seeds.items()},
        "overall": overall,
        "coverage": coverage,
        "metrics_by_source": metrics_by_source,
        "disagreement_queue_count": len(queue),
        "confirmations_count": len(confirmations),
        "confirmations": confirmations,
        "queue_top": queue[:25],  # full queue is in disagreement_queue.json
        "caveat": (
            "Diagnostic only. 30-ticker static seed (2026-05-07). "
            "Sample size is small and not stratified by sector or market cap. "
            "Use for calibration / disagreement triage; do NOT alter scoring weights."
        ),
        "thresholds": {
            "agree_gap_threshold": AGREE_GAP_THRESHOLD,
            "strong_disagree_gap": STRONG_DISAGREE_GAP,
            "severe_disagree_gap": SEVERE_DISAGREE_GAP,
            "min_sources_for_confirm": MIN_SOURCES_FOR_CONFIRM,
        },
    }
    return report, queue


def _overall_status(metrics_by_source: dict, queue: list[dict]) -> str:
    """OK if no severe disagreements and at least one source has direction
    agreement >= 0.5. WARN if there are severe queue entries OR poor
    agreement across the board. FAIL only if no seeds compared at all."""
    compared_total = sum(m.get("compared", 0) for m in metrics_by_source.values())
    if compared_total == 0:
        return "FAIL"
    severe = sum(1 for q in queue if q.get("headline_severity") == "severe")
    if severe >= 3:
        return "WARN"
    poor = [m for m in metrics_by_source.values()
            if isinstance(m.get("direction_agreement_rate"), (int, float))
            and m["direction_agreement_rate"] < 0.4]
    if len(poor) >= 2:
        return "WARN"
    if queue:
        return "WARN"
    return "OK"


# ---------- HTML ----------


STATUS_COLOR = {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}


def _fmt(v, n=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{n}f}"
    return escape(str(v))


def _fmt_pct(v):
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v * 100:.1f}%"
    return escape(str(v))


def _render_html(report: dict, queue: list[dict]) -> str:
    overall = report.get("overall", "OK")
    color = STATUS_COLOR.get(overall, "#666")
    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>External Benchmark Review</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:1200px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
h1{{margin:0 0 4px}} h2{{margin:18px 0 6px;font-size:18px}}
.meta{{color:#666;font-size:13px;margin-bottom:18px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;
       font-weight:600;font-size:12px;letter-spacing:.5px;background:{color}}}
.section{{border:1px solid #e3e3e3;border-radius:8px;padding:14px 16px;margin:14px 0}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}}
.card{{border:1px solid #e3e3e3;border-radius:8px;padding:10px 12px;background:#fafafa}}
.card h3{{margin:0 0 4px;font-size:13px;color:#666;font-weight:600;text-transform:uppercase;letter-spacing:.5px}}
.card .v{{font-size:22px;font-weight:600}}
.card .sub{{font-size:12px;color:#666;margin-top:2px}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
th{{background:#f7f7f7}}
.severity-severe{{color:#c0392b;font-weight:600}}
.severity-strong{{color:#b88a00;font-weight:600}}
.severity-moderate{{color:#666}}
.dir-bullish{{color:#3c8c3c}}
.dir-bearish{{color:#c0392b}}
.dir-neutral{{color:#666}}
.muted{{color:#666;font-size:12px}}
.back{{font-size:13px}}
.tag{{display:inline-block;padding:1px 6px;border-radius:3px;background:#eef;
     font-size:11px;color:#334;margin-right:3px}}
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>External Benchmark Review</h1>
<p class="meta">Generated {escape(report['generated_at'])}
&middot; rankings as_of {escape(str(report.get('as_of_rankings')))}
&middot; <span class="badge">{overall}</span></p>
<p class="muted">{escape(report.get('caveat',''))}</p>
""")

    # Summary cards
    parts.append('<div class="section"><h2>Summary</h2><div class="cards">')
    metrics = report.get("metrics_by_source", {}) or {}
    n_sources = len(metrics)
    total_compared = sum(m.get("compared", 0) for m in metrics.values())
    avg_agree = [m.get("direction_agreement_rate") for m in metrics.values()
                 if isinstance(m.get("direction_agreement_rate"), (int, float))]
    avg_agree_v = sum(avg_agree) / len(avg_agree) if avg_agree else None
    cards = [
        ("Sources", n_sources, ""),
        ("Comparisons", total_compared, "ticker × source pairs"),
        ("Avg dir-agreement", _fmt_pct(avg_agree_v), "across sources"),
        ("Disagreement queue", report.get("disagreement_queue_count", 0),
         "tickers flagged"),
        ("Confirmations", report.get("confirmations_count", 0),
         f">={MIN_SOURCES_FOR_CONFIRM} sources agreeing"),
    ]
    for title, v, sub in cards:
        parts.append(f'<div class="card"><h3>{escape(title)}</h3>'
                     f'<div class="v">{escape(str(v))}</div>'
                     f'<div class="sub">{escape(sub)}</div></div>')
    parts.append('</div></div>')

    # Per-source agreement table
    parts.append('<div class="section"><h2>Agreement by source</h2>')
    parts.append('<table><thead><tr><th>Source</th><th>Seed</th><th>Covered</th>'
                 '<th>Compared</th><th>Dir agreement</th><th>Mean gap</th>'
                 '<th>Mean |gap|</th><th>Strong agree</th><th>Strong disagree</th>'
                 '</tr></thead><tbody>')
    for s in sorted(metrics):
        m = metrics[s]
        parts.append(
            f"<tr><td><strong>{escape(s)}</strong></td>"
            f"<td>{m.get('seed_total',0)}</td>"
            f"<td>{m.get('covered',0)}</td>"
            f"<td>{m.get('compared',0)}</td>"
            f"<td>{_fmt_pct(m.get('direction_agreement_rate'))}</td>"
            f"<td>{_fmt(m.get('mean_gap'))}</td>"
            f"<td>{_fmt(m.get('mean_abs_gap'))}</td>"
            f"<td>{m.get('strong_agreements',0)}</td>"
            f"<td>{m.get('strong_disagreements',0)}</td></tr>"
        )
    parts.append('</tbody></table>')
    parts.append('<p class="muted">Each row pairs one external 1..5 score against the listed primary internal score: '
                 'tradingview→technical, fidelity/etrade→ai_score, zacks→ai_score, marketbeat→sentiment. '
                 'gap = external − internal (1..5 scale).</p>')
    parts.append('</div>')

    # Queue
    parts.append(f'<div class="section"><h2>Disagreement queue '
                 f'({len(queue)} tickers)</h2>')
    if not queue:
        parts.append('<p class="muted">No tickers exceed the strong-disagreement gap threshold.</p>')
    else:
        parts.append('<table><thead><tr><th>Ticker</th><th>Sector</th>'
                     '<th>AI</th><th>Severity</th><th>Headline source</th>'
                     '<th>Gap</th><th>Sources</th><th>Reason</th></tr></thead><tbody>')
        for q in queue[:30]:
            sev = q.get("headline_severity", "")
            parts.append(
                f"<tr><td><strong>{escape(q.get('ticker') or '')}</strong></td>"
                f"<td>{escape(q.get('sector') or '—')}</td>"
                f"<td>{_fmt(q.get('internal_ai_score_0to10'))}</td>"
                f"<td class=\"severity-{sev}\">{escape(sev)}</td>"
                f"<td>{escape(q.get('headline_source') or '')}</td>"
                f"<td>{_fmt(q.get('headline_gap'))}</td>"
                f"<td>{' '.join(f'<span class=tag>{escape(s)}</span>' for s in q.get('sources_flagging') or [])}</td>"
                f"<td>{escape(q.get('reason') or '')}</td></tr>"
            )
        parts.append('</tbody></table>')
        if len(queue) > 30:
            parts.append(f'<p class="muted">Showing 30 of {len(queue)}. '
                         f'Full list in <code>data/reports/disagreement_queue.json</code>.</p>')
    parts.append('</div>')

    # Confirmations
    confirmations = report.get("confirmations") or []
    parts.append(f'<div class="section"><h2>Confirmations '
                 f'(≥{MIN_SOURCES_FOR_CONFIRM} sources agreeing)</h2>')
    if not confirmations:
        parts.append('<p class="muted">No tickers reach the confirmation threshold yet (small sample).</p>')
    else:
        parts.append('<table><thead><tr><th>Ticker</th><th>Sector</th>'
                     '<th>AI</th><th>Direction</th><th>Sources</th>'
                     '<th>Mean ext 1..5</th></tr></thead><tbody>')
        for c in confirmations[:25]:
            d = c.get("internal_ai_direction") or ""
            parts.append(
                f"<tr><td><strong>{escape(c.get('ticker') or '')}</strong></td>"
                f"<td>{escape(c.get('sector') or '—')}</td>"
                f"<td>{_fmt(c.get('internal_ai_score_0to10'))}</td>"
                f"<td class=\"dir-{d}\">{escape(d)}</td>"
                f"<td>{' '.join(f'<span class=tag>{escape(s)}</span>' for s in c.get('confirming_sources') or [])}</td>"
                f"<td>{_fmt(c.get('mean_external_1to5'))}</td></tr>"
            )
        parts.append('</tbody></table>')
    parts.append('</div>')

    # Coverage
    parts.append('<div class="section"><h2>Coverage</h2>')
    parts.append('<table><thead><tr><th>Source</th><th>As of</th><th>Seed</th>'
                 '<th>Covered</th><th>Compared</th><th>Covered but not in internal</th>'
                 '</tr></thead><tbody>')
    for s, cov in sorted((report.get("coverage") or {}).items()):
        not_internal = ', '.join(cov.get("covered_but_not_internal") or []) or '—'
        parts.append(
            f"<tr><td><strong>{escape(s)}</strong></td>"
            f"<td>{escape(cov.get('as_of_date') or '—')}</td>"
            f"<td>{cov.get('seed_total',0)}</td>"
            f"<td>{cov.get('covered',0)}</td>"
            f"<td>{cov.get('covered_and_internal',0)}</td>"
            f"<td class=\"muted\">{escape(not_internal)}</td></tr>"
        )
    parts.append('</tbody></table></div>')

    # Recommendation
    parts.append('<div class="section"><h2>Recommendation</h2>')
    parts.append('<p>Use this report for <strong>calibration only</strong>. '
                 'Sample size is small (30 tickers, single capture day) and skewed toward '
                 'tickers we already flagged as interesting — direction-agreement rates '
                 'should not be read as accuracy. The disagreement queue is the actionable '
                 'output: review each entry against fundamentals/news before any manual '
                 'override. <strong>Do not change scoring weights based on this run.</strong></p>')
    parts.append('</div>')

    parts.append('</body></html>')
    return "\n".join(parts)


# ---------- Tasks.json stamping ----------


def _stamp_task(report: dict) -> None:
    tasks_path = DATA_DIR / "tasks.json"
    if not tasks_path.exists():
        return
    try:
        from _tasks_meta import update_task  # type: ignore
    except ImportError:
        sys.path.insert(0, str(Path(__file__).parent))
        try:
            from _tasks_meta import update_task  # type: ignore
        except ImportError:
            return

    metrics = report.get("metrics_by_source", {}) or {}
    overall_lc = (report.get("overall") or "OK").lower()
    if overall_lc == "fail":
        status_label = "FAIL"
    elif overall_lc == "warn":
        status_label = "warn"
    else:
        status_label = "OK"

    queue_n = report.get("disagreement_queue_count", 0)
    confirms_n = report.get("confirmations_count", 0)
    src_summary = "/".join(f"{s}:{m.get('compared',0)}" for s, m in sorted(metrics.items()))
    summary = f"queue={queue_n} confirms={confirms_n} sources({src_summary})"

    update_task(tasks_path, "external-benchmark-review",
                status=status_label,
                summary=summary,
                report_url="./reports/external-benchmark-review.html")


def _ensure_task_row(report: dict) -> None:
    """Append a tasks.json row for external-benchmark-review if absent.

    Idempotent: if the row already exists this is a no-op. Keeps the
    workflow deployment dirt-simple — no manual JSON edit required.
    """
    tasks_path = DATA_DIR / "tasks.json"
    if not tasks_path.exists():
        return
    try:
        data = json.loads(tasks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return
    if any(isinstance(t, dict) and t.get("id") == "external-benchmark-review"
           for t in tasks):
        return
    tasks.append({
        "id": "external-benchmark-review",
        "name": "External Benchmark Review",
        "schedule": "Every refresh (08:45/12:30/15:35 CT, weekdays)",
        "last_run": "—",
        "next_run": "—",
        "status": "Not Run",
        "summary": "—",
        "report_url": "./reports/external-benchmark-review.html",
    })
    tasks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------- Entry point ----------


def main(argv: list[str] | None = None) -> int:
    rankings = _load_json(RANKINGS_FILE)
    watchlist = _load_json(WATCHLIST_FILE)
    seeds = load_seeds()

    if not seeds:
        print("[external_benchmark_review] no seeds in data/external_benchmarks/", file=sys.stderr)
        # Still write empty outputs so the dashboard link doesn't 404.
        report = {
            "generated_at": _now_utc_str(),
            "overall": "FAIL",
            "metrics_by_source": {},
            "coverage": {},
            "disagreement_queue_count": 0,
            "confirmations_count": 0,
            "confirmations": [],
            "queue_top": [],
            "caveat": "No seeds found.",
        }
        queue: list[dict] = []
    else:
        report, queue = build_report(
            rankings if isinstance(rankings, dict) else None,
            watchlist if isinstance(watchlist, dict) else None,
            seeds,
        )

    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUTPUT.write_text(json.dumps(report, indent=2, default=str) + "\n",
                           encoding="utf-8")
    DISAGREEMENT_OUTPUT.write_text(
        json.dumps({"generated_at": report["generated_at"],
                    "queue": queue}, indent=2, default=str) + "\n",
        encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report, queue), encoding="utf-8")

    _ensure_task_row(report)
    _stamp_task(report)

    print(f"[external_benchmark_review] sources={len(report.get('metrics_by_source') or {})} "
          f"queue={report.get('disagreement_queue_count',0)} "
          f"confirmations={report.get('confirmations_count',0)} "
          f"overall={report.get('overall')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
