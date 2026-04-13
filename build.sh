#!/usr/bin/env bash
set -euo pipefail

# 1) Folders
mkdir -p server/providers web

# 2) .gitignore and env example
cat > .gitignore << 'EOF'
node_modules
.env
app.zip
EOF

cat > .env.example << 'EOF'
DANELFIN_API_KEY=replace_with_your_key
PORT=3000
EOF

# 3) package.json
cat > package.json << 'EOF'
{
  "name": "ai-stock-rankings",
  "private": true,
  "type": "module",
  "scripts": {
    "dev:api": "NODE_ENV=development node server/index.js",
    "dev:ui": "npx serve web -l 5173",
    "build": "echo \"Static UI build ready; backend is Node/Express.\""
  },
  "dependencies": {
    "cors": "^2.8.5",
    "dotenv": "^16.4.5",
    "express": "^4.19.2",
    "node-fetch": "^3.3.2"
  }
}
EOF

# 4) server/normalizer.js
cat > server/normalizer.js << 'EOF'
export function normalizeRanking(raw, universe = "Top Popular") {
  const dayKey = Object.keys(raw || {})[0] || null;
  const dayRows = (dayKey && raw[dayKey]) || {};
  const rows = Object.entries(dayRows).map(([ticker, v], i) => ({
    rank: i + 1,
    ticker,
    company: v.company ?? null,
    country: v.country ?? null,
    ai_score: v.aiscore ?? null,
    change: v.change ?? null,
    fundamental: v.fundamental ?? null,
    technical: v.technical ?? null,
    sentiment: v.sentiment ?? null,
    low_risk: v.low_risk ?? null,
    volume_millions: v.volume ?? null,
    industry: v.industry ?? null,
    sector: v.sector ?? null,
    buy_track_record: v.buy_track_record ?? null,
    sell_track_record: v.sell_track_record ?? null,
    provider_payload: {}
  }));
  return { as_of: dayKey, universe, market: "us", source: "danelfin", rows };
}
EOF

# 5) server/providers/danelfin.js
cat > server/providers/danelfin.js << 'EOF'
import fetch from "node-fetch";
const BASE_URL = "https://apirest.danelfin.com";

export async function getRanking({ apiKey, params }) {
  const qs = new URLSearchParams(params).toString();
  const url = `${BASE_URL}/ranking?${qs}`;
  const res = await fetch(url, { headers: { "x-api-key": apiKey } });
  if (!res.ok) throw new Error(`Upstream ${res.status}`);
  return res.json();
}

export async function getSectors({ apiKey }) {
  const r = await fetch(`${BASE_URL}/sectors`, { headers: { "x-api-key": apiKey } });
  if (!r.ok) throw new Error(`Upstream ${r.status}`);
  return r.json();
}

export async function getIndustries({ apiKey }) {
  const r = await fetch(`${BASE_URL}/industries`, { headers: { "x-api-key": apiKey } });
  if (!r.ok) throw new Error(`Upstream ${r.status}`);
  return r.json();
}
EOF

# 6) server/index.js
cat > server/index.js << 'EOF'
import express from "express";
import cors from "cors";
import dotenv from "dotenv";
import { normalizeRanking } from "./normalizer.js";
import { getRanking, getSectors, getIndustries } from "./providers/danelfin.js";

dotenv.config();
const app = express();
const PORT = process.env.PORT || 3000;
const API_KEY = process.env.DANELFIN_API_KEY;

// CORS: set your UI origin(s)
app.use(cors({ origin: [/^http:\/\/localhost: 5173$/, /^https:\/\/your-ui\.example\.com$/] }));

// Security headers
app.use((_, res, next) => {
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.setHeader("Referrer-Policy", "no-referrer");
  res.setHeader("Permissions-Policy", "geolocation=()");
  next();
});

// Tiny cache
const cache = new Map();
const setCache = (k, v, ttl = 300000) => cache.set(k, { v, e: Date.now() + ttl });
const getCache = (k) => { const it = cache.get(k); return it && it.e > Date.now() ? it.v : null; };

// S&P 500 placeholder
const spx = new Set();

// Universe shapes
const shape = {
  base: (r) => r,
  growth: (r) => ({ ...r, rows: r.rows.filter(x => (x.ai_score ?? 0) >= 8).sort((a,b)=> (b.fundamental??0)
-(a.fundamental??0)) }),
  lowrisk: (r) => ({ ...r, rows: r.rows.sort((a,b)=> (b.low_risk??0)
-(a.low_risk??0)) }),
  sp500: (r) => ({ ...r, rows: r.rows.filter(x => spx.has(x.ticker)) })
};

function label(k){ return ({ popular:"Top Popular", growth:"Top Growth", lowrisk:"Top Low Risk", sp500:"Top S&P 500" })[k]; }

function makeHandler(key){
  return async (req, res) => {
    try {
      const date = req.query.date || new Date().toISOString().slice(0,10);
      const ck = `${key}:${date}`;
      const got = getCache(ck); if (got) return res.json(got);

      const raw = await getRanking({ apiKey: API_KEY, params: { date, market: "us", asset: "stock" } });
      const base = normalizeRanking(raw, label(key));
      const shaped =
        key === "popular" ? shape.base(base) :
        key === "growth" ? shape.growth(base) :
        key === "lowrisk" ? shape.lowrisk(base) :
        shape.sp500(base);

      shaped.rows = shaped.rows.map((r,i)=> ({ ...r, rank: i+1 }));
      setCache(ck, shaped, 300000);
      res.json(shaped);
    } catch (e) {
      const y = new Date(Date.now()
-86400000).toISOString().slice(0,10);
      const fb = getCache(`${key}:${y}`);
      if (fb) return res.status(200).json({ ...fb, stale: true });
      res.status(502).json({ error: "Upstream error" });
    }
  };
}

app.get("/api/dashboard/top-popular", makeHandler("popular"));
app.get("/api/dashboard/top-growth", makeHandler("growth"));
app.get("/api/dashboard/top-low-risk", makeHandler("lowrisk"));
app.get("/api/dashboard/top-sp500", makeHandler("sp500"));

app.get("/api/dashboard/sectors", async (_req, res) => {
  try {
    const c = getCache("sectors"); if (c) return res.json(c);
    const j = await getSectors({ apiKey: API_KEY });
    setCache("sectors", j, 43200000);
    res.json(j);
  } catch { res.json([]); }
});

app.get("/api/dashboard/industries", async (_req, res) => {
  try {
    const c = getCache("industries"); if (c) return res.json(c);
    const j = await getIndustries({ apiKey: API_KEY });
    setCache("industries", j, 43200000);
    res.json(j);
  } catch { res.json([]); }
});

app.listen(PORT, () => console.log(`API on :${PORT}`));
EOF

# 7) web/index.html
cat > web/index.html << 'EOF'
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>AI Stock Rankings</title>
<style>
:root{--bg:#0b1220;--panel:#111827;--panel2:#172033;--line:#243043;--text:#e5eefc;--muted:#93a4bd;--pill:#1f2937}
*{box-sizing:border-box} body{margin: 0;font-family:Inter,Arial,sans-serif;background:var(--bg);color:var(--text)}
header{position:sticky;top: 0;z-index: 10;background:var(--panel2);border-bottom: 1px solid var(--line);padding: 12px 16px}
.row{display:flex;align-items:center;justify-content:space-between;gap: 12px;flex-wrap:wrap}
.controls{display:flex;gap: 8px;flex-wrap:wrap}
.btn,select,input{background:var(--panel);color:var(--text);border: 1px solid var(--line);padding: 8px 10px;border-radius: 6px}
main{padding: 16px}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap: 10px;margin: 12px 0}
.card{background:var(--panel);border: 1px solid var(--line);border-radius: 8px;padding: 12px}
.label{font-size: 12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.value{font-size: 22px;font-weight: 700;margin-top: 6px}
.table-wrap{overflow:auto;border: 1px solid var(--line);border-radius: 8px}
table{width: 100%;border-collapse:collapse;min-width: 1100px}
th,td{padding: 10px;border-bottom: 1px solid var(--line);text-align:left}
th{position:sticky;top: 0;background:var(--panel2);font-size: 12px;letter-spacing:.05em;text-transform:uppercase;cursor:pointer}
tr:hover{background:rgba(255,255,255,.03)}
.pill{display:inline-block;padding: 3px 8px;border-radius: 999px;background:var(--pill);font-weight: 700}
@media(max-width: 960px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width: 640px){.kpis{grid-template-columns: 1fr}}
</style>
</head>
<body>
<header>
  <div class="row">
    <div>
      <div class="label">Live rankings dashboard</div>
      <div style="font-size: 22px;font-weight: 700">AI Stock Rankings</div>
      <div id="updated" class="label" style="margin-top: 4px">Last Updated: —</div>
    </div>
    <div class="controls">
      <select id="universe">
        <option value="top-popular">Top Popular</option>
        <option value="top-growth">Top Growth</option>
        <option value="top-low-risk">Top Low Risk</option>
        <option value="top-sp500">Top S&P 500</option>
      </select>
      <input id="search" placeholder="Search ticker or company"/>
      <button class="btn" id="refreshBtn">Refresh</button>
      <button class="btn" id="exportBtn">Export CSV</button>
    </div>
  </div>
</header>
<main>
  <section class="kpis">
    <div class="card"><div class="label">Rows</div><div class="value" id="kpiRows">0</div></div>
    <div class="card"><div class="label">AI 9–10</div><div class="value" id="kpiTop">0</div></div>
    <div class="card"><div class="label">Avg Technical</div><div class="value" id="kpiTech">0.00</div></div>
    <div class="card"><div class="label">Avg Sentiment</div><div class="value" id="kpiSent">0.00</div></div>
  </section>
  <section class="table-wrap">
    <table>
      <thead>
        <tr id="thead">
          <th data-k="rank">Rank</th>
          <th data-k="ticker">Ticker</th>
          <th data-k="company">Company</th>
          <th data-k="country">Country</th>
          <th data-k="ai_score">AI</th>
          <th data-k="change">Chg</th>
          <th data-k="fundamental">Fund</th>
          <th data-k="technical">Tech</th>
          <th data-k="sentiment">Sent</th>
          <th data-k="low_risk">Risk</th>
          <th data-k="volume_millions">Vol (M)</th>
          <th data-k="industry">Industry</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </section>
</main>
<script>
let state={rows:[],as_of:null,universe:""};let sortKey="rank",sortDir="asc";
async function loadData(){const uni=document.getElementById("universe").value;const r=await fetch(`/api/dashboard/${uni}`);const d=await r.json();state=d;document.getElementById("updated").textContent=`Last Updated: ${d.as_of||"—"}`;applySearch();}
function render(rows){const tb=document.getElementById("tbody");tb.innerHTML=rows.map(r=>`
<tr>
  <td>${r.rank??""}</td>
  <td><strong>${r.ticker??""}</strong></td>
  <td>${r.company??""}</td>
  <td>${r.country??""}</td>
  <td><span class="pill">${r.ai_score??""}</span></td>
  <td>${r.change??""}</td>
  <td>${r.fundamental??""}</td>
  <td>${r.technical??""}</td>
  <td>${r.sentiment??""}</td>
  <td>${r.low_risk??""}</td>
  <td>${r.volume_millions??""}</td>
  <td>${r.industry??""}</td>
</tr>`).join("");
const n=rows.length;const aiHigh=rows.filter(x=>Number(x.ai_score)>=9).length;const avg=k=>n? (rows.reduce((a,b)=>a+(Number(b[k])||0),0)/n).toFixed(2):"0.00";
document.getElementById("kpiRows").textContent=n;document.getElementById("kpiTop").textContent=aiHigh;document.getElementById("kpiTech").textContent=avg("technical");document.getElementById("kpiSent").textContent=avg("sentiment");}
function applySearch(){const q=document.getElementById("search").value.trim().toLowerCase();let rows=state.rows||[];if(q){rows=rows.filter(r=>(r.ticker||"").toLowerCase().includes(q)||(r.company||"").toLowerCase().includes(q));}
rows=rows.slice().sort((a,b)=>{const av=a[sortKey],bv=b[sortKey];const na=Number(av),nb=Number(bv);const va=isNaN(na)?(av||""):na,vb=isNaN(nb)?(bv||""):nb;return sortDir==="asc"?(va>vb?1:va<vb?-1:0):(va<vb?1:va>vb?-1:0);});render(rows);}
function exportCsv(){const keys=["rank","ticker","company","country","ai_score","change","fundamental","technical","sentiment","low_risk","volume_millions","industry","sector","as_of","universe"];const rows=(state.rows||[]).map(r=>({...r,as_of:state.as_of,universe:state.universe}));const lines=[keys.join(",")].concat(rows.map(r=>keys.map(k=>JSON.stringify(r[k]??"")).join(",")));const blob=new Blob([lines.join("\n")],{type:"text/csv;charset=utf-8"});const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download="stock-rankings.csv";a.click();}
document.getElementById("refreshBtn").addEventListener("click",loadData);
document.getElementById("search").addEventListener("input",applySearch);
document.getElementById("exportBtn").addEventListener("click",exportCsv);
document.getElementById("universe").addEventListener("change",loadData);
document.getElementById("thead").addEventListener("click",e=>{const k=e.target.dataset.k;if(!k)return;sortKey=k;sortDir=sortDir==="asc"?"desc":"asc";applySearch();});
loadData();setInterval(loadData, 15*60*1000);
</script>
</body>
</html>
EOF

# 8) README
cat > README.md << 'EOF'
Run: 1) cp .env.example .env  && set DANELFIN_API_KEY
2) npm i
3) npm run dev:api  # http://localhost: 3000
4) npm run dev:ui   # http://localhost: 5173
Keep .env out of git.
EOF

# 9) Finish by zipping everything
zip -r app.zip . >/dev/null
echo "Done. ZIP: app.zip"