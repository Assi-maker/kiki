# High-Frequency Opportunity Engine V2 – research (2026-09-29/30)

This is research and shadow only. Nothing here affects LIVE.

The change to make the risk caps observe-only is in `git stash`. It will not be activated until the user has seen this report.

## Data and method

- **Universe:** 165 USDT perpetuals.
  - This is every symbol that has ever been a candidate, plus today's top-80 by volume.
  - Data is 1m klines from 1–29/9: 6.86 M minute bars.
- **Open interest:** Binance 5m history, used as a proxy for 134 symbols. BingX has no OI history.
- **Funding:** BingX settled funding for 165 symbols.
- **Events:** evaluated on a 5-minute grid at T = bar close, using data strictly before T.
  - An event is a condition that became true at T and was false at T − 5 min.
  - All thresholds were set before any outcome was seen.
  - Lookahead, recursive stability and point-in-time OI/funding are covered by tests in `tests/crypto_trading/entry_research/test_event_engine.py`.
- **Outcome:**
  - Entry at the first 1m open ≥ T + 5 min.
  - Stop 2×ATR15, target 3×ATR15, horizon 6 h.
  - Costs: 0.10 % fees and 0.15 % stop slippage.
  - Result is R after costs.
  - Sensitivity checks: T + 23 min (today's AI path), and a wider barrier (see below).
- **Split:**
  - TRAIN before 13/9: 10.6 days.
  - VALID 13/9–26/9: 12.7 days.
  - TEST from 26/9: 3.9 days.
  - Rows near a period boundary are purged.
  - Each symbol/type/side is thinned to at most one row per 60 min.
- **Statistics:** 4 h-block clustered p-values, cluster bootstrap confidence intervals, and Benjamini–Hochberg correction over all 34 hypotheses.
- **Comparisons:**
  - BASELINE: an entry every hour on every symbol, unconditional.
  - OLD_CANDIDATES: today's candidate pool, 2840 candidates, scored with the same outcome.

## 1–3. Frequency: baseline, now, and where the volume disappears

| per day | before P0 (26/9 17:27–28/9 11:15) | now (29/9 from 12:43) |
|---|---|---|
| markets per cycle | ~840 | ~1240 |
| candidates | 354 | 942 |
| AI analyses | ~59 | ~120, then the daily cap stops it |
| CONFIRMED | 23 | 36 |
| **LIVE** | **21** | **6.6**, 3 on 29/9 in practice |
| AI cost per LIVE position | 0.32 USD | 2.91 USD (5.45 USD 28–29/9) |
| realized | −135.43 USDT / 37 trades | −27.18 USDT / 2 trades |

- Candidates, AI analyses and CONFIRMED have **risen**.
- The volume disappears almost entirely at the Safety Kernel:
  - fixed 1000 notional against the risk caps;
  - 6 of 8 were rejected, all on `GROUP_RISK_CAP` / `PORTFOLIO_RISK_CAP`;
  - 3 more were lost to the stale bug, which is fixed at abff4cb.
- Secondary limits:
  - the daily AI cap of 500 calls runs out in the afternoon;
  - AI analyses per cycle are capped at the number of free LIVE slots;
  - P1 rejects about half of what is analysed: 18 QA rejections, 5 with R:R < 1, 3 stale.
- The AI is paid **before** the kernel rejects. That is why the cost per LIVE position has risen 9–17× while the cost per CONFIRMED is unchanged (0.26–0.53 USD).

## 4–6. New signal types: frequency and OOS expectancy

There are 17 families, each tested LONG and SHORT, for 34 hypotheses in total.

- **Breakout / expansion:** BRK_4H, BRK_24H, EARLY_BRK (breakout before the move is over-extended), SQUEEZE_EXP (volatility expansion after compression).
- **Acceleration / change-point:** ACCEL (15m z > 3), CHANGE_POINT (mean of the last 30 min vs the 2 h before).
- **Volume + price:** VOL_PRICE.
- **Trend transition:** TREND_TRANS (SMA 5 h crossing SMA 12.5 h).
- **Continuation after pullback:** PULLBACK_CONT.
- **Liquidity / structure:** SWEEP_RECLAIM (a 24 h low taken out, then reclaimed).
- **Reversal:** CAPITULATION_REV.
- **Multi-timeframe:** MTF_BRK (5 h, 12.5 h and 24 h aligned).
- **Relative strength:** RS_LEADER (cross-sectional top/bottom 3 % on 1 h, not over-extended).
- **Funding:** FUNDING_SQUEEZE (extreme funding plus a breakout).
- **OI + price:** OI_PRICE (OI +3 %/h with price), OI_COVER (OI −3 %/h, i.e. short covering or long liquidation), OI_BUILD_BRK (OI +5 %/4 h plus a breakout).

TEST results (per day = across 165 symbols, before capacity limits). "Stop %" is the natural stop distance: p25 / median / p75.

| type:side | class | per day | TEST R | 95 % CI | hit rate | SL | MFE % | MAE % | stop % (p25/p50/p75) | TRAIN R | VALID R |
|---|---|---|---|---|---|---|---|---|---|---|---|
| FUNDING_SQUEEZE:SHORT | NOISE | 20 | +0.05 | [−0.25, 0.37] | 54 % | 43 % | 2.53 | −1.42 | 0.9/1.5/2.9 | −0.36 | −0.40 |
| RS_LEADER:SHORT | NOISE | 210 | −0.02 | [−0.15, 0.12] | 47 % | 44 % | 2.56 | −2.15 | 1.8/2.4/3.5 | −0.08 | −0.15 |
| OI_BUILD_BRK:LONG | NOISE | 100 | −0.03 | [−0.21, 0.11] | 42 % | 50 % | 2.57 | −2.18 | 1.8/2.3/3.3 | −0.05 | −0.09 |
| OI_PRICE:LONG | NOISE | 130 | −0.12 | [−0.29, 0.03] | 40 % | 54 % | 2.38 | −2.14 | 1.8/2.3/3.1 | −0.07 | −0.05 |
| SWEEP_RECLAIM:SHORT | NOISE | 198 | −0.14 | [−0.37, 0.10] | 44 % | 52 % | 1.75 | −1.50 | 1.2/1.6/2.3 | −0.23 | −0.30 |
| RS_LEADER:LONG | NOISE | 146 | −0.16 | [−0.30, −0.01] | 39 % | 53 % | 2.45 | −2.07 | 1.6/2.2/3.2 | −0.08 | −0.05 |
| FUNDING_SQUEEZE:LONG | NOISE | 12 | −0.16 | [−0.43, 0.13] | 40 % | 57 % | 2.62 | −2.82 | 1.5/2.4/4.0 | −0.18 | −0.23 |
| VOL_PRICE:SHORT | NOISE | 117 | −0.17 | [−0.50, 0.17] | 47 % | 47 % | 1.74 | −1.59 | 0.9/1.6/2.4 | −0.22 | −0.25 |
| ACCEL:SHORT | NOISE | 208 | −0.23 | [−0.81, 0.35] | 46 % | 50 % | 1.54 | −1.28 | 0.9/1.5/2.1 | −0.29 | −0.37 |
| OI_COVER:SHORT | NOISE | 149 | −0.23 | [−0.50, 0.12] | 39 % | 53 % | 2.21 | −2.10 | 1.8/2.3/3.1 | −0.20 | −0.23 |
| SWEEP_RECLAIM:LONG | NOISE | 259 | −0.26 | [−0.64, 0.10] | 41 % | 53 % | 1.42 | −1.37 | 1.1/1.7/2.1 | −0.15 | −0.22 |
| BRK_24H:SHORT | NOISE | 268 | −0.26 | [−0.78, 0.20] | 43 % | 50 % | 1.47 | −1.27 | 1.0/1.7/2.1 | −0.34 | −0.38 |
| BRK_24H:LONG | NOISE | 176 | −0.27 | [−0.45, −0.08] | 39 % | 56 % | 1.73 | −1.50 | 1.2/1.6/2.2 | −0.21 | −0.14 |
| CHANGE_POINT:SHORT | NOISE | 107 | −0.28 | [−0.65, 0.06] | 45 % | 48 % | 1.85 | −1.56 | 0.9/1.7/2.7 | −0.23 | −0.41 |
| MTF_BRK:LONG | NOISE | 264 | −0.33 | [−0.47, −0.20] | 37 % | 58 % | 1.54 | −1.38 | 1.0/1.5/2.0 | −0.28 | −0.14 |
| PULLBACK_CONT:SHORT | NOISE | 452 | −0.34 | [−0.64, −0.03] | 43 % | 49 % | 1.39 | −1.18 | 0.9/1.4/2.0 | −0.25 | −0.37 |
| CAPITULATION_REV:SHORT | NOISE | 9 | −0.34 | [−0.69, 0.03] | 35 % | 59 % | 2.38 | −2.69 | 1.3/2.7/5.0 | −0.21 | −0.20 |
| MTF_BRK:SHORT | NOISE | 365 | −0.37 | [−0.78, 0.02] | 40 % | 54 % | 1.40 | −1.23 | 0.9/1.6/2.0 | −0.33 | −0.36 |
| BRK_4H:LONG | NOISE | 579 | −0.37 | [−0.65, −0.07] | 38 % | 57 % | 1.41 | −1.25 | 1.0/1.4/1.9 | −0.36 | −0.19 |
| BRK_4H:SHORT | FAILURE_HYP. | 655 | −0.39 | [−0.72, −0.04] | 41 % | 52 % | 1.41 | −1.22 | 0.9/1.5/2.0 | −0.39 | −0.38 |
| PULLBACK_CONT:LONG | NOISE | 362 | −0.40 | [−0.61, −0.18] | 35 % | 56 % | 1.38 | −1.31 | 1.0/1.4/2.0 | −0.27 | −0.07 |
| TREND_TRANS:LONG | NOISE | 176 | −0.42 | [−0.85, −0.09] | 40 % | 51 % | 1.38 | −1.14 | 0.9/1.4/1.9 | −0.45 | −0.18 |
| ACCEL:LONG | NOISE | 169 | −0.47 | [−0.69, −0.23] | 34 % | 58 % | 1.70 | −1.54 | 1.0/1.7/2.4 | −0.25 | −0.29 |
| TREND_TRANS:SHORT | FAILURE_HYP. | 178 | −0.47 | [−0.81, −0.11] | 36 % | 52 % | 1.30 | −1.27 | 1.0/1.4/2.1 | −0.47 | −0.44 |
| CHANGE_POINT:LONG | NOISE | 94 | −0.49 | [−0.78, −0.26] | 34 % | 55 % | 1.68 | −1.59 | 0.9/1.7/2.6 | −0.24 | −0.30 |
| VOL_PRICE:LONG | NOISE | 75 | −0.54 | [−0.77, −0.25] | 29 % | 65 % | 1.65 | −1.79 | 0.8/1.6/2.6 | −0.20 | −0.22 |
| EARLY_BRK:SHORT | FAILURE_HYP. | 347 | −0.57 | [−1.00, −0.19] | 42 % | 51 % | 1.04 | −0.80 | 0.5/1.0/1.5 | −0.53 | −0.47 |
| EARLY_BRK:LONG | FAILURE_HYP. | 332 | −0.64 | [−0.96, −0.31] | 34 % | 62 % | 0.92 | −0.91 | 0.6/1.1/1.5 | −0.47 | −0.34 |
| SQUEEZE_EXP:LONG | FAILURE_HYP. | 141 | −0.65 | [−1.16, −0.23] | 40 % | 53 % | 1.38 | −1.21 | 0.9/1.4/1.8 | −0.31 | −0.45 |
| SQUEEZE_EXP:SHORT | FAILURE_HYP. | 152 | −0.74 | [−1.29, −0.26] | 38 % | 56 % | 1.40 | −1.19 | 0.8/1.3/1.8 | −0.36 | −0.43 |
| OI_COVER:LONG, OI_PRICE:SHORT, OI_BUILD_BRK:SHORT, CAPITULATION_REV:LONG | INSUFFICIENT_DATA | 1–7 | | | | | | | | | |

For comparison, same outcome:

| comparison | TRAIN R | VALID R | TEST R | hit rate TEST | stop % median |
|---|---|---|---|---|---|
| BASELINE LONG (random entry every hour) | −0.28 | −0.14 | −0.42 | 38 % | 1.48 |
| BASELINE SHORT | −0.29 | −0.37 | −0.30 | 43 % | 1.48 |
| OLD_CANDIDATES (today's pool) | +0.10 | −0.19 | −0.62 | 28 % | 1.60 |

**Classification:** 0 EDGE, 0 WEAK_EDGE, 0 HYPOTHESIS, 0 REGIME_DEPENDENT, 24 NOISE, 6 FAILURE_HYPOTHESIS, 4 INSUFFICIENT_DATA. After multiple-testing correction, every TEST q = 1.0.

- **No signal type is positive in TRAIN, VALID and TEST.**
  - The two positive TEST values (FUNDING_SQUEEZE:SHORT +0.05 and nearly RS_LEADER:SHORT) were negative in both TRAIN and VALID.
- **The random baseline is itself negative** (−0.14 to −0.42 R).
  - Costs (fees plus stop slippage, about 0.15–0.2 R per trade on a ~1.2–1.5 % stop) put every short-horizon strategy on this barrier below zero unless it has a real predictive edge.
  - No event type beats the baseline in all three periods.
- **The OI hypotheses are the "least bad"** (OI_BUILD_BRK:LONG −0.03, OI_PRICE:LONG −0.12). They are still not positive, and they have wide natural stops (median ~2.3 %).

## 7–9. Combined V2: frequency, OOS expectancy, false positives

The combined engine may only take signal types that are positive in **both** TRAIN and VALID and better than the baseline in VALID.

- **No type met that requirement.** The combined V2 therefore has **0 members**, and TEST gives 0 trades.
- That is the correct result under the protocol. Picking members by looking at TEST would be data snooping.
- 11 types met the veto criterion instead (negative in both TRAIN and VALID and below baseline). They are candidates for no-trade filters.

LIVE-like simulation **without** risk caps, the target setup (max 4, 1 per symbol, 100×10, TEST 26–29/9):

| stream | LIVE trades/day | mean R | 95 % CI | hit rate | false positives (R ≤ 0) | SL | USDT/day on 1000 notional |
|---|---|---|---|---|---|---|---|
| today's candidates (no AI filter) | 17.0 | −0.44 | [−0.75, −0.09] | 30 % | 70 % | 59 % | −83.9 |
| BRK_4H:LONG | 42.0 | −0.71 | [−0.97, −0.45] | 31 % | 69 % | 64 % | −244 |
| MTF_BRK:LONG | 37.9 | −0.27 | [−0.54, −0.02] | 39 % | 61 % | 54 % | −92 |
| RS_LEADER:LONG | 37.9 | −0.22 | [−0.39, −0.03] | 37 % | 63 % | 59 % | −164 |
| OI_BUILD_BRK:LONG | 34.0 | +0.05 | [−0.18, 0.27] | 44 % | 56 % | 49 % | +102 (VALID: negative) |
| FUNDING_SQUEEZE:SHORT | 13.1 | −0.00 | [−0.34, 0.32] | 51 % | 49 % | 47 % | +34 (TRAIN/VALID: negative) |

(All 34 types are in `data/entry_research/event_engine_nocap_primary.json`.)

- High frequency is **easy** to reach: 25–40 LIVE trades per day per signal type without caps, 17/day with today's candidates.
- Positive expectancy at that frequency is **not** something any tested signal type delivers.
- With today's pool and no caps, the TEST period gives about −84 USDT/day on ~389 equity. The observed LIVE result before P0 (−135 USDT over 37 AI-filtered trades) points the same way.

## 10. Regime robustness

TEST regimes, BTC 4 h up/down, market volatility high/low, breadth high/low:

- **No type is positive in all regimes.** No type is REGIME_DEPENDENT under the rule "positive in the same regime in TRAIN, VALID and TEST with n ≥ 20".
- **Low-volatility periods are the worst** for almost every type, from −0.6 to −1.9 R (for example SQUEEZE_EXP:LONG vol_lo −1.88, CHANGE_POINT:LONG −1.78). This is a no-trade regime candidate across all types.
- **High breadth** (more than half the market up over 4 h) was weakly positive for a few SHORT types in TEST (CHANGE_POINT:SHORT +0.06, VOL_PRICE:SHORT +0.00, OI_COVER:SHORT +0.08). That is not stable over the periods.

## Sensitivity: is the result an artifact of the tight exit geometry?

This was one pre-registered rerun of the same 34 events, with stop 4×ATR15, target 6×ATR15 and a 24 h horizon (`data/entry_research/event_engine_wide.json`).

- **Baseline flips sign by period.**
  - LONG baseline: TRAIN −0.04, VALID **+0.11**, TEST −0.32.
  - VALID (13–26/9) was a rising market. With wide stops, *every* long did well there.
- **Classification:** 0 EDGE, 0 WEAK_EDGE, 21 NOISE, 7 FAILURE_HYPOTHESIS, 2 REGIME_DEPENDENT (FUNDING_SQUEEZE:SHORT and OI_PRICE:LONG, neither positive in TEST), 4 INSUFFICIENT_DATA.
- **The combined V2 got 2 members** under the protocol (positive in TRAIN and VALID): PULLBACK_CONT:LONG (TRAIN +0.07, VALID +0.21) and OI_PRICE:LONG (TRAIN +0.00, VALID +0.21). Tested once on TEST:

| combined V2 (wide) | trades/day | mean R | 95 % CI | hit rate | SL |
|---|---|---|---|---|---|
| VALID, all signals (in-sample for selection) | 599 | **+0.20** | [0.04, 0.36] | 55 % | 39 % |
| **TEST, all signals (OOS)** | 406 | **−0.24** | [−0.44, −0.04] | 37 % | 57 % |
| TEST, LIVE-like without caps (max 4, 1/symbol) | 10.8 | −0.08 | [−0.48, 0.36] | 43 % | 52 % |
| TEST, today's candidates, LIVE-like without caps | 5.7 | −0.50 | [−0.95, 0.17] | 23 % | 73 % |

- The pattern is classic.
  - What looked good in VALID was the market's upward direction, not the signal. It flipped to significantly **negative** OOS.
  - LIVE-like without caps, the combined engine was less bad than today's candidates (−0.08 vs −0.50 R). But it is not positive, and the confidence intervals overlap widely: 42 vs 22 trades.
- Wider stops also lengthen the hold time. The max-4 rule then caps the frequency at ~11/day, and the natural stop grows to ~2.5–5 %.

## AI cost per actual LIVE position

- Before P0: **0.32 USD**. After P0: **2.9–5.5 USD**.
- The AI costs are the same per CONFIRMED. What changed is that the kernel rejected them afterwards.
- If the caps are removed, the cost per LIVE position returns to ~0.3 USD. So the credit problem is solved without a new signal engine.
- The losses per trade remain, though: −3.7 USDT per trade on average before P0.

## Conclusion

1. **The new signal engine did not find a signal type with positive OOS expectancy.**
   - 34 hypotheses over 165 markets and 27 days.
   - Events versus states, early detection, OI/funding, relative strength, structure and MTF are all included.
   - This confirms the earlier findings (entry edge research, edge lab, opportunity v2): on this outcome with 5-minute to 6-hour horizons, there is no measurable edge in the entry signals.
2. **High frequency without caps means about 17–40 trades per day with negative expectancy** in every tested stream. By the TEST period, that is roughly −80 USDT/day at today's size.
3. **What is solid:**
   - The credit problem is caused by the kernel rejecting *after* the AI.
   - Low-volatility regimes and 6 signal types (EARLY_BRK, SQUEEZE_EXP, TREND_TRANS:SHORT, BRK_4H:SHORT) are consistently worse than random entry. They are candidates for no-trade filters, in shadow first.
4. **Sensitivity (wide exits, 24 h):** the only candidates that passed TRAIN+VALID failed significantly OOS (−0.24 R). Their VALID advantage was the market's direction.
5. **Recommendation:** do not activate the caps-off change (stash) on the basis of this research. Nothing here shows that more trades would be profitable.
6. **The most promising next step is not more entry signals.** The recurring finding is that the result is decided by market direction and costs. Two things are worth testing next, in shadow:
   - a regime filter that stops long trading when the market is not rising (a no-trade regime);
   - the dominant cost: stop slippage and fees on ~1.4 % stops.

Reproduce:
- `python -m crypto_trading.entry_research.universe_fill`
- `python -m crypto_trading.entry_research.derivs_data`
- `python -m crypto_trading.entry_research.event_engine`

Data lives in `data/entry_research/` (gitignored): `event_engine*.json`, `derivs.db`, `klines.db`.
