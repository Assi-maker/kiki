# GODFATHER Intelligence Layer (2026-09-25)

## Gap analysis (what already existed, verified against live code + `data/crypto_trading.db`)

| Requested capability | Already existed | Gap |
|---|---|---|
| Post-trade analysis | `detective/` (batch LLM observations + win/loss stats) | No per-trade structured record, no classification taxonomy, no price path, no MFE/MAE, no "what decision at time T" |
| Decision reconstruction | `detective/context.py` bundles all 7 assessments into an LLM prompt | Nothing scores *which component was right*; no structured per-component verdict |
| Experience memory | `guardian_authority_heuristics` + `godfather_priority_heuristics` (adjustment/confidence/sample_size) | No edge classification, no INSUFFICIENT_DATA state, no calibration, no MFE/MAE/time-to-event, no baseline lift, no multiple-testing control |
| Counterfactuals | `profit_protection_shadow_positions` (1 policy: breakeven SL at +x%), `guardian_authority_shadow_observations` | Only one hard-coded policy; no general engine, no reject/delay/reduce/safe-TP policies |
| Prediction error | `guardian_authority_decisions.expectation_correct`, `prediction_error` column on shadow rows | Only for Guardian Authority interventions; no EXPECTED/ACTUAL/ERROR/CAUSE/LESSON record for a *trade* |
| Position thesis tracking | `guardian_observations` (decay_score + HOLD/WATCH/PROTECT/EXIT, 21k rows) | State is decay-only; no thesis-vs-entry comparison, no REDUCE, no thesis lifecycle record |
| Entry quality layer | `quant_screener` + `eligibility_filter` + `risk_signal_gate` + `qa_gate` | Binary pass/fail; no expected-edge/regime-compat/historical-similarity/cost layer, no second REJECT |
| Optimization objective | `performance/metrics.py` (win rate, PF, expectancy) | No composite risk-adjusted objective incl. drawdown/giveback/turnover/costs |

## Data actually available (audited 2026-09-25)
- 148 positions (146 CLOSED), 124 with a real per-minute price path in `guardian_observations` (median 201 ticks/position, 21 043 rows total).
- Price is exactly reconstructible: `price = entry * (1 + unrealized_pnl / size)`, cross-checkable against `progress_ratio`.
- 2 141 candidates with full `evidence_record`; 4 995 assessments across 8 roles; 643 gate decisions (169 CONFIRMED).
- 51 LIVE executions, 168 PP shadow rows, 31+35 Guardian Authority shadow rows.

## What this plan builds
New, strictly additive package modules under `crypto_trading/godfather/`, each writing ONLY to its own new tables:
`stats.py`, `path.py`, `investigator.py`, `auditor.py`, `counterfactual.py`, `thesis.py`,
`experience.py`, `prediction_error.py`, `entry_quality.py`, `objective.py`, `pipeline.py`, `report.py`,
plus `crypto_trading/godfather_loop.py`.

## Safety contract (phase 1)
- No module here writes `positions`, `live_executions`, `demo_executions`, `guardian_authority_heuristics`,
  `godfather_priority_heuristics`, or any order primitive. Enforced by a grep/AST isolation test.
- No change to leverage, sizing, capital limits, safety kernel, Guardian Authority, SL removal protection,
  capacity, stale TTL or exchange primitives.
- `entry_quality` and `thesis` record advisory verdicts only; their enforcement flags ship `false`.

## Anti-noise contract
A pattern may not be called an edge unless it clears ALL of:
1. n >= 30 samples (else `INSUFFICIENT_DATA`, never an edge claim),
2. Wilson 95% lower bound on win rate above the population baseline,
3. bootstrap 95% CI on expectancy strictly above 0,
4. exact binomial p-value vs baseline surviving Benjamini-Hochberg FDR across the whole sweep,
5. sign-consistent lift in both chronological halves (else `WEAK_EDGE`/`REGIME_DEPENDENT`).
Failing 2-5 with a *negative* robust lift is `FAILURE_PATTERN` (actionable: avoid). Otherwise `NOISE`.

## Results of the first real sweep (2026-09-25, 146 closed positions)

Ran end to end against a copy of `data/crypto_trading.db`. Nothing below is
acted on: every conclusion is recorded, and every statistical gate says the
same thing.

**Coverage.** 146 investigations (137 with a scorable P/L; the other 9 are
LIVE-mirrored closes with no PAPER exit data and are recorded as UNKNOWN
rather than guessed), 146 decision audits, 1 008 counterfactual rows across 9
policies, 42 experience patterns, 320 prediction-error records, 146 advisory
entry-quality assessments. Price-path reconstruction matched its independent
cross-check on **all 122 positions with Guardian observations, 0 failures**.

**The book.** Net -513 USDT over 137 trades, win rate 35.8%, profit factor
0.72, max drawdown 1 049 USDT, MFE capture **-2.85** (the system ends, on
average, nearly three times its best unrealised gain below zero).

**Where the fault lies.** 67 entries rated GOOD versus 30 BAD, but only 37
managements GOOD versus 33 BAD, and `fault_domain` is POSITION_MANAGEMENT for
26 trades against 0 for SIGNAL_SELECTION alone. Combined with 28
GOOD_ENTRY_BAD_MANAGEMENT and 23 FALSE_BREAKOUT, the picture is that entries
find real moves more often than they fail to, and the system does not convert
them.

**Component scoreboard (RIGHT / WRONG / UNSCORABLE).** The Forecast role is
26/111 with a mean calibration error of 0.68 - it assigns, on average, only
32% of its probability mass to what actually happens. The Gate is 49/88 on
money. QA and News/Sentiment are structurally unscorable and say so rather
than being credited for the trades that happened to win.

**Counterfactual policies (96 positions comparable across all nine).** Every
single policy comes back `NOT_SIGNIFICANT` after the sign test and
Benjamini-Hochberg correction. `DELAY_ENTRY_30M` looks strongest (+841 USDT
total, +100 on winners, bootstrap CI [2.88, 15.01] USDT per trade, p = 0.10)
and is still not a finding: with nine policies tested over one small book,
that p-value does not survive FDR control. The system's own live
breakeven-stop mechanism (`TIGHTEN_SL_AFTER_FAVORABLE`) is the worst policy
measured, at -183 USDT.

**Experience Memory.** 42 patterns: 26 NOISE, 16 INSUFFICIENT_DATA, **zero**
EDGE, WEAK_EDGE or FAILURE_PATTERN. Every prediction-error lesson is therefore
recorded as OBSERVATION ONLY and nothing downstream may act on it.

**Avoidable losses.** After enforcing the winner-damage bar, only the two
delayed-entry policies remain as alternatives that both help the book and take
nothing out of winning trades: 1 152 USDT of improvement across 47 trades,
with 99 of 146 trades having no better alternative at all.

**The honest conclusion:** this history is large enough to build the
infrastructure on and far too small to justify changing a single trading rule.
That is what every gate in the layer currently reports, and it is why the two
enforcement flags ship `false`.
