"""Production scoring adjustment.

Reads the published rankings + ACT (activity_adjusted_review), GO
(pine_go_no_go_diagnostic) and ACC (accumulation_signal_meter) diagnostics,
applies a bounded multiplier to each ticker's ai_score, re-ranks, and writes
the result back to data/rankings.json (and data/watchlist_rankings.json).

The pre-adjustment score is preserved on every row as ``base_ai_score`` so the
production ai_score remains auditable. A before/after comparison artifact is
written to data/reports/production_scoring_change_review.json.

Run order (see .github/workflows/update-rankings.yml):
    score_tickers -> export_to_json -> generate_watchlist_rankings
    -> pine_go_no_go_diagnostic
    -> activity_adjusted_review
    -> accumulation_signal_meter
    -> apply_production_scoring_adjustment  (this script)

EXT (the external-benchmark / disagreement overlay) is intentionally NOT
incorporated — it remains a diagnostic overlay until separately greenlit.
The exclusion is enforced by tests: no code path here loads the EXT
artifacts, and the signature of compute_row_adjustment is frozen.

Design notes
------------
* Adjustments are MULTIPLICATIVE bumps in [MIN_MULT, MAX_MULT].
* Components: ACT (re-uses the activity-adjusted multiplier as one piece, so
  liquidity + rel-vol + Pine accumulation + overextended_bb all flow through),
  GO (Pine setup state band), ACC (accumulation meter band).
* All weights/caps live in TUNING below. Total bump is clamped, so a single
  signal cannot dominate even before the global cap kicks in.
* No sector names are referenced anywhere.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"

RANKINGS_FILE = DATA_DIR / "rankings.json"
WATCHLIST_FILE = DATA_DIR / "watchlist_rankings.json"
ACTIVITY_FILE = DATA_REPORTS_DIR / "activity_adjusted_review.json"
PINE_FILE = DATA_REPORTS_DIR / "pine_go_no_go_diagnostic.json"
ACC_FILE = DATA_REPORTS_DIR / "accumulation_signal_meter.json"
CHANGE_REVIEW_FILE = DATA_REPORTS_DIR / "production_scoring_change_review.json"

# --- TUNING -----------------------------------------------------------------
# All adjustments multiplicative. Total bump capped to MIN_MULT..MAX_MULT (±15%).
# The ACT subcomponent already enforces its own ±15% internally, but here we
# scale it by ACT_WEIGHT so the ACT piece can't single-handedly hit the global
# cap; GO/ACC then contribute the remainder.
MIN_MULT = 0.85
MAX_MULT = 1.15

# Weight applied to the ACT multiplier delta (so ACT contributes at most
# ACT_WEIGHT * 0.15 = +/-9% if uncapped). Keeps room for GO/ACC.
ACT_WEIGHT = 0.60

# GO band adjustments (clean GO is rewarded modestly; WAIT/WEAK penalised).
GO_BUMP_GO = +0.04         # clean GO (no blockers, normalized >= 0.7)
GO_BUMP_WAIT = -0.02       # WAIT (blocker present OR 0.4 <= norm < 0.7)
GO_BUMP_WEAK = -0.04       # WEAK (normalized < 0.4, no blocker)
GO_BUMP_MISSING = 0.0      # No Pine signal -> no adjustment

# Pine thresholds (must mirror index.html/watchlist.html deriveGoBadge).
GO_NORM_CLEAN = 0.7
GO_NORM_WEAK = 0.4

# ACC band adjustments (HIGH rewarded, LOW penalised).
ACC_BUMP_HIGH = +0.04      # accumulation score >= 7
ACC_BUMP_MID = 0.0         # neutral band (3 < score < 7)
ACC_BUMP_LOW = -0.03       # score <= 3
ACC_BUMP_MISSING = 0.0     # No accumulation signal -> no adjustment

ACC_HIGH_THRESHOLD = 7.0
ACC_LOW_THRESHOLD = 3.0

# Guardrails: if more than this fraction of the top-N would move beyond this
# rank-distance, the change review report flags "REVIEW" for human attention.
GUARDRAIL_TOP_N = 25
GUARDRAIL_MAX_MOVERS = 18              # >= 18 of 25 means churn warning
GUARDRAIL_MAX_RANK_DELTA = 50          # any single move beyond this triggers warn


# --- HELPERS ----------------------------------------------------------------

def _safe_float(v, default=None):
    try:
        if v is None:
            return default
        f = float(v)
        if f != f:  # NaN
            return default
        return f
    except (TypeError, ValueError):
        return default


def _load_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: could not load {path}: {exc}", file=sys.stderr)
        return None


def _activity_lookup(activity_payload, key="rows"):
    """Return {ticker: row} for activity_adjusted_review payload.

    `key` is "rows" for the main board overlay, "watchlist_rows" for the
    watchlist universe overlay. Each row exposes activity_score / ai_score
    so we can recover the multiplier the activity overlay applied.
    """
    if not activity_payload:
        return {}
    rows = activity_payload.get(key) or []
    return {r.get("ticker"): r for r in rows if r.get("ticker")}


def _pine_lookup(pine_payload):
    if not pine_payload:
        return {}
    pt = pine_payload.get("per_ticker") or []
    return {r.get("ticker"): r for r in pt if r.get("ticker")}


def _acc_lookup(acc_payload):
    if not acc_payload:
        return {}
    rows = acc_payload.get("rows") or []
    return {r.get("ticker"): r for r in rows if r.get("ticker")}


def _go_band(pine_row):
    """Return ('GO'|'WAIT'|'WEAK'|None, adjustment, normalized) for a ticker.

    Bands mirror the deriveGoBadge() logic in index.html / watchlist.html so
    the production adjustment moves in lockstep with the badge a user sees.
    """
    if not pine_row or pine_row.get("evaluated") is False:
        return None, GO_BUMP_MISSING, None
    norm = _safe_float(pine_row.get("go_no_go_score_normalized"))
    blockers = pine_row.get("blockers") or []
    if blockers:
        return "WAIT", GO_BUMP_WAIT, norm
    if norm is None:
        return None, GO_BUMP_MISSING, None
    if norm >= GO_NORM_CLEAN:
        return "GO", GO_BUMP_GO, norm
    if norm < GO_NORM_WEAK:
        return "WEAK", GO_BUMP_WEAK, norm
    return "WAIT", GO_BUMP_WAIT, norm


def _acc_band(acc_row):
    """Return ('HIGH'|'MID'|'LOW'|None, adjustment, score)."""
    if not acc_row:
        return None, ACC_BUMP_MISSING, None
    score = _safe_float(acc_row.get("score"))
    if score is None:
        return None, ACC_BUMP_MISSING, None
    if score >= ACC_HIGH_THRESHOLD:
        return "HIGH", ACC_BUMP_HIGH, score
    if score <= ACC_LOW_THRESHOLD:
        return "LOW", ACC_BUMP_LOW, score
    return "MID", ACC_BUMP_MID, score


def _act_multiplier_delta(act_row, base_score):
    """Recover the activity multiplier delta from the activity report row.

    activity_score = base * (1 + bump). We back out the bump and scale by
    ACT_WEIGHT. Returns (delta, components_dict_or_None).
    """
    if not act_row or base_score in (None, 0):
        return 0.0, None
    act_score = _safe_float(act_row.get("activity_score"))
    ref = _safe_float(act_row.get("ai_score"))
    if act_score is None or ref is None or ref == 0:
        return 0.0, None
    bump = (act_score / ref) - 1.0
    return bump * ACT_WEIGHT, {
        "raw_bump": round(bump, 4),
        "scaled_bump": round(bump * ACT_WEIGHT, 4),
        "liquidity_bump": _safe_float(act_row.get("liquidity_bump"), 0.0),
        "relvol_bump": _safe_float(act_row.get("relvol_bump"), 0.0),
        "pine_bump": _safe_float(act_row.get("pine_bump"), 0.0),
        "overextended_bb": bool(act_row.get("overextended_bb")),
    }


def compute_row_adjustment(base_score, act_row, pine_row, acc_row):
    """Return (final_score, audit_dict). Pure function; no I/O."""
    act_delta, act_components = _act_multiplier_delta(act_row, base_score)
    go_label, go_delta, go_norm = _go_band(pine_row)
    acc_label, acc_delta, acc_score = _acc_band(acc_row)

    raw_bump = act_delta + go_delta + acc_delta
    final_bump = max(MIN_MULT - 1.0, min(MAX_MULT - 1.0, raw_bump))
    if base_score is None:
        final_score = None
    else:
        final_score = round(base_score * (1.0 + final_bump), 2)

    return final_score, {
        "base_ai_score": round(base_score, 2) if base_score is not None else None,
        "act_delta": round(act_delta, 4),
        "go_label": go_label,
        "go_delta": round(go_delta, 4),
        "go_norm": round(go_norm, 3) if go_norm is not None else None,
        "acc_label": acc_label,
        "acc_delta": round(acc_delta, 4),
        "acc_score": round(acc_score, 2) if acc_score is not None else None,
        "raw_bump": round(raw_bump, 4),
        "final_bump": round(final_bump, 4),
        "capped": raw_bump != final_bump,
        "act_components": act_components,
    }


def _rerank_rows(rows, score_key="ai_score"):
    """Re-rank an already-mutated row list 1..N by score desc, stable by ticker."""
    rows.sort(key=lambda r: (-(r.get(score_key) if r.get(score_key) is not None else -1e9),
                             r.get("ticker") or ""))
    for new_rank, r in enumerate(rows, 1):
        r["rank"] = new_rank
    return rows


def apply_adjustment(rankings_payload, act_lookup, pine_lookup, acc_lookup):
    """Mutate rankings_payload in place: set base_ai_score, update ai_score,
    re-rank by new ai_score. Returns a list of per-ticker audit records (one
    per row, in original order). Idempotent: if a row already has
    ``base_ai_score`` we re-read FROM that value so re-running on an
    already-adjusted payload yields the same result rather than compounding.
    """
    if not rankings_payload or not rankings_payload.get("rows"):
        return []
    rows = rankings_payload["rows"]
    audit = []
    for r in rows:
        ticker = r.get("ticker")
        # Idempotency: prefer existing base_ai_score if present (replay-safe).
        base = _safe_float(r.get("base_ai_score"))
        if base is None:
            base = _safe_float(r.get("ai_score"))
        act_row = act_lookup.get(ticker)
        pine_row = pine_lookup.get(ticker)
        acc_row = acc_lookup.get(ticker)
        final_score, info = compute_row_adjustment(base, act_row, pine_row, acc_row)

        r["base_ai_score"] = info["base_ai_score"]
        r["adjusted_ai_score"] = final_score
        if final_score is not None:
            r["ai_score"] = final_score
        # Surface labels on the row so the dashboard JSON has them without
        # joining against external diagnostic files.
        r["go_label"] = info["go_label"]
        r["acc_label"] = info["acc_label"]
        r["adjustment_bump"] = info["final_bump"]

        audit.append({"ticker": ticker, **info})
    _rerank_rows(rows, "ai_score")
    return audit


def _top_n(rows, n):
    out = []
    for r in rows:
        out.append({
            "rank": r.get("rank"),
            "ticker": r.get("ticker"),
            "company": r.get("company"),
            "sector": r.get("sector"),
            "ai_score": r.get("ai_score"),
            "base_ai_score": r.get("base_ai_score"),
        })
        if len(out) >= n:
            break
    return out


def build_change_review(before_rows, after_rows, audit_main, audit_watchlist):
    """Diff before/after on the main board and report movers + guardrails."""
    before_idx = {r.get("ticker"): r for r in before_rows}
    after_idx = {r.get("ticker"): r for r in after_rows}

    movers = []
    for t, ar in after_idx.items():
        br = before_idx.get(t)
        if not br:
            continue
        delta = (br.get("rank") or 0) - (ar.get("rank") or 0)  # +ve = promoted
        movers.append({
            "ticker": t,
            "company": ar.get("company"),
            "sector": ar.get("sector"),
            "before_rank": br.get("rank"),
            "after_rank": ar.get("rank"),
            "rank_delta": delta,
            "base_ai_score": ar.get("base_ai_score"),
            "ai_score": ar.get("ai_score"),
            "go_label": ar.get("go_label"),
            "acc_label": ar.get("acc_label"),
            "adjustment_bump": ar.get("adjustment_bump"),
        })
    movers.sort(key=lambda m: -abs(m["rank_delta"] or 0))

    top_before = _top_n(before_rows, GUARDRAIL_TOP_N)
    top_after = _top_n(after_rows, GUARDRAIL_TOP_N)

    before_set = {r["ticker"] for r in top_before}
    after_set = {r["ticker"] for r in top_after}
    entrants = [r for r in top_after if r["ticker"] not in before_set]
    drops = [r for r in top_before if r["ticker"] not in after_set]
    top_movers = sum(1 for m in movers
                     if m["ticker"] in before_set or m["ticker"] in after_set
                     if abs(m["rank_delta"] or 0) >= 5)

    extreme_moves = [m for m in movers if abs(m["rank_delta"] or 0) >= GUARDRAIL_MAX_RANK_DELTA]

    # Sector distribution delta (top-N).
    def _sector_counts(rows):
        out = {}
        for r in rows:
            s = r.get("sector") or "Unknown"
            out[s] = out.get(s, 0) + 1
        return out
    sec_before = _sector_counts(top_before)
    sec_after = _sector_counts(top_after)
    all_sectors = sorted(set(sec_before) | set(sec_after))
    sector_distribution = [
        {"sector": s, "before": sec_before.get(s, 0), "after": sec_after.get(s, 0)}
        for s in all_sectors
    ]

    flags = []
    if top_movers > GUARDRAIL_MAX_MOVERS:
        flags.append(f"top{GUARDRAIL_TOP_N}_churn:{top_movers}>{GUARDRAIL_MAX_MOVERS}")
    if extreme_moves:
        flags.append(f"extreme_moves:{len(extreme_moves)}>={GUARDRAIL_MAX_RANK_DELTA}")
    verdict = "REVIEW" if flags else "OK"

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "verdict": verdict,
        "guardrail_flags": flags,
        "constants": {
            "min_mult": MIN_MULT,
            "max_mult": MAX_MULT,
            "act_weight": ACT_WEIGHT,
            "go_bump_go": GO_BUMP_GO,
            "go_bump_wait": GO_BUMP_WAIT,
            "go_bump_weak": GO_BUMP_WEAK,
            "acc_bump_high": ACC_BUMP_HIGH,
            "acc_bump_mid": ACC_BUMP_MID,
            "acc_bump_low": ACC_BUMP_LOW,
            "go_norm_clean": GO_NORM_CLEAN,
            "go_norm_weak": GO_NORM_WEAK,
            "acc_high_threshold": ACC_HIGH_THRESHOLD,
            "acc_low_threshold": ACC_LOW_THRESHOLD,
            "guardrail_top_n": GUARDRAIL_TOP_N,
            "guardrail_max_movers": GUARDRAIL_MAX_MOVERS,
            "guardrail_max_rank_delta": GUARDRAIL_MAX_RANK_DELTA,
        },
        "summary": {
            "rows_processed": len(after_rows),
            "top_n": GUARDRAIL_TOP_N,
            "top_n_entrants": len(entrants),
            "top_n_drops": len(drops),
            "top_n_overlap": len(before_set & after_set),
            "extreme_moves": len(extreme_moves),
        },
        "top_before": top_before,
        "top_after": top_after,
        "entrants_top_n": entrants,
        "drops_top_n": drops,
        "biggest_movers": movers[:25],
        "extreme_moves": extreme_moves,
        "sector_distribution_top_n": sector_distribution,
        "watchlist_audit_size": len(audit_watchlist or []),
        "main_audit_size": len(audit_main or []),
    }


def _snapshot_rows(payload):
    """Shallow snapshot of rows before mutation, capturing rank + ai_score so
    we can diff after re-ranking. Only keeps the fields we report on."""
    if not payload or not payload.get("rows"):
        return []
    keep = ("rank", "ticker", "company", "sector", "ai_score", "base_ai_score")
    return [{k: r.get(k) for k in keep} for r in payload["rows"]]


def main():
    if not RANKINGS_FILE.exists():
        print(f"missing rankings file: {RANKINGS_FILE}", file=sys.stderr)
        return 1

    rankings = _load_json(RANKINGS_FILE)
    watchlist = _load_json(WATCHLIST_FILE)
    activity = _load_json(ACTIVITY_FILE)
    pine = _load_json(PINE_FILE)
    acc = _load_json(ACC_FILE)

    if rankings is None:
        print("ERROR: rankings.json unreadable; aborting adjustment", file=sys.stderr)
        return 1

    main_act = _activity_lookup(activity, "rows")
    watch_act = _activity_lookup(activity, "watchlist_rows")
    pine_idx = _pine_lookup(pine)
    acc_idx = _acc_lookup(acc)

    before_main_rows = _snapshot_rows(rankings)
    audit_main = apply_adjustment(rankings, main_act, pine_idx, acc_idx)
    after_main_rows = _snapshot_rows(rankings)

    audit_watchlist = []
    if watchlist is not None:
        audit_watchlist = apply_adjustment(watchlist, watch_act, pine_idx, acc_idx)

    # Stamp metadata so consumers know an adjustment was applied.
    rankings["production_adjustment"] = {
        "applied": True,
        "applied_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "version": "v1.0",
        "constants_ref": "data/reports/production_scoring_change_review.json",
    }
    if watchlist is not None:
        watchlist["production_adjustment"] = rankings["production_adjustment"]

    RANKINGS_FILE.write_text(json.dumps(rankings, indent=2))
    if watchlist is not None:
        WATCHLIST_FILE.write_text(json.dumps(watchlist, indent=2))

    review = build_change_review(before_main_rows, after_main_rows,
                                 audit_main, audit_watchlist)
    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    CHANGE_REVIEW_FILE.write_text(json.dumps(review, indent=2))

    print(f"wrote {RANKINGS_FILE}")
    if watchlist is not None:
        print(f"wrote {WATCHLIST_FILE}")
    print(f"wrote {CHANGE_REVIEW_FILE}")
    summary = review["summary"]
    print(
        f"verdict: {review['verdict']} | top{summary['top_n']} overlap="
        f"{summary['top_n_overlap']}/{summary['top_n']} entrants="
        f"{summary['top_n_entrants']} drops={summary['top_n_drops']} "
        f"extreme={summary['extreme_moves']}"
    )
    movers = review["biggest_movers"][:10]
    for m in movers:
        bump_s = f"{m['adjustment_bump']:+.3f}" if m.get("adjustment_bump") is not None else "  —  "
        print(f"  {m['ticker']:<6} {m['before_rank']:>3} -> {m['after_rank']:>3} "
              f"({m['rank_delta']:+d}) base={m['base_ai_score']} ai={m['ai_score']} "
              f"GO={m['go_label']} ACC={m['acc_label']} bump={bump_s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
