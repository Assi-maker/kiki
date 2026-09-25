# GODFATHER Supervisor: entry + position management (2026-09-25)

Builds on `2026-09-25-godfather-intelligence-layer.md` and the
`TIGHTEN_SL_AFTER_FAVORABLE` evaluation (`docs/superpowers/reports/2026-09-25-tighten-sl-after-favorable.md`).
Nothing here is rebuilt; every row below names what is reused.

## Gap analysis (verified against live code and `data/crypto_trading.db`)

| Requirement | Already existed | Gap closed by this plan |
|---|---|---|
| 1 Entry Quality TAKE/WAIT/REJECT | `entry_quality.py`: absolute TRADE/WAIT/REJECT from 6 sub-scores; `backfill` scores only investigated trades using TODAY's Experience Memory (lookahead when replayed) | Cohort-relative selection (rank among the signals confirmed in the SAME discovery run - 36 runs confirmed 2-8 signals at once); as-of-time Experience Memory (no lookahead); walk-forward evaluation on real outcomes. `TRADE` is the stored value for TAKE (existing schema). |
| 2 Thesis state machine | `thesis.py`: STRONG/VALID/WEAKENING/INVALID/EXIT from Guardian factors + MFE/giveback/time, run for OPEN positions each tick | Reused unchanged; now also replayed over every closed trade (no-lookahead prefix) so its decisions can be scored. |
| 3 Decision policy HOLD/PROTECT/TIGHTEN_SL/REDUCE/EXIT | `thesis.decide_thesis_action` (already context-aware: STRONG + profit -> HOLD) | `position_decision.py`: explicit split into (A) profit protection and (B) thesis management, with an MFE-context input; one combined, safety-validated action. |
| 4 TIGHTEN_SL as learning case | `policy_evaluation.py` (NOISE, negative direction) | Registry status SUSPECT (kept, not removed); new breakdowns by thesis state and momentum at activation. |
| 5 MFE/MAE-centric management | Path MFE/MAE per trade (`path.py`) | `mfe_model.py`: empirical, walk-forward conditional estimates (further favourable move, giveback, reversal-to-entry) with INSUFFICIENT_DATA below a floor. |
| 6 Counterfactuals BASELINE/NO_INTERVENTION/BE/PROFIT_LOCK/TIGHTEN/EARLY_EXIT/REDUCE | `counterfactual.py`: 9 policies, no gap check, zero-size positions included | Adds NO_INTERVENTION, PROFIT_LOCK_HALF_MFE, THESIS_TIGHTEN, THESIS_POLICY; stop-type policies go through the shared `stop_simulation.py` (UNOBSERVABLE across Guardian holes); zero-size positions produce no rows. |
| 7 Experience Memory | `experience.py` (all 7 edge classes), `prediction_error.py` (EXPECTED/ACTUAL/ERROR/CAUSE/LESSON) | Zero-size trades excluded from samples; investigation detail carries entry-selection verdict, thesis timeline, observability. |
| 8 Portfolio / correlation | nothing | `portfolio.py`: theme map, concurrent-path return correlation, outcome correlation inside a cohort (effective number of independent bets), advisory diversified selection. Can only subtract. |
| 9 Cost awareness | fill/fee/funding model used by counterfactuals | Reused; moved to `costs.py` so every new simulation uses the same one. |
| 10 Self-improvement loop | `guardian/self_improvement.py` (LLM proposals -> OOS -> promote Guardian heuristics) | `policy_registry.py`: deterministic gate evaluation (sample size, effect CI, train/test, walk-forward, BH, regime, costs, baseline) and an append-only transition log; promotion/rollback state machine. Promotion needs `policy_promotion_enabled` (ships false) and has no execution reader. |
| 11 Objective | `objective.py` | Reused for every policy comparison. |
| 12 AI | - | No new LLM call anywhere. |
| 13 Safety | isolation tests, `validate_action_is_safe` | Extended to every new module and table. |
| 16 Data quality | - | UNOBSERVABLE / zero-size / unavailable separated everywhere, never counted as neutral. |

## Deployment

Evidence only. No new module is read by the trading path (AST-pinned).
`entry_quality_enforcement_enabled`, `thesis_enforcement_enabled` and the new
`policy_promotion_enabled` all ship false. The live Profit Protection
break-even rule is untouched.
