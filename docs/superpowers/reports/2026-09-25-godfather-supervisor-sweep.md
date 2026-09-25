<!-- Deliverable written by hand on 2026-09-25. Everything below the
     horizontal rule is generated verbatim by
     `python -m crypto_trading.godfather.supervisor` from the production
     database and is also stored in godfather_policies /
     godfather_policy_transitions. Gap analysis:
     docs/superpowers/plans/2026-09-25-godfather-supervisor.md -->

# GODFATHER Supervisor: what changed, and what the evidence says

**No live rule was changed.** Everything below collects evidence and
records decisions. Nothing enforces them: `entry_quality_enforcement_enabled`,
`thesis_enforcement_enabled` and `policy_promotion_enabled` are all false,
and no module in the trading path reads any GODFATHER table (AST-pinned).
The live Profit Protection break-even rule is untouched.

## 1. What already existed (reused, not rebuilt)
- Thesis states STRONG/VALID/WEAKENING/INVALID/EXIT and actions
  HOLD/PROTECT/TIGHTEN_SL/REDUCE/EXIT (`thesis.py`)
- Counterfactual engine (9 policies), Experience Memory (all 7 edge classes)
- Prediction errors (EXPECTED/ACTUAL/ERROR/CAUSE/LESSON)
- Objective metrics, advisory Entry Quality score
- Guardian factors and the paper fill/fee/funding model
- The TIGHTEN_SL evaluation

## 2. What was built
- `costs.py`: the single cost model.
- `stop_simulation.py`: shared, no-lookahead stop simulation with an
  UNOBSERVABLE rule.
- `mfe_model.py`: historical "what happens next from +X%".
- `position_decision.py`: profit protection (A) kept separate from thesis
  management (B).
- `entry_selection.py`: cohort-relative TAKE/WAIT/REJECT using as-of
  Experience Memory.
- `portfolio.py`: themes, correlation, outcome dependence, concentration
  cap.
- `policy_registry.py`: gates, status machine, promotion/rollback,
  transition log.
- `supervisor.py`: the sweep, run by the loop every 6 h.
- Counterfactual engine v2: new policies NO_INTERVENTION,
  PROFIT_LOCK_HALF_MFE, THESIS_TIGHTEN and THESIS_POLICY, plus
  UNOBSERVABLE/UNAVAILABLE handling and zero-size exclusion.
- Two new tables, one of them append-only.

## 3. Reused inside the new parts
- The thesis decision core, unchanged.
- Guardian's factors and thresholds.
- `assess_entry_quality`, with its uncalibrated cut points untouched.
- `build_experience_memory` and `detect_conflicts`.
- The paper execution cost functions.
- The BH, bootstrap and sign tests.

## 4. New decisions GODFATHER can now make (all recorded, none enforced)
- Entry: TAKE / WAIT / REJECT relative to the signals confirmed at the same
  moment, plus KEEP / SKIP_CONCENTRATION per theme.
- Position: HOLD / PROTECT / TIGHTEN_SL / REDUCE / EXIT for every open
  position, every tick, with both components recorded
  (`thesis_action`, `profit_protection`, `mfe_context`).
- Policy lifecycle: INSUFFICIENT_DATA / NOISE / SUSPECT / FAILED /
  VALIDATED, with CANARY / PROMOTED / ROLLED_BACK behind the promotion
  flag.

## 5. Still read-only
Every decision above. No code path lets any of them place, move or cancel
an order. Execution still runs only through Guardian Authority → safety
kernel → exchange, which this change does not touch.

## 6. Data each decision uses
- **Entry:** candidate evidence, the agent assessments via
  `detect_conflicts`, risk/reward, cost and regime. Also Experience Memory,
  built only from trades closed before the signal's UTC day.
- **Position:** Guardian factors, MFE/MAE, giveback, time, and distances
  to SL/TP from the observed prefix. Also the MFE model, built only from
  trades closed before the position opened.
- **Portfolio:** theme, positions open at the decision moment, and
  concurrent Guardian paths.

## 7. Sample sizes
- 146 closed positions, of which 82 are scorable. Excluded: 41 zero-size,
  9 with unknown P/L, 14 without a price path.
- 159 confirmed signals, 80 with a real outcome.
- Per policy: 15–70 observed trades. For stop-type policies, 7–17 trades
  are UNOBSERVABLE because of gaps in Guardian's observations, and are
  excluded rather than scored.

## 8. Robust findings
- **Zero edges, zero failure patterns, zero validated policies.**
- The one robust fact is about data, not strategy. Scoring zero-size
  trades or observation gaps as neutral flips conclusions. Both are now
  excluded structurally and in tests.
- Descriptive, not a policy verdict: positions held at the same time in
  the same theme have returns correlated at 0.23, versus 0.10 across
  themes. Outcome correlation inside a cohort is low (ICC 0.04, so about
  3.0 independent bets per 3.3 trades).

## 9. NOISE (enough data, no distinguishable effect)
- EXIT_ON_THESIS_INVALID (n=58)
- EXIT_ON_THESIS_WEAKENING (70)
- REDUCE_ON_WEAKENING (70)
- THESIS_POLICY (65)
- THESIS_TIGHTEN (43)
- PROFIT_LOCK_HALF_MFE (33)

Every one of them has a positive train half and a negative test half.
That is what an unstable, period-dependent effect looks like, and the
train/test and walk-forward gates reject it.

## 10. INSUFFICIENT_DATA
- DELAY_ENTRY_30M and DELAY_ENTRY_60M (21 and 23 observed)
- SAFE_TP_AT_HALF_TARGET (15)
- ENTRY_SELECTION_TOP_HALF (18 cohorts)
- PORTFOLIO_THEME_CAP (2 kept vs 37 skipped: the theme map puts almost
  every coin in `crypto_alt`, so this cap is too blunt to test)
- MFE model at +2% and +3%

## 11. Waiting for more data
- **TIGHTEN_SL_AFTER_FAVORABLE: SUSPECT.** It's live, and the evidence
  points negative without proving it: −6.24/trade, n=27, 17 unobservable.
  It is kept as-is. 23 of its 27 activations came while the thesis was
  still VALID or STRONG. The MFE model says trades at +1% come back to
  entry 45% of the time, reach target 65% of the time, and make a median
  further +2.3%. That matches the stopped-winner mechanism.
- **THESIS_TIGHTEN** loses much less (−0.62) than the unconditional
  break-even (−6.24). It's the lead hypothesis for "tighten only when the
  thesis weakens", but not proven.
- **ENTRY_SELECTION_TOP_HALF:** walk-forward blocks run −57, −15, +7,
  +16. The trend is improving but inconclusive.
- **DELAY_ENTRY_60M** was the strongest earlier candidate, but its sign
  flips between halves.

## 12. Promotion and rollback
Every sweep evaluates nine gates:
sample ≥ 30, effect CI > 0, BH across all 12 policies, train and test
both positive, ≥ 3 of 4 walk-forward blocks positive, net of costs and
positive under pessimistic fills, better expectancy than baseline (win
rate alone never counts), no regime cell significantly negative, and a
rollback path.

- VALIDATED requires all nine.
- CANARY requires VALIDATED plus `policy_promotion_enabled`.
- CANARY becomes PROMOTED, or ROLLED_BACK, on forward evidence (≥ 10
  trades after promotion).
- Every change is appended to `godfather_policy_transitions`, which is
  append-only by trigger.
- Even PROMOTED only records eligibility: letting a policy reach Guardian
  Authority is a separate, reviewed change.

## 13. How safety is shown
- AST/grep isolation tests: no new module calls an order, sizing or
  leverage primitive, or imports a connector, the gate or an execution
  module. No trading-path module reads a GODFATHER table or imports a
  GODFATHER module. The promotion flag is named in only 3 non-executing
  files, and ships false.
- Every position decision goes through `validate_action_is_safe`: it never
  widens a stop, never removes one, and never adds exposure.
- The trading path (`guardian/`, `paper_trading/`, `gate/`, `screening/`,
  `connectors/`, the live/risk/guardian YAML, and the loops) has **zero
  diff** against the previous commit.

## 14. Extra AI cost
**Zero.** No new LLM call anywhere. The sweep is deterministic and takes
~5 s over the full history.

## 15. Tests
The GODFATHER suite passes: 292 tests, 41 of them new.

Full suite: 2051 passed, 8 failed, and those 8 are pre-existing. The same
8 fail on the unmodified previous commit: they assert config defaults
that production deliberately switched on (`profit_protection_enabled`,
the authority flags).

## Next evidence requirements
- **Any policy:** ≥ 30 observed interventions, both chronological halves
  positive, and surviving BH.
- **Most constrained:** TIGHTEN_SL (27 now), ENTRY_SELECTION (18
  cohorts), and the delayed entries (about 20). A few weeks of trading
  with Guardian running without gaps would move them. The gaps currently
  cost 7–55 observations per policy.
- **Portfolio cap:** needs a finer theme map before it is testable.

---

# GODFATHER supervisor sweep

Run `309c88d0-7e9e-4834-9c4d-5eae4c4f8a2a` at 2026-09-25T20:46:31.007486+00:00. Counterfactual engine v2. AI calls: 0. Policy promotion enabled: False. **No live rule is changed by this sweep.**

Data: 146 closed positions; 82 scorable (real P/L, exposure, price path); 41 zero-size and 9 unknown-P/L positions UNAVAILABLE; 14 without a price path; 159 confirmed signals; train/test cut 2026-09-13T11:08.

## Policy registry

| policy | kind | status | n | mean effect / trade [95% CI] | p | BH | train / test | walk-forward blocks | unobservable | flags |
|---|---|---|---|---|---|---|---|---|---|---|
| DELAY_ENTRY_30M | ENTRY | **INSUFFICIENT_DATA** | 21 | +4.65 [-2.90, +13.08] | 0.290 | False | +12.36 / +1.57 | +12.4 +13.9 -6.3 -2.9 | 52 | - |
| DELAY_ENTRY_60M | ENTRY | **INSUFFICIENT_DATA** | 23 | +10.19 [-5.10, +27.03] | 0.266 | False | +33.62 / -7.83 | +35.4 +16.0 -2.4 -12.0 | 45 | - |
| EXIT_ON_THESIS_INVALID | POSITION | **NOISE** | 58 | -2.68 [-11.78, +5.73] | 0.581 | False | +3.56 / -7.75 | -3.6 +12.0 -10.2 -9.9 | 0 | NEGATIVE_DIRECTION |
| EXIT_ON_THESIS_WEAKENING | POSITION | **NOISE** | 70 | +2.63 [-5.89, +10.65] | 0.547 | False | +11.34 / -5.59 | +11.1 +11.3 -1.8 -11.1 | 0 | - |
| PROFIT_LOCK_HALF_MFE | POSITION | **NOISE** | 33 | -2.49 [-12.14, +8.09] | 0.631 | False | +1.44 / -5.05 | -13.3 +15.4 -6.4 -4.4 | 11 | NEGATIVE_DIRECTION, WIN_RATE_UP_EXPECTANCY_DOWN |
| REDUCE_ON_WEAKENING | POSITION | **NOISE** | 70 | +1.32 [-2.95, +5.32] | 0.547 | False | +5.67 / -2.79 | +5.5 +5.6 -0.9 -5.5 | 0 | - |
| SAFE_TP_AT_HALF_TARGET | POSITION | **INSUFFICIENT_DATA** | 15 | +0.90 [-8.42, +11.31] | 0.865 | False | +13.53 / -1.05 | +12.4 +4.3 -2.4 -14.6 | 55 | - |
| THESIS_POLICY | POSITION | **NOISE** | 65 | +1.44 [-6.52, +9.31] | 0.735 | False | +9.30 / -5.73 | +7.3 +11.3 -4.1 -9.1 | 5 | - |
| THESIS_TIGHTEN | POSITION | **NOISE** | 43 | -0.62 [-10.33, +8.96] | 0.908 | False | +8.56 / -6.06 | +2.0 +6.4 -4.7 -6.9 | 7 | NEGATIVE_DIRECTION |
| TIGHTEN_SL_AFTER_FAVORABLE | POSITION | **SUSPECT** | 27 | -6.24 [-19.91, +7.01] | 0.410 | False | -6.69 / -6.02 | -8.8 -3.7 -9.6 -2.2 | 17 | NEGATIVE_DIRECTION, LIVE_POLICY_UNPROVEN_NEGATIVE |
| ENTRY_SELECTION_TOP_HALF | ENTRY | **INSUFFICIENT_DATA** | 18 | -14.97 [-50.29, +11.57] | 0.524 | False | -41.86 / +6.53 | -57.3 -14.9 +7.1 +15.8 | 0 | NEGATIVE_DIRECTION |
| PORTFOLIO_THEME_CAP | PORTFOLIO | **INSUFFICIENT_DATA** | 2 | +21.50 [+3.54, +42.23] | 0.456 | False | +33.49 / +12.54 | - | 0 | - |

Gate matrix (PASS / FAIL / INSUFFICIENT_DATA):

| policy | sample_size | effect_size | multiple_testing | train_test | walk_forward | costs | baseline | regime | rollback_path |
|---|---|---|---|---|---|---|---|---|---|
| DELAY_ENTRY_30M | FAIL | INSUFFICIENT_DATA | INSUFFICIENT_DATA | PASS | FAIL | PASS | PASS | PASS | PASS |
| DELAY_ENTRY_60M | FAIL | INSUFFICIENT_DATA | INSUFFICIENT_DATA | FAIL | FAIL | PASS | PASS | PASS | PASS |
| EXIT_ON_THESIS_INVALID | PASS | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | PASS | PASS |
| EXIT_ON_THESIS_WEAKENING | PASS | FAIL | FAIL | FAIL | FAIL | PASS | PASS | PASS | PASS |
| PROFIT_LOCK_HALF_MFE | PASS | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | PASS | PASS |
| REDUCE_ON_WEAKENING | PASS | FAIL | FAIL | FAIL | FAIL | PASS | PASS | PASS | PASS |
| SAFE_TP_AT_HALF_TARGET | FAIL | INSUFFICIENT_DATA | INSUFFICIENT_DATA | FAIL | FAIL | PASS | PASS | PASS | PASS |
| THESIS_POLICY | PASS | FAIL | FAIL | FAIL | FAIL | PASS | PASS | PASS | PASS |
| THESIS_TIGHTEN | PASS | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL | PASS | PASS |
| TIGHTEN_SL_AFTER_FAVORABLE | FAIL | INSUFFICIENT_DATA | INSUFFICIENT_DATA | FAIL | FAIL | FAIL | FAIL | PASS | PASS |
| ENTRY_SELECTION_TOP_HALF | FAIL | INSUFFICIENT_DATA | INSUFFICIENT_DATA | FAIL | FAIL | FAIL | FAIL | PASS | PASS |
| PORTFOLIO_THEME_CAP | FAIL | INSUFFICIENT_DATA | INSUFFICIENT_DATA | PASS | INSUFFICIENT_DATA | PASS | PASS | PASS | PASS |

Transitions this sweep: DELAY_ENTRY_30M None -> INSUFFICIENT_DATA; DELAY_ENTRY_60M None -> INSUFFICIENT_DATA; EXIT_ON_THESIS_INVALID None -> NOISE; EXIT_ON_THESIS_WEAKENING None -> NOISE; PROFIT_LOCK_HALF_MFE None -> NOISE; REDUCE_ON_WEAKENING None -> NOISE; SAFE_TP_AT_HALF_TARGET None -> INSUFFICIENT_DATA; THESIS_POLICY None -> NOISE; THESIS_TIGHTEN None -> NOISE; TIGHTEN_SL_AFTER_FAVORABLE None -> SUSPECT; ENTRY_SELECTION_TOP_HALF None -> INSUFFICIENT_DATA; PORTFOLIO_THEME_CAP None -> INSUFFICIENT_DATA

## MFE model (what happened next, historically, from each favourable level)

| level | status | n | P(further +1%) | median further MFE % | P(back to entry) | median final giveback | P(target) |
|---|---|---|---|---|---|---|---|
| +0.5% | ESTIMATE | 22 | 59.1% | +2.34 | 40.9% | 13.1% | 59.1% |
| +1.0% | ESTIMATE | 20 | 60.0% | +2.28 | 45.0% | 11.1% | 65.0% |
| +2.0% | INSUFFICIENT_DATA | 18 | n/a | n/a | n/a | n/a | n/a |
| +3.0% | INSUFFICIENT_DATA | 16 | n/a | n/a | n/a | n/a | n/a |

## Entry selection (cohort-relative TAKE / WAIT / REJECT)

159 confirmed signals, 80 with a real outcome, 79 UNAVAILABLE (never opened, zero size or unknown P/L).

| verdict | n | total P/L | mean | win rate |
|---|---|---|---|---|
| TAKE | 39 | -93.40 | -2.39 | 59.0% |
| WAIT | 21 | -374.52 | -17.83 | 38.1% |
| REJECT | 20 | -140.01 | -7.00 | 40.0% |

Within the same cohort (same moment, same market): TAKE minus rest -14.97 USDT [-50.29, +11.57] over 18 cohorts, p=0.524; train -41.86 (n=8) / test +6.53 (n=10). Pooled TAKE vs rest: +10.15 [-12.82, +30.36], p=0.384. Book if only TAKE had been traded: -93.40 vs actual -607.93 USDT - not evidence on its own: removing trades from a losing book always looks good.

## Portfolio / correlation

- Return correlation of concurrently held positions: same theme +0.231 over 136 pairs, cross theme +0.103 over 183 pairs. BTC beta: UNAVAILABLE (no BTC series stored).
- Outcome dependence inside a cohort: ICC +0.040 across 23 cohorts / 76 trades; a cohort of 3.3 trades is worth about 3.0 independent bets.
- Exposure: LONG_ONLY, peak concurrent notional +12000 USDT, peak single-theme share 100.0%.
- Theme cap (advisory): kept 2, skipped 37 (skipped trades made -129.40 USDT).

## Counterfactual coverage (observed / unobservable / unavailable)

| policy | acted | observed | unobservable | unavailable |
|---|---|---|---|---|
| BASELINE | 82 | 82 | 0 | 0 |
| DELAY_ENTRY_30M | 21 | 30 | 52 | 0 |
| DELAY_ENTRY_60M | 23 | 37 | 45 | 0 |
| EXIT_ON_THESIS_INVALID | 58 | 82 | 0 | 0 |
| EXIT_ON_THESIS_WEAKENING | 70 | 82 | 0 | 0 |
| NO_INTERVENTION | 0 | 81 | 0 | 1 |
| PROFIT_LOCK_HALF_MFE | 44 | 71 | 11 | 0 |
| REDUCE_ON_WEAKENING | 70 | 82 | 0 | 0 |
| REJECT_ENTRY | 82 | 82 | 0 | 0 |
| SAFE_TP_AT_HALF_TARGET | 31 | 27 | 55 | 0 |
| THESIS_POLICY | 70 | 77 | 5 | 0 |
| THESIS_TIGHTEN | 50 | 75 | 7 | 0 |
| TIGHTEN_SL_AFTER_FAVORABLE | 44 | 65 | 17 | 0 |

THESIS_POLICY decisions over all replayed ticks: EXIT 48, HOLD 1530, REDUCE 1450, STOPPED 10, TIGHTEN_SL 481.

## Experience Memory

82 samples; patterns by class: INSUFFICIENT_DATA 20, NOISE 19. INSUFFICIENT_DATA and NOISE authorise no strategic change.
