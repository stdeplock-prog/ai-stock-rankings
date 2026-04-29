"""Options / Earnings Watchlist — renders reports/options-watchlist.html from
reports/highest-conviction-options-calls-2026-04-28.md (the canonical source).

Run after editing the markdown source to refresh the HTML.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_MD = REPO_ROOT / "reports" / "highest-conviction-options-calls-2026-04-28.md"
OUT_HTML = REPO_ROOT / "reports" / "options-watchlist.html"

CSS = """
:root { --bg:#0b1220; --panel:#111827; --panel2:#172033; --line:#243043; --text:#e5eefc; --muted:#93a4bd; }
body { margin:0; font-family:Inter,Arial,sans-serif; background:var(--bg); color:var(--text); }
header { background:var(--panel2); border-bottom:1px solid var(--line); padding:14px 18px; }
h1 { margin:0; font-size:20px; }
.meta { color:var(--muted); font-size:12px; margin-top:4px; }
main { padding:18px; max-width:980px; margin:0 auto; }
article { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px 22px; }
article h1, article h2, article h3 { color:var(--text); }
article h2 { border-bottom:1px solid var(--line); padding-bottom:6px; margin-top:28px; }
article h3 { color:#d1d5db; margin-top:18px; }
article p { line-height:1.55; color:#e5eefc; }
article table { width:100%; border-collapse:collapse; margin:8px 0 12px; font-size:13px; }
article th, article td { padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; }
article th { color:var(--muted); text-transform:uppercase; font-size:11px; letter-spacing:.05em; background:var(--panel2); }
article a { color:#60a5fa; }
article code { background:#1f2937; padding:1px 6px; border-radius:4px; font-size:12px; }
article hr { border:none; border-top:1px solid var(--line); margin:24px 0; }
article ul { line-height:1.55; }
article blockquote { border-left:3px solid var(--line); padding-left:12px; color:var(--muted); }
.back { display:inline-block; margin-top:14px; color:#60a5fa; text-decoration:none; }
.back:hover { text-decoration:underline; }
sup { font-size:10px; color:var(--muted); }
"""


def render():
    if not SRC_MD.exists():
        print(f"Source markdown not found: {SRC_MD}", file=sys.stderr)
        return 1
    try:
        import markdown
    except ImportError:
        print("Python 'markdown' package required.", file=sys.stderr)
        return 2

    md_text = SRC_MD.read_text(encoding="utf-8")
    body_html = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "footnotes", "sane_lists"],
    )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = "Highest Conviction Options Calls — April 28, 2026"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>{escape(title)}</title>
  <style>{CSS}</style>
</head>
<body>
  <header>
    <h1>Options / Earnings Watchlist</h1>
    <div class="meta">Source: <a href="./highest-conviction-options-calls-2026-04-28.md">highest-conviction-options-calls-2026-04-28.md</a> &middot; Rendered {escape(generated)}</div>
  </header>
  <main>
    <article>
      {body_html}
    </article>
    <a class="back" href="../index.html">&larr; Back to dashboard</a>
  </main>
</body>
</html>
"""
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"Wrote {OUT_HTML.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(render())
