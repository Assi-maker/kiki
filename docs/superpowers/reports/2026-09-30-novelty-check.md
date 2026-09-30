# Novelty check – taker flow and order book vs existing features (2026-09-30)

## Result

- **No new feature passed.** Both taker flow and order-book depth are dropped under the pre-registered criteria.
- **No event lab was built.**
- **TEST and HOLDOUT were never loaded:** `novelty.py` reads only TRAIN and VALID.
- **LIVE is unchanged.**

## Method (fixed before the results; `crypto_trading/entry_research/novelty.py`)

- **Grid:** every symbol at every full hour, point-in-time.
- **Target:** the log return from the first open ≥ T + 5 min (the LIVE latency) to 1 h and 4 h later.
- **Control set (existing features):** ret12, ret48, ret288, vz, z3, z12, atr_l, funding, oi1h, oi4h.
- **Partial IC:** the rank correlation after BOTH the feature and the target have been regressed on the control set. This is the information the existing features do not already carry.
- **Clustered p:** 4 h blocks.
- **Magnitude:** mean target of the top decile minus the bottom decile. The cut-offs are set on TRAIN and compared with the round-trip cost of ~0.15 %.
- **PASS requires all of:**
  - TRAIN |partial IC| ≥ 0.02 with p < 0.01;
  - VALID the same sign and ≥ 0.01;
  - redundancy < 0.7;
  - a decile spread ≥ 0.15 % in both periods.

## Taker flow

- **Data:** the 5m klines' taker-buy volume, 135 symbols.
- **Sample:** TRAIN n = 526k, VALID n = 277k.

| Feature | TRAIN partial IC 1h / 4h | VALID partial IC 1h / 4h | Decile spread TRAIN 1h / 4h | Redundancy |
|---|---|---|---|---|
| TI_5m (imbalance last 5m) | −0.002 / +0.002 | −0.005 / +0.002 | −0.00 % / +0.05 % | 0.26 |
| TI_1h | −0.004 / +0.004 | −0.004 / −0.002 | +0.01 % / +0.02 % | 0.49 |
| dTI (change) | +0.004 / −0.000 | +0.004 / +0.002 | +0.01 % / +0.01 % | 0.37 |
| accTI (acceleration) | +0.011 / +0.001 | +0.007 / +0.004 | +0.03 % / +0.01 % | 0.21 |
| TIz (vs normal activity) | −0.005 / +0.004 | −0.004 / −0.002 | +0.01 % / +0.01 % | 0.50 |

- The information is ≈ 0 beyond price, volume, funding and OI.
- The spreads are a factor of 3–50 below the cost.

## Order book

- **Data:** archive bookDepth for the 30 most liquid symbols, every 3rd day.
- **Sample:** TRAIN n ≈ 41k, VALID n ≈ 21k.

| Feature | TRAIN partial IC 1h / 4h | VALID partial IC 1h / 4h | Decile spread TRAIN 1h / 4h | VALID 1h / 4h |
|---|---|---|---|---|
| IMB1 (imbalance ±1 %) | −0.009 / −0.010 (p 0.16 / 0.23) | −0.017 / −0.009 | −0.10 % / −0.24 % | −0.07 % / −0.11 % |
| dIMB1 (15 min change) | +0.006 / +0.007 | −0.002 / +0.009 | +0.05 % / +0.22 % | +0.03 % / +0.01 % |
| DEPTHr (depth vs 7 days) | +0.011 / +0.004 | +0.024 / +0.017 | +0.03 % / +0.10 % | +0.04 % / −0.10 % |
| IMB02 (±0.2 %; only from ~2026-01, TRAIN n 3.5k) | −0.014 / −0.001 | −0.019 / −0.014 | −0.01 % / −0.14 % | −0.02 % / −0.07 % |

- **Closest to anything is IMB1 at 4 h:** −0.24 % in TRAIN, above the cost.
  - The partial IC is only −0.010 (p 0.23), so the effect is not significant.
  - VALID drops to −0.11 %, below the cost.
  - It does not pass.
- **Note on direction:** a positive imbalance (more bids) is followed by a slightly *lower* return. That is the reverse of the naive "buying pressure" story.

## Why this is structurally expected

- **These are fast signals.** Order-book imbalance and taker flow are known to predict prices over seconds to minutes.
- **We cannot trade at that speed:**
  - LIVE enters at the earliest 5 min after an event;
  - the AI chain takes ~20 min;
  - the horizons tested here are 1–4 h.
- **Liquidation bursts belong in the same category.**
- **Conclusion:** even if such information exists, today's execution architecture cannot capture it.

## Bugs found and fixed along the way (transparency)

1. **The order-book group required all four features at once.** The 7-day depth baseline needed 72 samples, which every-3rd-day data never provides, so only 1,380 rows were left.
   - Fix: separate groups, and the baseline needs ≥ 24 samples.
2. **Files before ~2026-01 lack the ±0.2 % band.** The parser discarded the entire snapshot.
   - Fix: ±1 % is enough; the empty files were refetched (TRAIN went from 51k to 515k points).
3. **The pass criteria were never changed.**
4. **Tests:** `tests/crypto_trading/entry_research/test_novelty.py` covers a copy vs a new feature, the verdict, "only TRAIN/VALID", snapshot age ≤ 120 s, and the old file format.

## Not tested (data missing)

- **Spread:** bookTicker is not archived after 2024.
- **Liquidations:** Binance publishes no history.
- **What they would need:** both require forward recording through a separate process, and several weeks of data before any OOS test is possible.
- **Cross-exchange divergence:** priority 4, not started.

Reproduce:
- `python -m crypto_trading.entry_research.book_fill`
- `python -m crypto_trading.entry_research.novelty`, and `--book-only` for the order-book part.
