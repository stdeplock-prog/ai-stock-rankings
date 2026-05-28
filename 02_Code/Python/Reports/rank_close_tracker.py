"""Rank vs Close Tracker — rolling diagnostic that appends a daily
snapshot of (production rank, close, close-change) per ticker and
renders a wide table for visual review.

User ask: "track the stocks that are ranked versus their daily close".
The ticker universe is dynamic — a ticker only needs to appear in *one*
retained snapshot to occupy a row in the report; it does not have to be
ranked today.

Inputs (read-only):
  - data/rankings.json                       (today's production board)
  - data/reports/rank_close_snapshots.jsonl  (rolling history, this script's own)

Outputs:
  - data/reports/rank_close_snapshots.jsonl  (rolling history, append/replace
                                              the row for today's open_date,
                                              bounded retention)
  - data/reports/rank_close_tracker.json     (machine-readable wide table)
  - reports/rank-close-tracker.html          (human-readable)
  - data/tasks.json row id=rank-close-tracker stamped on each run.

Append/replace logic: snapshots are keyed on the rankings file's
`open_date` (the trading day the board reflects), so re-runs within the
same trading day (e.g. close refresh after midday) overwrite that day's
snapshot rather than duplicating it. The ticker universe across the
retained snapshots is the union — once a ticker has appeared it keeps
its row even after it drops out of the rankings.

Close & close-change:
  * Close is the last value in `rows[i].closes` (the production OHLC
    array). If unavailable we fall back to `current_price` / `price`
    fields if the rankings file ever grows them.
  * Close change is computed two ways and the report displays both when
    both are available:
      - intraday `(close - prior_close_in_same_closes_array) / prior_close`
        (matches what the Cboe close panel shows for the day)
      - day-over-day vs the immediately-prior retained snapshot for the
        same ticker (handles weekends/holidays — the report uses
        whichever prior snapshot date is closest before today).
  * If neither is available the cell shows the close without a change.

Read-only with respect to scoring — the production `rank` / `ai_score`
are copied through untouched.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"
HTML_REPORTS_DIR = REPO_ROOT / "reports"

RANKINGS_FILE = DATA_DIR / "rankings.json"
SNAPSHOTS_FILE = DATA_REPORTS_DIR / "rank_close_snapshots.jsonl"
JSON_OUTPUT = DATA_REPORTS_DIR / "rank_close_tracker.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "rank-close-tracker.html"
TASKS_FILE = DATA_DIR / "tasks.json"
REPORT_URL = "./reports/rank-close-tracker.html"

# Retention: keep this many of the most recent trading-day snapshots.
# 30 is enough for a month of close-state history without bloating the
# repo, and matches the rolling window the benchmark snapshots use.
MAX_SNAPSHOTS = 30


# ----------------- helpers -----------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_chicago(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    chi_utc = dt.astimezone(timezone.utc)
    offset_h = -5 if 3 <= chi_utc.month <= 10 else -6
    return chi_utc.astimezone(timezone(timedelta(hours=offset_h)))


def _load_json(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_snapshots(path: Path) -> list[dict]:
    """Read the rolling JSONL into memory. Returns sorted by date asc."""
    out: list[dict] = []
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("date"):
                    out.append(rec)
    except OSError:
        return []
    out.sort(key=lambda r: r.get("date") or "")
    return out


def _write_snapshots(path: Path, snapshots: list[dict]) -> None:
    """Write snapshots back as JSONL (one line per snapshot)."""
    lines = [json.dumps(s, default=str) for s in snapshots]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _safe_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Reject inf/NaN — they make HTML cells unreadable and aren't real prices.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _row_close(row: dict) -> float | None:
    """Pull today's close from a rankings row. Prefer the last value in
    the production `closes` array; fall back to a current/price field
    if one exists in the future."""
    if not isinstance(row, dict):
        return None
    closes = row.get("closes")
    if isinstance(closes, list) and closes:
        return _safe_float(closes[-1])
    for k in ("current_price", "price", "close"):
        v = _safe_float(row.get(k))
        if v is not None:
            return v
    return None


def _row_prior_close(row: dict) -> float | None:
    """Second-to-last value in the closes array — intraday change anchor."""
    if not isinstance(row, dict):
        return None
    closes = row.get("closes")
    if isinstance(closes, list) and len(closes) >= 2:
        return _safe_float(closes[-2])
    return None


def _format_mmddyyyy(d: str) -> str:
    """Convert ISO 'YYYY-MM-DD' to 'MM/DD/YYYY' for the column headers
    (which the user specified explicitly in that format)."""
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return dt.strftime("%m/%d/%Y")
    except (TypeError, ValueError):
        return d or "—"


# ----------------- snapshot build -----------------


def build_today_snapshot(rankings: dict | None) -> dict | None:
    """Convert today's rankings.json into a compact per-ticker snapshot.

    Returns None when rankings.json is missing or has no rows — the
    tracker still produces a report from prior snapshots but doesn't
    invent a new row.
    """
    if not isinstance(rankings, dict):
        return None
    rows = rankings.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    open_date = rankings.get("open_date")
    if not isinstance(open_date, str) or not open_date:
        # Fall back to today in Chicago if open_date is somehow missing.
        open_date = _to_chicago(_now_utc()).date().strftime("%Y-%m-%d")

    tickers: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = r.get("ticker")
        if not isinstance(t, str) or not t:
            continue
        close = _row_close(r)
        prior_close = _row_prior_close(r)
        intraday_pct = None
        intraday_abs = None
        if close is not None and prior_close is not None and prior_close != 0:
            intraday_abs = close - prior_close
            intraday_pct = (close - prior_close) / prior_close * 100.0
        tickers[t] = {
            "ticker": t,
            "rank": r.get("rank"),
            "ai_score": r.get("ai_score"),
            "close": close,
            "prior_close": prior_close,
            "intraday_change_abs": intraday_abs,
            "intraday_change_pct": intraday_pct,
        }
    return {
        "date": open_date,
        "as_of": rankings.get("as_of"),
        "tickers": tickers,
    }


def upsert_snapshot(snapshots: list[dict], today: dict, *, max_keep: int = MAX_SNAPSHOTS) -> list[dict]:
    """Append today's snapshot to the rolling list — or replace the
    existing entry for the same `date` — and trim to the most recent
    `max_keep` snapshots. Returns the new list, sorted asc by date."""
    if not isinstance(today, dict) or not today.get("date"):
        return snapshots
    out = [s for s in snapshots if s.get("date") != today["date"]]
    out.append(today)
    out.sort(key=lambda r: r.get("date") or "")
    if max_keep and len(out) > max_keep:
        out = out[-max_keep:]
    return out


# ----------------- table assembly -----------------


def _prior_snapshot_close(snapshots: list[dict], ticker: str, before_date: str) -> tuple[str | None, float | None]:
    """Walk snapshots backwards from before_date and return the first
    (date, close) entry where the ticker has a non-null close. Used to
    compute a day-over-day change when the intraday close-array signal
    isn't available (e.g. ticker freshly added without prior_close)."""
    for s in reversed(snapshots):
        d = s.get("date")
        if not isinstance(d, str) or d >= before_date:
            continue
        rec = (s.get("tickers") or {}).get(ticker)
        if isinstance(rec, dict):
            c = rec.get("close")
            if isinstance(c, (int, float)):
                return d, float(c)
    return None, None


def build_table(snapshots: list[dict]) -> dict:
    """Wide table: rows=tickers, columns paired per snapshot date.

    Sort order:
      1. Tickers currently ranked (latest snapshot has a numeric rank)
         sorted by that rank ascending.
      2. Tickers absent from the latest snapshot, sorted alphabetically.
    """
    dates = [s.get("date") for s in snapshots if s.get("date")]
    dates.sort()
    latest_date = dates[-1] if dates else None
    latest = next((s for s in snapshots if s.get("date") == latest_date), None) if latest_date else None
    latest_tickers = (latest or {}).get("tickers") or {}

    ticker_union: set[str] = set()
    for s in snapshots:
        for t in (s.get("tickers") or {}).keys():
            ticker_union.add(t)

    def _sort_key(t: str) -> tuple:
        rec = latest_tickers.get(t)
        rank = rec.get("rank") if isinstance(rec, dict) else None
        try:
            rank_n = int(rank)
            return (0, rank_n, t)
        except (TypeError, ValueError):
            return (1, 0, t)

    sorted_tickers = sorted(ticker_union, key=_sort_key)

    rows: list[dict] = []
    for t in sorted_tickers:
        cells: list[dict] = []
        for d in dates:
            snap = next((s for s in snapshots if s.get("date") == d), None)
            rec = ((snap or {}).get("tickers") or {}).get(t) if snap else None
            cell: dict = {"date": d}
            if isinstance(rec, dict):
                cell["rank"] = rec.get("rank")
                cell["ai_score"] = rec.get("ai_score")
                cell["close"] = rec.get("close")
                cell["intraday_change_abs"] = rec.get("intraday_change_abs")
                cell["intraday_change_pct"] = rec.get("intraday_change_pct")
                # Fall back to prior-snapshot delta when the closes-array
                # didn't give us an intraday change (e.g. new ticker).
                if (
                    cell["close"] is not None
                    and cell["intraday_change_pct"] is None
                ):
                    prior_d, prior_c = _prior_snapshot_close(snapshots, t, d)
                    if prior_c is not None and prior_c != 0:
                        cell["dod_change_abs"] = cell["close"] - prior_c
                        cell["dod_change_pct"] = (cell["close"] - prior_c) / prior_c * 100.0
                        cell["dod_compared_to"] = prior_d
                    else:
                        cell["dod_change_abs"] = None
                        cell["dod_change_pct"] = None
                        cell["dod_compared_to"] = None
                else:
                    cell["dod_change_abs"] = None
                    cell["dod_change_pct"] = None
                    cell["dod_compared_to"] = None
            cells.append(cell)
        rows.append({"ticker": t, "cells": cells})

    return {
        "dates": dates,
        "latest_date": latest_date,
        "rows": rows,
        "ticker_count": len(rows),
    }


def summarize(table: dict, snapshots: list[dict]) -> dict:
    """Compact summary: entrants/exits vs the immediately prior snapshot.
    Used both for the JSON `summary` and the HTML banner."""
    dates = table.get("dates") or []
    if len(dates) >= 2:
        latest = dates[-1]
        prior = dates[-2]
        latest_snap = next((s for s in snapshots if s.get("date") == latest), {})
        prior_snap = next((s for s in snapshots if s.get("date") == prior), {})
        latest_t = set((latest_snap.get("tickers") or {}).keys())
        prior_t = set((prior_snap.get("tickers") or {}).keys())
        entrants = sorted(latest_t - prior_t)
        exits = sorted(prior_t - latest_t)
    else:
        entrants = []
        exits = []
        prior = None
    return {
        "snapshot_count": len(snapshots),
        "latest_date": table.get("latest_date"),
        "prior_date": prior,
        "ticker_universe_count": table.get("ticker_count"),
        "ranked_today_count": sum(
            1 for r in table.get("rows", [])
            if r["cells"] and r["cells"][-1].get("rank") is not None
        ),
        "entrants_vs_prior": entrants,
        "exits_vs_prior": exits,
    }


# ----------------- rendering -----------------


def _fmt_rank(rank) -> str:
    if rank is None:
        return "—"
    try:
        return str(int(rank))
    except (TypeError, ValueError):
        return str(rank)


def _fmt_close_change(cell: dict) -> tuple[str, str]:
    """Return (text, css_class) for the close+change cell.
    Prefers intraday change from the production `closes` array; if not
    present, falls back to day-over-day from the prior snapshot.
    css_class is 'pos' / 'neg' / '' so the renderer can color it."""
    close = cell.get("close")
    if close is None:
        return "—", ""
    close_str = f"${close:,.2f}"
    pct = cell.get("intraday_change_pct")
    abs_ = cell.get("intraday_change_abs")
    label_src = "intra"
    if pct is None:
        pct = cell.get("dod_change_pct")
        abs_ = cell.get("dod_change_abs")
        label_src = "vs " + (cell.get("dod_compared_to") or "prior")
    if pct is None:
        return close_str, ""
    sign = "+" if pct >= 0 else "-"
    abs_part = ""
    if abs_ is not None:
        abs_part = f" / {sign}${abs(abs_):,.2f}"
    css = "pos" if pct >= 0 else "neg"
    suffix = f" ({label_src})" if label_src != "intra" else ""
    pct_str = f"{sign}{abs(pct):.2f}%"
    return f"{close_str} ({pct_str}{abs_part}){suffix}", css


def _render_html(report: dict) -> str:
    table = report["table"]
    summary = report["summary"]
    dates = table.get("dates") or []
    rows = table.get("rows") or []

    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Rank vs Close Tracker</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:1400px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
h1{{margin:0 0 4px}}
.meta{{color:#666;font-size:13px;margin-bottom:18px}}
.summary{{font-size:14px;background:#f6f8fa;padding:10px 12px;border-radius:6px;margin:8px 0 14px}}
.summary strong{{color:#222}}
.scroll{{overflow-x:auto;border:1px solid #e3e3e3;border-radius:8px}}
table{{border-collapse:collapse;font-size:12px;min-width:100%}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;white-space:nowrap;vertical-align:top}}
th{{background:#fafafa;position:sticky;top:0;z-index:2}}
th.ticker, td.ticker{{position:sticky;left:0;background:#fff;border-right:1px solid #ddd;font-weight:600;z-index:1}}
th.ticker{{background:#fafafa;z-index:3}}
.muted{{color:#999}}
.pos{{color:#2e7d32}}
.neg{{color:#c0392b}}
.rank{{font-variant-numeric:tabular-nums}}
.back{{font-size:13px}}
.legend{{font-size:12px;color:#555;margin-top:8px}}
.legend code{{background:#f0f0f0;padding:1px 4px;border-radius:3px}}
</style></head><body>
<p class="back"><a href="../diagnostics.html">&larr; Back to diagnostics</a></p>
<h1>Rank vs Close Tracker</h1>
<p class="meta">Generated {escape(report.get("generated_at_chicago",""))}</p>
""")

    entrants = summary.get("entrants_vs_prior") or []
    exits = summary.get("exits_vs_prior") or []
    prior_date = summary.get("prior_date")
    snap_count = summary.get("snapshot_count") or 0
    latest_date = summary.get("latest_date") or "—"
    universe = summary.get("ticker_universe_count") or 0
    ranked_today = summary.get("ranked_today_count") or 0

    entrants_str = ", ".join(entrants[:10]) + (f" (+{len(entrants)-10} more)" if len(entrants) > 10 else "") if entrants else "—"
    exits_str = ", ".join(exits[:10]) + (f" (+{len(exits)-10} more)" if len(exits) > 10 else "") if exits else "—"

    parts.append('<div class="summary">')
    parts.append(f"<strong>Snapshots retained:</strong> {snap_count} &middot; "
                 f"<strong>Latest:</strong> {escape(_format_mmddyyyy(latest_date))} &middot; "
                 f"<strong>Ticker universe:</strong> {universe} &middot; "
                 f"<strong>Ranked today:</strong> {ranked_today}")
    if prior_date:
        parts.append(f"<br><strong>Entrants vs {escape(_format_mmddyyyy(prior_date))}:</strong> {escape(entrants_str)}")
        parts.append(f"<br><strong>Exits vs {escape(_format_mmddyyyy(prior_date))}:</strong> {escape(exits_str)}")
    parts.append("</div>")

    parts.append('<div class="scroll"><table>')
    parts.append("<thead><tr><th class='ticker'>Ticker</th>")
    for d in dates:
        dlabel = _format_mmddyyyy(d)
        parts.append(f"<th>AI Ranking - {escape(dlabel)}</th>")
        parts.append(f"<th>Close &amp; Change - {escape(dlabel)}</th>")
    parts.append("</tr></thead><tbody>")

    if not rows:
        ncols = 1 + 2 * len(dates) if dates else 1
        parts.append(f"<tr><td colspan='{ncols}' class='muted'>No snapshots yet — the first run on a day with rankings.json will seed the history.</td></tr>")
    for r in rows:
        parts.append(f"<tr><td class='ticker'>{escape(r['ticker'])}</td>")
        for cell in r["cells"]:
            rank_txt = _fmt_rank(cell.get("rank"))
            rank_cls = "rank" if rank_txt != "—" else "rank muted"
            parts.append(f"<td class='{rank_cls}'>{escape(rank_txt)}</td>")
            close_txt, css = _fmt_close_change(cell)
            parts.append(f"<td class='{css}'>{escape(close_txt)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></div>")

    parts.append(
        '<p class="legend">Close change is intraday (today\'s close vs the prior bar in the production '
        "<code>closes</code> array) when available; otherwise it falls back to day-over-day vs the most "
        "recent prior snapshot for the same ticker, marked <code>(vs YYYY-MM-DD)</code>. "
        "Tickers persist across snapshots even after they drop out of the rankings — a missing "
        "rank/close on a given date renders as <code>—</code>.</p>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


# ----------------- task stamp -----------------


def _stamp_task(report: dict) -> None:
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
    summary = report.get("summary_line") or "Rank vs close tracker"
    last_run = report.get("generated_at_chicago") or "—"
    row_id = "rank-close-tracker"
    existing = next((r for r in tasks if isinstance(r, dict) and r.get("id") == row_id), None)
    payload = {
        "id": row_id,
        "name": "Rank vs Close Tracker",
        "schedule": "Every refresh (08:45/12:30/15:35 CT, weekdays)",
        "last_run": last_run,
        "next_run": "—",
        "status": "OK",
        "summary": summary,
        "report_url": REPORT_URL,
    }
    if existing is None:
        tasks.append(payload)
    else:
        existing.update(payload)
    TASKS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ----------------- core -----------------


def build_report() -> dict:
    rankings = _load_json(RANKINGS_FILE)
    snapshots = _load_snapshots(SNAPSHOTS_FILE)
    today_snap = build_today_snapshot(rankings)
    if today_snap is not None:
        snapshots = upsert_snapshot(snapshots, today_snap)
    table = build_table(snapshots)
    summary = summarize(table, snapshots)

    now_utc = _now_utc()
    chi_now = _to_chicago(now_utc)
    chi_label = "CDT" if chi_now.utcoffset().total_seconds() == -5 * 3600 else "CST"
    chi_str = chi_now.strftime("%Y-%m-%d %I:%M %p ") + chi_label

    summary_line = (
        f"{summary['snapshot_count']} snapshot(s); latest {summary.get('latest_date') or '—'}; "
        f"universe {summary['ticker_universe_count']} ticker(s); "
        f"ranked today {summary['ranked_today_count']}"
    )

    return {
        "generated_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_chicago": chi_str,
        "snapshots_to_persist": snapshots,
        "table": table,
        "summary": summary,
        "summary_line": summary_line,
        "today_snapshot_built": today_snap is not None,
    }


def main() -> int:
    DATA_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    HTML_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = build_report()
    # Persist the rolling snapshot file BEFORE writing the report JSON so
    # the JSON output (which holds the assembled table) reflects what was
    # actually saved to disk.
    _write_snapshots(SNAPSHOTS_FILE, report["snapshots_to_persist"])
    # Drop the snapshots_to_persist key from the JSON output (it's just
    # the on-disk JSONL contents — duplicating it would bloat the file).
    json_out = {k: v for k, v in report.items() if k != "snapshots_to_persist"}
    JSON_OUTPUT.write_text(
        json.dumps(json_out, indent=2, default=str) + "\n", encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")
    _stamp_task(report)
    print(f"[rank_close_tracker] {report['summary_line']} -> {JSON_OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
