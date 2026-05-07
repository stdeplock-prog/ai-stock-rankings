"""Disagreement Queue Review — actionable workflow on top of the
external-benchmark disagreement queue.

This report does NOT change scoring formulas or rankings. It joins the
generated disagreement queue with Pine Go/No-Go classifications and the
cool-off cohort blockers so each disagreement carries the supporting
context a human reviewer needs to make a keep / watchlist_only / ignore /
needs_more_data call. It also keeps a persistent review-state artifact so
prior decisions and notes survive across runs even when the underlying
queue changes.

Inputs (read-only, no network):
  - data/reports/disagreement_queue.json
  - data/reports/external_benchmark_review.json
  - data/reports/pine_go_no_go_diagnostic.json
  - data/reports/cooloff_cohort_tracking.json
  - data/reports/disagreement_review_state.json (existing review state, if any)

Outputs:
  - data/reports/disagreement_queue_review.json
  - data/reports/disagreement_review_state.json (rewritten preserving prior
    manual edits — decision, notes, reviewed_at, follow_up_date)
  - reports/disagreement-queue-review.html
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
DATA_REPORTS_DIR = DATA_DIR / "reports"
HTML_REPORTS_DIR = REPO_ROOT / "reports"

QUEUE_FILE = DATA_REPORTS_DIR / "disagreement_queue.json"
EXTERNAL_FILE = DATA_REPORTS_DIR / "external_benchmark_review.json"
PINE_FILE = DATA_REPORTS_DIR / "pine_go_no_go_diagnostic.json"
COOLOFF_FILE = DATA_REPORTS_DIR / "cooloff_cohort_tracking.json"
STATE_FILE = DATA_REPORTS_DIR / "disagreement_review_state.json"

JSON_OUTPUT = DATA_REPORTS_DIR / "disagreement_queue_review.json"
HTML_OUTPUT = HTML_REPORTS_DIR / "disagreement-queue-review.html"
TASKS_FILE = DATA_DIR / "tasks.json"
REPORT_URL = "./reports/disagreement-queue-review.html"
TASK_ID = "disagreement-queue-review"

VALID_DECISIONS = {
    "", "keep", "watchlist_only", "ignore", "needs_more_data",
}

SEVERITY_RANK = {"severe": 3, "strong": 2, "moderate": 1, "": 0, None: 0}


# ---------- IO ----------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _load_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _today_iso() -> str:
    return _now_utc().date().isoformat()


# ---------- Stable key ----------


def make_key(ticker: str, headline_source: str | None) -> str:
    """Stable key for a queue entry. Ticker + primary headline source.
    A ticker can in principle disagree on multiple sources at different
    runs; we key by the headline (most-severe) source so the same kind
    of disagreement preserves its decision history. If the headline
    source changes (e.g. severity flips between sources), it's treated
    as a new disagreement — that's intentional, the reviewer should
    re-look.
    """
    t = (ticker or "").strip().upper()
    s = (headline_source or "").strip().lower()
    return f"{t}|{s}" if t else ""


# ---------- Pine + cool-off context indexes ----------


def index_pine(pine: dict | None) -> dict[str, dict]:
    """Build {ticker: pine_entry} from Pine diagnostic. Skips entries
    that weren't evaluated."""
    out: dict[str, dict] = {}
    per = (pine or {}).get("per_ticker") or [] if isinstance(pine, dict) else []
    for entry in per:
        if not isinstance(entry, dict):
            continue
        t = entry.get("ticker")
        if not t:
            continue
        out[t] = entry
    return out


def index_cooloff(cool: dict | None) -> dict[str, list[str]]:
    """Map each ticker to its cool-off blockers if it appears in the
    overextended_bb cohort. Returns {ticker: [blocker, ...]}.
    """
    if not isinstance(cool, dict):
        return {}
    members = ((cool.get("current_cohort_members") or {})
               .get("overextended_bb") or [])
    return {t: ["overextended_bb"] for t in members if isinstance(t, str)}


# ---------- Suggested decision ----------


def suggest_decision(*, queue_entry: dict, pine_entry: dict | None,
                     cooloff_blockers: list[str] | None) -> tuple[str, str]:
    """Return (suggested_decision, rationale).

    Rules — diagnostic only, never auto-applied:
      * Pine supports_internal -> 'keep' if external severity not severe,
        else 'needs_more_data'
      * Pine supports_external_caution OR overextended_bb blocker
        -> 'watchlist_only' for severe headline, else 'needs_more_data'
      * Pine weak/poor + multiple bearish external sources -> 'watchlist_only'
      * Otherwise -> 'needs_more_data'
    """
    severity = (queue_entry.get("headline_severity") or "").lower()
    n_sources = queue_entry.get("confidence_n_sources") or 0
    external = queue_entry.get("external_signals") or []
    bearish_n = sum(
        1 for s in external
        if isinstance(s, dict)
        and not s.get("direction_agrees")
        and isinstance(s.get("gap"), (int, float))
        and s.get("gap") < 0
    )

    pine_class = ""
    pine_score = None
    pine_blockers: list[str] = []
    if isinstance(pine_entry, dict):
        dis = pine_entry.get("disagreement") or {}
        pine_class = (dis.get("classification") or "").lower()
        pine_score = pine_entry.get("go_no_go_score_normalized")
        pine_blockers = list(pine_entry.get("blockers") or [])

    has_overextended = bool(cooloff_blockers) or any(
        "overextended_bb" in str(b) for b in pine_blockers
    )

    if pine_class == "supports_internal":
        if severity == "severe":
            return ("needs_more_data",
                    "Pine supports the internal bullish view but a severe "
                    "external disagreement remains — collect more evidence "
                    "before committing.")
        return ("keep",
                "Pine confirms the internal bullish view and external "
                "disagreement is not severe.")

    if pine_class == "supports_external_caution" or has_overextended:
        if severity == "severe" or bearish_n >= 2:
            return ("watchlist_only",
                    "Pine flags caution (or overextended_bb cohort) AND "
                    f"{bearish_n} external source(s) disagree bearishly. "
                    "Hold from active buys; monitor on watchlist.")
        return ("needs_more_data",
                "Pine flags caution but external disagreement is mild — "
                "keep on radar pending more evidence.")

    if isinstance(pine_score, (int, float)) and pine_score < 0.4 and bearish_n >= 2:
        return ("watchlist_only",
                f"Pine score {pine_score:.2f} is weak and {bearish_n} "
                "external source(s) disagree bearishly.")

    if pine_class == "supports_external_bearish":
        return ("watchlist_only",
                "Pine confirms the external bearish read; downgrade to "
                "watchlist while internal remains bullish.")

    return ("needs_more_data",
            "Mixed or insufficient evidence — collect more data before "
            "deciding.")


# ---------- Review-state merge ----------


def _normalize_state_entry(raw: dict | None, *, key: str, ticker: str,
                           today_iso: str) -> dict:
    """Normalize an existing state record to the canonical schema.
    Missing fields default to blanks. Used both for loading prior state
    and for shaping fresh entries."""
    raw = raw or {}
    decision = raw.get("decision")
    if decision not in VALID_DECISIONS:
        decision = ""
    return {
        "key": raw.get("key") or key,
        "ticker": raw.get("ticker") or ticker,
        "reviewed": bool(raw.get("reviewed", False)),
        "decision": decision or "",
        "notes": str(raw.get("notes") or ""),
        "reviewed_at": str(raw.get("reviewed_at") or ""),
        "follow_up_date": str(raw.get("follow_up_date") or ""),
        "first_seen": str(raw.get("first_seen") or today_iso),
        "last_seen": str(raw.get("last_seen") or today_iso),
        "current": bool(raw.get("current", True)),
    }


def load_state(path: Path = STATE_FILE) -> dict[str, dict]:
    """Load state from disk, returning {key: entry}. Tolerant to either
    a {"entries": [...]} list shape or a flat dict."""
    raw = _load_json(path)
    if not isinstance(raw, dict):
        return {}
    entries = raw.get("entries")
    if isinstance(entries, list):
        out: dict[str, dict] = {}
        for e in entries:
            if not isinstance(e, dict):
                continue
            k = e.get("key")
            if not k:
                continue
            out[k] = e
        return out
    if isinstance(entries, dict):
        return {k: v for k, v in entries.items() if isinstance(v, dict)}
    return {}


def merge_state(*, prior: dict[str, dict], queue: list[dict],
                today_iso: str) -> dict[str, dict]:
    """Return updated state {key: entry}.

    * For every queue entry we ensure a state row exists. Existing rows
      keep their decision/notes/reviewed/etc. — only `last_seen` and
      `current` are refreshed. New rows get first_seen=today.
    * Prior rows that are NOT in today's queue are kept (so manual edits
      survive) but flagged current=false. last_seen is left alone so the
      reviewer can see how stale the row is.
    """
    new_state: dict[str, dict] = {}

    seen_keys: set[str] = set()
    for q in queue:
        ticker = q.get("ticker") or ""
        key = make_key(ticker, q.get("headline_source"))
        if not key:
            continue
        seen_keys.add(key)
        existing = prior.get(key)
        merged = _normalize_state_entry(existing, key=key, ticker=ticker,
                                        today_iso=today_iso)
        merged["ticker"] = ticker  # always trust the live ticker spelling
        merged["last_seen"] = today_iso
        merged["current"] = True
        new_state[key] = merged

    for key, prev in prior.items():
        if key in seen_keys:
            continue
        ticker = prev.get("ticker") or key.split("|")[0]
        carried = _normalize_state_entry(prev, key=key, ticker=ticker,
                                         today_iso=today_iso)
        carried["current"] = False
        new_state[key] = carried

    return new_state


def state_to_disk_payload(state: dict[str, dict],
                           generated_at: str) -> dict:
    entries = sorted(state.values(),
                     key=lambda e: (not e.get("current", False), e.get("key", "")))
    return {
        "generated_at": generated_at,
        "schema_version": 1,
        "n_entries": len(entries),
        "n_current": sum(1 for e in entries if e.get("current")),
        "entries": entries,
    }


def save_state(payload: dict, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


# ---------- Report build ----------


def build_review_rows(*, queue: list[dict], state: dict[str, dict],
                      pine_idx: dict[str, dict],
                      cooloff_idx: dict[str, list[str]]) -> list[dict]:
    """Combine queue + state + pine + cool-off into one row per current
    disagreement. Sorted by severity desc, then confidence desc."""
    rows: list[dict] = []
    for q in queue:
        ticker = q.get("ticker") or ""
        key = make_key(ticker, q.get("headline_source"))
        if not key:
            continue
        st = state.get(key) or {}
        pine_entry = pine_idx.get(ticker)
        cooloff_blockers = cooloff_idx.get(ticker, [])
        suggested, rationale = suggest_decision(
            queue_entry=q, pine_entry=pine_entry,
            cooloff_blockers=cooloff_blockers,
        )

        pine_class = ""
        pine_score = None
        pine_action = ""
        pine_blockers: list[str] = []
        if isinstance(pine_entry, dict):
            dis = pine_entry.get("disagreement") or {}
            pine_class = dis.get("classification") or ""
            pine_action = dis.get("action") or ""
            pine_score = pine_entry.get("go_no_go_score_normalized")
            pine_blockers = list(pine_entry.get("blockers") or [])

        rows.append({
            "key": key,
            "ticker": ticker,
            "sector": q.get("sector"),
            "internal_ai_score_0to10": q.get("internal_ai_score_0to10"),
            "internal_ai_direction": q.get("internal_ai_direction"),
            "headline_source": q.get("headline_source"),
            "headline_severity": q.get("headline_severity"),
            "headline_gap": q.get("headline_gap"),
            "reason": q.get("reason"),
            "confidence_n_sources": q.get("confidence_n_sources"),
            "sources_flagging": list(q.get("sources_flagging") or []),
            "external_signals": list(q.get("external_signals") or []),
            "pine_classification": pine_class,
            "pine_action": pine_action,
            "pine_score_normalized": pine_score,
            "pine_blockers": pine_blockers,
            "cooloff_blockers": cooloff_blockers,
            "suggested_decision": suggested,
            "suggested_rationale": rationale,
            "review": {
                "reviewed": bool(st.get("reviewed", False)),
                "decision": st.get("decision") or "",
                "notes": st.get("notes") or "",
                "reviewed_at": st.get("reviewed_at") or "",
                "follow_up_date": st.get("follow_up_date") or "",
                "first_seen": st.get("first_seen") or "",
                "last_seen": st.get("last_seen") or "",
            },
        })

    rows.sort(key=lambda r: (
        -SEVERITY_RANK.get((r.get("headline_severity") or "").lower(), 0),
        -(r.get("confidence_n_sources") or 0),
        r.get("ticker") or "",
    ))
    return rows


def build_summary(*, rows: list[dict], state: dict[str, dict]) -> dict:
    severe = sum(1 for r in rows
                 if (r.get("headline_severity") or "").lower() == "severe")
    strong = sum(1 for r in rows
                 if (r.get("headline_severity") or "").lower() == "strong")
    reviewed = sum(1 for r in rows if r["review"]["reviewed"])
    needs_more = sum(1 for r in rows
                     if r["review"]["decision"] == "needs_more_data")
    unresolved = sum(1 for r in rows if not r["review"]["reviewed"])
    decisions: dict[str, int] = {}
    for r in rows:
        d = r["review"]["decision"] or "blank"
        decisions[d] = decisions.get(d, 0) + 1

    stale = sum(1 for v in state.values() if not v.get("current"))

    return {
        "total": len(rows),
        "unresolved": unresolved,
        "reviewed": reviewed,
        "severe": severe,
        "strong": strong,
        "needs_more_data": needs_more,
        "decisions": decisions,
        "stale_state_entries": stale,
    }


def build_report(*, queue_payload: dict | None,
                 external_payload: dict | None,
                 pine_payload: dict | None,
                 cooloff_payload: dict | None,
                 prior_state: dict[str, dict],
                 today_iso: str | None = None) -> tuple[dict, dict[str, dict]]:
    """Returns (report_dict, updated_state)."""
    today_iso = today_iso or _today_iso()
    queue = []
    if isinstance(queue_payload, dict):
        q = queue_payload.get("queue")
        if isinstance(q, list):
            queue = [x for x in q if isinstance(x, dict)]

    pine_idx = index_pine(pine_payload)
    cooloff_idx = index_cooloff(cooloff_payload)

    new_state = merge_state(prior=prior_state, queue=queue, today_iso=today_iso)
    rows = build_review_rows(queue=queue, state=new_state,
                             pine_idx=pine_idx, cooloff_idx=cooloff_idx)
    summary = build_summary(rows=rows, state=new_state)

    severe_unresolved = [
        r for r in rows
        if (r.get("headline_severity") or "").lower() == "severe"
        and not r["review"]["reviewed"]
    ]
    reviewed_rows = [r for r in rows if r["review"]["reviewed"]]

    overall = "OK"
    if summary["severe"] > 0 and summary["reviewed"] == 0:
        overall = "WARN"
    if summary["unresolved"] > 5 and summary["severe"] >= 3:
        overall = "WARN"

    n_external_sources = 0
    if isinstance(external_payload, dict):
        m = external_payload.get("metrics_by_source")
        if isinstance(m, dict):
            n_external_sources = len(m)

    report = {
        "generated_at": _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_date": today_iso,
        "overall": overall,
        "caveat": (
            "REVIEW WORKFLOW — these are review suggestions, not financial "
            "advice. Production scoring formulas and rankings are NOT "
            "modified by this report. Suggested decisions are diagnostic "
            "only; manual edits to data/reports/disagreement_review_state.json "
            "are preserved across runs."
        ),
        "inputs": {
            "queue_present": queue_payload is not None,
            "queue_size": len(queue),
            "external_present": external_payload is not None,
            "external_sources": n_external_sources,
            "pine_present": pine_payload is not None,
            "pine_evaluated": len(pine_idx),
            "cooloff_present": cooloff_payload is not None,
            "cooloff_overextended_n": len(cooloff_idx),
            "prior_state_entries": len(prior_state),
        },
        "summary_counts": summary,
        "unresolved_severe": severe_unresolved,
        "reviewed_decisions": reviewed_rows,
        "rows": rows,
    }
    report["summary"] = build_summary_text(report)
    return report, new_state


def build_summary_text(report: dict) -> str:
    s = report.get("summary_counts") or {}
    parts = [
        f"total={s.get('total', 0)}",
        f"unresolved={s.get('unresolved', 0)}",
        f"severe={s.get('severe', 0)}",
        f"reviewed={s.get('reviewed', 0)}",
    ]
    decisions = s.get("decisions") or {}
    nonblank = {k: v for k, v in decisions.items() if k != "blank"}
    if nonblank:
        parts.append("decisions=" + ",".join(
            f"{k}:{v}" for k, v in sorted(nonblank.items())))
    return " · ".join(parts)


# ---------- HTML ----------


def _badge_color(overall: str) -> str:
    return {"OK": "#3c8c3c", "WARN": "#b88a00", "FAIL": "#c0392b"}.get(overall, "#666")


def _fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}"
    return escape(str(v))


def _render_signals(signals: list[dict]) -> str:
    if not signals:
        return "<span class='muted'>—</span>"
    bits: list[str] = []
    for s in signals:
        if not isinstance(s, dict):
            continue
        src = escape(str(s.get("source") or ""))
        gap = s.get("gap")
        sev = (s.get("severity") or "").lower()
        agree = s.get("direction_agrees")
        gap_s = f"{gap:+.2f}" if isinstance(gap, (int, float)) else "—"
        cls = "agree" if agree else "disagree"
        bits.append(
            f"<span class='sig {cls} sev-{escape(sev)}'>{src} "
            f"{gap_s} <em>{escape(sev or '—')}</em></span>"
        )
    return " ".join(bits)


def _render_review_row(r: dict) -> str:
    rev = r["review"]
    decision = rev.get("decision") or ""
    decision_cell = (f"<span class='pill pill-{escape(decision)}'>"
                     f"{escape(decision or 'blank')}</span>")
    suggest = r.get("suggested_decision") or ""
    suggest_cell = (f"<span class='pill suggest pill-{escape(suggest)}'>"
                    f"{escape(suggest)}</span>")
    pine_class = r.get("pine_classification") or ""
    pine_score = r.get("pine_score_normalized")
    pine_score_s = f"{pine_score:.2f}" if isinstance(pine_score, (int, float)) else "—"
    cooloff = "✓" if r.get("cooloff_blockers") else "—"
    severity = (r.get("headline_severity") or "").lower()

    return (
        f"<tr class='sev-{escape(severity)}'>"
        f"<td><strong>{escape(r.get('ticker') or '')}</strong>"
        f"<div class='muted'>{escape(r.get('sector') or '')}</div></td>"
        f"<td>{_fmt(r.get('internal_ai_score_0to10'))}<br>"
        f"<span class='muted'>{escape(r.get('internal_ai_direction') or '')}</span></td>"
        f"<td>{escape(r.get('headline_source') or '')}<br>"
        f"<span class='muted'>{escape(severity or '')} "
        f"({_fmt(r.get('headline_gap'))})</span></td>"
        f"<td>{_render_signals(r.get('external_signals') or [])}</td>"
        f"<td>{escape(pine_class or '—')}<br>"
        f"<span class='muted'>score {pine_score_s}</span></td>"
        f"<td>{cooloff}</td>"
        f"<td>{suggest_cell}<div class='muted suggest-rationale'>"
        f"{escape(r.get('suggested_rationale') or '')}</div></td>"
        f"<td>{decision_cell}<br>"
        f"<span class='muted'>{escape(rev.get('notes') or '')}</span></td>"
        f"<td><span class='muted'>first {escape(rev.get('first_seen') or '—')}<br>"
        f"last {escape(rev.get('last_seen') or '—')}</span></td>"
        f"</tr>"
    )


def _render_table(rows: list[dict], *, empty_msg: str) -> str:
    if not rows:
        return f"<p class='muted'>{escape(empty_msg)}</p>"
    return (
        "<div class='table-wrap'><table><thead><tr>"
        "<th>Ticker</th><th>Internal</th><th>Headline</th>"
        "<th>External signals</th><th>Pine</th><th>Cool-off</th>"
        "<th>Suggested</th><th>Decision / notes</th><th>Seen</th>"
        "</tr></thead><tbody>"
        + "".join(_render_review_row(r) for r in rows)
        + "</tbody></table></div>"
    )


def _render_html(report: dict) -> str:
    overall = report.get("overall") or "OK"
    color = _badge_color(overall)
    summary_counts = report.get("summary_counts") or {}
    rows = report.get("rows") or []
    severe_unresolved = report.get("unresolved_severe") or []
    reviewed = report.get("reviewed_decisions") or []
    decisions = summary_counts.get("decisions") or {}

    decision_parts = " · ".join(
        f"{escape(k)}: <strong>{v}</strong>"
        for k, v in sorted(decisions.items())
    ) or "<span class='muted'>none</span>"

    parts: list[str] = []
    parts.append(f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Disagreement Queue Review</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;
     max-width:1280px;margin:24px auto;padding:0 16px;color:#1a1a1a;}}
h1{{margin:0 0 4px}} h2{{margin:18px 0 6px;font-size:18px}}
.meta{{color:#666;font-size:13px;margin-bottom:18px}}
.badge{{display:inline-block;padding:2px 10px;border-radius:12px;color:#fff;
       font-weight:600;font-size:12px;letter-spacing:.5px;background:{color}}}
.section{{border:1px solid #e3e3e3;border-radius:8px;padding:14px 16px;margin:14px 0}}
.summary{{font-size:14px;background:#f6f8fa;padding:10px 12px;border-radius:6px;margin-top:8px}}
.caveat{{background:#fff6e0;border:1px solid #f0d49a;color:#8a5a00;padding:10px 12px;
        border-radius:6px;margin:14px 0;font-size:13px}}
.kpis{{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin:8px 0 16px 0}}
.kpi{{background:#f6f8fa;border:1px solid #e3e3e3;border-radius:6px;padding:8px 10px;font-size:13px}}
.kpi .label{{color:#666;font-size:11px;text-transform:uppercase;letter-spacing:.06em}}
.kpi .value{{font-weight:700;font-size:18px;margin-top:2px}}
.table-wrap{{overflow:auto;border:1px solid #eaeaea;border-radius:6px;margin-top:8px}}
table{{border-collapse:collapse;width:100%;font-size:13px}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #eee;vertical-align:top}}
th{{background:#fafafa;position:sticky;top:0}}
tr.sev-severe td:first-child{{border-left:3px solid #c0392b}}
tr.sev-strong td:first-child{{border-left:3px solid #d68910}}
.muted{{color:#666;font-size:12px}}
.sig{{display:inline-block;border:1px solid #ddd;border-radius:10px;padding:1px 6px;
     font-size:11px;margin:1px 2px;background:#fafafa}}
.sig.disagree{{background:#fdecea;border-color:#f5b7b1}}
.sig.agree{{background:#eafaf1;border-color:#abebc6}}
.sig em{{font-style:normal;color:#888;font-size:10px;margin-left:2px}}
.pill{{display:inline-block;border-radius:10px;padding:1px 8px;font-size:11px;
      font-weight:600;color:#fff;background:#888}}
.pill-keep{{background:#27ae60}}
.pill-watchlist_only{{background:#d68910}}
.pill-needs_more_data{{background:#5d6d7e}}
.pill-ignore{{background:#a93226}}
.pill-blank,.pill-{{background:#bbb}}
.pill.suggest{{opacity:.85;border:1px dashed rgba(0,0,0,.2)}}
.suggest-rationale{{margin-top:3px;max-width:280px}}
.back{{font-size:13px}}
</style></head><body>
<p class="back"><a href="../index.html">&larr; Back to dashboard</a></p>
<h1>Disagreement Queue Review</h1>
<p class="meta">Generated {escape(report.get("generated_at",""))} &middot;
   as_of {escape(report.get("as_of_date",""))} &middot;
   Overall: <span class="badge">{escape(overall)}</span></p>
<div class="summary"><strong>Summary:</strong> {escape(report.get("summary",""))}</div>
<div class="caveat"><strong>Review suggestions, not financial advice:</strong>
   {escape(report.get("caveat",""))}</div>
""")

    parts.append("<div class='kpis'>")
    for label, val in [
        ("Total", summary_counts.get("total", 0)),
        ("Unresolved", summary_counts.get("unresolved", 0)),
        ("Reviewed", summary_counts.get("reviewed", 0)),
        ("Severe", summary_counts.get("severe", 0)),
        ("Strong", summary_counts.get("strong", 0)),
        ("Needs more data", summary_counts.get("needs_more_data", 0)),
    ]:
        parts.append(
            f"<div class='kpi'><div class='label'>{escape(label)}</div>"
            f"<div class='value'>{val}</div></div>"
        )
    parts.append("</div>")

    parts.append(f"<div class='section'><h2>Decision tally</h2>"
                 f"<p>{decision_parts}</p>"
                 f"<p class='muted'>Stale state entries (no longer in queue, "
                 f"kept for history): "
                 f"{summary_counts.get('stale_state_entries', 0)}</p>"
                 f"</div>")

    parts.append("<div class='section'><h2>Unresolved severe queue</h2>")
    parts.append("<p class='muted'>Sorted by severity, then by number of "
                 "external sources flagging the disagreement.</p>")
    parts.append(_render_table(severe_unresolved,
                               empty_msg="No unresolved severe disagreements."))
    parts.append("</div>")

    parts.append("<div class='section'><h2>All current disagreements</h2>")
    parts.append(_render_table(rows,
                               empty_msg="No current disagreements."))
    parts.append("</div>")

    parts.append("<div class='section'><h2>Reviewed decisions</h2>")
    parts.append(_render_table(reviewed,
                               empty_msg="No reviewed decisions yet — edit "
                                         "data/reports/disagreement_review_state.json "
                                         "to record reviews."))
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
        "name": "Disagreement Queue Review",
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
    queue_payload = _load_json(QUEUE_FILE)
    external_payload = _load_json(EXTERNAL_FILE)
    pine_payload = _load_json(PINE_FILE)
    cooloff_payload = _load_json(COOLOFF_FILE)
    prior_state = load_state()

    report, new_state = build_report(
        queue_payload=queue_payload if isinstance(queue_payload, dict) else None,
        external_payload=external_payload if isinstance(external_payload, dict) else None,
        pine_payload=pine_payload if isinstance(pine_payload, dict) else None,
        cooloff_payload=cooloff_payload if isinstance(cooloff_payload, dict) else None,
        prior_state=prior_state,
    )

    JSON_OUTPUT.write_text(
        json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    HTML_OUTPUT.write_text(_render_html(report), encoding="utf-8")
    save_state(state_to_disk_payload(new_state, report["generated_at"]))
    _ensure_task_row()
    _stamp_task(report)
    print(f"[disagreement_queue_review] overall={report['overall']} -> {JSON_OUTPUT}")
    print(f"[disagreement_queue_review] summary: {report['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
