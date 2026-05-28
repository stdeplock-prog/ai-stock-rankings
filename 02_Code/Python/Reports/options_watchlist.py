"""Options & Earnings Watchlist — rolling, data-driven research list.

Generates a per-refresh research watchlist of names that look like reasonable
candidates for a longer-dated options *research* list (NOT trade advice) based
on signals already produced elsewhere in the pipeline. Replaces the previous
static April 2026 markdown-driven report so the dashboard row is no longer
frozen on stale picks.

Inputs (read-only, no network):
  - data/rankings.json                                  (main rankings)
  - data/watchlist_rankings.json                        (user watchlist)
  - data/reports/pine_go_no_go_diagnostic.json          (optional)
  - data/reports/cooloff_cohort_tracking.json           (optional)
  - data/reports/external_benchmark_review.json         (optional)

Outputs:
  - data/reports/options_watchlist.json
  - reports/options-watchlist.html
  - data/tasks.json row id=options-earnings-watchlist stamped each run

Selection (conservative, research only):
  Universe: union of top-50 from main rankings + top-50 from watchlist.
  Hard filters:
    - go_label == "GO"
    - acc_label in {"HIGH", "MID"}
    - ai_score >= 8.0
    - volume_millions >= 0.5
    - ticker NOT in cool-off overextended_bb cohort
    - ticker NOT a blocker per Pine diagnostic
  Composite ranking:
    composite = ai_score
              + 0.30 * swing_score
              + 1.00  if Pine clean-go normalized score >= 0.7
              + 0.50  if external benchmark confirms internal bullish view
              - 0.50  if days_to_earnings is not None and <= 7
  Top 15 (or fewer when filters cut deep) are surfaced as candidates with
  reasons + cautions. Earnings within 7d are flagged but not excluded.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"
HTML_REPORTS_DIR = REPO_ROOT / "reports"

RANKINGS_FILE = DATA_DIR / "rankings.json"
WATCHLIST_FILE = DATA_DIR / "watchlist_rankings.json"
PINE_FILE = DATA_REPORTS_DIR / "pine_go_no_go_diagnostic.json"
COOLOFF_FILE = DATA_REPORTS_DIR / "cooloff_cohort_tracking.json"
EXTERNAL_FILE = DATA_REPORTS_DIR / "external_benchmark_review.json"

JSON_OUT = DATA_REPORTS_DIR / "options_watchlist.json"
HTML_OUT = HTML_REPORTS_DIR / "options-watchlist.html"
TASKS_FILE = DATA_DIR / "tasks.json"
TASK_ID = "options-earnings-watchlist"
REPORT_URL = "./reports/options-watchlist.html"

TOP_N_CANDIDATES = 15
UNIVERSE_DEPTH = 50

MIN_AI_SCORE = 8.0
MIN_VOLUME_M = 0.5
EARNINGS_CAUTION_DAYS = 7
PINE_CLEAN_GO_THRESHOLD = 0.7


def _load_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _num(v, default=None):
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_universe(main_rows, watchlist_rows, depth=UNIVERSE_DEPTH):
    """Union of top-`depth` rows from each input, de-duped by ticker.

    Watchlist row wins on duplicate so SUPP-only enrichment is preserved.
    """
    by_ticker = {}
    for row in (main_rows or [])[:depth]:
        t = row.get("ticker")
        if t:
            by_ticker[t] = {**row, "_source": "main"}
    for row in (watchlist_rows or [])[:depth]:
        t = row.get("ticker")
        if not t:
            continue
        if t in by_ticker:
            by_ticker[t] = {**by_ticker[t], **row, "_source": "both"}
        else:
            by_ticker[t] = {**row, "_source": "watchlist"}
    return list(by_ticker.values())


def _build_pine_index(pine):
    """Return dict ticker -> {go_normalized, blockers, blocker_count}."""
    out = {}
    if not isinstance(pine, dict):
        return out
    for row in pine.get("per_ticker") or []:
        t = row.get("ticker")
        if not t:
            continue
        out[t] = {
            "go_normalized": _num(row.get("go_no_go_score_normalized"), 0.0),
            "blocker_count": int(row.get("blocker_count") or 0),
            "blockers": list(row.get("blockers") or []),
        }
    return out


def _build_external_confirms(pine):
    """Return set of tickers where Pine's disagreement_supports_internal
    surfaces them as 'keep' (internal bullish thesis confirmed by gate stack
    when external sources pushed back). This is a conservative confirmation
    proxy — true ticker-level external agreement is not currently produced
    by external_benchmark_review, so we use the diagnostic that does map
    1-to-1 with internal-bullish-supported-by-evidence tickers.
    """
    confirms = set()
    if not isinstance(pine, dict):
        return confirms
    for row in pine.get("disagreement_supports_internal") or []:
        t = row.get("ticker")
        if t and (row.get("action") or "").lower() == "keep":
            confirms.add(t)
    return confirms


def _build_overextended_set(cooloff):
    if not isinstance(cooloff, dict):
        return set()
    members = (cooloff.get("current_cohort_members") or {}).get("overextended_bb") or []
    return {t for t in members if isinstance(t, str)}


def evaluate_candidate(row, pine_idx, overextended, confirms):
    """Apply filters and compute composite. Returns dict with status."""
    ticker = row.get("ticker")
    ai_score = _num(row.get("ai_score"), 0.0)
    swing_score = _num(row.get("swing_score"), 0.0)
    volume_m = _num(row.get("volume_millions"), 0.0)
    go_label = (row.get("go_label") or "").upper()
    acc_label = (row.get("acc_label") or "").upper()
    days_to_earnings = row.get("days_to_earnings")
    try:
        dte = int(days_to_earnings) if days_to_earnings is not None else None
    except (TypeError, ValueError):
        dte = None

    reasons = []
    cautions = []

    # Hard filters.
    if go_label != "GO":
        return {"ticker": ticker, "include": False, "reject": f"go_label={go_label or 'n/a'}"}
    if acc_label not in {"HIGH", "MID"}:
        return {"ticker": ticker, "include": False, "reject": f"acc_label={acc_label or 'n/a'}"}
    if ai_score < MIN_AI_SCORE:
        return {"ticker": ticker, "include": False, "reject": f"ai_score={ai_score:.2f}"}
    if volume_m < MIN_VOLUME_M:
        return {"ticker": ticker, "include": False, "reject": f"volume_m={volume_m:.2f}"}
    if ticker in overextended:
        return {"ticker": ticker, "include": False, "reject": "cool-off overextended_bb"}
    pine_info = pine_idx.get(ticker, {})
    if pine_info.get("blocker_count", 0) > 0:
        return {"ticker": ticker, "include": False, "reject": "pine blocker"}

    # Composite.
    composite = ai_score + 0.30 * swing_score
    reasons.append(f"AI {ai_score:.2f}")
    reasons.append(f"GO/{acc_label}")

    pine_go = pine_info.get("go_normalized", 0.0)
    pine_clean = pine_go >= PINE_CLEAN_GO_THRESHOLD
    if pine_clean:
        composite += 1.0
        reasons.append(f"Pine clean-go {pine_go:.2f}")

    if ticker in confirms:
        composite += 0.5
        reasons.append("external confirms")

    if swing_score and swing_score >= 7.0:
        reasons.append(f"swing {swing_score:.1f}")

    if dte is not None and dte <= EARNINGS_CAUTION_DAYS:
        composite -= 0.5
        cautions.append(f"earnings in {dte}d")

    if volume_m < 1.0:
        cautions.append(f"thinner liquidity ({volume_m:.1f}M)")

    return {
        "ticker": ticker,
        "company": row.get("company"),
        "sector": row.get("sector"),
        "ai_score": ai_score,
        "swing_score": swing_score,
        "go_label": go_label,
        "acc_label": acc_label,
        "volume_m": volume_m,
        "days_to_earnings": dte,
        "next_earnings": row.get("next_earnings"),
        "pine_go_normalized": pine_go,
        "pine_clean_go": pine_clean,
        "external_confirms": ticker in confirms,
        "source": row.get("_source"),
        "composite": round(composite, 3),
        "reasons": reasons,
        "cautions": cautions,
        "include": True,
    }


def select_candidates(universe, pine_idx, overextended, confirms, top_n=TOP_N_CANDIDATES):
    evaluated = [evaluate_candidate(r, pine_idx, overextended, confirms) for r in universe]
    included = [e for e in evaluated if e.get("include")]
    included.sort(key=lambda e: e["composite"], reverse=True)
    rejected = [e for e in evaluated if not e.get("include")]
    return included[:top_n], rejected


def build_report():
    rankings = _load_json(RANKINGS_FILE) or {}
    watchlist = _load_json(WATCHLIST_FILE) or {}
    pine = _load_json(PINE_FILE)
    cooloff = _load_json(COOLOFF_FILE)
    external = _load_json(EXTERNAL_FILE)

    main_rows = rankings.get("rows") or []
    wl_rows = watchlist.get("rows") or []

    pine_idx = _build_pine_index(pine)
    overextended = _build_overextended_set(cooloff)
    confirms = _build_external_confirms(pine)
    _ = external  # external_benchmark_review reserved for future ticker-level confirms

    universe = build_universe(main_rows, wl_rows)
    candidates, rejected = select_candidates(universe, pine_idx, overextended, confirms)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    report = {
        "generated_at": generated_at,
        "as_of_rankings": rankings.get("as_of"),
        "as_of_watchlist": watchlist.get("as_of"),
        "universe_size": len(universe),
        "candidate_count": len(candidates),
        "constants": {
            "min_ai_score": MIN_AI_SCORE,
            "min_volume_m": MIN_VOLUME_M,
            "earnings_caution_days": EARNINGS_CAUTION_DAYS,
            "pine_clean_go_threshold": PINE_CLEAN_GO_THRESHOLD,
            "universe_depth": UNIVERSE_DEPTH,
            "top_n_candidates": TOP_N_CANDIDATES,
        },
        "inputs": {
            "rankings_present": bool(main_rows),
            "watchlist_present": bool(wl_rows),
            "pine_present": isinstance(pine, dict),
            "cooloff_present": isinstance(cooloff, dict),
            "external_present": isinstance(external, dict),
            "overextended_excluded_count": len(overextended),
            "external_confirms_count": len(confirms),
        },
        "candidates": candidates,
        "rejected_sample": rejected[:20],
        "disclaimer": (
            "Research watchlist only — NOT trade advice. Candidate selection is "
            "diagnostic and derived from already-published rankings/diagnostics. "
            "No options chains, IV data, or order sizing is implied."
        ),
    }
    return report


def render_html(report):
    generated = report["generated_at"]
    as_of_r = report.get("as_of_rankings") or "—"
    as_of_w = report.get("as_of_watchlist") or "—"
    candidates = report.get("candidates") or []
    constants = report.get("constants") or {}
    inputs = report.get("inputs") or {}

    rows_html = []
    for i, c in enumerate(candidates, 1):
        reasons = ", ".join(c.get("reasons") or [])
        cautions = "; ".join(c.get("cautions") or []) or "—"
        earn = c.get("next_earnings") or "—"
        dte = c.get("days_to_earnings")
        dte_str = f"{dte}d" if isinstance(dte, int) else "—"
        rows_html.append(f"""
          <tr>
            <td>{i}</td>
            <td><strong>{escape(c.get('ticker') or '')}</strong></td>
            <td>{escape(c.get('company') or '')}</td>
            <td>{escape(c.get('sector') or '')}</td>
            <td>{c.get('ai_score', 0):.2f}</td>
            <td>{c.get('swing_score') or 0:.1f}</td>
            <td>{c.get('volume_m') or 0:.1f}M</td>
            <td>{escape(earn)} ({dte_str})</td>
            <td>{c.get('composite', 0):.2f}</td>
            <td>{escape(reasons)}</td>
            <td>{escape(cautions)}</td>
          </tr>
        """)

    table = "".join(rows_html) if rows_html else (
        '<tr><td colspan="11" style="color:var(--muted)">No candidates met filters this run.</td></tr>'
    )

    filters_html = (
        f"<li>go_label = GO</li>"
        f"<li>acc_label in {{HIGH, MID}}</li>"
        f"<li>ai_score &ge; {constants.get('min_ai_score')}</li>"
        f"<li>volume &ge; {constants.get('min_volume_m')}M</li>"
        f"<li>not in cool-off overextended_bb (excluded: {inputs.get('overextended_excluded_count', 0)})</li>"
        f"<li>no Pine blockers</li>"
    )

    composite_html = (
        "<li>composite = ai_score + 0.30 &middot; swing_score</li>"
        f"<li>+ 1.00 if Pine clean-go (normalized &ge; {constants.get('pine_clean_go_threshold')})</li>"
        "<li>+ 0.50 if external benchmark confirms internal bullish view</li>"
        f"<li>&minus; 0.50 if days_to_earnings &le; {constants.get('earnings_caution_days')}</li>"
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Options &amp; Earnings Watchlist</title>
  <style>
    :root {{ --bg:#0b1220; --panel:#111827; --panel2:#172033; --line:#243043; --text:#e5eefc; --muted:#93a4bd; }}
    body {{ margin:0; font-family:Inter,Arial,sans-serif; background:var(--bg); color:var(--text); }}
    header {{ background:var(--panel2); border-bottom:1px solid var(--line); padding:14px 18px; }}
    h1 {{ margin:0; font-size:20px; }}
    .meta {{ color:var(--muted); font-size:12px; margin-top:4px; }}
    main {{ padding:18px; max-width:1180px; margin:0 auto; }}
    section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px 18px; margin-bottom:14px; }}
    section h2 {{ margin:0 0 8px; font-size:15px; color:var(--text); }}
    table {{ width:100%; border-collapse:collapse; font-size:12.5px; }}
    th, td {{ padding:6px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ color:var(--muted); text-transform:uppercase; font-size:10.5px; letter-spacing:.05em; background:var(--panel2); }}
    ul {{ margin:4px 0 0 18px; padding:0; color:#d1d5db; font-size:13px; line-height:1.6; }}
    .disclaimer {{ color:#fbbf24; font-size:12px; }}
    .back {{ display:inline-block; margin-top:14px; color:#60a5fa; text-decoration:none; }}
    .back:hover {{ text-decoration:underline; }}
  </style>
</head>
<body>
  <header>
    <h1>Options &amp; Earnings Watchlist</h1>
    <div class="meta">
      Generated {escape(generated)} &middot;
      rankings as_of {escape(as_of_r)} &middot;
      watchlist as_of {escape(as_of_w)} &middot;
      universe={report.get('universe_size', 0)} candidates={report.get('candidate_count', 0)}
    </div>
  </header>
  <main>
    <section>
      <h2>Research watchlist (NOT trade advice)</h2>
      <p class="disclaimer">{escape(report.get('disclaimer', ''))}</p>
    </section>

    <section>
      <h2>Top candidates</h2>
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Ticker</th>
            <th>Company</th>
            <th>Sector</th>
            <th>AI</th>
            <th>Swing</th>
            <th>Vol</th>
            <th>Earnings</th>
            <th>Composite</th>
            <th>Reasons</th>
            <th>Cautions</th>
          </tr>
        </thead>
        <tbody>
          {table}
        </tbody>
      </table>
    </section>

    <section>
      <h2>Selection formula</h2>
      <strong>Hard filters:</strong>
      <ul>{filters_html}</ul>
      <strong>Composite ranking:</strong>
      <ul>{composite_html}</ul>
      <div class="meta" style="margin-top:8px">
        Pine clean-go names enriched: {sum(1 for c in candidates if c.get('pine_clean_go'))} /
        external-confirmed names: {sum(1 for c in candidates if c.get('external_confirms'))}.
      </div>
    </section>

    <a class="back" href="../index.html">&larr; Back to dashboard</a>
  </main>
</body>
</html>
"""
    return html


def _summary(report) -> str:
    candidates = report.get("candidates") or []
    if not candidates:
        return "No candidates passed conservative filters this refresh."
    top = ", ".join((c.get("ticker") or "") for c in candidates[:5] if c.get("ticker"))
    near_earn = sum(
        1 for c in candidates
        if isinstance(c.get("days_to_earnings"), int) and c["days_to_earnings"] <= EARNINGS_CAUTION_DAYS
    )
    return (
        f"{len(candidates)} candidates (top: {top})"
        f" · {near_earn} flagged near-earnings"
    )


def render():
    report = build_report()
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    html = render_html(report)
    HTML_OUT.parent.mkdir(parents=True, exist_ok=True)
    HTML_OUT.write_text(html, encoding="utf-8")

    try:
        from _tasks_meta import update_task
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _tasks_meta import update_task
    try:
        update_task(
            TASKS_FILE,
            task_id=TASK_ID,
            status="OK",
            summary=_summary(report),
            report_url=REPORT_URL,
        )
    except Exception as e:
        print(f"Warning: could not update tasks.json for {TASK_ID}: {e}")

    def _rel(p):
        try:
            return p.relative_to(REPO_ROOT)
        except ValueError:
            return p
    print(f"Wrote {_rel(JSON_OUT)} and {_rel(HTML_OUT)}")
    return 0


if __name__ == "__main__":
    sys.exit(render())
