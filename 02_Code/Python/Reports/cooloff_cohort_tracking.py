"""Cool-off / Overextended Cohort Tracking — A/B-style forward-return
diagnostic for the Pine `overextended_bb` blocker.

Hypothesis under test: do tickers that the Pine Go/No-Go Diagnostic flags
with `overextended_bb` underperform clean-go names over 1d / 3d / 5d / 10d?
If so, we can promote the blocker into LOW_RISK / SWING gating later.
This report does NOT mutate scoring formulas — it only collects evidence.

Cohorts (built each run from the latest pine_go_no_go_diagnostic.json):
  * clean_go        : Pine score >= 0.7 AND no blockers (any source)
  * overextended_bb : ticker has a blocker entry containing "overextended_bb"
  * weak_go         : Pine score < 0.4 (any source) — control / sanity check

Each run:
  1. Append a snapshot to data/reports/cooloff_cohort_snapshots.jsonl with the
     date, members of each cohort, and the reference last close.
  2. Re-evaluate prior snapshots: for every horizon (1/3/5/10 trading days)
     where enough trading days have elapsed, compute per-ticker forward
     returns using the latest available close. No lookahead — pending
     horizons stay marked "pending".
  3. Aggregate completed horizons across all snapshots into a comparison
     table (mean / median return, %positive, n) per cohort.
  4. Emit JSON + HTML, including a decision recommendation block.

Inputs (read-only, no network):
  - data/reports/pine_go_no_go_diagnostic.json
  - data/rankings.json, data/watchlist_rankings.json (for current prices)

Outputs:
  - data/reports/cooloff_cohort_tracking.json
  - reports/cooloff-cohort-tracking.html
  - data/reports/cooloff_cohort_snapshots.jsonl
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from statistics import mean, median

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"
HTML_REPORTS_DIR = REPO_ROOT / "reports"

PINE_FILE = DATA_REPORTS_DIR / "pine_go_no_go_diagnostic.json"
RANKINGS_FILE = DATA_DIR / "rankings.json"
WATCHLIST_FILE = DATA_DIR / "watchlist_rankings.json"

JSON_OUTPUT = DATA_REPORTS_DIR / "cooloff_cohort_tracking.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "cooloff-cohort-tracking.html"
SNAPSHOTS_FILE = DATA_REPORTS_DIR / "cooloff_cohort_snapshots.jsonl"
TASKS_FILE = DATA_DIR / "tasks.json"
REPORT_URL = "./reports/cooloff-cohort-tracking.html"
TASK_ID = "cooloff-cohort-tracking"

CLEAN_GO_THRESHOLD = 0.7
WEAK_GO_THRESHOLD = 0.4
SNAPSHOT_RETENTION_DAYS = 90
SNAPSHOT_MAX_ROWS = 500
FORWARD_HORIZONS_TRADING_DAYS = (1, 3, 5, 10)
# Minimum total observations (snapshot * ticker) per cohort/horizon before
# we'd consider the comparison strong enough to act on. Below this it stays
# "advisory only".
MIN_OBS_FOR_DECISION = 30


# ---------- IO ----------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _load_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _round(v, n: int = 4):
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return round(float(v), n)


def _last_close_from_row(row: dict):
    closes = (row or {}).get("closes")
    if not isinstance(closes, list):
        return None
    for v in reversed(closes):
        if isinstance(v, (int, float)) and v == v and not math.isinf(v):
            return float(v)
    return None


def latest_price_index(rankings: dict | None,
                       watchlist: dict | None) -> dict[str, float]:
    """Build a {ticker: latest_close} index from the freshest rankings."""
    out: dict[str, float] = {}
    for payload in (rankings, watchlist):
        rows = (payload or {}).get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            continue
        for r in rows:
            if not isinstance(r, dict):
                continue
            t = r.get("ticker")
            if not t:
                continue
            v = _last_close_from_row(r)
            if v is not None:
                out[t] = v  # later sources overwrite, that's fine
    return out


# ---------- Cohort classification ----------


def classify_cohorts(pine_report: dict | None) -> dict:
    """Partition Pine per-ticker entries into clean_go / overextended_bb /
    weak_go. Returns {cohort_name: [member_dict, ...]}.

    A ticker is included if it was Pine-evaluated (i.e. had enough OHLCV
    bars). Members can appear in clean_go OR overextended_bb but not both
    by construction (clean_go has no blockers). weak_go can overlap
    with overextended_bb if the score happens to be <0.4 — that's fine,
    weak_go is a sanity-check control cohort.
    """
    out: dict[str, list[dict]] = {
        "clean_go": [], "overextended_bb": [], "weak_go": []
    }
    if not isinstance(pine_report, dict):
        return out
    per = pine_report.get("per_ticker") or []
    for entry in per:
        if not isinstance(entry, dict):
            continue
        if not entry.get("evaluated"):
            continue
        score = entry.get("go_no_go_score_normalized")
        if not isinstance(score, (int, float)):
            continue
        blockers = entry.get("blockers") or []
        member = {
            "ticker": entry.get("ticker"),
            "sector": entry.get("sector"),
            "ai_score": entry.get("ai_score"),
            "swing_score": entry.get("swing_score"),
            "go_no_go_score_normalized": score,
            "blockers": list(blockers),
            "sources": entry.get("sources") or [],
        }
        if not blockers and score >= CLEAN_GO_THRESHOLD:
            out["clean_go"].append(member)
        if any("overextended_bb" in str(b) for b in blockers):
            out["overextended_bb"].append(member)
        if score < WEAK_GO_THRESHOLD:
            out["weak_go"].append(member)
    return out


# ---------- Snapshot persistence ----------


def load_snapshots(path: Path = SNAPSHOTS_FILE) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def save_snapshots(records: list[dict], path: Path = SNAPSHOTS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, default=str) + "\n")
    os.replace(tmp, path)


def _parse_iso_date(s):
    if not isinstance(s, str) or len(s) != 10:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def prune_snapshots(records: list[dict], today: date,
                    retention_days: int = SNAPSHOT_RETENTION_DAYS,
                    max_rows: int = SNAPSHOT_MAX_ROWS) -> list[dict]:
    """Drop records older than retention_days, then cap to max_rows
    keeping the newest. Records without a parsable date are kept (they
    sort to the end of the list since we only check the date)."""
    cutoff = today - timedelta(days=retention_days)
    kept: list[dict] = []
    for r in records:
        d = _parse_iso_date(r.get("as_of_date"))
        if d is None or d >= cutoff:
            kept.append(r)
    if len(kept) > max_rows:
        # Keep newest by date when possible.
        kept.sort(key=lambda r: _parse_iso_date(r.get("as_of_date")) or date.min)
        kept = kept[-max_rows:]
    return kept


def _trading_days_between(start: date, end: date) -> int:
    """Approximate trading-day count via Mon-Fri filter (no holidays)."""
    if end <= start:
        return 0
    days = 0
    cur = start
    one = timedelta(days=1)
    while cur < end:
        cur += one
        if cur.weekday() < 5:
            days += 1
    return days


# ---------- Snapshot record build ----------


def _today_date_iso(rankings: dict | None) -> str:
    if isinstance(rankings, dict):
        d = rankings.get("open_date")
        if isinstance(d, str) and len(d) == 10 and d[4] == "-" and d[7] == "-":
            return d
    return _now_utc().date().isoformat()


def build_snapshot_record(*, as_of_date: str, cohorts: dict,
                          prices: dict[str, float]) -> dict:
    """Build today's snapshot. Per-cohort members get their reference
    last close stamped now; forward returns are filled in later runs.
    Tickers without a current price are still recorded but with
    ref_close=None — they'll be skipped during return evaluation.
    """
    record = {
        "as_of_date": as_of_date,
        "captured_at": _now_utc().isoformat() + "Z",
        "cohorts": {},
        "forward": {},
    }
    for cname, members in cohorts.items():
        snap_members = []
        for m in members:
            tic = m.get("ticker")
            if not tic:
                continue
            snap_members.append({
                "ticker": tic,
                "sector": m.get("sector"),
                "ai_score": m.get("ai_score"),
                "go_no_go_score_normalized": m.get("go_no_go_score_normalized"),
                "blockers": list(m.get("blockers") or []),
                "ref_close": prices.get(tic),
            })
        record["cohorts"][cname] = {
            "size": len(snap_members),
            "members": snap_members,
        }
    return record


# ---------- Forward return evaluation ----------


def _evaluate_horizon_for_record(rec: dict,
                                 latest_prices: dict[str, float]) -> dict:
    """Compute per-cohort forward returns for one snapshot using current
    prices vs. its captured ref_close. Returns a dict of
    {cohort_name: aggregate}.
    """
    cohorts = rec.get("cohorts") or {}
    out: dict = {}
    for cname, cdata in cohorts.items():
        rets: list[float] = []
        n_eval = 0
        n_missing = 0
        members = cdata.get("members") or []
        for m in members:
            ref = m.get("ref_close")
            cur = latest_prices.get(m.get("ticker"))
            if (not isinstance(ref, (int, float)) or ref <= 0
                    or not isinstance(cur, (int, float))):
                n_missing += 1
                continue
            rets.append((cur - ref) / ref)
            n_eval += 1
        out[cname] = {
            "n_members": len(members),
            "evaluated": n_eval,
            "missing": n_missing,
            "mean_return": _round(mean(rets)) if rets else None,
            "median_return": _round(median(rets)) if rets else None,
            "pct_positive": _round(sum(1 for r in rets if r > 0) / len(rets), 4)
                            if rets else None,
            "returns": [_round(r, 6) for r in rets],
        }
    return out


def evaluate_snapshots(records: list[dict], *, today: date,
                       latest_prices: dict[str, float]) -> list[dict]:
    """Re-fill forward[<horizon>d] for every snapshot whose horizon has
    elapsed but is still pending. Already-completed horizons are NOT
    overwritten — the returns captured on the first run that satisfied
    the horizon are closer to the true horizon close than today's price.
    """
    for rec in records:
        as_of = _parse_iso_date(rec.get("as_of_date"))
        if as_of is None:
            continue
        elapsed = _trading_days_between(as_of, today)
        rec.setdefault("forward", {})
        for horizon in FORWARD_HORIZONS_TRADING_DAYS:
            key = f"{horizon}d"
            existing = rec["forward"].get(key)
            if isinstance(existing, dict) and existing.get("status") == "completed":
                continue  # don't overwrite earlier-resolved horizon
            if elapsed < horizon:
                rec["forward"][key] = {
                    "status": "pending",
                    "elapsed_trading_days": elapsed,
                }
                continue
            cohort_results = _evaluate_horizon_for_record(rec, latest_prices)
            rec["forward"][key] = {
                "status": "completed",
                "elapsed_trading_days": elapsed,
                "evaluated_at": _now_utc().isoformat() + "Z",
                "cohorts": cohort_results,
            }
    return records


# ---------- Cross-snapshot aggregation ----------


def aggregate_horizon_comparison(records: list[dict]) -> dict:
    """For each horizon, pool every completed cohort's per-ticker returns
    across all snapshots and report mean/median/pct-positive/n. The
    report is built around the per-ticker observations rather than
    per-snapshot means — that mirrors how the test we want to run
    treats each (snapshot, ticker) as one observation.
    """
    summary: dict = {}
    for horizon in FORWARD_HORIZONS_TRADING_DAYS:
        key = f"{horizon}d"
        cohort_pool: dict[str, list[float]] = {}
        n_completed_snapshots = 0
        n_pending_snapshots = 0
        for rec in records:
            slot = (rec.get("forward") or {}).get(key)
            if not isinstance(slot, dict):
                continue
            if slot.get("status") == "pending":
                n_pending_snapshots += 1
                continue
            if slot.get("status") != "completed":
                continue
            n_completed_snapshots += 1
            for cname, cres in (slot.get("cohorts") or {}).items():
                rets = cres.get("returns") or []
                if not isinstance(rets, list):
                    continue
                pool = cohort_pool.setdefault(cname, [])
                for r in rets:
                    if isinstance(r, (int, float)) and not math.isnan(r):
                        pool.append(float(r))

        cohort_block: dict = {}
        for cname, pool in cohort_pool.items():
            if not pool:
                cohort_block[cname] = {
                    "n_observations": 0, "mean_return": None,
                    "median_return": None, "pct_positive": None,
                }
                continue
            cohort_block[cname] = {
                "n_observations": len(pool),
                "mean_return": _round(mean(pool)),
                "median_return": _round(median(pool)),
                "pct_positive": _round(sum(1 for r in pool if r > 0) / len(pool), 4),
            }
        summary[key] = {
            "snapshots_completed": n_completed_snapshots,
            "snapshots_pending": n_pending_snapshots,
            "cohorts": cohort_block,
        }
    return summary


# ---------- Decision logic ----------


def decide_recommendation(comparison: dict,
                          min_obs: int = MIN_OBS_FOR_DECISION) -> dict:
    """Decide whether the data so far justifies promoting overextended_bb
    out of advisory. Returns a dict {recommendation, rationale, ready}.

    The signal we want before recommending action: at the 5d horizon, we
    have at least min_obs paired observations in BOTH clean_go and
    overextended_bb cohorts AND the gap between their mean returns
    exceeds 1 percentage point (overextended underperforms by >100bps).
    Below that, we keep the blocker advisory.
    """
    five = comparison.get("5d") or {}
    cohorts = five.get("cohorts") or {}
    clean = cohorts.get("clean_go") or {}
    overext = cohorts.get("overextended_bb") or {}
    clean_n = clean.get("n_observations") or 0
    overext_n = overext.get("n_observations") or 0
    clean_mean = clean.get("mean_return")
    overext_mean = overext.get("mean_return")

    if clean_n < min_obs or overext_n < min_obs:
        return {
            "recommendation": "keep_advisory",
            "ready": False,
            "rationale": (
                f"Insufficient observations at 5d horizon "
                f"(clean_go n={clean_n}, overextended_bb n={overext_n}; "
                f"need >={min_obs} in both)."
            ),
        }
    if not isinstance(clean_mean, (int, float)) or not isinstance(overext_mean, (int, float)):
        return {
            "recommendation": "keep_advisory",
            "ready": False,
            "rationale": "5d means not yet computable.",
        }
    gap = clean_mean - overext_mean
    if gap > 0.01:
        return {
            "recommendation": "consider_scoring_change",
            "ready": True,
            "rationale": (
                f"Clean-go beats overextended_bb at 5d by "
                f"{gap*100:+.2f}pp (clean={clean_mean*100:+.2f}%, "
                f"overext={overext_mean*100:+.2f}%, n={clean_n}/{overext_n}). "
                f"Worth a controlled scoring experiment."
            ),
        }
    return {
        "recommendation": "keep_advisory",
        "ready": True,
        "rationale": (
            f"5d gap is {gap*100:+.2f}pp (clean={clean_mean*100:+.2f}%, "
            f"overext={overext_mean*100:+.2f}%, n={clean_n}/{overext_n}); "
            f"not large enough to justify scoring change."
        ),
    }


# ---------- Top-level assembly ----------


def build_report(*, pine_report: dict | None,
                 rankings: dict | None, watchlist: dict | None,
                 today: date | None = None,
                 snapshots_path: Path | None = None) -> tuple[dict, list[dict]]:
    """Returns (report_dict, snapshots_list). Caller is responsible for
    persisting both artifacts. The snapshots list is also written to
    `snapshots_path` (defaults to SNAPSHOTS_FILE) inside this function so
    re-runs see the new state. Pass an explicit path in tests.
    """
    today = today or _now_utc().date()
    path = snapshots_path or SNAPSHOTS_FILE

    cohorts = classify_cohorts(pine_report)
    prices = latest_price_index(rankings, watchlist)
    today_iso = _today_date_iso(rankings)
    today_record = build_snapshot_record(
        as_of_date=today_iso, cohorts=cohorts, prices=prices)

    snapshots = load_snapshots(path)
    snapshots = [s for s in snapshots if s.get("as_of_date") != today_iso]
    snapshots.append(today_record)

    snapshots = prune_snapshots(snapshots, today=today)
    snapshots = evaluate_snapshots(snapshots, today=today, latest_prices=prices)
    save_snapshots(snapshots, path=path)

    comparison = aggregate_horizon_comparison(snapshots)
    decision = decide_recommendation(comparison)

    cohort_sizes = {k: len(v) for k, v in cohorts.items()}

    overall = "OK"
    if cohort_sizes.get("clean_go", 0) == 0 and cohort_sizes.get("overextended_bb", 0) == 0:
        overall = "WARN"  # nothing to track

    report = {
        "generated_at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_date": today_iso,
        "overall": overall,
        "caveat": (
            "DIAGNOSTIC ONLY — production scoring formulas are NOT mutated. "
            "Tracks per-ticker forward returns of Pine cohorts to test whether "
            "overextended_bb names underperform clean-go names before promoting "
            "the blocker into LOW_RISK / SWING gating."
        ),
        "thresholds": {
            "clean_go_score_min": CLEAN_GO_THRESHOLD,
            "weak_go_score_max_excl": WEAK_GO_THRESHOLD,
            "horizons_trading_days": list(FORWARD_HORIZONS_TRADING_DAYS),
            "min_obs_for_decision": MIN_OBS_FOR_DECISION,
            "snapshot_retention_days": SNAPSHOT_RETENTION_DAYS,
            "snapshot_max_rows": SNAPSHOT_MAX_ROWS,
        },
        "current_cohort_sizes": cohort_sizes,
        "current_cohort_members": {
            k: [m.get("ticker") for m in v if m.get("ticker")]
            for k, v in cohorts.items()
        },
        "horizon_comparison": comparison,
        "decision": decision,
        "snapshots_kept": len(snapshots),
        "inputs": {
            "pine_report_present": pine_report is not None,
            "rankings_present": rankings is not None,
            "watchlist_present": watchlist is not None,
            "prices_indexed": len(prices),
        },
    }
    report["summary"] = build_summary(report)
    return report, snapshots


def build_summary(report: dict) -> str:
    sizes = report.get("current_cohort_sizes") or {}
    parts = [
        f"clean_go={sizes.get('clean_go', 0)}",
        f"overext_bb={sizes.get('overextended_bb', 0)}",
        f"weak_go={sizes.get('weak_go', 0)}",
        f"snaps={report.get('snapshots_kept', 0)}",
    ]
    five = (report.get("horizon_comparison") or {}).get("5d") or {}
    five_cohorts = five.get("cohorts") or {}
    cg = five_cohorts.get("clean_go") or {}
    oe = five_cohorts.get("overextended_bb") or {}
    cg_n = cg.get("n_observations") or 0
    oe_n = oe.get("n_observations") or 0
    if cg_n and oe_n:
        cg_m = cg.get("mean_return")
        oe_m = oe.get("mean_return")
        if isinstance(cg_m, (int, float)) and isinstance(oe_m, (int, float)):
            parts.append(
                f"5d clean={cg_m*100:+.2f}%/overext={oe_m*100:+.2f}% "
                f"(n={cg_n}/{oe_n})"
            )
        else:
            parts.append(f"5d obs n={cg_n}/{oe_n}")
    else:
        parts.append("5d pending")
    rec = (report.get("decision") or {}).get("recommendation") or "—"
    parts.append(f"rec={rec}")
    return " · ".join(parts)


# ---------- HTML rendering ----------


def _badge_color(overall: str) -> str:
    return {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}.get(overall, "#666")


def _fmt_pct(v):
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v * 100:.2f}%"
    return escape(str(v))


def _fmt_int(v):
    if v is None:
        return "—"
    return str(v)


def _render_comparison_table(comparison: dict) -> str:
    out = ["<table><thead><tr><th>Horizon</th><th>Cohort</th>"
           "<th>n obs</th><th>mean</th><th>median</th><th>%+</th>"
           "<th>snaps done / pending</th></tr></thead><tbody>"]
    for hkey in (f"{h}d" for h in FORWARD_HORIZONS_TRADING_DAYS):
        h = comparison.get(hkey) or {}
        cohorts = h.get("cohorts") or {}
        done = h.get("snapshots_completed", 0)
        pending = h.get("snapshots_pending", 0)
        if not cohorts:
            out.append(
                f"<tr><td>{escape(hkey)}</td>"
                f"<td colspan=\"5\" class=\"muted\">no completed snapshots yet</td>"
                f"<td>{done} / {pending}</td></tr>"
            )
            continue
        for cname in ("clean_go", "overextended_bb", "weak_go"):
            c = cohorts.get(cname)
            if c is None:
                continue
            out.append(
                f"<tr><td>{escape(hkey)}</td>"
                f"<td>{escape(cname)}</td>"
                f"<td>{_fmt_int(c.get('n_observations'))}</td>"
                f"<td>{_fmt_pct(c.get('mean_return'))}</td>"
                f"<td>{_fmt_pct(c.get('median_return'))}</td>"
                f"<td>{_fmt_pct(c.get('pct_positive'))}</td>"
                f"<td>{done} / {pending}</td></tr>"
            )
    out.append("</tbody></table>")
    return "".join(out)


def _render_member_list(members: list[str], cap: int = 25) -> str:
    if not members:
        return "<span class='muted'>—</span>"
    show = members[:cap]
    extra = len(members) - len(show)
    s = ", ".join(escape(m) for m in show)
    if extra > 0:
        s += f" <span class='muted'>(+{extra} more)</span>"
    return s


def _render_html(report: dict) -> str:
    overall = report.get("overall") or "OK"
    color = _badge_color(overall)
    sizes = report.get("current_cohort_sizes") or {}
    members = report.get("current_cohort_members") or {}
    decision = report.get("decision") or {}

    rec = decision.get("recommendation") or "—"
    rec_color = "#3c8c3c" if rec == "consider_scoring_change" else "#b88a00"

    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Cool-off Cohort Tracking</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:1100px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
h1{{margin:0 0 4px}} h2{{margin:18px 0 6px;font-size:18px}}
.meta{{color:#666;font-size:13px;margin-bottom:18px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;
       font-weight:600;font-size:12px;letter-spacing:.5px;background:{color}}}
.section{{border:1px solid #e3e3e3;border-radius:8px;padding:14px 16px;margin:14px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
.kv{{font-size:13px;color:#444}}
.muted{{color:#666;font-size:12px}}
.summary{{font-size:14px;background:#f6f8fa;padding:10px 12px;border-radius:6px;margin-top:8px}}
.caveat{{background:#fff6e0;border:1px solid #f0d49a;color:#8a5a00;padding:10px 12px;
        border-radius:6px;margin:14px 0;font-size:13px}}
.recommend{{background:#eef7ee;border:1px solid #b8d8b8;padding:10px 12px;border-radius:6px;
            margin:8px 0 14px 0;font-size:14px;border-left:4px solid {rec_color}}}
.back{{font-size:13px}}
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>Cool-off / Overextended Cohort Tracking</h1>
<p class="meta">Generated {escape(report.get("generated_at",""))} &middot;
   as_of {escape(report.get("as_of_date",""))} &middot;
   Overall: <span class="badge">{escape(overall)}</span></p>
<div class="summary"><strong>Summary:</strong> {escape(report.get("summary",""))}</div>
<div class="caveat"><strong>Caveat:</strong> {escape(report.get("caveat",""))}</div>
<div class="recommend"><strong>Recommendation:</strong> {escape(rec)}<br>
<span class="muted">{escape(decision.get("rationale",""))}</span></div>
""")

    parts.append("<div class='section'><h2>Current cohort sizes</h2>")
    parts.append("<table><thead><tr><th>Cohort</th><th>Size</th><th>Members</th></tr></thead><tbody>")
    for cname in ("clean_go", "overextended_bb", "weak_go"):
        parts.append(
            f"<tr><td>{escape(cname)}</td>"
            f"<td>{sizes.get(cname,0)}</td>"
            f"<td>{_render_member_list(members.get(cname) or [])}</td></tr>"
        )
    parts.append("</tbody></table></div>")

    parts.append("<div class='section'><h2>Forward-return comparison by horizon</h2>")
    parts.append("<p class='kv'>Each observation is one (snapshot, ticker) pair "
                 "where the horizon has elapsed (no lookahead). Pending horizons "
                 "are the count of snapshots whose elapsed trading days are still "
                 "below the horizon.</p>")
    parts.append(_render_comparison_table(report.get("horizon_comparison") or {}))
    parts.append("</div>")

    parts.append("<div class='section'><h2>Snapshot scaffold</h2>")
    parts.append(f"<p class='kv'>Snapshots tracked: {report.get('snapshots_kept',0)} "
                 f"(retention {SNAPSHOT_RETENTION_DAYS} days, "
                 f"cap {SNAPSHOT_MAX_ROWS} rows). "
                 f"Min observations before promoting blocker into "
                 f"scoring change: {MIN_OBS_FOR_DECISION} per cohort at 5d.</p>")
    parts.append("</div>")

    parts.append("</body></html>")
    return "\n".join(parts)


# ---------- tasks.json wiring ----------


def _stamp_task(report: dict) -> None:
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
    overall = (report.get("overall") or "OK").lower()
    status = "FAIL" if overall == "fail" else ("warn" if overall == "warn" else "OK")
    update_task(TASKS_FILE, TASK_ID,
                status=status,
                summary=report.get("summary", ""),
                report_url=REPORT_URL)


def _ensure_task_row() -> None:
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
        "name": "Cool-off Cohort Tracking",
        "schedule": "Every refresh (08:45/12:30/15:35 CT, weekdays)",
        "last_run": "—",
        "next_run": "—",
        "status": "Not Run",
        "summary": "—",
        "report_url": REPORT_URL,
    })
    TASKS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ---------- entry point ----------


def main() -> int:
    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    pine_report = _load_json(PINE_FILE)
    rankings = _load_json(RANKINGS_FILE)
    watchlist = _load_json(WATCHLIST_FILE)
    report, _snaps = build_report(
        pine_report=pine_report if isinstance(pine_report, dict) else None,
        rankings=rankings if isinstance(rankings, dict) else None,
        watchlist=watchlist if isinstance(watchlist, dict) else None,
    )
    JSON_OUTPUT.write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")
    _ensure_task_row()
    _stamp_task(report)
    print(f"[cooloff_cohort_tracking] overall={report['overall']} -> {JSON_OUTPUT}")
    print(f"[cooloff_cohort_tracking] summary: {report['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
