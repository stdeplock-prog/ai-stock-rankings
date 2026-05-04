"""Scoring Parity Review — compares score semantics and coverage across
the three published row populations:

  * main rankings (data/rankings.json)
  * watchlist main_pipeline rows (data/watchlist_rankings.json filtered)
  * watchlist supplemental_yfinance ("SUPP") rows

The point of the report is to make it obvious which scores are
apples-to-apples across groups and which rows are technical-only or
otherwise partial. Without this, blending scores or tuning weights across
populations risks treating SUPP rows as if they had full fundamentals when
they don't.

Inputs (read-only):
  - data/rankings.json
  - data/watchlist_rankings.json

Outputs:
  - data/reports/scoring_parity_review.json (machine-readable)
  - reports/scoring-parity-review.html       (human-readable)

Verdict levels per group/component and overall: OK / WARN / FAIL. Missing
inputs are findings, not exceptions.
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

JSON_OUTPUT = DATA_REPORTS_DIR / "scoring_parity_review.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "scoring-parity-review.html"

SCORE_FIELDS = (
    "ai_score",
    "fundamental",
    "technical",
    "sentiment",
    "low_risk",
    "swing_score",
)

PROVENANCE_FIELDS = (
    "data_source",
    "source",
    "fundamental_source",
    "ai_score_basis",
    "eodhd_fundamentals",
    "eodhd_deferred",
    "enrichment_source",
    "instrument_kind",
)

LEVEL_RANK = {"OK": 0, "WARN": 1, "FAIL": 2}


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


# ---------- Numeric helpers ----------


def _is_numeric(v) -> bool:
    return isinstance(v, (int, float)) and not (isinstance(v, float) and v != v)


def distribution(values) -> dict:
    """Return mean/median/min/max/n/null_count for a sequence of mixed values.

    Non-numeric and NaN entries are counted as null but do not poison stats.
    Returns deterministic shape even when no numeric values are present.
    """
    nums = [float(v) for v in values if _is_numeric(v)]
    null_count = sum(1 for v in values if not _is_numeric(v))
    n = len(nums)
    if n == 0:
        return {
            "n": 0,
            "null_count": null_count,
            "mean": None,
            "median": None,
            "min": None,
            "max": None,
        }
    s = sorted(nums)
    if n % 2 == 1:
        median = s[n // 2]
    else:
        median = (s[n // 2 - 1] + s[n // 2]) / 2.0
    return {
        "n": n,
        "null_count": null_count,
        "mean": round(sum(nums) / n, 4),
        "median": round(median, 4),
        "min": round(min(nums), 4),
        "max": round(max(nums), 4),
    }


# ---------- Group extraction ----------


def split_groups(rankings, watchlist) -> dict:
    main = (rankings or {}).get("rows") or []
    wl_rows = (watchlist or {}).get("rows") or []
    wl_main = [r for r in wl_rows if r.get("data_source") == "main_pipeline"]
    wl_supp = [r for r in wl_rows if (r.get("data_source") or "").startswith("supplemental")]
    return {
        "main_rankings": main,
        "watchlist_main_pipeline": wl_main,
        "watchlist_supp": wl_supp,
    }


# ---------- Per-group analysis ----------


def coverage(rows: list) -> dict:
    """Score availability and provenance counts for a single group."""
    n = len(rows)
    score_present = {f: 0 for f in SCORE_FIELDS}
    score_null = {f: 0 for f in SCORE_FIELDS}
    distributions = {}
    for f in SCORE_FIELDS:
        vals = [r.get(f) for r in rows]
        for v in vals:
            if _is_numeric(v):
                score_present[f] += 1
            else:
                score_null[f] += 1
        distributions[f] = distribution(vals)

    provenance: dict = {}
    for f in PROVENANCE_FIELDS:
        c: Counter = Counter()
        absent = 0
        for r in rows:
            if f in r:
                key = r.get(f)
                if isinstance(key, bool):
                    key = f"bool:{key}"
                elif key is None:
                    key = "null"
                else:
                    key = str(key)
                c[key] += 1
            else:
                absent += 1
        provenance[f] = {
            "values": dict(c),
            "field_absent_rows": absent,
        }

    return {
        "row_count": n,
        "score_present": score_present,
        "score_null": score_null,
        "score_present_pct": {
            f: (round(score_present[f] / n, 4) if n else None)
            for f in SCORE_FIELDS
        },
        "distributions": distributions,
        "provenance": provenance,
    }


# ---------- Verdict logic ----------


def verdict_for_component(group: str, field: str, present: int, total: int) -> dict:
    """Decide OK/WARN/FAIL per (group, score field).

    Rules — calibrated so verdicts read sensibly given known SUPP fundamentals
    weakness today:

      * main_rankings: any score with >5% null → WARN, >25% null → FAIL.
      * watchlist_main_pipeline: same thresholds as main; this group is
        meant to share semantics with main rankings.
      * watchlist_supp: ai_score and technical are expected to exist.
        fundamental/sentiment/low_risk/swing_score are KNOWN to be partial
        in the absence of EODHD enrichment, so missing values there are
        WARN (not FAIL) and noted as "by design until EODHD enriched".
    """
    if total <= 0:
        return {"status": "OK", "message": "no rows", "present": 0, "total": 0}
    pct_null = 1.0 - (present / total)

    if group == "watchlist_supp" and field != "ai_score" and field != "technical":
        # Partial-by-design fields on SUPP: WARN cap, even at 100% null.
        if pct_null >= 0.20:
            status = "WARN"
            note = "partial-by-design until EODHD enrichment lands"
        else:
            status = "OK"
            note = "partial-by-design; coverage unexpectedly high — sanity-check"
        return {
            "status": status,
            "message": f"{present}/{total} present ({1 - pct_null:.0%}) — {note}",
            "present": present,
            "total": total,
            "pct_null": round(pct_null, 4),
            "rationale": note,
        }

    # All other (group, field) combinations: full-coverage expectation.
    if pct_null >= 0.25:
        status = "FAIL"
    elif pct_null >= 0.05:
        status = "WARN"
    else:
        status = "OK"
    return {
        "status": status,
        "message": f"{present}/{total} present ({1 - pct_null:.0%})",
        "present": present,
        "total": total,
        "pct_null": round(pct_null, 4),
    }


def parity_verdicts(group_coverage: dict) -> dict:
    """Build the per-group verdict matrix."""
    out: dict = {}
    for group, cov in group_coverage.items():
        total = cov["row_count"]
        components: dict = {}
        for f in SCORE_FIELDS:
            present = cov["score_present"][f]
            components[f] = verdict_for_component(group, f, present, total)
        # Group-level rollup is the worst component.
        rollup = "OK"
        for v in components.values():
            rollup = _worst(rollup, v["status"])
        out[group] = {"status": rollup, "components": components}
    return out


# ---------- Cross-group parity verdicts ----------


def cross_group_parity(group_coverage: dict) -> dict:
    """Compare distribution alignment between main_rankings and watchlist
    main_pipeline. These two are expected to share semantics; large
    distribution drift would suggest weights or pipeline drift.

    SUPP is intentionally NOT compared here — it is partial-by-design.
    """
    main = group_coverage.get("main_rankings", {})
    wlm = group_coverage.get("watchlist_main_pipeline", {})
    main_d = main.get("distributions") or {}
    wlm_d = wlm.get("distributions") or {}
    drift: dict = {}
    rollup = "OK"
    for f in SCORE_FIELDS:
        a = main_d.get(f) or {}
        b = wlm_d.get(f) or {}
        if a.get("mean") is None or b.get("mean") is None:
            drift[f] = {
                "status": "WARN",
                "message": "missing distribution in one of the groups",
                "main_mean": a.get("mean"),
                "watchlist_main_mean": b.get("mean"),
            }
            rollup = _worst(rollup, "WARN")
            continue
        delta = round(b["mean"] - a["mean"], 4)
        abs_delta = abs(delta)
        # Scores are on a 0-10 scale; >1.0 mean drift between two universes
        # that are supposed to score with the same engine is a meaningful gap.
        if abs_delta >= 1.5:
            status = "FAIL"
        elif abs_delta >= 0.75:
            status = "WARN"
        else:
            status = "OK"
        drift[f] = {
            "status": status,
            "message": (
                f"mean delta watchlist_main - main = {delta:+.2f} "
                f"(main={a['mean']}, wlm={b['mean']})"
            ),
            "main_mean": a["mean"],
            "watchlist_main_mean": b["mean"],
            "delta": delta,
        }
        rollup = _worst(rollup, status)
    return {"status": rollup, "by_field": drift}


# ---------- Examples / spotlight ----------


def supp_examples(supp_rows: list, limit: int = 10) -> list:
    """Top SUPP rows by ai_score where AI is technical-only or fundamentals
    are missing. These are the canonical 'looks great, but only on
    technicals' rows the user wants to eyeball.
    """
    candidates = []
    for r in supp_rows:
        ai = r.get("ai_score")
        if not _is_numeric(ai):
            continue
        is_tech_only = (r.get("ai_score_basis") == "supp_technical_only")
        no_fund = not _is_numeric(r.get("fundamental"))
        if is_tech_only or no_fund:
            candidates.append({
                "ticker": r.get("ticker"),
                "company": r.get("company"),
                "ai_score": ai,
                "ai_score_basis": r.get("ai_score_basis"),
                "fundamental": r.get("fundamental"),
                "technical": r.get("technical"),
                "sentiment": r.get("sentiment"),
                "low_risk": r.get("low_risk"),
                "swing_score": r.get("swing_score"),
                "data_source": r.get("data_source"),
                "fundamental_source": r.get("fundamental_source"),
                "eodhd_fundamentals": r.get("eodhd_fundamentals"),
                "eodhd_deferred": r.get("eodhd_deferred"),
                "instrument_kind": r.get("instrument_kind"),
                "enrichment_source": r.get("enrichment_source"),
            })
    candidates.sort(key=lambda x: (-(x["ai_score"] or 0), x["ticker"] or ""))
    return candidates[:limit]


# ---------- Recommendations ----------


def recommendations(group_coverage: dict, group_verdicts: dict, cross: dict) -> list:
    """Return a short, ordered list of actionable items. Empty list means
    'safe to blend / tune weights as-is' — which is rare in practice.
    """
    out: list = []
    supp_cov = group_coverage.get("watchlist_supp", {})
    supp_score_null = supp_cov.get("score_null") or {}
    supp_total = supp_cov.get("row_count") or 0

    if supp_total > 0 and supp_score_null.get("fundamental", 0) >= supp_total * 0.5:
        out.append(
            "Do NOT blend SUPP ai_score with main ai_score until EODHD "
            "fundamentals enrichment is restored — current SUPP rows are "
            "majority technical-only."
        )

    eodhd_present = (supp_cov.get("provenance") or {}).get("eodhd_fundamentals") or {}
    eodhd_vals = eodhd_present.get("values") or {}
    if supp_total > 0 and eodhd_vals.get("bool:True", 0) == 0:
        out.append(
            "EODHD fundamentals on SUPP rows is 0 — confirm `EODHD_API_KEY` "
            "secret is populated in CI before treating SUPP fundamentals as live."
        )

    if cross.get("status") in ("WARN", "FAIL"):
        worst_field = None
        worst_delta = 0.0
        for f, info in cross["by_field"].items():
            d = info.get("delta")
            if d is not None and abs(d) > abs(worst_delta):
                worst_field = f
                worst_delta = d
        if worst_field:
            out.append(
                f"Investigate score drift between main_rankings and "
                f"watchlist_main_pipeline on `{worst_field}` "
                f"(mean delta {worst_delta:+.2f}). Same scoring engine should "
                f"produce near-identical means; gap suggests universe mix or "
                f"weight drift."
            )

    main_v = group_verdicts.get("main_rankings", {}).get("status", "OK")
    wlm_v = group_verdicts.get("watchlist_main_pipeline", {}).get("status", "OK")
    if "FAIL" in (main_v, wlm_v):
        out.append(
            "Main/watchlist-main coverage has FAIL components — fix before "
            "tuning weights; partial coverage skews any optimization."
        )

    if not out:
        out.append(
            "All checks OK — score blending across main_rankings and "
            "watchlist_main_pipeline is justifiable; SUPP remains separate."
        )
    return out


# ---------- Top-level build ----------


def build_report(rankings, watchlist) -> dict:
    groups = split_groups(rankings, watchlist)
    group_coverage = {g: coverage(rows) for g, rows in groups.items()}
    group_verdicts = parity_verdicts(group_coverage)
    cross = cross_group_parity(group_coverage)
    examples = supp_examples(groups["watchlist_supp"])
    recs = recommendations(group_coverage, group_verdicts, cross)

    overall = "OK"
    for v in group_verdicts.values():
        overall = _worst(overall, v.get("status", "OK"))
    overall = _worst(overall, cross.get("status", "OK"))
    if rankings is None:
        overall = _worst(overall, "FAIL")
    if watchlist is None:
        overall = _worst(overall, "WARN")

    return {
        "generated_at": _now_utc_iso(),
        "overall": overall,
        "inputs": {
            "rankings_present": rankings is not None,
            "watchlist_present": watchlist is not None,
            "rankings_as_of": (rankings or {}).get("as_of"),
            "watchlist_as_of": (watchlist or {}).get("as_of"),
        },
        "groups": group_coverage,
        "verdicts": group_verdicts,
        "cross_group_parity": cross,
        "supp_examples": examples,
        "recommendations": recs,
    }


# ---------- HTML rendering ----------


_LEVEL_COLOR = {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}


def _render_html(report: dict) -> str:
    overall = report["overall"]
    overall_color = _LEVEL_COLOR[overall]
    parts: list = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Scoring Parity Review</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:1080px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
h1{{margin:0 0 4px}} h2{{margin:18px 0 8px;font-size:18px}}
h3{{margin:14px 0 6px;font-size:15px}}
.meta{{color:#666;font-size:13px;margin-bottom:14px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;
       font-weight:600;font-size:12px;letter-spacing:.5px;background:{overall_color}}}
.section{{border:1px solid #e3e3e3;border-radius:8px;padding:14px 16px;margin:14px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
.OK{{color:#3c8c3c;font-weight:600}}
.WARN{{color:#b88a00;font-weight:600}}
.FAIL{{color:#c0392b;font-weight:600}}
.kv{{font-size:13px;color:#444}} .kv pre{{background:#f7f7f7;padding:8px;border-radius:4px;overflow-x:auto}}
.recs li{{margin:4px 0}}
.back{{font-size:13px}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>Scoring Parity Review</h1>
<p class="meta">Generated {escape(report["generated_at"])} &middot; Overall: <span class="badge">{escape(overall)}</span></p>
""")

    # Inputs
    inp = report["inputs"]
    parts.append('<div class="section"><h2>Inputs</h2><table>'
                 '<tr><th>File</th><th>Present</th><th>as_of</th></tr>'
                 f'<tr><td>data/rankings.json</td><td>{inp["rankings_present"]}</td>'
                 f'<td>{escape(str(inp.get("rankings_as_of") or "—"))}</td></tr>'
                 f'<tr><td>data/watchlist_rankings.json</td><td>{inp["watchlist_present"]}</td>'
                 f'<td>{escape(str(inp.get("watchlist_as_of") or "—"))}</td></tr>'
                 '</table></div>')

    # Coverage matrix per group
    parts.append('<div class="section"><h2>Coverage by group</h2>')
    parts.append('<table><thead><tr><th>Group</th><th class="num">Rows</th>'
                 + ''.join(f'<th class="num">{f}</th>' for f in SCORE_FIELDS)
                 + '</tr></thead><tbody>')
    for group, cov in report["groups"].items():
        parts.append(f'<tr><td>{escape(group)}</td><td class="num">{cov["row_count"]}</td>')
        for f in SCORE_FIELDS:
            present = cov["score_present"][f]
            total = cov["row_count"]
            pct = (present / total) if total else 0.0
            parts.append(f'<td class="num">{present}/{total} ({pct:.0%})</td>')
        parts.append('</tr>')
    parts.append('</tbody></table></div>')

    # Verdict matrix per group
    parts.append('<div class="section"><h2>Per-group verdicts</h2>')
    for group, ver in report["verdicts"].items():
        st = ver.get("status", "OK")
        parts.append(f'<h3>{escape(group)} <span class="{st}">{st}</span></h3>')
        parts.append('<table><thead><tr><th>Score</th><th>Status</th><th>Detail</th></tr></thead><tbody>')
        for f, comp in ver["components"].items():
            parts.append(
                f'<tr><td>{escape(f)}</td>'
                f'<td class="{escape(comp["status"])}">{escape(comp["status"])}</td>'
                f'<td>{escape(comp.get("message", ""))}</td></tr>'
            )
        parts.append('</tbody></table>')
    parts.append('</div>')

    # Cross-group parity
    cross = report["cross_group_parity"]
    parts.append(f'<div class="section"><h2>Cross-group parity '
                 f'(main_rankings vs watchlist_main_pipeline) '
                 f'<span class="{cross["status"]}">{cross["status"]}</span></h2>')
    parts.append('<table><thead><tr><th>Score</th><th>Status</th><th>Detail</th></tr></thead><tbody>')
    for f, info in cross["by_field"].items():
        parts.append(
            f'<tr><td>{escape(f)}</td>'
            f'<td class="{escape(info["status"])}">{escape(info["status"])}</td>'
            f'<td>{escape(info.get("message", ""))}</td></tr>'
        )
    parts.append('</tbody></table></div>')

    # Distribution summary
    parts.append('<div class="section"><h2>Distribution summary</h2>')
    parts.append('<table><thead><tr><th>Group</th><th>Field</th>'
                 '<th class="num">n</th><th class="num">mean</th>'
                 '<th class="num">median</th><th class="num">min</th>'
                 '<th class="num">max</th><th class="num">null</th>'
                 '</tr></thead><tbody>')
    for group, cov in report["groups"].items():
        for f, d in cov["distributions"].items():
            parts.append(
                f'<tr><td>{escape(group)}</td><td>{escape(f)}</td>'
                f'<td class="num">{d["n"]}</td>'
                f'<td class="num">{d["mean"] if d["mean"] is not None else "—"}</td>'
                f'<td class="num">{d["median"] if d["median"] is not None else "—"}</td>'
                f'<td class="num">{d["min"] if d["min"] is not None else "—"}</td>'
                f'<td class="num">{d["max"] if d["max"] is not None else "—"}</td>'
                f'<td class="num">{d["null_count"]}</td></tr>'
            )
    parts.append('</tbody></table></div>')

    # Provenance breakdowns
    parts.append('<div class="section"><h2>Provenance / basis counts</h2>')
    for group, cov in report["groups"].items():
        parts.append(f'<h3>{escape(group)}</h3>')
        parts.append('<div class="kv"><pre>'
                     + escape(json.dumps(cov["provenance"], indent=2, default=str))
                     + '</pre></div>')
    parts.append('</div>')

    # SUPP examples
    examples = report["supp_examples"]
    parts.append('<div class="section"><h2>Top SUPP rows: technical-only or '
                 f'missing fundamentals (showing {len(examples)})</h2>')
    if not examples:
        parts.append('<p>None found.</p>')
    else:
        parts.append('<table><thead><tr><th>Ticker</th><th>Company</th>'
                     '<th class="num">AI</th><th>Basis</th>'
                     '<th class="num">Fund</th><th class="num">Tech</th>'
                     '<th class="num">Sent</th><th class="num">Risk</th>'
                     '<th>Source</th><th>EODHD</th><th>Deferred</th><th>Kind</th>'
                     '</tr></thead><tbody>')
        for x in examples:
            parts.append(
                '<tr>'
                f'<td>{escape(str(x.get("ticker") or ""))}</td>'
                f'<td>{escape(str(x.get("company") or ""))}</td>'
                f'<td class="num">{escape(str(x.get("ai_score") if x.get("ai_score") is not None else "—"))}</td>'
                f'<td>{escape(str(x.get("ai_score_basis") or "—"))}</td>'
                f'<td class="num">{escape(str(x.get("fundamental") if x.get("fundamental") is not None else "—"))}</td>'
                f'<td class="num">{escape(str(x.get("technical") if x.get("technical") is not None else "—"))}</td>'
                f'<td class="num">{escape(str(x.get("sentiment") if x.get("sentiment") is not None else "—"))}</td>'
                f'<td class="num">{escape(str(x.get("low_risk") if x.get("low_risk") is not None else "—"))}</td>'
                f'<td>{escape(str(x.get("data_source") or "—"))}</td>'
                f'<td>{escape(str(x.get("eodhd_fundamentals")))}</td>'
                f'<td>{escape(str(x.get("eodhd_deferred")))}</td>'
                f'<td>{escape(str(x.get("instrument_kind") or "—"))}</td>'
                '</tr>'
            )
        parts.append('</tbody></table>')
    parts.append('</div>')

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
    rankings = rankings if isinstance(rankings, dict) else None
    watchlist = watchlist if isinstance(watchlist, dict) else None

    report = build_report(rankings, watchlist)

    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    JSON_OUTPUT.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")

    print(f"[scoring_parity_review] overall={report['overall']} -> {JSON_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
