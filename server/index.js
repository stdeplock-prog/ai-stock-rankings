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
