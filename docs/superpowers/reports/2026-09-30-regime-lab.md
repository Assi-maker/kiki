# Regime lab – 14 months, signal type × regime (2026-09-30)

## Result

- **No filter was approved.**
  - The selection was frozen before any TEST or HOLDOUT number was computed (sha256 `1b2b48aa725ab1b4956002db8f9cb2ea460955c59ca194749446fa37c47b808c`).
  - 0 of 710 signal/regime combinations passed TRAIN, where Benjamini-Hochberg q < 0.10 was required. So nothing went on to VALID, TEST or HOLDOUT.
  - The same holds for both exit geometries.
- **No LIVE strategy is changed.**

## Data and method

- **Data:** Binance USD-M public archive (`archive_fill.py`).
  - 135 symbols, 109 of them with the full window.
  - 15.8 M 5m bars, 15.7 M OI snapshots and 257k funding settlements, 2025-08-01 → 2026-09-29.
- **Signals:** the 17 event families × LONG/SHORT from `event_engine`, plus a BASELINE (every symbol, both sides, every 8 h).
  - "ANY" pools all event types, thinned to one per symbol/side/hour.
- **Regimes, point-in-time, thresholds fixed in advance:**
  - mkt_trend: bull, bear, side (BTC 7-day return ±4 %).
  - mkt_vol: BTC realised vol vs its 30-day median.
  - breadth: share of symbols with a positive 4 h return.
  - mkt_funding and sym_funding: neg, base, hot.
  - mkt_oi and sym_oi: 24 h change.
  - sym_vol: the symbol's ATR vs its 30-day median.
- **Outcome (primary, G_WIDE):**
  - Stop 4 × ATR15, target 6 × ATR15, 24 h. Entry at the first 5m open ≥ T + 5 min. Stop first within a bar.
  - Costs: 0.10 % fees, 0.15 % stop slippage and **the real funding** of every settlement during the holding period.
- **Sensitivity (G_LEGACY):** 2/3 × ATR15, 6 h.
- **Periods (purged):**
  - TRAIN 2025-08 → 2026-01
  - VALID 2026-02 → 04
  - TEST 2026-05 → 07
  - HOLDOUT 2026-08 → 09
- **Approval (pre-registered):**
  - Positive mean R after all costs, net USDT/day > 0, and ≥ 3 executed trades/day under the LIVE portfolio rules (max 4, 1 per symbol, no % caps).
  - This must hold on both TEST (p < 0.05) and HOLDOUT.
  - "Loses less" does not pass.
- **Code and tests:**
  - Code: `crypto_trading/entry_research/regime_lab.py`.
  - Tests: `tests/crypto_trading/entry_research/test_regime_lab.py` (point-in-time, costs, purge, portfolio rules, and that TEST/HOLDOUT never affect the selection).

## Baseline: without skill, every period is negative (G_WIDE, mean R per trade, signals/day)

| | TRAIN | VALID | TEST | HOLDOUT |
|---|---|---|---|---|
| BASELINE LONG | −0.135 | −0.108 | −0.138 | −0.018 |
| BASELINE SHORT | −0.041 | −0.044 | −0.054 | −0.197 |
| ANY LONG (~1000/d) | −0.114 | −0.086 | −0.132 | −0.006 |
| ANY SHORT (~1050/d) | −0.051 | −0.051 | −0.073 | −0.193 |

- G_LEGACY (tight stop) is worse everywhere: −0.14 to −0.26 R.
- Costs weigh relatively more on short stops.

## Per signal type (G_WIDE, all regimes, mean R)

| Signal | TRAIN | VALID | TEST | HOLDOUT | signals/day |
|---|---|---|---|---|---|
| ACCEL L / S | −0.122 / −0.048 | −0.012 / −0.102 | −0.154 / −0.062 | +0.002 / −0.202 | 140 / 130 |
| BRK_24H L / S | −0.120 / −0.121 | −0.092 / −0.130 | −0.135 / −0.120 | +0.112 / −0.205 | 150 / 150 |
| BRK_4H L / S | −0.125 / −0.068 | −0.102 / −0.051 | −0.170 / −0.098 | −0.010 / −0.206 | 450 / 450 |
| CAPITULATION_REV L / S | −0.238 / −0.046 | +0.176 / −0.085 | −0.168 / −0.079 | +0.137 / −0.177 | 12 / 18 |
| CHANGE_POINT L / S | −0.106 / +0.027 | −0.006 / −0.093 | −0.117 / −0.061 | −0.014 / −0.237 | 90 / 90 |
| EARLY_BRK L / S | −0.146 / −0.064 | −0.128 / −0.046 | −0.199 / −0.114 | −0.125 / −0.225 | 270 / 270 |
| FUNDING_SQUEEZE L / S | −0.052 / −0.049 | −0.107 / +0.076 | −0.049 / −0.089 | −0.109 / −0.038 | 5–29 / 1–4 |
| MTF_BRK L / S | −0.130 / −0.104 | −0.105 / −0.084 | −0.148 / −0.093 | +0.078 / −0.206 | 210 / 225 |
| OI_BUILD_BRK L | −0.050 | −0.035 | −0.092 | +0.098 | 80 |
| OI_COVER S | −0.024 | −0.077 | −0.053 | −0.184 | 112 |
| OI_PRICE L | −0.041 | −0.012 | −0.085 | +0.079 | 120 |
| PULLBACK_CONT L / S | −0.138 / −0.067 | −0.093 / −0.065 | −0.142 / −0.060 | +0.037 / −0.148 | 330 / 350 |
| SQUEEZE_EXP L / S | −0.079 / −0.088 | −0.115 / −0.024 | −0.080 / −0.156 | −0.010 / −0.247 | 125 / 125 |
| SWEEP_RECLAIM L / S | −0.053 / −0.023 | −0.053 / −0.055 | −0.064 / −0.042 | −0.029 / −0.241 | 170 / 170 |
| TREND_TRANS L / S | −0.143 / −0.025 | −0.066 / −0.018 | −0.137 / −0.110 | −0.109 / −0.195 | 140 / 140 |
| VOL_PRICE L / S | −0.099 / −0.040 | −0.003 / −0.078 | −0.125 / −0.087 | +0.010 / −0.164 | 150 / 180 |

- In TEST, every signal type is negative.
- HOLDOUT (Aug–Sep 2026) is positive for several LONG types, but not for the baseline's SHORT side. That is market direction, not signal.

## Per regime (all signals pooled, G_WIDE, mean R)

| Regime | LONG TR / VA / TE / HO | SHORT TR / VA / TE / HO |
|---|---|---|
| mkt_trend=bull | −0.224 / −0.086 / −0.101 / +0.121 | +0.014 / −0.065 / −0.123 / −0.267 |
| mkt_trend=bear | −0.112 / −0.092 / −0.204 / +0.182 | −0.069 / −0.049 / −0.001 / −0.597 |
| mkt_trend=side | −0.117 / −0.084 / −0.106 / −0.066 | −0.040 / −0.044 / −0.095 / −0.147 |
| mkt_vol=hi | −0.134 / −0.036 / −0.159 / +0.140 | −0.023 / −0.075 / +0.023 / −0.259 |
| mkt_vol=lo | −0.140 / −0.119 / −0.111 / −0.135 | −0.042 / −0.035 / −0.147 / −0.139 |
| mkt_funding=neg | −0.025 / +0.036 / (2/d) / – | −0.203 / −0.107 / – / – |
| mkt_oi=down | +0.097 / +0.007 / −0.421 / +0.385 | −0.299 / −0.096 / +0.410 / −0.636 |
| mkt_oi=up | −0.244 / −0.338 / +0.019 / +0.366 | +0.094 / +0.193 / −0.093 / −0.378 |
| sym_oi=up | −0.028 / −0.070 / −0.053 / +0.161 | −0.044 / −0.009 / −0.089 / −0.189 |
| sym_vol=hi | −0.119 / −0.065 / −0.139 / +0.095 | −0.023 / −0.025 / −0.010 / −0.217 |

- **Key pattern:** regimes flip sign between periods.
  - Long in a "bull" market was *worse* than long in general in TRAIN.
  - The OI regimes swing between −0.6 and +0.4 R from period to period.
- There is no regime where long or short is positive in all four periods.

## Why nothing passed TRAIN

- **Positive candidates were few:** of 669 TRAIN combinations with n ≥ 100, 98 had a positive mean. The best raw p was 0.004 (CHANGE_POINT SHORT in bull), and after correction for 710 tests nothing is significant.
- **The ten best TRAIN combinations all failed in TEST:** −0.05 to −0.22 R. Examples:
  - CHANGE_POINT SHORT in bull: TRAIN +0.27 → TEST −0.14.
  - SWEEP_RECLAIM SHORT with mkt_oi up: +0.15 / +0.19 in TRAIN/VALID → TEST −0.09.
- So the protocol correctly stopped them.

## Descriptive only, NOT a validated result

Three combinations (out of 752) are positive in all four periods. They are OI_PRICE, VOL_PRICE and ACCEL, all LONG with **symbol OI up > 10 % in 24 h**:

| | TRAIN | VALID | TEST | HOLDOUT | signals/day |
|---|---|---|---|---|---|
| OI_PRICE L, sym_oi=up | +0.013 | +0.002 | +0.003 | +0.101 | 32–50 |
| VOL_PRICE L, sym_oi=up | +0.013 | +0.004 | +0.029 | +0.129 | 21–32 |
| ACCEL L, sym_oi=up | +0.004 | +0.017 | +0.013 | +0.111 | 19–31 |

- **Win rate and path:** win rate 42–48 %, MFE ~0.9–1.0 R, MAE ~−0.75 R.
- **Why this is not an approved filter:**
  - TRAIN to TEST is ≈ 0 R (p 0.17–0.48).
  - Only HOLDOUT is clearly positive, and that is the same up-market that lifts all LONG signals in HOLDOUT.
  - They were found by looking at all four periods. That is exactly the data snooping the protocol forbids: among 752 combinations, a handful of chance "survivors" is expected.
- **At most a hypothesis:** it could be tested forward in shadow on new data (Oct 2026 →). It must not be activated on this basis.

## Conclusion

1. The fourth independent study, and the first over 14 months and several market regimes, finds **no signal type and no regime with positive OOS expectancy after costs**.
2. The earlier studies were: entry edge, edge lab, and the HF engine with 34 hypotheses.
3. The random baseline is negative in every period, so costs and funding set the barrier.
4. What differs between periods is market direction. No regime label captures it in advance: bull/bear/side, vol, funding and OI all flip sign.
5. **Recommendation:** keep today's LIVE unchanged until something passes the protocol. No filter has earned activation. Rather than building more entry variants on the same price and volume information, the next thing to test would be a genuinely new source of information or a different exit/holding model. That is the user's call.

## Known limits

- **Survivorship:** the universe is today's listed symbols.
- **Binance as proxy for BingX:** prices and funding come from Binance.
- **No AI step in the replay:** the AI step cannot be replayed over 14 months.
- **Intrabar order unknown:** 5m bars with stop first is conservative.
- **"net USDT/day" and "max DD" in the CSV:** these apply to the WHOLE signal stream, with every signal at 1000 notional and overlapping. The "live_*" columns show the LIVE portfolio rules.

## Reproduce

- `python -m crypto_trading.entry_research.archive_fill`: resumable, WAL.
- `python -m crypto_trading.entry_research.regime_lab`: ~30 min with 4 processes.
- Every combination × period × geometry: `2026-09-30-regime-lab-cells.csv` (5930 rows).
- Frozen selection: `data/entry_research/regime_lab_selection.json`.
