"""Market Risk Monitor — generates reports/market-risk-monitor.html and
data/reports/market_risk_monitor.json.

Indicators:
  - VIX (yfinance ^VIX)
  - "When the Generals Fail" — count of leading 7 S&P 500 stocks below 200DMA
  - Put/Call Ratio: Cboe daily market statistics (market-wide, risk context only)
  - POLLS / ADR / NDR: Source needed (no committed feed)

Pass/warn coloring:
  - VIX >= 20 = warn (elevated)
  - Generals Fail >= 3 below 200DMA = warn
  - POLLS >= 18 = warn
  - Equity P/C extremes: conservative WARN at <= 0.40 (speculative complacency)
    or >= 1.20 (fear/hedging). Anything in between = OK / risk context only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data" / "reports"
HTML_DIR = REPO_ROOT / "reports"
TASKS_FILE = REPO_ROOT / "data" / "tasks.json"

LEADING_7 = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA"]


def _fetch_close_history(ticker: str, period: str = "1y"):
    """Fetch Close series with yfinance. Returns list[float] or None on error."""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        h = yf.Ticker(ticker).history(period=period, auto_adjust=False)
        if h is None or h.empty:
            return None
        return [float(x) for x in h["Close"].tolist()]
    except Exception:
        return None


def _build_generals_fail():
    rows = []
    below = 0
    available = 0
    for t in LEADING_7:
        closes = _fetch_close_history(t, period="1y")
        if not closes or len(closes) < 200:
            rows.append({"ticker": t, "last": None, "ma200": None, "below": None, "status": "unavailable"})
            continue
        available += 1
        last = closes[-1]
        ma200 = sum(closes[-200:]) / 200.0
        is_below = last < ma200
        if is_below:
            below += 1
        rows.append({
            "ticker": t,
            "last": round(last, 2),
            "ma200": round(ma200, 2),
            "below": is_below,
            "status": "below_200dma" if is_below else "above_200dma",
        })
    return {
        "rows": rows,
        "below_count": below,
        "available_count": available,
        "threshold": 3,
        "alert": below >= 3,
    }


def _build_vix():
    closes = _fetch_close_history("^VIX", period="1mo")
    if not closes:
        return {"value": None, "status": "unavailable"}
    last = closes[-1]
    return {
        "value": round(last, 2),
        "status": "elevated" if last >= 20 else "normal",
        "alert": last >= 20,
    }


CBOE_DAILY_STATS_URL = "https://www.cboe.com/us/options/market_statistics/daily/"

# Series we surface from the Cboe daily stats page. Keys are the JSON field
# names; values are the human-readable label on the Cboe page (matched
# case-insensitively).
_CBOE_SERIES = [
    ("total", "TOTAL PUT/CALL RATIO"),
    ("equity", "EQUITY PUT/CALL RATIO"),
    ("index", "INDEX PUT/CALL RATIO"),
    ("etp", "EXCHANGE TRADED PRODUCTS PUT/CALL RATIO"),
    ("vix", "CBOE VOLATILITY INDEX (VIX) PUT/CALL RATIO"),
    ("spx", "SPX + SPXW PUT/CALL RATIO"),
]


def _parse_cboe_html(html: str) -> dict:
    """Extract put/call ratios from Cboe daily stats HTML.

    The page renders each ratio as adjacent table cells:
        <td ...>TOTAL PUT/CALL RATIO</td><td ...>0.83</td>
    We match label -> next numeric value robustly via regex.
    """
    ratios: dict[str, float] = {}
    for key, label in _CBOE_SERIES:
        # Match the label text, then the first numeric value appearing in a
        # following <td> within ~400 chars. Tolerant of attribute changes.
        pat = re.compile(
            re.escape(label) + r"\s*</td>\s*<td[^>]*>\s*([0-9]+\.[0-9]+)\s*</td>",
            re.IGNORECASE,
        )
        m = pat.search(html)
        if m:
            try:
                ratios[key] = float(m.group(1))
            except ValueError:
                pass
    return ratios


def _fetch_cboe_html(timeout: float = 10.0) -> str | None:
    """Fetch the Cboe daily stats page. Returns HTML text or None on error."""
    try:
        import urllib.request
        req = urllib.request.Request(
            CBOE_DAILY_STATS_URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ai-stock-rankings/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None


def _classify_equity_pc(value: float) -> tuple[str, bool, str]:
    """Conservative interpretation of equity P/C ratio.

    Returns (status_label, alert_bool, kind) where kind is 'pass'|'warn'.
    Thresholds intentionally wide to avoid overfitting on a single day.
    """
    if value <= 0.40:
        return ("Low — speculative", True, "warn")
    if value >= 1.20:
        return ("High — defensive", True, "warn")
    return ("Neutral range", False, "pass")


def _build_put_call(html: str | None = None) -> dict:
    """Build the put/call indicator payload from Cboe daily stats.

    Accepts an optional pre-fetched HTML string to make testing trivial.
    """
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if html is None:
        html = _fetch_cboe_html()
    if not html:
        return {
            "value": None,
            "status": "source_needed",
            "note": "Cboe daily stats fetch failed. Market-wide put/call unavailable.",
            "source": CBOE_DAILY_STATS_URL,
            "fetched_at": fetched_at,
        }
    ratios = _parse_cboe_html(html)
    if not ratios:
        return {
            "value": None,
            "status": "source_needed",
            "note": "Cboe daily stats parse failed. Page format may have changed.",
            "source": CBOE_DAILY_STATS_URL,
            "fetched_at": fetched_at,
        }
    equity = ratios.get("equity")
    if equity is not None:
        label, alert, _kind = _classify_equity_pc(equity)
    else:
        label, alert = "Equity ratio missing", False
    return {
        "ratios": ratios,
        "value": equity,  # headline value = equity P/C
        "status": "warn" if alert else "ok",
        "alert": alert,
        "label": label,
        "note": (
            "Market-wide context only (not per-ticker). Equity P/C extremes: "
            "<= 0.40 speculative complacency; >= 1.20 fear/hedging."
        ),
        "source": CBOE_DAILY_STATS_URL,
        "fetched_at": fetched_at,
    }


def _placeholder(label: str, threshold: str | None = None):
    return {
        "value": None,
        "status": "source_needed",
        "note": f"Source needed for {label}. Not available in repo data feeds.",
        "threshold": threshold,
    }


def build_report():
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = {
        "generated_at": generated_at,
        "indicators": {
            "polls": _placeholder("POLLS Indicator", threshold=">= 18 triggers alert"),
            "adr": _placeholder("ADR Indicator"),
            "vix": _build_vix(),
            "ndr": _placeholder("NDR Indicator"),
            "put_call_ratio": _build_put_call(),
            "generals_fail": _build_generals_fail(),
        },
    }
    return payload


# ---- Rendering ---------------------------------------------------------

CSS = """
:root { --bg:#0b1220; --panel:#111827; --panel2:#172033; --line:#243043; --text:#e5eefc; --muted:#93a4bd; }
body { margin:0; font-family:Inter,Arial,sans-serif; background:var(--bg); color:var(--text); }
header { background:var(--panel2); border-bottom:1px solid var(--line); padding:14px 18px; }
h1 { margin:0; font-size:20px; }
.meta { color:var(--muted); font-size:12px; margin-top:4px; }
main { padding:18px; max-width:1100px; margin:0 auto; }
section { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px 16px; margin-bottom:14px; }
section h2 { margin:0 0 10px; font-size:15px; letter-spacing:.04em; text-transform:uppercase; color:var(--muted); }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; }
th { color:var(--muted); text-transform:uppercase; font-size:11px; letter-spacing:.05em; }
.pill { display:inline-block; padding:3px 10px; border-radius:999px; font-weight:700; font-size:12px; }
.pill-pass { background:#064e3b; color:#d1fae5; }
.pill-warn { background:#78350f; color:#fde68a; }
.pill-info { background:#1f2937; color:#cbd5e1; }
.signal-row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:6px; }
.signal-row .label { min-width:170px; color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.05em; }
.signal-row .val { font-weight:700; font-size:15px; }
.note { color:var(--muted); font-size:12px; margin-top:6px; }
a { color:#60a5fa; }
.back { display:inline-block; margin-top:12px; color:#60a5fa; text-decoration:none; }
.back:hover { text-decoration:underline; }
"""


def _pill(text: str, kind: str) -> str:
    return f'<span class="pill pill-{kind}">{escape(text)}</span>'


def _signal_row(label: str, value_html: str, pill_html: str, note: str = "") -> str:
    note_html = f'<div class="note">{escape(note)}</div>' if note else ""
    return f"""
    <div class="signal-row">
      <div class="label">{escape(label)}</div>
      <div class="val">{value_html}</div>
      {pill_html}
    </div>
    {note_html}
    """


def render_html(payload: dict) -> str:
    ind = payload["indicators"]

    # POLLS
    polls = ind["polls"]
    polls_row = _signal_row(
        "POLLS Indicator",
        '<span style="color:#9ca3af">—</span>',
        _pill("Source needed", "info"),
        polls.get("note", ""),
    )

    # ADR
    adr = ind["adr"]
    adr_row = _signal_row(
        "ADR Indicator",
        '<span style="color:#9ca3af">—</span>',
        _pill("Source needed", "info"),
        adr.get("note", ""),
    )

    # VIX
    vix = ind["vix"]
    if vix.get("value") is None:
        vix_row = _signal_row("VIX", "—", _pill("Unavailable", "info"))
    else:
        kind = "warn" if vix.get("alert") else "pass"
        label = "Elevated" if vix.get("alert") else "Normal"
        vix_row = _signal_row("VIX", f'{vix["value"]:.2f}', _pill(label, kind), "Threshold: warn at >= 20")

    # NDR
    ndr = ind["ndr"]
    ndr_row = _signal_row(
        "NDR Indicator",
        '<span style="color:#9ca3af">—</span>',
        _pill("Source needed", "info"),
        ndr.get("note", ""),
    )

    # Put/Call Ratio — Cboe daily market statistics
    pcr = ind["put_call_ratio"]
    if pcr.get("status") == "source_needed":
        pcr_row = _signal_row(
            "Put/Call Ratio (Cboe)",
            '<span style="color:#9ca3af">—</span>',
            _pill("Source needed", "info"),
            pcr.get("note", ""),
        )
        pcr_table_html = ""
    else:
        ratios = pcr.get("ratios", {})
        equity = pcr.get("value")
        if equity is None:
            head_html = '<span style="color:#9ca3af">—</span>'
            head_pill = _pill("No equity value", "info")
        else:
            head_html = f'{equity:.2f}'
            kind = "warn" if pcr.get("alert") else "pass"
            head_pill = _pill(pcr.get("label", ""), kind)
        pcr_row = _signal_row(
            "Equity Put/Call (Cboe)",
            head_html,
            head_pill,
            pcr.get("note", ""),
        )
        # Detail table of all ratios we extracted.
        labels = {
            "total": "Total",
            "equity": "Equity",
            "index": "Index",
            "etp": "ETP",
            "vix": "VIX",
            "spx": "SPX + SPXW",
        }
        rows = []
        for key in ("total", "equity", "index", "etp", "vix", "spx"):
            if key in ratios:
                rows.append(
                    f"<tr><td>{escape(labels[key])}</td>"
                    f"<td>{ratios[key]:.2f}</td></tr>"
                )
        src = escape(pcr.get("source", ""))
        fetched_at = escape(pcr.get("fetched_at", ""))
        pcr_table_html = f"""
    <section>
      <h2>Put/Call Ratios — Cboe Daily</h2>
      <table>
        <thead><tr><th>Series</th><th>Ratio</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      <div class="note">Market-wide risk context only — not used in per-ticker scoring. Source: <a href="{src}">Cboe daily market statistics</a>. Fetched {fetched_at}.</div>
    </section>
    """

    # Generals Fail
    gf = ind["generals_fail"]
    rows_html_parts = []
    for r in gf["rows"]:
        if r["last"] is None:
            rows_html_parts.append(
                f"<tr><td>{escape(r['ticker'])}</td><td>—</td><td>—</td><td>{_pill('Unavailable', 'info')}</td></tr>"
            )
        else:
            kind = "warn" if r["below"] else "pass"
            label = "Below 200DMA" if r["below"] else "Above 200DMA"
            rows_html_parts.append(
                f"<tr><td>{escape(r['ticker'])}</td><td>{r['last']:.2f}</td><td>{r['ma200']:.2f}</td><td>{_pill(label, kind)}</td></tr>"
            )

    gf_pill_kind = "warn" if gf["alert"] else "pass"
    gf_pill_label = (
        f"ALERT: {gf['below_count']}/{gf['available_count']} below 200DMA"
        if gf["alert"]
        else f"OK: {gf['below_count']}/{gf['available_count']} below 200DMA"
    )

    polls_alert = polls.get("value") is not None and polls.get("alert")
    if polls.get("value") is None:
        compare_polls_html = _pill("POLLS source needed", "info")
    else:
        compare_polls_html = _pill(
            f"POLLS = {polls['value']} ({'>=' if polls_alert else '<'} 18)",
            "warn" if polls_alert else "pass",
        )
    compare_gf_html = _pill(
        f"Generals Fail = {gf['below_count']}/{gf['available_count']} ({'>=' if gf['alert'] else '<'} 3)",
        "warn" if gf["alert"] else "pass",
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Market Risk Monitor</title>
  <style>{CSS}</style>
</head>
<body>
  <header>
    <h1>Market Risk Monitor</h1>
    <div class="meta">Generated {escape(payload['generated_at'])}</div>
  </header>
  <main>
    <section>
      <h2>Headline Comparison</h2>
      <div class="signal-row">
        <div class="label">POLLS &gt;= 18</div>
        <div class="val">{compare_polls_html}</div>
      </div>
      <div class="signal-row">
        <div class="label">Generals Fail &gt;= 3 below 200DMA</div>
        <div class="val">{compare_gf_html}</div>
      </div>
    </section>

    <section>
      <h2>Risk Indicators (last available values)</h2>
      {polls_row}
      {adr_row}
      {vix_row}
      {ndr_row}
      {pcr_row}
    </section>
    {pcr_table_html}
    <section>
      <h2>When the Generals Fail — Leading 7 vs 200DMA</h2>
      <div class="signal-row">
        <div class="label">Headline</div>
        <div class="val">{_pill(gf_pill_label, gf_pill_kind)}</div>
      </div>
      <table>
        <thead><tr><th>Ticker</th><th>Last</th><th>200DMA</th><th>Status</th></tr></thead>
        <tbody>
          {''.join(rows_html_parts)}
        </tbody>
      </table>
      <div class="note">Leading 7 = Magnificent 7 (AAPL, MSFT, NVDA, AMZN, GOOGL, META, TSLA). Source: yfinance daily closes.</div>
    </section>

    <a class="back" href="../index.html">&larr; Back to dashboard</a>
  </main>
</body>
</html>
"""
    return html


def _summary_from_payload(payload: dict) -> tuple[str, str]:
    """Return (status, summary) for tasks.json based on actual signal state.

    Status: 'warn' if any wired indicator is in alert; 'OK' otherwise. The
    'source needed' indicators do not flip the status — they're surfaced in
    the summary text so the dashboard reflects real state, not fabrications.
    """
    ind = payload["indicators"]
    parts = []
    alert = False

    gf = ind["generals_fail"]
    below = gf.get("below_count")
    avail = gf.get("available_count")
    if avail:
        below_tickers = [r["ticker"] for r in gf.get("rows", []) if r.get("below")]
        if gf.get("alert"):
            alert = True
            tail = f" ({', '.join(below_tickers)})" if below_tickers else ""
            parts.append(f"Generals Fail {below}/{avail} below 200DMA{tail} — alert")
        else:
            parts.append(f"Generals Fail {below}/{avail} below 200DMA")
    else:
        parts.append("Generals Fail: unavailable")

    vix = ind["vix"]
    if vix.get("value") is not None:
        label = "elevated" if vix.get("alert") else "normal"
        if vix.get("alert"):
            alert = True
        parts.append(f"VIX {vix['value']:.2f} {label}")
    else:
        parts.append("VIX unavailable")

    pcr = ind["put_call_ratio"]
    if pcr.get("status") != "source_needed" and pcr.get("value") is not None:
        if pcr.get("alert"):
            alert = True
        parts.append(
            f"Equity P/C {pcr['value']:.2f} ({pcr.get('label', '').lower()})"
        )

    sn = [name for name, key in [
        ("POLLS", "polls"),
        ("ADR", "adr"),
        ("NDR", "ndr"),
        ("Put-Call", "put_call_ratio"),
    ] if ind[key].get("status") == "source_needed"]
    if sn:
        parts.append("/".join(sn) + ": source needed")

    return ("warn" if alert else "OK", ". ".join(parts) + ".")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_report()
    (DATA_DIR / "market_risk_monitor.json").write_text(json.dumps(payload, indent=2))
    (HTML_DIR / "market-risk-monitor.html").write_text(render_html(payload))

    # Stamp tasks.json so the dashboard reflects this regeneration. Failures
    # here must not break the report itself.
    try:
        from _tasks_meta import update_task
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _tasks_meta import update_task
    status, summary = _summary_from_payload(payload)
    try:
        update_task(
            TASKS_FILE,
            task_id="market-risk-monitor",
            status=status,
            summary=summary,
            report_url="./reports/market-risk-monitor.html",
        )
    except Exception as e:
        print(f"Warning: could not update tasks.json for market-risk-monitor: {e}")

    print(f"Wrote market risk monitor report ({payload['generated_at']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
