# External Benchmark and Pine Signal Meta-Analysis

## Executive summary

The first external benchmark pass suggests the internal model is directionally closest to TradingView technical ratings, moderately aligned with Fidelity ESS, and less aligned with E*TRADE/LSEG and MarketBeat analyst-style ratings. TradingView is the best first external source to operationalize because it maps directly to TECH and SWING. Fidelity is the best second source because its Equity Summary Score is a useful composite/fundamental disagreement detector. E*TRADE and MarketBeat are useful as analyst-consensus/outlier checks, not as direct replacements for the internal AI score.

The Pine inventory shows a consistent design intent: a gate-stacked momentum and call-option setup engine. The strongest portable signals are daily OHLCV-based gates: 5/8/13 SMA alignment, RSI 55-70 with rising slope, Bollinger midline/cool-off, 20-day return, relative volume, bar strength, MFI, MA50 slope, and near-20-day-high logic. These map naturally into a future Go/No-Go score, SWING enhancement, and Accumulation Signal Meter.

The main recommendation is to add two diagnostic layers before changing production scores:

| Recommendation | First use | Why |
|---|---|---|
| External Benchmark Review | Compare internal scores to TradingView, Fidelity, E*TRADE, Zacks, MarketBeat | Shows where the model agrees/disagrees with accessible outside ratings |
| Pine-derived Go/No-Go prototype | Add non-production diagnostics from portable Pine gates | Tests whether personal TradingView rules improve forward returns before changing rankings |

No score weights should be changed yet. The benchmark sample is useful, but it is still one small point-in-time sample. Treat it as calibration data, not proof.

## Data collected

The sample used the same 30 randomly selected tickers across platforms. TradingView collected visible 1D Technical, Oscillator, and Moving Average ratings for 29 of 30 tickers; XE was not found on TradingView ([TradingView GS technicals](https://www.tradingview.com/symbols/NYSE-GS/technicals/), [TradingView SMCI technicals](https://www.tradingview.com/symbols/NASDAQ-SMCI/technicals/)). E*TRADE collected LSEG consensus, Morgan Stanley ratings, TipRanks targets, MarketEdge where visible, and other research-provider fields for 28 of 30 tickers; XE and MSOS lacked standard stock-style LSEG coverage ([E*TRADE GS research](https://www.etrade.wallst.com/etrade-web/research?symbol=GS), [E*TRADE MDB research](https://www.etrade.wallst.com/etrade-web/research?symbol=MDB)). Fidelity collected Equity Summary Score values for 26 of 30 tickers; XE, APLD, MSOS, and LLYVA lacked ESS coverage ([Fidelity GS ratings](https://digital.fidelity.com/prgw/digital/research/quote/dashboard/ratings-sentiment?symbol=GS), [Fidelity AVGO ratings](https://digital.fidelity.com/prgw/digital/research/quote/dashboard/ratings-sentiment?symbol=AVGO)). Zacks collected public Zacks Rank, style scores, industry rank, and Earnings ESP fields for the 30-ticker sample where available ([Zacks GS](https://www.zacks.com/stock/quote/GS), [Zacks FAF](https://www.zacks.com/stock/quote/FAF)). MarketBeat collected public analyst-consensus, price-target, upside/downside, and MarketRank-style fields where visible ([MarketBeat GS](https://www.marketbeat.com/stocks/NYSE/GS/), [MarketBeat MDB](https://www.marketbeat.com/stocks/NASDAQ/MDB/)).

## Agreement summary

| External source | Internal comparison | Coverage | Agreement result | Initial read |
|---|---:|---:|---:|---|
| TradingView 1D Technical | Internal TECH | 29/30 | 62.1% directional agreement | Best first operational benchmark |
| Fidelity ESS | Internal AI | 26/30 | 57.7% directional agreement | Strong disagreement detector |
| E*TRADE/LSEG | Internal AI | 28/30 | 42.9% directional agreement | Useful analyst/outlier cross-check |
| Zacks Rank/VGM | Internal AI/FUND/TECH | 27-ish equity ranks usable; ETFs/NA excluded | Not yet normalized | Useful for earnings-revision/style-score overlay |
| MarketBeat consensus/PT | Internal SENT/AI | 28 stock pages accessible | Not yet normalized | Useful for analyst consensus and target divergence |

The most actionable observation is that TradingView and internal TECH agree more often than fundamental/analyst-oriented sources agree with internal AI. That is expected because the internal AI score still blends technical, fundamental, sentiment, and risk fields, while Fidelity, E*TRADE, Zacks, and MarketBeat emphasize analyst, revision, valuation, or provider-composite views.

## Platform-by-platform read

### TradingView

TradingView is the cleanest match for the technical side of the dashboard. It captured Overall, Oscillator, and Moving Average ratings on the 1D timeframe for the sample ([TradingView AVGO technicals](https://www.tradingview.com/symbols/NASDAQ-AVGO/technicals/), [TradingView FSLR technicals](https://www.tradingview.com/symbols/NASDAQ-FSLR/technicals/)).

| Metric | Result |
|---|---:|
| Found | 29/30 |
| Strong Buy | 13 |
| Buy | 8 |
| Neutral | 8 |
| Not found | 1 |
| Direction agreement vs internal TECH | 62.1% |
| Average TV minus internal TECH gap | +0.30 on 1-5 scale |

TradingView is best used to benchmark TECH and SWING, not FUND. The main disagreement patterns are useful: some names such as TSM and RJF scored stronger on TradingView than internal TECH, while LYV and DVA were more neutral on TradingView despite higher internal TECH.

### Fidelity

Fidelity ESS is the most useful composite/fundamental disagreement source. The ESS scale is visible as a bullish/bearish color-coded rating, which makes it useful for dashboard-style flagging ([Fidelity TSM ratings](https://digital.fidelity.com/prgw/digital/research/quote/dashboard/ratings-sentiment?symbol=TSM), [Fidelity JOBY ratings](https://digital.fidelity.com/prgw/digital/research/quote/dashboard/ratings-sentiment?symbol=JOBY)).

| Metric | Result |
|---|---:|
| ESS coverage | 26/30 |
| No ESS | XE, APLD, MSOS, LLYVA |
| Direction agreement vs internal AI | 57.7% |
| Average Fidelity ESS minus internal AI gap | -0.64 on 1-5 scale |
| Median gap | -0.05 |

Fidelity’s biggest disagreements were mostly bearish relative to the internal model: LYV, MDB, ECG, JOBY, GS, SMCI, BWA, CACC, and CVNA. This suggests Fidelity should be treated as a “risk/disagreement overlay” rather than a direct ranking input.

### E*TRADE

E*TRADE is useful because it exposes multiple research-provider views on the same page: LSEG, Morgan Stanley, TipRanks, Argus, MarketEdge, and SmartConsensus where available ([E*TRADE QCOM research](https://www.etrade.wallst.com/etrade-web/research?symbol=QCOM), [E*TRADE EQIX research](https://www.etrade.wallst.com/etrade-web/research?symbol=EQIX)).

| Metric | Result |
|---|---:|
| LSEG coverage | 28/30 |
| No LSEG | XE, MSOS |
| Direction agreement vs internal AI | 42.9% |
| Average LSEG bullish score minus internal AI gap | +0.27 on 1-5 scale |

E*TRADE/LSEG had lower agreement with internal AI than Fidelity. This is still valuable because it identifies analyst-side outliers, especially where price targets or Morgan Stanley views conflict with internal technical strength.

### Zacks

Zacks is useful for earnings-revision, VGM, and style-score context. Public pages yielded Zacks Rank, Value/Growth/Momentum scores, VGM score, industry rank, and Earnings ESP for most sample tickers ([Zacks MDB](https://www.zacks.com/stock/quote/MDB), [Zacks AVGO](https://www.zacks.com/stock/quote/AVGO)).

Important sample observations:

| Ticker | Zacks signal | Internal implication |
|---|---|---|
| FAF | Rank 1-Strong Buy | Strong external confirmation candidate |
| AVGO, DVA, VIAV, ADI, EQIX, CACC | Rank 2-Buy | Positive external confirmation, context-dependent |
| MDB, LYV | Rank 5-Strong Sell | Strong disagreement cases |
| ECG, JOBY, CNM, BWA | Rank 4-Sell | Review before trusting high internal score |
| MSOS | ETF-N/A | Do not compare with stock-style rank |

Zacks should not be treated as a technical benchmark. It is better as a FUND/SENT/Earnings-revision overlay.

### MarketBeat

MarketBeat is useful for public analyst-consensus and price-target divergence. It loaded all pages without blocking and surfaced useful target-price anomalies ([MarketBeat CVNA](https://www.marketbeat.com/stocks/NYSE/CVNA/), [MarketBeat VIAV](https://www.marketbeat.com/stocks/NASDAQ/VIAV/)).

Important sample observations:

| Ticker | MarketBeat signal | Internal implication |
|---|---|---|
| CVNA | Moderate Buy label but price target far below current price | Treat as a major analyst-target divergence |
| VIAV | Moderate Buy but target below current price | Positive label conflicts with downside target |
| QBTS | Moderate Buy with large upside | External upside confirmation |
| JOBY, LLYVA | Reduce | Bearish review candidates |
| GS, SMCI, DVA, RJF, QCOM | Hold | Use as neutral/confirmation check |

MarketBeat is best for analyst-consensus and price-target sanity checks, not for TECH/SWING validation.

## Pine script intent analysis

The Pine scripts are an evolving go/no-go engine for momentum trades and daily call-option setups. The scripts repeatedly use a gate-stacking architecture: a signal only fires when multiple trend, momentum, volatility, volume, and risk gates pass together.

| Pine signal family | Repeated purpose | Best dashboard mapping |
|---|---|---|
| 5/8/13 SMA stack | Short-term trend alignment | TECH |
| RSI 55-70 with slope | Momentum in a strong but not exhausted zone | TECH / SWING |
| Bollinger midline and cool-off | Trend support and overextension control | TECH / RISK |
| 20-day return threshold | Momentum qualification | TECH |
| Relative volume | Participation/confirmation | SWING / Accumulation |
| MFI vs MFI average | Money-flow confirmation | SWING / Accumulation |
| Close above prior high | Bar strength | RISK / SWING |
| MA50 rising | Intermediate trend anchor | SWING |
| Open Drift/VWAP | Intraday setup detection | Market Open Scan / later intraday module |

The strongest near-term implementation is not a full Pine port. The best first step is a diagnostic Go/No-Go score computed from daily OHLCV and shown in reports, not production ranking.

## Recommended incorporation roadmap

### Phase 1: External benchmark report

Build an External Benchmark Review report that stores and compares the sampled platform data.

| Output | Purpose |
|---|---|
| external_benchmark_samples.json | Append-only raw captures from TradingView, Fidelity, E*TRADE, Zacks, MarketBeat |
| external_benchmark_review.json | Normalized source comparison metrics |
| external-benchmark-review.html | Agreement rates, disagreements, and review queue |
| disagreement_queue.json | Tickers where internal and external signals diverge sharply |

Initial report should include:

- TradingView vs TECH/SWING agreement
- Fidelity ESS vs AI/FUND/SENT agreement
- E*TRADE/LSEG vs AI/FUND agreement
- Zacks Rank/VGM vs FUND/momentum context
- MarketBeat consensus/target divergence vs SENT/upside context

### Phase 2: Disagreement queue

Prioritize manual review of tickers with strong multi-source disagreement.

| Ticker | Why it belongs in the first queue |
|---|---|
| MDB | Fidelity and Zacks bearish, TradingView neutral, E*TRADE/MarketBeat more positive |
| LYV | Fidelity and Zacks bearish while internal TECH is strong |
| ECG | Fidelity/Zacks bearish and MarketBeat target downside |
| JOBY | Fidelity very bearish, Zacks sell, MarketBeat reduce, but TradingView buy |
| GS | Fidelity bearish, MarketBeat/E*TRADE more moderate |
| CVNA | MarketBeat target sharply below current price despite Moderate Buy label |
| QCOM | Fidelity very bullish but E*TRADE/Morgan Stanley/MarketBeat target context is mixed |
| CACC | Internal high rank but Fidelity neutral and E*TRADE/MarketBeat targets not compelling |

### Phase 3: Pine-derived diagnostics

Add a non-production Pine-derived diagnostics module.

| Signal | First implementation |
|---|---|
| Go/No-Go daily score | Count of daily OHLCV gates passed |
| Accumulation meter | Relative volume + MFI + bar strength + near-high + RSI zone |
| Call Stacker proxy | MA50 trend + near 20-day high + relative volume + MFI |
| Risk blockers | Overextended Bollinger, no-trade-zone, earnings proximity |

Recommended first formula:

| Component | Weight |
|---|---:|
| 5/8/13 SMA alignment | 1 |
| RSI in zone and rising | 1 |
| Close above 20-day SMA / Bollinger midline | 1 |
| 20-day return above threshold | 1 |
| Relative volume | 1 |
| Bar strength | 1 |
| MA50 rising | 1 |
| MFI confirmation | 1 |
| Near 20-day high | 1 |
| Not overextended / not in no-trade zone | 1 |

Show this as a diagnostic score first. Do not add it to AI/TECH/SWING until it has forward-return evidence.

## What to add to the site first

| Priority | Site/report addition | Rationale |
|---:|---|---|
| 1 | External Benchmark Review report | Converts one-off captures into a repeatable calibration workflow |
| 2 | Disagreement Queue report | Gives the user a concrete review list instead of raw ratings |
| 3 | Pine Go/No-Go diagnostic report | Tests personal Pine logic against current rankings |
| 4 | Accumulation Signal Meter prototype | Fits existing backlog and Pine intent |
| 5 | Optional dashboard badges | Only after signal stability is proven |

## Final recommendation

The next build should be External Benchmark Review. It should ingest the captured TradingView, Fidelity, E*TRADE, Zacks, and MarketBeat samples, normalize ratings, calculate agreement/disagreement, and publish a review queue. The second build should be a Pine-derived Go/No-Go diagnostic score using daily OHLCV only. Production score weights should remain unchanged until benchmark samples and forward-performance snapshots show persistent evidence over multiple weeks.

This is research and analysis only, not personalized financial advice. Consult a qualified financial advisor before making investment decisions.
