<!-- Interpretation written by hand on 2026-09-25; everything below the
     horizontal rule is generated verbatim by
     `python -m crypto_trading.godfather.policy_evaluation` and is also
     stored in `godfather_policy_evaluations` (promotion_allowed = 0). -->

# Interpretation

**Short answer: not proven worse, not proven better, and nothing in the data
supports "it is actually good".** Every point estimate is negative: both
data sources (Guardian tick path, forward-recorded 1-minute candle shadow),
both fill assumptions, and both thresholds (1.0% and 1.5%). But no
confirmatory test clears the pre-registered bar (n >= 30 activated trades,
bootstrap CI excluding 0, Benjamini-Hochberg across the 5-test family, same
sign in both chronological halves). Correct label: `NOISE`, leaning harm.
**No live rule is changed on the basis of this report.**

What the data does show consistently:

* **Mechanism.** The damage comes from winners that are stopped at entry
  and then continue: 15 of the 16 observable break-even exits later made a
  new high, and 6 went on to hit the real target. Stopped winners
  (-331 USDT) outweigh limited losses (+162 USDT). The +1% trigger sits
  at about 0.24 R (median SL distance 4.1%), well inside normal
  retracement for these instruments.
* **The threshold is not the fix.** The pre-registered 1.5% variant is no
  better than 1.0% on the same trades (paired +0.27 USDT, CI [-3.6, +4.5]).
  In the candle shadow it is actually worse (-12.4/trade; its CI excludes 0
  and it survives BH, but n = 25 < 30).
* **Selection effect vs. policy effect.** Trades that reach +1% earn
  +41.7 USDT more than trades that never do. That is a property of the
  trade, not of the policy. The paired effect of the policy on those same
  trades is negative.
* **LIVE (n = 12, descriptive).** 8 of 12 real activations ended as a
  break-even stop, 4 of them while the PAPER twin with the original SL hit
  its target. Mean live-minus-paper return was -1.9 percentage points.
* **Profit lock** (lock half of the MFE) loses least of the three versus
  no policy (-2.5/trade, not significant). It beats the live policy on
  the same trades (+4.3, CI [+1.0, +8.2]), but that comparison is
  descriptive only and is not in the confirmatory family.

Two corrections were made during the analysis, and both are enforced in
code and tests:

1. 41 zero-size (no exposure) positions were excluded. They would have
   entered as "neutral" trades and pulled every mean toward 0.
2. A break-even exit that falls inside a gap longer than 10 minutes in
   Guardian's observations is `UNOBSERVABLE`, not scored. Before this rule,
   five exits in 15-74 hour gaps (one worth +287 USDT) flipped the sign of
   the whole path result.

When a verdict becomes possible: roughly 30+ additional observable
activations, which at the current rate is a few weeks of trading.
Re-running the command appends a new row and leaves earlier ones intact.

---

# Policy evaluation: `TIGHTEN_SL_AFTER_FAVORABLE`

Evaluated 2026-09-25T20:14:34.249811+00:00 (run `e23efdac-5345-4ef6-a16b-eb7d878e0654`). Diagnostic only - **promotion_allowed = false, no live rule was changed.**

**Verdict: NOISE** (confidence LOW; path data INSUFFICIENT_DATA, candle shadow NOISE, pessimistic fills agree: None). Direction of the point estimates across both sources and both fill assumptions: **NEGATIVE**.

Data: 82 closed trades with a real P/L and a real price path, entries 2026-09-04T13:24 .. 2026-09-19T10:39; train/test cut at 2026-09-13T11:08; 168 shadow rows; 41 zero-size (no exposure) positions excluded from both sources; 12 real LIVE activations.

## Answers

| Question | Answer |
|---|---|
| 1. Policy generally bad | NOISE (negative in 91.7% of testable cells) |
| 2. Activates too early | INSUFFICIENT_DATA |
| 3. Threshold wrong | INSUFFICIENT_DATA |
| 4. Only works in some regimes | INSUFFICIENT_DATA |
| 5. Good, but looks bad on a small sample | NOT_SUPPORTED |
| Robust alternative | none |

Where the money goes (path data, sum of per-trade deltas): winners stopped early -331.02 USDT, losses limited +162.41 USDT, losers made worse +0.00 USDT, winners improved +0.00 USDT.

## Policy level (each policy over the trades where it activated)

| | TIGHTEN_SL_AFTER_FAVORABLE | BREAKEVEN_AT_1_5PCT | PROFIT_LOCK_HALF_MFE |
|---|---|---|---|
| activated & observable / stopped by new SL | 27 / 16 | 25 / 10 | 33 / 24 |
| excluded: stop armed across a >10 min hole | 17 | 13 | 11 |
| baseline total P/L | +459.29 | +540.52 | +507.28 |
| policy total P/L | +290.68 | +379.19 | +424.95 |
| baseline mean / median | +17.01 / +20.15 | +21.62 / +23.64 | +15.37 / +17.28 |
| policy mean / median | +10.77 / -0.70 | +15.17 / +11.21 | +12.88 / +5.16 |
| win rate baseline -> policy | 85.2% -> 40.7% | 96.0% -> 60.0% | 81.8% -> 100.0% |
| MFE capture baseline -> policy | 0.39 -> 0.25 | 0.46 -> 0.32 | 0.40 -> 0.34 |
| mean MAE % baseline -> policy | -1.31 -> -0.73 | -0.95 -> -0.77 | -1.14 -> -0.68 |
| winners stopped early | 12 | 9 | 14 |
| losses limited | 4 | 1 | 6 |
| neutral (stop never hit) | 11 | 15 | 9 |
| stopped, real trade hit target | 6 | 4 | 10 |
| uplift total (policy - baseline) | -168.61 | -161.33 | -82.32 |
| mean uplift [95% bootstrap CI] | -6.24 [-19.91, +7.01] | -6.45 [-20.57, +7.07] | -2.49 [-12.14, +8.09] |
| pessimistic-fill mean uplift | -7.20 | -7.32 | -4.68 |
| p (sign-flip) / sign test | 0.4104 / 0.0768 | 0.4410 / 0.0215 | 0.6306 / 0.5413 |
| train mean uplift (n) | -6.69 (9) | -6.86 (9) | +1.44 (13) |
| test mean uplift (n) | -6.02 (18) | -6.22 (16) | -5.05 (20) |
| BH significant (q=0.10, family of 5) | False | False | False |
| verdict | **INSUFFICIENT_DATA** | **INSUFFICIENT_DATA** | **NOISE** |

## Independent replication: forward-recorded PAPER shadow (1-minute candles)

| | n | baseline total | shadow total | uplift mean [CI] | winners stopped early | losses limited | train / test mean | verdict |
|---|---|---|---|---|---|---|---|---|
| TIGHTEN_SL_AFTER_FAVORABLE (+1.0%) | 30 | +177.78 | -75.06 | -8.43 [-16.42, -0.47] | 15 | 8 | +4.96 / -11.11 | **NOISE** |
| BREAKEVEN_AT_1_5PCT (+1.5%) | 25 | +275.96 | -33.67 | -12.39 [-19.94, -4.83] | 14 | 4 | -6.84 / -12.87 | **INSUFFICIENT_DATA** |

Fidelity TIGHTEN_SL_AFTER_FAVORABLE: 19 never-activated shadow rows, 0 with a non-zero delta (must be 0).
Fidelity BREAKEVEN_AT_1_5PCT: 24 never-activated shadow rows, 0 with a non-zero delta (must be 0).

Source overlap: shadow activated 30, of which the tick path also activated 20 (10 only visible in candles - the wick a ~97 s tick misses).

## Policy effect vs. trade selection

Naive: activated trades averaged +17.01 USDT real P/L vs -24.68 for trades that never reached +1% - a selection effect of +41.69 USDT that says the policy is applied to BETTER trades, nothing about the policy. Paired, same trade with vs without the policy: -6.24 USDT per activated trade.

## Mechanism

Of 27 activated trades, 16 came back to the entry price at some point afterwards; 16 were stopped by the new SL, 15 of those later exceeded their pre-intervention high, and 6 went on to hit the real target. Median original SL distance 4.09%, so a +1% trigger sits at roughly 0.24 R - inside ordinary noise for these instruments.

- Paired 1.5% vs live 1.0%: n=27, mean +0.27 [-3.61, +4.50], p=0.9060 (descriptive, not in the confirmatory family).
- Paired profit lock vs live: n=27, mean +4.33 [+1.02, +8.22], p=0.0174 (descriptive, not in the confirmatory family).

## Exploratory breakdowns (live policy, BH across 12 cells with n >= 8)

| dimension | value | n | baseline mean | uplift mean [CI] | status |
|---|---|---|---|---|---|
| signal_type | funding_oi | 3 | -19.78 | +19.08 [+12.63, +26.50] | INSUFFICIENT_DATA |
| signal_type | funding_oi,momentum_breakout | 1 | +26.74 | +0.00 [n/a, n/a] | INSUFFICIENT_DATA |
| signal_type | funding_oi,momentum_breakout,price_volatility | 1 | +38.02 | +0.00 [n/a, n/a] | INSUFFICIENT_DATA |
| signal_type | momentum_breakout | 12 | +12.48 | -9.26 [-15.82, -3.12] | NOISE |
| signal_type | momentum_breakout,price_volatility | 3 | +38.39 | -23.16 [-38.04, +0.00] | INSUFFICIENT_DATA |
| signal_type | price_volatility | 5 | +8.71 | +16.04 [-14.99, +63.11] | INSUFFICIENT_DATA |
| signal_type | price_volatility,volume | 1 | +121.72 | -125.45 [n/a, n/a] | INSUFFICIENT_DATA |
| signal_type | volume | 1 | +23.64 | +0.00 [n/a, n/a] | INSUFFICIENT_DATA |
| entry_quality | TRADE | 1 | +38.02 | +0.00 [n/a, n/a] | INSUFFICIENT_DATA |
| entry_quality | WAIT | 26 | +16.20 | -6.48 [-20.05, +7.48] | NOISE |
| regime | btc_bad | 3 | +2.76 | +0.51 [-24.99, +26.50] | INSUFFICIENT_DATA |
| regime | btc_strong | 22 | +17.15 | -7.73 [-23.24, +8.33] | NOISE |
| regime | btc_weak | 2 | +36.89 | +0.00 [+0.00, +0.00] | INSUFFICIENT_DATA |
| sl_distance | 3.5-5% | 10 | +32.62 | -16.54 [-41.82, -1.08] | NOISE |
| sl_distance | <3.5% | 11 | +11.44 | -1.39 [-11.18, +8.12] | NOISE |
| sl_distance | >=5% | 6 | +1.21 | +2.02 [-27.74, +46.90] | INSUFFICIENT_DATA |
| mfe_at_activation | 1.25-1.75% | 8 | +7.34 | -4.15 [-25.83, +31.40] | NOISE |
| mfe_at_activation | <1.25% | 11 | +13.70 | -8.85 [-33.99, +8.36] | NOISE |
| mfe_at_activation | >=1.75% | 8 | +31.22 | -4.76 [-14.27, +0.00] | NOISE |
| minutes_to_activation | 30-120m | 10 | +5.58 | +4.12 [-15.54, +30.79] | NOISE |
| minutes_to_activation | <30m | 4 | +19.83 | -1.72 [-18.74, +13.57] | INSUFFICIENT_DATA |
| minutes_to_activation | >=120m | 13 | +24.94 | -15.61 [-35.80, -1.21] | NOISE |
| activation_progress_to_target | 0.20-0.33 | 11 | +23.80 | -15.49 [-40.42, +1.95] | NOISE |
| activation_progress_to_target | <0.20 | 3 | -28.14 | +27.21 [-20.87, +105.18] | INSUFFICIENT_DATA |
| activation_progress_to_target | >=0.33 | 13 | +21.69 | -6.14 [-15.16, +1.87] | NOISE |

## Out-of-sample check of the diagnosis (worst cell picked on TRAIN, checked on TEST)

| dimension | train-worst cell | train mean | test mean in cell | test mean rest | status |
|---|---|---|---|---|---|
| signal_type | - | n/a | n/a | n/a | INSUFFICIENT_DATA |
| entry_quality | - | n/a | n/a | n/a | INSUFFICIENT_DATA |
| regime | - | n/a | n/a | n/a | INSUFFICIENT_DATA |
| sl_distance | - | n/a | n/a | n/a | INSUFFICIENT_DATA |
| mfe_at_activation | - | n/a | n/a | n/a | INSUFFICIENT_DATA |
| minutes_to_activation | - | n/a | n/a | n/a | INSUFFICIENT_DATA |
| activation_progress_to_target | - | n/a | n/a | n/a | INSUFFICIENT_DATA |

## Real LIVE activations (exchange) - descriptive, INSUFFICIENT_DATA

12 real activations; 8 ended as a stop at break-even, 4 of them while the PAPER twin (original SL) hit its target. Mean live-minus-paper return on the 9 comparable trades: -1.91 percentage points.

| position | PP status | live exit | live % | paper exit | paper % |
|---|---|---|---|---|---|
| 3f25b460ad | UNCERTAIN_NEW_SL_STATUS | stop_loss | -0.2378 | target | 3.0354 |
| cd145e2500 | UNCERTAIN_NEW_SL_STATUS | stop_loss | -0.0416 | target | 4.9878 |
| 2085be4e65 | UNCERTAIN_NEW_SL_STATUS | TIME_LIMIT | 0.4374 | stop_loss | -2.6266 |
| 943b4ba591 | UNCERTAIN_NEW_SL_STATUS | stop_loss | 3.5829 | target | 6.1870 |
| b75a04783e | UNCERTAIN_NEW_SL_STATUS | stop_loss | 0.3414 | target | 7.6430 |
| 9123abd683 | UNCERTAIN_NEW_SL_STATUS | stop_loss | -0.0592 | target | 6.0402 |
| 446f1494d8 | UNCERTAIN_NEW_SL_STATUS | target | 7.7872 | target | 5.3887 |
| 475f327124 | UNCERTAIN_NEW_SL_STATUS | stop_loss | -0.1887 | stop_loss | -3.7177 |
| 4936ea8f7f | SL_REPLACED | stop_loss | -0.0239 | stop_loss | None |
| 1f12c7db44 | SL_REPLACED | stop_loss | -0.0939 | stop_loss | None |
| d7a44441b9 | SL_REPLACED | target | 3.4405 | target | 5.2777 |
| ec56c25825 | SL_REPLACED | stop_loss | 0.0000 | stop_loss | None |

## Per-trade record (live policy; every activated trade)

| entry | instrument | activated after | MFE before | MAE before | stopped | MFE after | MAE after | real exit | actual = original SL | break-even | BE 1.5% | profit lock | effect |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-04T13:24 @ 6.24123 | UNI-USDT | 1200m | 1.09% | -1.13% | yes | 14.06% | -0.16% | target | +121.72 | -3.73 | -3.73 | +31.87 | WINNER_STOPPED_EARLY |
| 2026-09-04T14:03 @ 0.124224 | BEAT-USDT | 1055m | 1.51% | 0.00% | yes | 1.59% | -0.26% | time_limit | +30.61 | -6.65 | -6.65 | +19.35 | WINNER_STOPPED_EARLY |
| 2026-09-04T17:25 @ 1719.64 | NCSKSNDK2USD-USDT | 169m | 1.08% | 0.00% | no | 3.52% | 0.51% | time_limit | +17.28 | +17.28 | +17.28 | +1.94 | UNOBSERVABLE |
| 2026-09-08T06:46 @ 0.243633 | USELESS-USDT | 763m | 11.77% | -4.90% | no | 6.31% | 6.31% | target | +61.35 | +61.35 | +61.35 | +61.35 | NEUTRAL |
| 2026-09-08T19:29 @ 0.00363563 | 1000PEPE-USDT | 650m | 1.71% | -0.23% | yes | 2.01% | -8.84% | stop_loss | -90.16 | -1.86 | -1.86 | +8.17 | UNOBSERVABLE |
| 2026-09-08T19:29 @ 0.61061 | ETHFI-USDT | 650m | 1.57% | -1.39% | no | 4.81% | 1.64% | target | +46.43 | +46.43 | +46.43 | +46.43 | UNOBSERVABLE |
| 2026-09-08T19:29 @ 96.6466 | NCSKCRCL2USD-USDT | 650m | 1.15% | -0.67% | yes | 1.16% | -4.72% | time_limit | -50.08 | -2.88 | -50.08 | +2.93 | UNOBSERVABLE |
| 2026-09-09T06:33 @ 0.327117 | USELESS-USDT | 17m | 2.32% | 0.00% | yes | 2.63% | -28.76% | stop_loss | -288.88 | -1.60 | -1.60 | +11.54 | UNOBSERVABLE |
| 2026-09-11T14:55 @ 0.673773 | ETHFI-USDT | 31m | 1.15% | 0.00% | no | 3.60% | 0.76% | target | +34.52 | +34.52 | +34.52 | +34.52 | NEUTRAL |
| 2026-09-11T14:55 @ 0.018979 | BLUR-USDT | 27m | 9.12% | 0.00% | no | 4.85% | 4.85% | target | +47.08 | +47.08 | +47.08 | +47.08 | NEUTRAL |
| 2026-09-12T07:58 @ 0.434834 | JTO-USDT | 176m | 1.05% | 0.00% | no | 2.34% | 0.50% | target | +21.90 | +21.90 | +21.90 | +3.84 | UNOBSERVABLE |
| 2026-09-12T07:58 @ 6.34434 | UNI-USDT | 255m | 1.44% | -0.34% | yes | 3.19% | -1.09% | guardian_exit | +2.81 | -1.40 | -1.40 | +8.34 | WINNER_STOPPED_EARLY |
| 2026-09-12T07:58 @ 0.0912912 | 1INCH-USDT | 451m | 1.01% | -0.32% | yes | 0.96% | -0.41% | time_limit | -5.76 | -1.50 | -5.76 | +3.63 | UNOBSERVABLE |
| 2026-09-12T07:58 @ 0.00358258 | PUMP-USDT | 383m | 1.27% | -1.30% | no | 7.46% | 0.68% | target | +73.12 | +73.12 | +73.12 | +73.12 | UNOBSERVABLE |
| 2026-09-12T07:58 @ 0.0733533 | LAB-USDT | 72m | 1.51% | -1.75% | yes | 5.42% | -10.53% | stop_loss | -106.58 | -1.40 | -1.40 | +8.45 | LOSS_LIMITED |
| 2026-09-13T06:33 @ 54.3343 | LTC-USDT | 637m | 1.10% | -1.85% | yes | 1.00% | -0.96% | time_limit | -11.34 | -1.70 | -11.34 | +3.98 | UNOBSERVABLE |
| 2026-09-13T10:41 @ 0.151902 | LONGXIA-USDT | 17m | 1.38% | 0.00% | yes | 5.00% | -0.11% | target | +24.29 | -0.70 | +24.29 | +2.74 | WINNER_STOPPED_EARLY |
| 2026-09-13T10:41 @ 0.00441341 | SOPH-USDT | 65m | 1.06% | 0.00% | yes | 1.19% | -5.31% | stop_loss | -27.20 | -0.70 | -27.20 | +2.28 | LOSS_LIMITED |
| 2026-09-13T11:08 @ 100.891 | NCCO1OILBRENT2USD-USDT | 1312m | 2.45% | -0.01% | no | 2.65% | 1.71% | time_limit | +11.21 | +11.21 | +11.21 | +11.21 | NEUTRAL |
| 2026-09-13T11:34 @ 97.0469 | NCCO1OILWTI2USD-USDT | 1287m | 2.38% | -0.53% | no | 2.75% | 1.54% | time_limit | +12.43 | +12.43 | +12.43 | +12.43 | NEUTRAL |
| 2026-09-13T12:55 @ 125.515 | AAVE-USDT | 54m | 1.04% | -0.09% | yes | 1.61% | -0.14% | time_limit | -1.42 | -0.79 | -0.79 | +3.33 | UNOBSERVABLE |
| 2026-09-13T12:55 @ 101.732 | NCCO1OILBRENT2USD-USDT | 371m | 1.53% | -0.82% | no | 2.65% | 0.85% | time_limit | +11.93 | +11.93 | +11.93 | +5.32 | UNOBSERVABLE |
| 2026-09-13T12:55 @ 0.0133363 | GRIFFAIN-USDT | 34m | 1.56% | 0.00% | yes | 3.87% | -4.88% | stop_loss | -25.09 | -0.70 | -0.70 | +8.96 | UNOBSERVABLE |
| 2026-09-13T12:55 @ 0.0037017 | REZ-USDT | 49m | 1.33% | -1.37% | no | 9.41% | 0.93% | target | +46.30 | +46.30 | +46.30 | +46.30 | UNOBSERVABLE |
| 2026-09-13T14:07 @ 0.0788107 | BULLA-USDT | 19m | 1.03% | 0.00% | yes | 6.72% | -3.08% | time_limit | +0.82 | -0.70 | +0.82 | +1.88 | UNOBSERVABLE |
| 2026-09-13T16:43 @ 130.37 | NCSKMSTR2USD-USDT | 1029m | 1.51% | -0.61% | yes | 3.93% | -0.32% | target | +18.73 | -0.93 | -0.93 | +3.58 | WINNER_STOPPED_EARLY |
| 2026-09-13T16:43 @ 0.0969669 | POL-USDT | 1181m | 1.13% | -1.46% | no | 1.58% | 0.04% | time_limit | +2.90 | +2.90 | +2.90 | +3.20 | NEUTRAL |
| 2026-09-13T17:13 @ 101.08 | SOL-USDC | 999m | 1.04% | -0.28% | yes | 1.65% | -0.42% | time_limit | +7.28 | -0.80 | +7.28 | +1.88 | WINNER_STOPPED_EARLY |
| 2026-09-13T17:13 @ 130.891 | NCSKMSTR2USD-USDT | 999m | 1.26% | -1.02% | yes | 4.76% | -0.75% | time_limit | +20.15 | -0.71 | -0.71 | +2.80 | WINNER_STOPPED_EARLY |
| 2026-09-14T09:03 @ 101.562 | SOL-USDT | 436m | 1.01% | -0.90% | yes | 2.89% | -0.59% | time_limit | -4.06 | -1.10 | -1.10 | +6.11 | UNOBSERVABLE |
| 2026-09-14T09:03 @ 1.38649 | XRP-USDT | 49m | 1.19% | 0.00% | yes | 3.14% | -0.18% | target | +14.93 | -0.70 | -0.70 | +3.18 | WINNER_STOPPED_EARLY |
| 2026-09-14T09:03 @ 103.323 | NCCO1OILBRENT2USD-USDT | 215m | 1.05% | -0.69% | yes | 1.07% | -2.53% | stop_loss | -13.33 | -0.70 | -13.33 | +1.98 | LOSS_LIMITED |
| 2026-09-14T09:03 @ 2.42642 | NEAR-USDT | 468m | 1.05% | -1.75% | no | 5.09% | 1.30% | target | +24.71 | +24.71 | +24.71 | +24.71 | NEUTRAL |
| 2026-09-18T14:51 @ 0.732232 | ETHFI-USDT | 67m | 1.20% | -0.48% | yes | 3.41% | -1.37% | time_limit | +2.00 | -0.70 | -0.70 | +5.39 | WINNER_STOPPED_EARLY |
| 2026-09-18T14:51 @ 2579.25 | ETH-USDC | 102m | 1.07% | -0.28% | no | 2.74% | 0.21% | target | +12.95 | +12.95 | +12.95 | +1.98 | UNOBSERVABLE |
| 2026-09-18T14:51 @ 0.859539 | BR-USDT | 46m | 5.45% | 0.00% | yes | 8.46% | -0.53% | target | +37.34 | -0.70 | -0.70 | +12.90 | WINNER_STOPPED_EARLY |
| 2026-09-18T14:51 @ 0.732232 | ETHFI-USDT | 67m | 1.01% | -0.56% | yes | 3.40% | -1.37% | time_limit | +2.00 | -0.70 | -0.70 | +5.16 | WINNER_STOPPED_EARLY |
| 2026-09-18T14:51 @ 2579.25 | ETH-USDC | 102m | 1.10% | -0.30% | no | 2.74% | 0.24% | target | +12.95 | +12.95 | +12.95 | +2.06 | UNOBSERVABLE |
| 2026-09-18T20:53 @ 250.29 | TAO-USDT | 706m | 2.94% | -0.35% | no | 5.08% | 2.61% | target | +23.64 | +23.64 | +23.64 | +23.64 | NEUTRAL |
| 2026-09-19T08:49 @ 0.192863 | ENA-USDT | 103m | 1.36% | -2.70% | yes | 6.29% | -1.93% | target | +30.73 | -0.70 | +30.73 | +2.69 | WINNER_STOPPED_EARLY |
| 2026-09-19T08:49 @ 0.0593963 | AKE-USDT | 35m | 1.66% | 0.00% | no | 9.35% | 2.01% | target | +38.02 | +38.02 | +38.02 | +38.02 | NEUTRAL |
| 2026-09-19T09:38 @ 0.848828 | BR-USDT | 35m | 3.02% | -1.25% | no | 6.15% | 6.15% | target | +30.00 | +30.00 | +30.00 | +30.00 | NEUTRAL |
| 2026-09-19T09:38 @ 0.061046 | AKE-USDT | 11m | 3.08% | 0.00% | no | 9.02% | 1.00% | target | +26.74 | +26.74 | +26.74 | +6.99 | NEUTRAL |
| 2026-09-19T09:38 @ 0.00423123 | F-USDT | 13m | 1.01% | 0.00% | yes | 0.89% | -3.62% | stop_loss | -18.79 | -0.70 | -18.79 | +1.82 | LOSS_LIMITED |

Columns 9 (no intervention) and 11 (original SL) of the request are the same number by construction: the PAPER book never moved its stop, so its real outcome IS the original-SL outcome.
