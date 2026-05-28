"""Activity-Adjusted Ranking Review.

Tunes the existing `ai_score` with a transparent, sector-neutral
activity overlay so we can see how the top of the board would shift if
liquidity participation and momentum confirmation were rewarded and
overextended names were nudged down.

This report is computed BEFORE the production scoring adjustment
(``apply_production_scoring_adjustment.py``) runs. The downstream
adjuster reads this report's ``rows`` / ``watchlist_rows`` and folds the
activity multiplier (scaled by ACT_WEIGHT) into the production
``ai_score``. The numbers in this report therefore reflect the overlay
in isolation — useful for auditing the ACT component independently of
the bundled GO/ACC adjustments applied to production rank.

Why this exists
---------------
The current `ai_score` (see Scoring_Engine/score_tickers.py) blends
technical / fundamental / sentiment / risk components but has no
absolute-liquidity dimension and a risk term that rewards beta ~ 1.
The result is a top-25 dominated by mid-cap, low-volume "quality"
names while higher-participation momentum leaders sit lower. Users
expected the ranking to track market activity more closely. We don't
want to hard-code sector preferences; instead we add a small overlay
built from signals that are already gathered:

  * dollar-volume participation (liquidity, log-scaled, sector-blind)
  * relative-volume confirmation (already in technical score; reused)
  * Pine accumulation / go-no-go score (when available)
  * Pine `overextended_bb` cool-off penalty (avoid chasing extended)

The overlay is computed as a multiplicative bump in [0.85, 1.15]
applied to `ai_score`. No constants reference any sector name.

Inputs (read-only, no network):
  - data/rankings.json
  - data/reports/pine_go_no_go_diagnostic.json (optional)

Outputs:
  - data/reports/activity_adjusted_review.json
  - reports/activity-adjusted-review.html
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

RANKINGS_FILE = DATA_DIR / "rankings.json"
WATCHLIST_RANKINGS_FILE = DATA_DIR / "watchlist_rankings.json"
PINE_FILE = DATA_REPORTS_DIR / "pine_go_no_go_diagnostic.json"
JSON_OUT = DATA_REPORTS_DIR / "activity_adjusted_review.json"
HTML_OUT = HTML_REPORTS_DIR / "activity-adjusted-review.html"
TASKS_FILE = DATA_DIR / "tasks.json"
TASK_ID = "activity-adjusted-review"
REPORT_URL = "./reports/activity-adjusted-review.html"
TOP_N_COMPARE = 25


# --- TUNING CONSTANTS -------------------------------------------------------
# All bounded; total bump kept in [MIN_MULT, MAX_MULT] so a single signal
# cannot dominate. Tweak here, not by editing call sites.
LIQUIDITY_PIVOT_DOLLARS = 1.0e8     # $100M/day -> midpoint of liquidity scale
LIQUIDITY_SCALE = 1.5               # log10 spread above pivot for full credit
LIQUIDITY_MAX_BUMP = 0.08           # ±8 % from liquidity term
RELVOL_FULL_BUMP_AT = 1.5           # rel-vol >= 1.5x for full bonus
RELVOL_MAX_BUMP = 0.04              # +4 % from rel-vol term
PINE_GO_FULL_BUMP_AT = 0.8          # Pine go_no_go_score_normalized for full +
PINE_GO_MAX_BUMP = 0.05             # +5 % from Pine accumulation
OVEREXTENDED_PENALTY = -0.06        # -6 % if Pine flags overextended_bb
MIN_MULT = 0.85
MAX_MULT = 1.15


def _safe_float(v, default=0.0):
    try:
        if v is None:
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _liquidity_bump(volume_millions, last_close):
    """Log-scaled dollar-volume bonus, sector-blind.

    dollar_vol < pivot -> negative bump (illiquid penalty)
    dollar_vol > pivot * 10**SCALE -> full positive bump.
    Linear in log10(dollar_vol).
    """
    vol = _safe_float(volume_millions) * 1_000_000.0
    px = _safe_float(last_close)
    if vol <= 0 or px <= 0:
        return 0.0
    dv = vol * px
    # Map log10(dv) onto [-1, +1] around log10(pivot), full scale = LIQUIDITY_SCALE decades
    excess = math.log10(dv) - math.log10(LIQUIDITY_PIVOT_DOLLARS)
    norm = max(-1.0, min(1.0, excess / LIQUIDITY_SCALE))
    return norm * LIQUIDITY_MAX_BUMP


def _relvol_bump(volume_millions, vol_avg_millions):
    """Reward unusual participation. vol_avg unavailable -> 0."""
    v = _safe_float(volume_millions)
    a = _safe_float(vol_avg_millions)
    if v <= 0 or a <= 0:
        return 0.0
    ratio = v / a
    # Linear above 1.0, full bonus at RELVOL_FULL_BUMP_AT.
    norm = max(0.0, min(1.0, (ratio - 1.0) / (RELVOL_FULL_BUMP_AT - 1.0)))
    return norm * RELVOL_MAX_BUMP


def _pine_bump(pine_row):
    """Combine Pine accumulation bonus + overextended_bb penalty."""
    if not pine_row:
        return 0.0, False, 0.0
    score_norm = _safe_float(pine_row.get("go_no_go_score_normalized"))
    bonus = max(0.0, min(1.0, score_norm / PINE_GO_FULL_BUMP_AT)) * PINE_GO_MAX_BUMP
    blockers = pine_row.get("blockers") or []
    overextended = any("overextended_bb" in str(b) for b in blockers)
    penalty = OVEREXTENDED_PENALTY if overextended else 0.0
    return bonus + penalty, overextended, score_norm


def _build_pine_lookup(pine_data):
    if not pine_data:
        return {}
    per_ticker = pine_data.get("per_ticker") or []
    return {row.get("ticker"): row for row in per_ticker if row.get("ticker")}


def _vol_avg_proxy(rows):
    """Median volume across the published top-N is a coarse stand-in for a
    universe-wide rel-vol baseline; it's only used to scale the rel-vol term
    when per-ticker `Vol_SMA_20` isn't exposed in rankings.json. The penalty
    is small and bounded so this proxy can be wrong without doing damage.
    """
    vols = [_safe_float(r.get("volume_millions")) for r in rows]
    vols = [v for v in vols if v > 0]
    if not vols:
        return 1.0
    vols.sort()
    return vols[len(vols) // 2]


def compute_adjustments(rankings, pine_data):
    rows = rankings.get("rows") or []
    pine_lookup = _build_pine_lookup(pine_data)
    median_vol = _vol_avg_proxy(rows)

    enriched = []
    for r in rows:
        ticker = r.get("ticker")
        ai_score = _safe_float(r.get("ai_score"))
        last_close = (r.get("closes") or [None])[-1]

        liq = _liquidity_bump(r.get("volume_millions"), last_close)
        rv = _relvol_bump(r.get("volume_millions"), median_vol)
        pine_b, overextended, pine_norm = _pine_bump(pine_lookup.get(ticker))
        bump = liq + rv + pine_b
        bump = max(MIN_MULT - 1.0, min(MAX_MULT - 1.0, bump))
        activity_score = round(ai_score * (1.0 + bump), 2)

        enriched.append({
            "ticker": ticker,
            "company": r.get("company"),
            "sector": r.get("sector"),
            "industry": r.get("industry"),
            "ai_score": round(ai_score, 2),
            "activity_score": activity_score,
            "delta": round(activity_score - ai_score, 2),
            "ai_rank": r.get("rank"),
            "volume_millions": _safe_float(r.get("volume_millions")),
            "last_close": _safe_float(last_close),
            "liquidity_bump": round(liq, 4),
            "relvol_bump": round(rv, 4),
            "pine_bump": round(pine_b, 4),
            "overextended_bb": overextended,
            "pine_go_norm": round(pine_norm, 3),
        })

    # Re-rank by activity_score (stable: ai_score then ticker).
    enriched.sort(key=lambda x: (-x["activity_score"], -x["ai_score"], x["ticker"] or ""))
    for new_rank, row in enumerate(enriched, 1):
        row["activity_rank"] = new_rank
        row["rank_delta"] = (row["ai_rank"] or new_rank) - new_rank
    return enriched


def top_n_comparison(enriched, n=TOP_N_COMPARE):
    """Build a diagnostic comparison of current top-N vs activity-adjusted
    top-N. Returns dict with `current_top`, `activity_top`, `entrants`
    (in activity top, not current top) and `drops` (in current top, not
    activity top). Each list preserves the rank ordering of its source.
    """
    if not enriched:
        return {
            "n": n,
            "current_top": [],
            "activity_top": [],
            "entrants": [],
            "drops": [],
            "overlap_n": 0,
        }
    by_ai = sorted(enriched, key=lambda r: (r["ai_rank"] if r["ai_rank"] is not None else 10_000))
    current_top = by_ai[:n]
    activity_top = enriched[:n]  # already sorted by activity_rank
    current_set = {r["ticker"] for r in current_top}
    activity_set = {r["ticker"] for r in activity_top}
    entrants = [
        {
            "ticker": r["ticker"],
            "company": r.get("company"),
            "sector": r.get("sector"),
            "ai_rank": r["ai_rank"],
            "activity_rank": r["activity_rank"],
            "rank_delta": r["rank_delta"],
            "ai_score": r["ai_score"],
            "activity_score": r["activity_score"],
        }
        for r in activity_top if r["ticker"] not in current_set
    ]
    drops = [
        {
            "ticker": r["ticker"],
            "company": r.get("company"),
            "sector": r.get("sector"),
            "ai_rank": r["ai_rank"],
            "activity_rank": r["activity_rank"],
            "rank_delta": r["rank_delta"],
            "ai_score": r["ai_score"],
            "activity_score": r["activity_score"],
        }
        for r in current_top if r["ticker"] not in activity_set
    ]
    overlap = len(current_set & activity_set)
    return {
        "n": n,
        "current_top": [
            {
                "ticker": r["ticker"],
                "company": r.get("company"),
                "sector": r.get("sector"),
                "ai_rank": r["ai_rank"],
                "activity_rank": r["activity_rank"],
                "ai_score": r["ai_score"],
                "activity_score": r["activity_score"],
            } for r in current_top
        ],
        "activity_top": [
            {
                "ticker": r["ticker"],
                "company": r.get("company"),
                "sector": r.get("sector"),
                "ai_rank": r["ai_rank"],
                "activity_rank": r["activity_rank"],
                "ai_score": r["ai_score"],
                "activity_score": r["activity_score"],
            } for r in activity_top
        ],
        "entrants": entrants,
        "drops": drops,
        "overlap_n": overlap,
    }


def _verdict(enriched):
    if not enriched:
        return "FAIL", "no rows in rankings.json"
    # Count tickers whose rank moves >= 5 positions either direction.
    movers = sum(1 for r in enriched if abs(r["rank_delta"]) >= 5)
    if movers == 0:
        return "OK", "no meaningful re-ranking; live board already activity-aligned"
    return "INFO", f"{movers} tickers shift >=5 places under activity overlay"


def render_html(payload):
    rows = payload["rows"][:25]
    style = (
        "body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:24px;color:#1f2937;}"
        "h1{font-size:20px;}h2{font-size:16px;margin-top:24px;}"
        "table{border-collapse:collapse;font-size:12px;margin-top:8px;}"
        "th,td{border:1px solid #e5e7eb;padding:4px 8px;text-align:right;}"
        "th{background:#f3f4f6;}td.t,th.t{text-align:left;}"
        ".up{color:#047857;}.down{color:#b91c1c;}.muted{color:#6b7280;}"
    )
    head = (
        f"<!doctype html><meta charset=utf-8><title>Activity-Adjusted Review</title>"
        f"<style>{style}</style>"
        f"<h1>Activity-Adjusted Ranking Review</h1>"
        f"<p class=muted>Generated {escape(payload['generated_at'])} &middot; "
        f"verdict <b>{escape(payload['verdict'])}</b> &middot; {escape(payload['note'])}.</p>"
        f"<p class=muted>Component report. The published <code>ai_score</code> "
        f"already folds this overlay in via the production scoring adjustment "
        f"(scaled by ACT_WEIGHT). Numbers shown here are the ACT piece in "
        f"isolation. Overlay = liquidity (log dollar-vol) + rel-vol + Pine "
        f"accumulation/cool-off, capped at &plusmn;{int((MAX_MULT-1)*100)}%.</p>"
    )
    rows_html = []
    rows_html.append(
        "<table><thead><tr>"
        "<th>#</th><th class=t>Ticker</th><th class=t>Sector</th>"
        "<th>AI</th><th>Activity</th><th>&Delta;</th>"
        "<th>Rank&nbsp;&Delta;</th><th>Vol&nbsp;M</th><th>Pine&nbsp;norm</th><th>Flags</th>"
        "</tr></thead><tbody>"
    )
    for r in rows:
        rd = r["rank_delta"]
        rd_cls = "up" if rd > 0 else ("down" if rd < 0 else "muted")
        flags = []
        if r["overextended_bb"]:
            flags.append("ovx")
        if r["liquidity_bump"] >= 0.04:
            flags.append("liq+")
        elif r["liquidity_bump"] <= -0.04:
            flags.append("liq-")
        if r["relvol_bump"] >= 0.02:
            flags.append("rv+")
        rows_html.append(
            f"<tr><td>{r['activity_rank']}</td>"
            f"<td class=t><b>{escape(str(r['ticker'] or ''))}</b></td>"
            f"<td class=t>{escape(str(r['sector'] or ''))}</td>"
            f"<td>{r['ai_score']:.2f}</td>"
            f"<td>{r['activity_score']:.2f}</td>"
            f"<td class={'up' if r['delta']>0 else ('down' if r['delta']<0 else 'muted')}>{r['delta']:+.2f}</td>"
            f"<td class={rd_cls}>{rd:+d}</td>"
            f"<td>{r['volume_millions']:.1f}</td>"
            f"<td>{r['pine_go_norm']:.2f}</td>"
            f"<td class=t>{escape(' '.join(flags))}</td></tr>"
        )
    rows_html.append("</tbody></table>")
    legend = (
        "<h2>Overlay terms</h2>"
        f"<ul><li><b>Liquidity bump</b>: log10(dollar volume) around ${LIQUIDITY_PIVOT_DOLLARS:,.0f}/day pivot; "
        f"max &plusmn;{int(LIQUIDITY_MAX_BUMP*100)}%.</li>"
        f"<li><b>Rel-vol bump</b>: bonus when today's volume &gt; median of published board; max +{int(RELVOL_MAX_BUMP*100)}%.</li>"
        f"<li><b>Pine bump</b>: + up to {int(PINE_GO_MAX_BUMP*100)}% from <code>go_no_go_score_normalized</code>; "
        f"{int(OVEREXTENDED_PENALTY*100)}% when Pine flags <code>overextended_bb</code>.</li>"
        f"<li><b>Bounds</b>: total multiplier clamped to [{MIN_MULT:.2f}, {MAX_MULT:.2f}].</li></ul>"
    )

    comp = payload.get("top_n_comparison") or {}
    comp_html = _render_top_n_comparison(comp)
    return head + "".join(rows_html) + comp_html + legend


def _render_top_n_comparison(comp):
    if not comp:
        return ""
    n = comp.get("n") or TOP_N_COMPARE
    entrants = comp.get("entrants") or []
    drops = comp.get("drops") or []
    overlap = comp.get("overlap_n") or 0
    activity_top = comp.get("activity_top") or []
    current_top = comp.get("current_top") or []

    def _row(r, link_rank_key):
        rank_val = r.get(link_rank_key)
        delta = (r.get("ai_rank") or 0) - (r.get("activity_rank") or 0)
        cls = "up" if delta > 0 else ("down" if delta < 0 else "muted")
        return (
            f"<tr><td>{rank_val if rank_val is not None else '—'}</td>"
            f"<td class=t><b>{escape(str(r.get('ticker') or ''))}</b></td>"
            f"<td class=t>{escape(str(r.get('sector') or ''))}</td>"
            f"<td>{r.get('ai_rank') if r.get('ai_rank') is not None else '—'}</td>"
            f"<td>{r.get('activity_rank') if r.get('activity_rank') is not None else '—'}</td>"
            f"<td class={cls}>{delta:+d}</td>"
            f"<td>{(r.get('ai_score') or 0):.2f}</td>"
            f"<td>{(r.get('activity_score') or 0):.2f}</td></tr>"
        )

    def _ticker_row(r):
        delta = (r.get("ai_rank") or 0) - (r.get("activity_rank") or 0)
        cls = "up" if delta > 0 else ("down" if delta < 0 else "muted")
        return (
            f"<tr><td class=t><b>{escape(str(r.get('ticker') or ''))}</b></td>"
            f"<td class=t>{escape(str(r.get('company') or ''))}</td>"
            f"<td class=t>{escape(str(r.get('sector') or ''))}</td>"
            f"<td>{r.get('ai_rank') if r.get('ai_rank') is not None else '—'}</td>"
            f"<td>{r.get('activity_rank') if r.get('activity_rank') is not None else '—'}</td>"
            f"<td class={cls}>{delta:+d}</td></tr>"
        )

    def _delta_table(items, caption):
        if not items:
            return (f"<p class=muted>{escape(caption)}: <i>none</i></p>")
        body = "".join(_ticker_row(r) for r in items)
        return (
            f"<h3 style='margin-top:14px'>{escape(caption)} ({len(items)})</h3>"
            "<table><thead><tr><th class=t>Ticker</th><th class=t>Company</th>"
            "<th class=t>Sector</th><th>AI rank</th><th>Act rank</th>"
            "<th>&Delta;</th></tr></thead><tbody>"
            f"{body}</tbody></table>"
        )

    parts = []
    parts.append(f"<h2>Top {n}: current vs activity-adjusted</h2>")
    parts.append(
        f"<p class=muted>Overlap: <b>{overlap}/{n}</b> tickers in both top-{n}s. "
        "Diagnostic only — production rank not changed.</p>"
    )
    parts.append(_delta_table(entrants,
                              f"Activity-only entrants (rise into top {n})"))
    parts.append(_delta_table(drops,
                              f"Current-only drops (fall out of top {n})"))

    # Side-by-side top-N table
    if activity_top and current_top:
        rows = []
        for i in range(min(n, max(len(activity_top), len(current_top)))):
            a = activity_top[i] if i < len(activity_top) else None
            c = current_top[i] if i < len(current_top) else None
            def _cell(r):
                if not r:
                    return "<td class=t>—</td><td>—</td>"
                return (
                    f"<td class=t><b>{escape(str(r.get('ticker') or ''))}</b> "
                    f"<span class=muted>{escape(str(r.get('sector') or ''))}</span></td>"
                    f"<td>{(r.get('activity_score') or 0):.2f}</td>"
                )
            rows.append(
                f"<tr><td>{i+1}</td>"
                f"<td class=t><b>{escape(str(c['ticker'] or '')) if c else '—'}</b> "
                f"<span class=muted>{escape(str(c.get('sector') or '')) if c else ''}</span></td>"
                f"<td>{(c['ai_score'] or 0):.2f}</td>"
                + _cell(a) + "</tr>"
            )
        parts.append("<h3 style='margin-top:14px'>Side-by-side ranks</h3>")
        parts.append(
            "<table><thead><tr><th>#</th>"
            "<th class=t>Current top</th><th>AI</th>"
            "<th class=t>Activity top</th><th>Act</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>"
        )
    return "".join(parts)


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
                summary=payload.get("summary", payload.get("note", "")),
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
        "name": "Activity-Adjusted Review",
        "schedule": "Every refresh (08:45/12:30/15:35 CT, weekdays)",
        "last_run": "—",
        "next_run": "—",
        "status": "Not Run",
        "summary": "—",
        "report_url": REPORT_URL,
    })
    TASKS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main():
    if not RANKINGS_FILE.exists():
        print(f"missing rankings file: {RANKINGS_FILE}", file=sys.stderr)
        return 1
    rankings = json.loads(RANKINGS_FILE.read_text())
    pine_data = None
    if PINE_FILE.exists():
        try:
            pine_data = json.loads(PINE_FILE.read_text())
        except Exception as exc:
            print(f"warning: could not load pine diagnostic: {exc}", file=sys.stderr)

    enriched = compute_adjustments(rankings, pine_data)
    verdict, note = _verdict(enriched)
    comparison = top_n_comparison(enriched, TOP_N_COMPARE)

    # Compute the same overlay for the watchlist board so the watchlist page
    # can surface ACT rank / ACT Δ alongside production rank. Independent
    # re-ranking inside the watchlist universe — production rank is unchanged.
    watchlist_rows = []
    if WATCHLIST_RANKINGS_FILE.exists():
        try:
            wl_rankings = json.loads(WATCHLIST_RANKINGS_FILE.read_text())
            watchlist_rows = compute_adjustments(wl_rankings, pine_data)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: could not load watchlist rankings: {exc}", file=sys.stderr)

    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "verdict": verdict,
        "note": note,
        "overall": verdict,
        "summary": (
            f"top{TOP_N_COMPARE} overlap={comparison['overlap_n']}/{TOP_N_COMPARE} "
            f"entrants={len(comparison['entrants'])} drops={len(comparison['drops'])} "
            f"· {note}"
        ),
        "constants": {
            "liquidity_pivot_dollars": LIQUIDITY_PIVOT_DOLLARS,
            "liquidity_scale_decades": LIQUIDITY_SCALE,
            "liquidity_max_bump": LIQUIDITY_MAX_BUMP,
            "relvol_full_bump_at": RELVOL_FULL_BUMP_AT,
            "relvol_max_bump": RELVOL_MAX_BUMP,
            "pine_go_full_bump_at": PINE_GO_FULL_BUMP_AT,
            "pine_go_max_bump": PINE_GO_MAX_BUMP,
            "overextended_penalty": OVEREXTENDED_PENALTY,
            "min_mult": MIN_MULT,
            "max_mult": MAX_MULT,
        },
        "top_n_comparison": comparison,
        "rows": enriched,
        "watchlist_rows": watchlist_rows,
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
    print(
        f"top{TOP_N_COMPARE} overlap={comparison['overlap_n']}/{TOP_N_COMPARE} "
        f"entrants={len(comparison['entrants'])} drops={len(comparison['drops'])}"
    )
    # Print top 10 movers for log visibility.
    movers = sorted(enriched, key=lambda r: -abs(r["rank_delta"]))[:10]
    for m in movers:
        print(f"  {m['ticker']:<6} ai_rank={m['ai_rank']:>3} -> act_rank={m['activity_rank']:>3} "
              f"({m['rank_delta']:+d}) ai={m['ai_score']:.2f} act={m['activity_score']:.2f} vol={m['volume_millions']:.1f}M")
    return 0


if __name__ == "__main__":
    sys.exit(main())
