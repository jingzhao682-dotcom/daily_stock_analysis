# 2026-10-19 US ETF close report QA — P0 pricing/MA regression

**Reproduction/report date:** Beijing 2026-10-20, underlying US regular trading session **2026-10-19**.  
**Baseline:** 2026-10-20 morning email pair, `04:55 UTC` (stock dashboard + market recap).  
**Replayed:** 2026-10-20 `07:08 UTC` new email pair from [run 39384115402](https://github.com/jingzhao682-dotcom/daily_stock_analysis/actions/runs/39384115402).  
**Fix:** [PR #4](https://github.com/jingzhao682-dotcom/daily_stock_analysis/pull/4), merge commit `058526c5`.  
**Tests:** [CI 39383091378](https://github.com/jingzhao682-dotcom/daily_stock_analysis/actions/runs/39383091378) success (3 backend shards, docker build and backend gate; 8436 pytest passes = 2503 + 2607 + 3326, not including subtests).  
**Scope:** observation-only reporting; no IBKR trading/execution enabled.

## Before vs after
Prices in USD. Daily return is the prior-session regular close comparison and SMA5 is computed from five unique trading-day unadjusted closes.

| Ticker | Close both | Old prior | Fixed prior | Old daily % | Fixed daily % | Old MA5 | Fixed MA5 |
|---|---:|---:|---:|---:|---:|---:|---:|
| SGOV | 100.59 | 100.58 | 100.59 | +0.01% | 0.00% | 100.571998 | 100.557999 |
| SPYM | 90.23 | 89.66 | 89.83 | +0.64% | +0.45% | 90.340 | 90.606001 |
| GLD | 379.69 | 381.40 | 382.01 | -0.45% | -0.61% | 380.020 | 380.410 |
| NVDA | 217.09 | 213.75 | 214.13 | +1.56% | +1.38% | 217.367999 | 219.654001 |
| SPCX | 147.96 | 145.47 | 146.03 | +1.71% | +1.32% | 148.550 | 150.260 |
| SOXQ | 89.06 | 87.86 | 87.64 | +1.37% | +1.62% | 89.769998 | 91.160 |
| QQQM | 295.78 | 292.07 | 292.26 | +1.27% | +1.20% | 296.750 | 298.824005 |
| VXUS | 83.29 | 83.30 | 83.43 | -0.01% | -0.17% | 83.486 | 83.736 |
| DRAM | 48.33 | 46.27 | 46.45 | +4.45% | +4.05% | 48.516 | 49.420 |
| SLV | 51.54 | 51.51 | 51.51 | +0.06% | +0.06% | 51.346 | 51.736 |

## Verification
- All ten fixed reports show `data_date: 2026-10-19` and `realtime:yfinance` instead of `N/A` and `realtime:fallback`.
- Of the 10 old daily returns, 9 were wrong under the same-close regular-session basis; all 10 fixed returns reconcile with matching prior closes.
- Independent checks: [SPYM 10/19 $90.23 and 10/16 $89.83](https://www.financecharts.com/etfs/SPYM/summary/price); [QQQM $295.78 vs $292.26](https://stockanalysis.com/etf/qqqm/history/); [SOXQ $89.06 vs $87.64](https://www.investing.com/etfs/soxq-historical-data); [DRAM $48.33 vs $46.45 and five daily closes](https://finance.yahoo.com/quote/DRAM/history/).
- Independently recomputed five-day SMA: SPYM 90.606, QQQM 298.824, SOXQ 91.164, DRAM 49.418 (small differences reflect full-precision data and display rounding).
- Replayed market recap still reports S&P 500 7690.73, NASDAQ Composite 26430.89, Dow 51966.03, unchanged from baseline. **VIX 18.44 was present in baseline but omitted in replay**; treat as missing coverage, not a verified zero.

## Remaining P1 blockers for independent investment decisions
1. News: run logs clearly state no search engine configured; `暂无新闻证据` persists for ten instruments. Claims about catalysts and sentiment are not independently backed.
2. Quote time: ten instruments still have `行情时点：N/A`. The date is now correct, but the vendor timestamp is not available. Show “10/19 US close” rather than implying a live quote.
3. Volume interpretation: `volume_ratio=None` and `turnover_rate=None` for Yahoo quotes; language like “平量/放量” must only appear when independently computed historic share-volume comparison exists.
4. Market completeness: VIX silently missing from new market recap. Failed/partial index series should be exposed as missing with reason rather than simply omitted.
5. AI 0–100 scores changed after fixing market data (e.g., DRAM 47→35), but no grounded news evidence; do not present AI scores as verified backtested probabilities.

**Acceptance decision:** P0 verified for historical US close/MA calculations; **full AI investment-research standard not yet met** due to P1 evidence and coverage gaps. Do not enable autonomous trade execution.

## Replay lifecycle
One-time `push` trigger was removed after the successful replay. Normal scheduled analysis remains Beijing Tue–Sat 08:15.
