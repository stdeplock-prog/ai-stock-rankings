# TradingView 1D Technical Ratings – Benchmark Sample
**Date collected:** 2026-05-07 ~13:22 UTC  
**Timeframe:** 1 Day (default selected on all pages)  
**Method:** Visited `/symbols/EXCHANGE-TICKER/technicals/` for each ticker; waited for data to load; read Summary gauge, Oscillator gauge, Moving Average gauge and numeric counts from page.

---

| ticker | tradingview_symbol_used | overall_1d | oscillator_1d | moving_average_1d | counts_if_visible (Summary S/N/B) | url | notes |
|--------|------------------------|------------|---------------|-------------------|-----------------------------------|-----|-------|
| GS | NYSE:GS | Neutral | Neutral | Neutral | S:1 / N:9 / B:16 (Osc S:1/N:8/B:2; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NYSE-GS/technicals/ | Goldman Sachs; data loaded OK |
| SMCI | NASDAQ:SMCI | Buy | Buy | Strong Buy | S:1/N:10/B:15 (Osc S:0/N:9/B:2; MA S:1/N:1/B:13) | https://www.tradingview.com/symbols/NASDAQ-SMCI/technicals/ | Super Micro Computer |
| XE | N/A | N/A | N/A | N/A | N/A | N/A | **Not found** on TradingView (tried NYSE:XE and NASDAQ:XE – both 404). Ticker XE does not map to a recognized US equity on TradingView; may be delisted or ambiguous. |
| MDB | NASDAQ:MDB | Neutral | Neutral | Buy | S:8/N:9/B:9 (Osc S:2/N:8/B:1; MA S:6/N:1/B:8) | https://www.tradingview.com/symbols/NASDAQ-MDB/technicals/ | MongoDB |
| ECG | NYSE:ECG | Buy | Neutral | Strong Buy | S:4/N:6/B:16 (Osc S:4/N:5/B:2; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NYSE-ECG/technicals/ | Everus Construction Group |
| LYV | NYSE:LYV | Neutral | Neutral | Strong Buy | S:0/N:10/B:16 (Osc S:0/N:9/B:2; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NYSE-LYV/technicals/ | Live Nation Entertainment; Overall label showed "Neutral" despite high buy counts — likely near threshold |
| TSM | NYSE:TSM | Strong Buy | Buy | Strong Buy | S:0/N:9/B:17 (Osc S:0/N:9/B:3; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NYSE-TSM/technicals/ | Taiwan Semiconductor (ADR) |
| JOBY | NYSE:JOBY | Buy | Buy | Buy | S:4/N:8/B:14 (Osc S:4/N:8/B:4; MA S:4/N:1/B:10) | https://www.tradingview.com/symbols/NYSE-JOBY/technicals/ | Joby Aviation |
| AVGO | NASDAQ:AVGO | Buy | Sell | Strong Buy | S:4/N:9/B:13 (Osc S:3/N:8/B:0; MA S:1/N:1/B:13) | https://www.tradingview.com/symbols/NASDAQ-AVGO/technicals/ | Broadcom; Oscillator shows Sell but MA is Strong Buy |
| DVA | NYSE:DVA | Neutral | Neutral | Neutral | S:0/N:9/B:17 (Osc S:0/N:8/B:3; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NYSE-DVA/technicals/ | DaVita; overall label "Neutral" despite buy-heavy counts |
| APLD | NASDAQ:APLD | Strong Buy | Buy | Strong Buy | S:0/N:9/B:17 (Osc S:0/N:8/B:3; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NASDAQ-APLD/technicals/ | Applied Digital Corporation |
| VIAV | NASDAQ:VIAV | Neutral | Neutral | Neutral | S:3/N:9/B:14 (Osc S:2/N:8/B:1; MA S:1/N:1/B:13) | https://www.tradingview.com/symbols/NASDAQ-VIAV/technicals/ | Viavi Solutions |
| CNM | NYSE:CNM | Buy | Neutral | Buy | S:6/N:10/B:10 (Osc S:1/N:9/B:1; MA S:5/N:1/B:9) | https://www.tradingview.com/symbols/NYSE-CNM/technicals/ | Core & Main |
| EWBC | NASDAQ:EWBC | Neutral | Neutral | Neutral | S:1/N:10/B:15 (Osc S:1/N:9/B:1; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NASDAQ-EWBC/technicals/ | East West Bancorp |
| RJF | NYSE:RJF | Strong Buy | Buy | Strong Buy | S:2/N:8/B:16 (Osc S:1/N:7/B:3; MA S:1/N:1/B:13) | https://www.tradingview.com/symbols/NYSE-RJF/technicals/ | Raymond James Financial |
| ADI | NASDAQ:ADI | Strong Buy | Buy | Strong Buy | S:0/N:9/B:17 (Osc S:0/N:8/B:3; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NASDAQ-ADI/technicals/ | Analog Devices |
| QBTS | NYSE:QBTS | Strong Buy | Buy | Strong Buy | S:0/N:9/B:17 (Osc S:0/N:8/B:3; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NYSE-QBTS/technicals/ | D-Wave Quantum |
| QCOM | NASDAQ:QCOM | Strong Buy | Buy | Strong Buy | S:1/N:9/B:16 (Osc S:1/N:8/B:2; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NASDAQ-QCOM/technicals/ | QUALCOMM |
| MSOS | AMEX:MSOS | Neutral | Neutral | Neutral | S:1/N:9/B:16 (Osc S:1/N:8/B:2; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/AMEX-MSOS/technicals/ | AdvisorShares Pure US Cannabis ETF; listed NYSEARCA/AMEX |
| FAF | NYSE:FAF | Strong Buy | Buy | Strong Buy | S:0/N:10/B:16 (Osc S:0/N:9/B:2; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NYSE-FAF/technicals/ | First American Corporation |
| BWA | NYSE:BWA | Strong Buy | Neutral | Strong Buy | S:1/N:9/B:16 (Osc S:1/N:8/B:2; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NYSE-BWA/technicals/ | BorgWarner |
| LUNMF | OTC:LUNMF | Neutral | Neutral | Neutral | S:3/N:8/B:15 (Osc S:1/N:7/B:3; MA S:2/N:1/B:12) | https://www.tradingview.com/symbols/OTC-LUNMF/technicals/ | Lundin Mining Corp. (OTC pink sheets); primary listing TSX:LUN |
| FSLR | NASDAQ:FSLR | Buy | Sell | Strong Buy | S:7/N:7/B:12 (Osc S:4/N:6/B:1; MA S:3/N:1/B:11) | https://www.tradingview.com/symbols/NASDAQ-FSLR/technicals/ | First Solar; mixed signal — Osc Sell, MA Strong Buy |
| DOCN | NYSE:DOCN | Strong Buy | Buy | Strong Buy | S:2/N:7/B:17 (Osc S:2/N:6/B:3; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NYSE-DOCN/technicals/ | DigitalOcean |
| EQIX | NASDAQ:EQIX | Buy | Neutral | Strong Buy | S:2/N:9/B:15 (Osc S:1/N:8/B:2; MA S:1/N:1/B:13) | https://www.tradingview.com/symbols/NASDAQ-EQIX/technicals/ | Equinix REIT |
| HST | NASDAQ:HST | Strong Buy | Buy | Strong Buy | S:0/N:9/B:17 (Osc S:0/N:8/B:3; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NASDAQ-HST/technicals/ | Host Hotels & Resorts REIT |
| CVNA | NYSE:CVNA | Buy | Neutral | Strong Buy | S:3/N:9/B:14 (Osc S:2/N:8/B:1; MA S:1/N:1/B:13) | https://www.tradingview.com/symbols/NYSE-CVNA/technicals/ | Carvana |
| LLYVA | NASDAQ:LLYVA | Strong Buy | Buy | Strong Buy | S:0/N:9/B:17 (Osc S:0/N:8/B:3; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NASDAQ-LLYVA/technicals/ | Liberty Live Holdings Series A |
| CACC | NASDAQ:CACC | Strong Buy | Buy | Strong Buy | S:0/N:9/B:17 (Osc S:0/N:8/B:3; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NASDAQ-CACC/technicals/ | Credit Acceptance Corporation |
| MLI | NYSE:MLI | Strong Buy | Buy | Strong Buy | S:0/N:9/B:17 (Osc S:0/N:8/B:3; MA S:0/N:1/B:14) | https://www.tradingview.com/symbols/NYSE-MLI/technicals/ | Mueller Industries |

---

## Notes
- All data captured on **1D timeframe** (default/selected).
- Counts format: S = Sell, N = Neutral, B = Buy (oscillators + moving averages combined for Summary total = 26 indicators).
- **XE**: Not found on TradingView on NYSE or NASDAQ. This ticker is not a recognized US-listed equity on TradingView as of data collection date.
- **LUNMF**: Trades OTC in the US; primary exchange is TSX (Toronto). Limited liquidity data.
- **MSOS**: ETF (AdvisorShares Pure US Cannabis ETF), listed on NYSE American (AMEX).
- The "Overall" rating label in TradingView's summary gauge does not always perfectly match the numeric count direction due to weighting — e.g., LYV shows "Neutral" on the label yet has high Buy counts.
- Data captured on May 6–7, 2026 trading session (pre-market hours for most tickers).
