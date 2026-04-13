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
