"""Low-Risk Drift Review — diagnostic / model-validation report focused on
the `low_risk` score divergence between the main rankings universe and the
watchlist universe.

The scoring parity review surfaces a persistent FAIL on `low_risk` where
watchlist_main_pipeline rows score materially lower (mean delta around
-2.0) than main_rankings rows. This report exists to determine *why* —
specifically whether the gap is:

  * selection_bias — the watchlist is intentionally weighted toward
    speculative / high-beta / high-vol names, so a lower mean low_risk is
    expected and the same scoring formula is faithfully producing it.
  * data_gap — the underlying risk inputs (atr_pct, vol_bucket, etc.) are
    missing or inconsistent on watchlist rows and the score is therefore
    mechanical garbage rather than a real assessment.
  * formula_issue — the same ticker has materially different `low_risk`
    in the two files, implying the score is being computed differently on
    the two paths even when inputs are nominally identical.
  * mixed — multiple of the above, or evidence too weak to call cleanly.

This report does NOT change scoring weights. It only describes what the
data says and recommends whether the parity blocker should be demoted to
a known selection-bias finding.

Inputs (read-only):
  - data/rankings.json
  - data/watchlist_rankings.json
  - data/reports/scoring_parity_review.json (optional — used for context)

Outputs:
  - data/reports/low_risk_drift_review.json
  - reports/low-risk-drift-review.html
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
PARITY_FILE = DATA_REPORTS_DIR / "scoring_parity_review.json"

JSON_OUTPUT = DATA_REPORTS_DIR / "low_risk_drift_review.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "low-risk-drift-review.html"

# Risk-input fields we look at to characterize the "shape" of risk inputs
# across populations. These are the proxies the low_risk score is built on.
RISK_INPUT_FIELDS = ("atr_pct", "vol_bucket", "swing_score", "market_cap")

# "Speculative" sectors as a heuristic. Not authoritative — used only to
# describe the universe mix, not to alter scoring.
SPECULATIVE_SECTORS = {
    "Technology",
    "Communication Services",
    "Consumer Cyclical",
    "Healthcare",  # biotech tail
    "Energy",
}

# Verdict thresholds (selection bias vs formula issue).
SAME_TICKER_DIFF_FAIL = 0.5     # |Δlow_risk| above this on a shared ticker = formula
SAME_TICKER_DIFF_WARN = 0.2
DATA_GAP_PCT_FAIL = 0.25        # >=25% null on key risk inputs in either group
HIGH_VOL_BUCKET_GAP = 0.15      # 15pt gap in 'High'-bucket share supports selection bias
SPEC_SECTOR_GAP = 0.10          # 10pt gap in speculative-sector share supports selection bias


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


# ---------- numeric helpers ----------


def _is_numeric(v) -> bool:
    return isinstance(v, (int, float)) and not (isinstance(v, float) and v != v)


def _percentile(sorted_nums: list, p: float) -> float | None:
    if not sorted_nums:
        return None
    if p <= 0:
        return sorted_nums[0]
    if p >= 100:
        return sorted_nums[-1]
    k = (len(sorted_nums) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_nums) - 1)
    frac = k - lo
    return sorted_nums[lo] * (1 - frac) + sorted_nums[hi] * frac


def distribution(values) -> dict:
    """Mean/median/min/max/p25/p75/null_count for a list of mixed values."""
    nums = [float(v) for v in values if _is_numeric(v)]
    null_count = sum(1 for v in values if not _is_numeric(v))
    n = len(nums)
    if n == 0:
        return {
            "n": 0,
            "null_count": null_count,
            "mean": None, "median": None,
            "min": None, "max": None,
            "p25": None, "p75": None,
        }
    s = sorted(nums)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    return {
        "n": n,
        "null_count": null_count,
        "mean": round(sum(nums) / n, 4),
        "median": round(median, 4),
        "min": round(min(nums), 4),
        "max": round(max(nums), 4),
        "p25": round(_percentile(s, 25) or 0.0, 4),
        "p75": round(_percentile(s, 75) or 0.0, 4),
    }


def _market_cap_to_float(v) -> float | None:
    """Parse market_cap string like '9.58B' / '4.17T' / '732.4M' into a
    float in dollars. Returns None for already-numeric or unparseable values
    inconsistently — we keep this lenient because the report only uses it
    for distribution comparison, never for ranking decisions.
    """
    if _is_numeric(v):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip().upper().replace(",", "")
    if not s:
        return None
    mult = 1.0
    if s.endswith("T"):
        mult, s = 1e12, s[:-1]
    elif s.endswith("B"):
        mult, s = 1e9, s[:-1]
    elif s.endswith("M"):
        mult, s = 1e6, s[:-1]
    elif s.endswith("K"):
        mult, s = 1e3, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


# ---------- group construction ----------


def split_groups(rankings, watchlist) -> dict:
    main = (rankings or {}).get("rows") or []
    wl_rows = (watchlist or {}).get("rows") or []
    wl_main = [r for r in wl_rows if r.get("data_source") == "main_pipeline"]
    wl_supp = [r for r in wl_rows if (r.get("data_source") or "").startswith("supplemental")]

    # Sort main rankings by ai_score (or by rank if available) for top-N slicing.
    def _sort_key(r):
        return (r.get("rank") if isinstance(r.get("rank"), (int, float)) else 1e9,
                -(r.get("ai_score") or 0.0))
    main_sorted = sorted(main, key=_sort_key)

    return {
        "main_rankings": main_sorted,
        "main_top25": main_sorted[:25],
        "main_top10": main_sorted[:10],
        "watchlist_main_pipeline": wl_main,
        "watchlist_supp": wl_supp,
    }


# ---------- per-group analysis ----------


def low_risk_stats(rows: list) -> dict:
    return distribution([r.get("low_risk") for r in rows])


def coverage_metrics(rows: list) -> dict:
    """How well-populated risk inputs are on this group."""
    n = len(rows)
    out: dict = {"row_count": n}
    for f in RISK_INPUT_FIELDS:
        present = 0
        nulls = 0
        for r in rows:
            v = r.get(f)
            if f == "market_cap":
                ok = _market_cap_to_float(v) is not None
            elif f == "vol_bucket":
                ok = isinstance(v, str) and v.strip() != ""
            else:
                ok = _is_numeric(v)
            if ok:
                present += 1
            else:
                nulls += 1
        out[f] = {
            "present": present,
            "null": nulls,
            "pct_null": (round(nulls / n, 4) if n else None),
        }
    return out


def vol_bucket_breakdown(rows: list) -> dict:
    c: Counter = Counter()
    for r in rows:
        b = r.get("vol_bucket")
        if isinstance(b, str) and b.strip():
            c[b] += 1
        else:
            c["__missing__"] += 1
    total = sum(c.values()) or 0
    return {
        "counts": dict(c),
        "share": ({k: round(v / total, 4) for k, v in c.items()} if total else {}),
        "total": total,
    }


def sector_breakdown(rows: list) -> dict:
    c: Counter = Counter()
    for r in rows:
        s = r.get("sector")
        if isinstance(s, str) and s.strip():
            c[s] += 1
        else:
            c["__missing__"] += 1
    total = sum(c.values()) or 0
    spec = sum(v for k, v in c.items() if k in SPECULATIVE_SECTORS)
    return {
        "counts": dict(c),
        "share": ({k: round(v / total, 4) for k, v in c.items()} if total else {}),
        "total": total,
        "speculative_share": (round(spec / total, 4) if total else 0.0),
    }


def market_cap_distribution(rows: list) -> dict:
    return distribution([_market_cap_to_float(r.get("market_cap")) for r in rows])


def atr_distribution(rows: list) -> dict:
    return distribution([r.get("atr_pct") for r in rows])


def source_breakdown(rows: list) -> dict:
    """For watchlist rows, split by `source` label (csv/tradingview/both)
    and report low_risk distribution per slice. Returns empty dict when
    `source` is not present on any row (i.e. main rankings).
    """
    out: dict = {}
    by_label: dict = {}
    for r in rows:
        lbl = r.get("source")
        if not isinstance(lbl, str):
            continue
        by_label.setdefault(lbl, []).append(r)
    for lbl, sub in by_label.items():
        out[lbl] = {
            "row_count": len(sub),
            "low_risk": low_risk_stats(sub),
            "vol_bucket": vol_bucket_breakdown(sub),
            "sector": sector_breakdown(sub),
        }
    return out


def names_extremes(rows: list, n: int = 10) -> dict:
    """Top-N highest and lowest low_risk names in the group."""
    scored = [
        {
            "ticker": r.get("ticker"),
            "company": r.get("company"),
            "sector": r.get("sector"),
            "low_risk": r.get("low_risk"),
            "atr_pct": r.get("atr_pct"),
            "vol_bucket": r.get("vol_bucket"),
            "market_cap": r.get("market_cap"),
            "source": r.get("source"),
            "data_source": r.get("data_source"),
        }
        for r in rows if _is_numeric(r.get("low_risk"))
    ]
    scored_high = sorted(scored, key=lambda x: -(x["low_risk"] or 0.0))[:n]
    scored_low = sorted(scored, key=lambda x: (x["low_risk"] or 0.0))[:n]
    return {"highest": scored_high, "lowest": scored_low}


# ---------- overlap analysis (cross-population, same ticker) ----------


def overlap_analysis(main_rows: list, wl_main_rows: list, atol: float = 0.001) -> dict:
    """For tickers present in both main_rankings and watchlist_main_pipeline,
    compare their low_risk values. A material per-ticker delta on the same
    score formula and (presumably) the same inputs is the only signal that
    cleanly indicates a *formula or pipeline* problem rather than selection
    bias.
    """
    main_idx = {
        r.get("ticker"): r for r in main_rows
        if isinstance(r.get("ticker"), str)
    }
    shared = []
    only_in_wl = []
    diffs = []
    for r in wl_main_rows:
        t = r.get("ticker")
        if not isinstance(t, str):
            continue
        if t in main_idx:
            m = main_idx[t]
            mv, wv = m.get("low_risk"), r.get("low_risk")
            both_num = _is_numeric(mv) and _is_numeric(wv)
            delta = (round(float(wv) - float(mv), 4) if both_num else None)
            shared.append({
                "ticker": t,
                "main_low_risk": mv,
                "wl_low_risk": wv,
                "delta": delta,
                "both_numeric": both_num,
            })
            if both_num and abs(float(delta)) > atol:
                diffs.append({
                    "ticker": t, "delta": delta,
                    "main_low_risk": mv, "wl_low_risk": wv,
                })
        else:
            only_in_wl.append(t)

    only_in_main = [t for t in main_idx.keys() if t not in {x["ticker"] for x in shared}]

    diffs_sorted = sorted(diffs, key=lambda x: -abs(x["delta"]))
    max_abs_delta = (abs(diffs_sorted[0]["delta"]) if diffs_sorted else 0.0)
    mean_abs_delta = (
        round(sum(abs(d["delta"]) for d in diffs) / len(diffs), 4)
        if diffs else 0.0
    )

    return {
        "shared_count": len(shared),
        "only_in_watchlist_main": len(only_in_wl),
        "only_in_main_rankings": len(only_in_main),
        "differing_count": len(diffs),
        "max_abs_delta": round(max_abs_delta, 4),
        "mean_abs_delta": mean_abs_delta,
        "top_diffs": diffs_sorted[:10],
    }


# ---------- verdict ----------


def _flag_data_gap(coverage: dict) -> tuple[bool, list]:
    """Return (is_gap, evidence) — true if any key risk input has >=
    DATA_GAP_PCT_FAIL nulls on watchlist_main_pipeline.
    """
    evidence = []
    is_gap = False
    for f in ("atr_pct", "vol_bucket", "swing_score"):
        c = coverage.get(f) or {}
        pct_null = c.get("pct_null")
        if isinstance(pct_null, (int, float)) and pct_null >= DATA_GAP_PCT_FAIL:
            is_gap = True
            evidence.append({
                "field": f,
                "pct_null": pct_null,
                "present": c.get("present"),
                "null": c.get("null"),
            })
    return is_gap, evidence


def _flag_formula_issue(overlap: dict) -> tuple[bool, str, list]:
    """Use the per-ticker shared overlap as the key signal."""
    diffs = overlap.get("top_diffs") or []
    max_abs = overlap.get("max_abs_delta") or 0.0
    if max_abs >= SAME_TICKER_DIFF_FAIL:
        return True, "FAIL", diffs
    if max_abs >= SAME_TICKER_DIFF_WARN:
        return True, "WARN", diffs
    return False, "OK", diffs


def _flag_selection_bias(main_breakdown: dict, wl_breakdown: dict,
                        main_vol: dict, wl_vol: dict) -> tuple[bool, list]:
    """Selection bias if watchlist is materially more speculative or has
    a materially larger 'High' vol_bucket share than main.
    """
    evidence: list = []
    is_bias = False
    main_spec = main_breakdown.get("speculative_share") or 0.0
    wl_spec = wl_breakdown.get("speculative_share") or 0.0
    if (wl_spec - main_spec) >= SPEC_SECTOR_GAP:
        is_bias = True
        evidence.append({
            "kind": "speculative_sector_share",
            "main": main_spec,
            "watchlist_main": wl_spec,
            "delta": round(wl_spec - main_spec, 4),
        })
    main_high = (main_vol.get("share") or {}).get("High", 0.0)
    wl_high = (wl_vol.get("share") or {}).get("High", 0.0)
    if (wl_high - main_high) >= HIGH_VOL_BUCKET_GAP:
        is_bias = True
        evidence.append({
            "kind": "high_vol_bucket_share",
            "main": main_high,
            "watchlist_main": wl_high,
            "delta": round(wl_high - main_high, 4),
        })
    return is_bias, evidence


def determine_verdict(
    main_lr: dict, wlm_lr: dict,
    coverage_main: dict, coverage_wlm: dict,
    overlap: dict,
    sector_main: dict, sector_wlm: dict,
    vol_main: dict, vol_wlm: dict,
) -> dict:
    """Pull it all together. Returns a dict with `verdict`, `confidence`,
    `evidence`, and a list of contributing flags."""
    main_mean = main_lr.get("mean")
    wlm_mean = wlm_lr.get("mean")
    delta = None
    if isinstance(main_mean, (int, float)) and isinstance(wlm_mean, (int, float)):
        delta = round(wlm_mean - main_mean, 4)

    formula_flag, formula_severity, formula_diffs = _flag_formula_issue(overlap)
    data_gap_main, gap_main_ev = _flag_data_gap(coverage_main)
    data_gap_wlm, gap_wlm_ev = _flag_data_gap(coverage_wlm)
    bias_flag, bias_ev = _flag_selection_bias(sector_main, sector_wlm, vol_main, vol_wlm)

    flags = []
    if formula_flag:
        flags.append("formula_issue")
    if data_gap_main or data_gap_wlm:
        flags.append("data_gap")
    if bias_flag:
        flags.append("selection_bias")

    if not flags:
        verdict = "indeterminate"
        confidence = "low"
    elif len(flags) == 1:
        verdict = flags[0]
        confidence = "high" if formula_flag and formula_severity == "FAIL" else "medium"
    else:
        verdict = "mixed"
        confidence = "medium"

    return {
        "verdict": verdict,
        "confidence": confidence,
        "delta_mean_low_risk_wl_minus_main": delta,
        "flags": flags,
        "formula_flag": {
            "triggered": formula_flag,
            "severity": formula_severity,
            "evidence": formula_diffs[:5],
        },
        "data_gap_flag": {
            "triggered": data_gap_main or data_gap_wlm,
            "main_rankings_evidence": gap_main_ev,
            "watchlist_main_evidence": gap_wlm_ev,
        },
        "selection_bias_flag": {
            "triggered": bias_flag,
            "evidence": bias_ev,
        },
    }


# ---------- recommendations ----------


def recommendations(verdict: dict, overlap: dict) -> list:
    """Plain-text guidance based on the verdict. Conservative — never tells
    the caller to alter weights, only how to interpret / surface the gap."""
    out: list = []
    v = verdict.get("verdict")
    flags = verdict.get("flags") or []

    if v == "selection_bias":
        out.append(
            "Leave the low_risk formula unchanged. The watchlist universe is "
            "materially more speculative / higher-volatility than the main "
            "universe, so a lower mean low_risk is expected and faithful to "
            "the inputs."
        )
        out.append(
            "Consider demoting low_risk drift in cross-group parity from "
            "FAIL to a known selection-bias finding so it stops drowning out "
            "real parity blockers in the Midday Health Check."
        )
        out.append(
            "Treat low_risk as explanatory in the watchlist context, not "
            "punitive — surface it next to the score rather than as a tag "
            "that demotes a row."
        )
    elif v == "data_gap":
        out.append(
            "Do NOT change low_risk weights yet. The drift is being driven "
            "by missing risk inputs (atr_pct / vol_bucket / swing_score) on "
            "one of the two populations — fix the inputs first, then re-run "
            "this report."
        )
    elif v == "formula_issue":
        out.append(
            "STOP. Same-ticker low_risk values diverge across main_rankings "
            "and watchlist_main_pipeline by more than the rounding tolerance. "
            "This indicates a formula or transformation difference between "
            "the two paths — investigate before tuning weights."
        )
        if (overlap.get("top_diffs") or []):
            sample = ", ".join(d["ticker"] for d in overlap["top_diffs"][:5] if d.get("ticker"))
            if sample:
                out.append(f"Top divergent tickers to inspect: {sample}.")
    elif v == "mixed":
        out.append(
            "Mixed signal: more than one root cause is plausible "
            f"({', '.join(flags)}). Resolve any data_gap and formula_issue "
            "evidence before treating the rest as selection bias."
        )
    else:
        out.append(
            "No strong signal from this report alone. Keep low_risk drift "
            "flagged as a parity WARN until evidence is stronger in either "
            "direction."
        )

    out.append(
        "A future improvement (not in scope here) is to normalize low_risk "
        "by sector or market cap before cross-group comparison so the "
        "universe-mix effect is removed."
    )
    return out


# ---------- top-level build ----------


def build_report(rankings, watchlist, parity=None) -> dict:
    groups = split_groups(rankings, watchlist)
    main = groups["main_rankings"]
    wlm = groups["watchlist_main_pipeline"]
    supp = groups["watchlist_supp"]

    # Per-group descriptive stats.
    per_group = {}
    for name, rows in groups.items():
        per_group[name] = {
            "row_count": len(rows),
            "low_risk": low_risk_stats(rows),
            "atr_pct": atr_distribution(rows),
            "market_cap_dollars": market_cap_distribution(rows),
            "coverage": coverage_metrics(rows),
            "vol_bucket": vol_bucket_breakdown(rows),
            "sector": sector_breakdown(rows),
            "extremes": names_extremes(rows),
        }

    # Source-label slices (CSV / TradingView / BOTH).
    by_source_wl_main = source_breakdown(wlm)
    by_source_wl_supp = source_breakdown(supp)

    # Overlap on shared tickers (the formula-issue signal).
    overlap = overlap_analysis(main, wlm)

    # Verdict.
    verdict = determine_verdict(
        per_group["main_rankings"]["low_risk"],
        per_group["watchlist_main_pipeline"]["low_risk"],
        per_group["main_rankings"]["coverage"],
        per_group["watchlist_main_pipeline"]["coverage"],
        overlap,
        per_group["main_rankings"]["sector"],
        per_group["watchlist_main_pipeline"]["sector"],
        per_group["main_rankings"]["vol_bucket"],
        per_group["watchlist_main_pipeline"]["vol_bucket"],
    )

    recs = recommendations(verdict, overlap)

    parity_context = None
    if isinstance(parity, dict):
        cgp = parity.get("cross_group_parity") or {}
        by_field = cgp.get("by_field") or {}
        lr = by_field.get("low_risk") or {}
        parity_context = {
            "parity_overall": parity.get("overall"),
            "low_risk_status": lr.get("status"),
            "low_risk_message": lr.get("message"),
            "low_risk_delta": lr.get("delta"),
        }

    return {
        "generated_at": _now_utc_iso(),
        "inputs": {
            "rankings_present": rankings is not None,
            "watchlist_present": watchlist is not None,
            "parity_present": parity is not None,
            "rankings_as_of": (rankings or {}).get("as_of"),
            "watchlist_as_of": (watchlist or {}).get("as_of"),
        },
        "verdict": verdict,
        "groups": per_group,
        "watchlist_by_source": {
            "watchlist_main_pipeline": by_source_wl_main,
            "watchlist_supp": by_source_wl_supp,
        },
        "overlap_main_vs_watchlist": overlap,
        "parity_context": parity_context,
        "recommendations": recs,
    }


# ---------- HTML rendering ----------


_VERDICT_COLOR = {
    "selection_bias": "#3c8c3c",
    "data_gap": "#b88a00",
    "formula_issue": "#c0392b",
    "mixed": "#b88a00",
    "indeterminate": "#666666",
}


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".") or "0"
    return str(v)


def _render_html(report: dict) -> str:
    v = report["verdict"]["verdict"]
    color = _VERDICT_COLOR.get(v, "#666666")

    parts: list = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Low-Risk Drift Review</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:1080px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
h1{{margin:0 0 4px}} h2{{margin:18px 0 8px;font-size:18px}}
h3{{margin:14px 0 6px;font-size:15px}}
.meta{{color:#666;font-size:13px;margin-bottom:14px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;
       font-weight:600;font-size:12px;letter-spacing:.5px;background:{color}}}
.section{{border:1px solid #e3e3e3;border-radius:8px;padding:14px 16px;margin:14px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.kv{{font-size:13px;color:#444}} .kv pre{{background:#f7f7f7;padding:8px;border-radius:4px;overflow-x:auto}}
.recs li{{margin:4px 0}}
.back{{font-size:13px}}
.flag-on{{color:#c0392b;font-weight:600}}
.flag-off{{color:#888}}
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>Low-Risk Drift Review</h1>
<p class="meta">Generated {escape(report["generated_at"])} &middot; Verdict: <span class="badge">{escape(v)}</span> &middot; confidence: {escape(report["verdict"].get("confidence", ""))}</p>
""")

    # Inputs
    inp = report["inputs"]
    parts.append('<div class="section"><h2>Inputs</h2><table>'
                 '<tr><th>Source</th><th>Present</th><th>as_of</th></tr>'
                 f'<tr><td>data/rankings.json</td><td>{inp["rankings_present"]}</td>'
                 f'<td>{escape(str(inp.get("rankings_as_of") or "—"))}</td></tr>'
                 f'<tr><td>data/watchlist_rankings.json</td><td>{inp["watchlist_present"]}</td>'
                 f'<td>{escape(str(inp.get("watchlist_as_of") or "—"))}</td></tr>'
                 f'<tr><td>data/reports/scoring_parity_review.json</td><td>{inp["parity_present"]}</td>'
                 f'<td>—</td></tr>'
                 '</table></div>')

    # Verdict box
    vd = report["verdict"]
    parts.append('<div class="section"><h2>Verdict</h2>')
    parts.append(f'<p>delta(mean low_risk) wl_main - main = <b>{_fmt(vd.get("delta_mean_low_risk_wl_minus_main"))}</b> '
                 f'&middot; flags: {", ".join(escape(f) for f in vd.get("flags") or []) or "—"}</p>')
    parts.append('<table><tr><th>Flag</th><th>Triggered</th><th>Detail</th></tr>')
    fi = vd.get("formula_flag") or {}
    parts.append(f'<tr><td>formula_issue</td>'
                 f'<td class="{"flag-on" if fi.get("triggered") else "flag-off"}">{fi.get("triggered")}</td>'
                 f'<td>severity={escape(str(fi.get("severity")))} &middot; '
                 f'examples={escape(json.dumps(fi.get("evidence") or [], default=str))[:300]}</td></tr>')
    dg = vd.get("data_gap_flag") or {}
    parts.append(f'<tr><td>data_gap</td>'
                 f'<td class="{"flag-on" if dg.get("triggered") else "flag-off"}">{dg.get("triggered")}</td>'
                 f'<td>main={escape(json.dumps(dg.get("main_rankings_evidence") or [], default=str))[:200]} '
                 f'wlm={escape(json.dumps(dg.get("watchlist_main_evidence") or [], default=str))[:200]}</td></tr>')
    sb = vd.get("selection_bias_flag") or {}
    parts.append(f'<tr><td>selection_bias</td>'
                 f'<td class="{"flag-on" if sb.get("triggered") else "flag-off"}">{sb.get("triggered")}</td>'
                 f'<td>{escape(json.dumps(sb.get("evidence") or [], default=str))[:400]}</td></tr>')
    parts.append('</table></div>')

    # Per-group low_risk distribution
    parts.append('<div class="section"><h2>low_risk distribution by group</h2>')
    parts.append('<table><thead><tr><th>Group</th><th class="num">rows</th>'
                 '<th class="num">n</th><th class="num">mean</th><th class="num">median</th>'
                 '<th class="num">min</th><th class="num">p25</th><th class="num">p75</th>'
                 '<th class="num">max</th><th class="num">null</th></tr></thead><tbody>')
    for g, info in report["groups"].items():
        d = info["low_risk"]
        parts.append(
            f'<tr><td>{escape(g)}</td><td class="num">{info["row_count"]}</td>'
            f'<td class="num">{d["n"]}</td><td class="num">{_fmt(d["mean"])}</td>'
            f'<td class="num">{_fmt(d["median"])}</td><td class="num">{_fmt(d["min"])}</td>'
            f'<td class="num">{_fmt(d["p25"])}</td><td class="num">{_fmt(d["p75"])}</td>'
            f'<td class="num">{_fmt(d["max"])}</td><td class="num">{d["null_count"]}</td></tr>'
        )
    parts.append('</tbody></table></div>')

    # Risk-input coverage by group
    parts.append('<div class="section"><h2>Risk-input coverage</h2>')
    parts.append('<table><thead><tr><th>Group</th><th>field</th>'
                 '<th class="num">present</th><th class="num">null</th>'
                 '<th class="num">% null</th></tr></thead><tbody>')
    for g, info in report["groups"].items():
        cov = info["coverage"]
        for f in RISK_INPUT_FIELDS:
            c = cov.get(f) or {}
            parts.append(
                f'<tr><td>{escape(g)}</td><td>{escape(f)}</td>'
                f'<td class="num">{c.get("present", 0)}</td>'
                f'<td class="num">{c.get("null", 0)}</td>'
                f'<td class="num">{_fmt(c.get("pct_null"))}</td></tr>'
            )
    parts.append('</tbody></table></div>')

    # Vol bucket + sector breakdown by group
    parts.append('<div class="section"><h2>vol_bucket share</h2>'
                 '<table><thead><tr><th>Group</th><th>bucket</th>'
                 '<th class="num">count</th><th class="num">share</th></tr></thead><tbody>')
    for g, info in report["groups"].items():
        vb = info["vol_bucket"]
        for k, c in sorted(vb["counts"].items()):
            share = (vb.get("share") or {}).get(k)
            parts.append(
                f'<tr><td>{escape(g)}</td><td>{escape(k)}</td>'
                f'<td class="num">{c}</td><td class="num">{_fmt(share)}</td></tr>'
            )
    parts.append('</tbody></table></div>')

    parts.append('<div class="section"><h2>Sector mix</h2>'
                 '<table><thead><tr><th>Group</th><th>speculative_share</th>'
                 '<th>top sectors (count)</th></tr></thead><tbody>')
    for g, info in report["groups"].items():
        sb = info["sector"]
        top = sorted(sb["counts"].items(), key=lambda x: -x[1])[:6]
        top_str = ", ".join(f"{escape(k)}={v}" for k, v in top)
        parts.append(
            f'<tr><td>{escape(g)}</td><td>{_fmt(sb.get("speculative_share"))}</td>'
            f'<td>{top_str}</td></tr>'
        )
    parts.append('</tbody></table></div>')

    # Watchlist by source-label
    src = report["watchlist_by_source"]["watchlist_main_pipeline"]
    if src:
        parts.append('<div class="section"><h2>watchlist_main_pipeline by source label</h2>'
                     '<table><thead><tr><th>label</th><th class="num">rows</th>'
                     '<th class="num">low_risk mean</th><th class="num">median</th>'
                     '<th class="num">spec_share</th></tr></thead><tbody>')
        for lbl, info in src.items():
            d = info["low_risk"]
            sb = info["sector"]
            parts.append(
                f'<tr><td>{escape(lbl)}</td><td class="num">{info["row_count"]}</td>'
                f'<td class="num">{_fmt(d.get("mean"))}</td>'
                f'<td class="num">{_fmt(d.get("median"))}</td>'
                f'<td class="num">{_fmt(sb.get("speculative_share"))}</td></tr>'
            )
        parts.append('</tbody></table></div>')

    # Overlap (same-ticker)
    ov = report["overlap_main_vs_watchlist"]
    parts.append('<div class="section"><h2>Same-ticker overlap (main vs watchlist_main_pipeline)</h2>')
    parts.append(f'<p>shared tickers: <b>{ov["shared_count"]}</b> &middot; '
                 f'differing low_risk: <b>{ov["differing_count"]}</b> &middot; '
                 f'max |Δ|: <b>{_fmt(ov["max_abs_delta"])}</b> &middot; '
                 f'mean |Δ| (over differing): <b>{_fmt(ov["mean_abs_delta"])}</b></p>')
    if ov.get("top_diffs"):
        parts.append('<table><thead><tr><th>ticker</th>'
                     '<th class="num">main</th><th class="num">watchlist</th>'
                     '<th class="num">delta</th></tr></thead><tbody>')
        for d in ov["top_diffs"]:
            parts.append(
                f'<tr><td>{escape(str(d.get("ticker") or ""))}</td>'
                f'<td class="num">{_fmt(d.get("main_low_risk"))}</td>'
                f'<td class="num">{_fmt(d.get("wl_low_risk"))}</td>'
                f'<td class="num">{_fmt(d.get("delta"))}</td></tr>'
            )
        parts.append('</tbody></table>')
    parts.append('</div>')

    # Extremes
    parts.append('<div class="section"><h2>Highest / lowest low_risk names per group</h2>')
    for g, info in report["groups"].items():
        ex = info["extremes"]
        parts.append(f'<h3>{escape(g)} — top 5 highest</h3>')
        parts.append('<table><thead><tr><th>ticker</th><th>company</th><th>sector</th>'
                     '<th class="num">low_risk</th><th class="num">atr_pct</th>'
                     '<th>vol</th><th>cap</th></tr></thead><tbody>')
        for r in ex["highest"][:5]:
            parts.append(
                f'<tr><td>{escape(str(r.get("ticker") or ""))}</td>'
                f'<td>{escape(str(r.get("company") or ""))}</td>'
                f'<td>{escape(str(r.get("sector") or ""))}</td>'
                f'<td class="num">{_fmt(r.get("low_risk"))}</td>'
                f'<td class="num">{_fmt(r.get("atr_pct"))}</td>'
                f'<td>{escape(str(r.get("vol_bucket") or ""))}</td>'
                f'<td>{escape(str(r.get("market_cap") or ""))}</td></tr>'
            )
        parts.append('</tbody></table>')
        parts.append(f'<h3>{escape(g)} — bottom 5 lowest</h3>')
        parts.append('<table><thead><tr><th>ticker</th><th>company</th><th>sector</th>'
                     '<th class="num">low_risk</th><th class="num">atr_pct</th>'
                     '<th>vol</th><th>cap</th></tr></thead><tbody>')
        for r in ex["lowest"][:5]:
            parts.append(
                f'<tr><td>{escape(str(r.get("ticker") or ""))}</td>'
                f'<td>{escape(str(r.get("company") or ""))}</td>'
                f'<td>{escape(str(r.get("sector") or ""))}</td>'
                f'<td class="num">{_fmt(r.get("low_risk"))}</td>'
                f'<td class="num">{_fmt(r.get("atr_pct"))}</td>'
                f'<td>{escape(str(r.get("vol_bucket") or ""))}</td>'
                f'<td>{escape(str(r.get("market_cap") or ""))}</td></tr>'
            )
        parts.append('</tbody></table>')
    parts.append('</div>')

    # Parity context
    if report.get("parity_context"):
        parts.append('<div class="section"><h2>Cross-reference: scoring parity</h2>'
                     '<div class="kv"><pre>'
                     + escape(json.dumps(report["parity_context"], indent=2, default=str))
                     + '</pre></div></div>')

    # Recommendations
    parts.append('<div class="section"><h2>Recommendations</h2><ol class="recs">')
    for r in report["recommendations"]:
        parts.append(f'<li>{escape(r)}</li>')
    parts.append('</ol></div>')

    parts.append('</body></html>')
    return "\n".join(parts)


# ---------- main ----------


def main() -> int:
    rankings = _load_json(RANKINGS_FILE)
    watchlist = _load_json(WATCHLIST_FILE)
    parity = _load_json(PARITY_FILE)
    rankings = rankings if isinstance(rankings, dict) else None
    watchlist = watchlist if isinstance(watchlist, dict) else None
    parity = parity if isinstance(parity, dict) else None

    report = build_report(rankings, watchlist, parity)

    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUTPUT.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")

    print(f"[low_risk_drift_review] verdict={report['verdict']['verdict']} "
          f"confidence={report['verdict']['confidence']} -> {JSON_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
