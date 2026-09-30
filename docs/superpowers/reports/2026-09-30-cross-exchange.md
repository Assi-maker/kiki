# Cross-exchange lead/lag Binance → BingX – feasibility check (2026-09-30)

## Result

- **No usable lead.**
  - BingX follows Binance within the same minute.
  - After ≥ 1 minute the return correlation is noise (|r| ≤ 0.04), and after 5, 10 and 20 minutes it is ≈ 0.
  - Divergences between the venues are only a few basis points, below the round-trip cost.
- **The large lab was never built.** Per the user's instruction the research stops here.
- LIVE is unchanged.
- VALID, TEST and HOLDOUT were not read.

## Data verification

- **Both venues have 1-minute history for the whole window (2025-08 → 2026-09):**
  - BingX: `swap/v3/quote/klines` returns 1,440 minutes per call, back to 2025-08-01.
  - Binance: the archive's daily and monthly 1m files.
- **Sample:** TRAIN only, 5 symbols (BTC, ETH, SOL, DOGE, WIF) × 10 days spread across TRAIN.
- **Coverage:** 100 % (14,400 of 14,400 minutes per symbol on both venues). Time stamps are aligned (open time).
- **Calls:** BingX was called at ≤ 2 requests/s, so the bot's API quota was untouched.

## Measurements

| | BTC | ETH | SOL | DOGE | WIF |
|---|---|---|---|---|---|
| corr Binance 1m return (t) vs BingX (t) | 0.993 | 0.996 | 0.985 | 0.981 | 0.965 |
| … vs BingX (t+1 min) | −0.008 | +0.010 | +0.038 | +0.041 | +0.015 |
| … (t+2 / t+3 min) | +0.010 / −0.013 | −0.015 / 0.000 | −0.023 / −0.010 | −0.025 / −0.013 | −0.028 / −0.016 |
| … (t+5 min) | +0.012 | +0.017 | +0.029 | +0.019 | +0.012 |
| … (t+10 / t+20 min) | −0.012 / +0.015 | +0.002 / +0.026 | −0.003 / +0.021 | +0.008 / +0.011 | −0.002 / +0.019 |
| Basis log(Binance/BingX), p1 / p50 / p99 | −1.8 / 0.0 / +1.6 bp | −2.3 / +0.1 / +1.9 | −3.6 / +0.1 / +3.8 | −4.2 / 0.0 / +4.0 | −5.5 / 0.0 / +7.6 |
| "Binance ahead" event: top 1 % of the 5 min return difference | ≥ 2.7 bp | ≥ 3.1 | ≥ 5.4 | ≥ 6.0 | ≥ 10.6 |

BingX's 5-minute return in the direction of the lead, after such an event, starting at t + L:

| | L = 0 | L = 1 | L = 5 | L = 10 | L = 20 min |
|---|---|---|---|---|---|
| BTC | −0.7 | −1.2 | +0.2 | +0.9 | +2.5 bp |
| ETH | +1.2 | −2.1 | −0.5 | +0.4 | +1.5 |
| SOL | +0.3 | −4.2 | +3.5 | −3.9 | +3.4 |
| DOGE | +2.8 | −1.8 | −1.7 | +0.1 | +0.7 |
| WIF | −1.6 | −1.9 | +3.9 | −3.5 | −6.6 |

- **n ≈ 144 events per symbol.**
- **The sign changes between horizons and symbols.**
- **The magnitude is ±4 bp**, against a cost of ~10 bp in fees plus stop slippage.

## Interpretation

1. **Arbitrage keeps BingX and Binance synchronised within seconds.** At 1-minute resolution the lead is already used up. The small positive lag-1 correlation for SOL/DOGE (0.04) is a few bp, disappears within 2 minutes, and lies below the cost.
2. **Even with zero latency the edge is below cost.** At our LIVE latency (≥ 5 min, AI ~20 min) nothing is left.
3. **The criterion** "new information that survives costs and latency" is clearly not met. Per the user's rule, no feature grid is built on top of this.

## Summary of the research phase (6 studies)

| Study | Result |
|---|---|
| Entry edge (2126 patterns) | 0 edge |
| Edge lab (4534 combinations) | 0 edge |
| HF event engine (34 hypotheses) | 0 edge |
| Regime lab (14 months, 710 cells) | 0 pass TRAIN |
| Exit lab (22 exits × 76 cells) | 0 pass TRAIN |
| Novelty: taker flow + order book | no new information |
| Cross-exchange lead/lag | lead < 1 min, below cost |

## Reproduce

- `python -m crypto_trading.entry_research.cross_exchange feasibility`
- The result is written to `data/entry_research/cross_feasibility.json`.
