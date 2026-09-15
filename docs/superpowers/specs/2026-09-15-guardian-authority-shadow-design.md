# Guardian Authority Shadow/Observation Mode — Design Spec

**Spec of record:** the user's explicit 12-point instruction (2026-09-15, Swedish, verbatim preserved in the session record) is the binding requirement this spec argues from. Reproduced and resolved point-by-point below.

## Why this exists

Guardian Authority ("GODFATHER") was built, hardened (I1/I2/I3), and merged to `master` — fully reviewed, `authority_enabled: false` throughout. During a post-merge verification pass (2026-09-15) a structural fact was discovered: **with `active_heuristics_count == 0` (the real, current production state), `authority_enabled: true` is a pure no-op.** `evaluate_heuristics` sums matched heuristics' `adjustment`; with zero heuristics the sum is always `0.0`, which never clears `tighten_threshold`/`close_threshold`/`veto_threshold`, so every decision is `APPROVE`/`NO_ACTION`. And `NO_ACTION` decisions are never saved (`guardian/tick.py`: `if decision != "NO_ACTION":`) — so no data ever accumulates for Task 9's self-critique to learn from. This is a genuine cold-start deadlock: the feature as built cannot bootstrap itself into doing anything, ever, regardless of the flag.

The user explicitly refused the obvious workaround (hand-authoring a seed heuristic — "implementera inte en påhittad heuristik bara för att få GODFATHER att börja agera") and instead asked for a **structural, deterministic** fix: let GODFATHER observe and log hypothetical decisions on PAPER — including hypothetical `NO_ACTION` — without ever acting on real state, so real data accumulates for later, human-reviewed evaluation. LIVE and `authority_enabled` stay exactly as they are.

## Architectural precedent (why this is low-risk)

This is not new architecture. `crypto_trading/paper_trading/profit_protection_experiment.py` + `profit_protection_shadow_positions` (table) + `crypto_trading/performance/profit_protection_report.py` already solve the EXACT same shape of problem for a different feature: track a hypothetical intervention in parallel with a real, untouched position; compare the shadow's hypothetical outcome against the real position's own actual outcome (the "baseline" — trivially available because the real position was never touched); resolve and report once both sides are known. This spec adapts that proven, already-reviewed pattern to Guardian Authority's decision vocabulary. Where this spec's design and that precedent's design agree, the precedent wins — do not invent a fresh mechanism where an adapted one already exists and is proven.

## Global Constraints (binding, same rigor as the original Guardian Authority plan)

- Never modify `crypto_trading/paper_trading/position_opening.py`, `position_sizing.py`, `crypto_trading/gate/`, `crypto_trading/screening/`, any AI role `.md` file, `crypto_trading/config/risk_limits.yaml`, or any leverage/capital-limit config value anywhere.
- Never modify `crypto_trading/guardian/authority.py`'s pure decision engine (`decide_pre_entry`, `decide_open_position`, `evaluate_heuristics`, `heuristic_condition_matches`, `_compute_proposed_new_sl`) or `authority_live.py` — this feature REUSES those functions unmodified (same import, same call signature), never forks or edits them.
- Never call `open_position_for_candidate`, `tighten_position_stop_loss`, `apply_live_sl_tightening`, `place_stop_loss_order`, `cancel_order`, `set_leverage`, or `repo.save_guardian_observation(..., state="EXIT")` from anywhere in this feature's code. Shadow mode is read + its-own-table-write only — this is what makes proving "cannot open positions / cannot move SL / cannot close positions / cannot touch leverage or sizing" a structural, checkable-by-grep property rather than a behavioral promise.
- `authority_enabled` (the real-intervention flag, PAPER+LIVE) is untouched by this feature and stays `false`. This feature introduces a SEPARATE flag (`authority_shadow_enabled`) that never gates anything `authority_enabled` also gates.
- Shadow mode never runs against LIVE positions or LIVE execution state — same PAPER-only scoping `profit_protection_experiment.py` already uses (reads `open_positions`/`closed_positions` from the PAPER positions table only, exactly like the precedent).
- No new column, table, or code path may let heuristics DERIVED FROM SHADOW DATA reach the real decision engine (`guardian_authority_heuristics`, read by `evaluate_heuristics` inside `decide_pre_entry`/`decide_open_position`). This is a hard, structural, no-shared-table guarantee (see "Self-critique from shadow data" below) — not a filter that could be forgotten.
- TDD throughout. Full suite green before any task is considered done. Same 2 pre-existing/documented `profit_protection_enabled` baseline failures are not this feature's concern.

## Data model

### `guardian_authority_shadow_observations` (tick-time TIGHTEN_SL/CLOSE_EARLY/NO_ACTION shadow — one row per PAPER position)

Modeled on `profit_protection_shadow_positions`, adapted to Guardian Authority's own vocabulary (`guardian_authority_decisions`' field names for the decision-shaped fields, so a reader who already knows one table reads the other for free).

```
shadow_id           TEXT PRIMARY KEY   -- = position_id (1:1, unlike PP's multi-threshold shadow_id shape — GODFATHER has no parallel-threshold concept)
position_id         TEXT NOT NULL
candidate_id         TEXT NOT NULL
instrument           TEXT NOT NULL
status               TEXT NOT NULL     -- 'OBSERVING' -> 'DECIDED' -> 'RESOLVED', or 'ABANDONED' (same abandon-on-orphan discipline as PP shadow)
opened_at            TEXT NOT NULL     -- real position's opened_at
created_at           TEXT NOT NULL
updated_at           TEXT NOT NULL

-- Immutable once first set (requirement-10-style: registered BEFORE the outcome is known, never rewritten). Set the FIRST tick the hypothetical decision is not NO_ACTION, OR at position close if it was NO_ACTION for the position's entire life (see "One row, not one row per tick" below).
shadow_decision       TEXT              -- 'NO_ACTION' | 'TIGHTEN_SL' | 'CLOSE_EARLY', NULL while status='OBSERVING'
decided_at            TEXT
expected_outcome      TEXT
expected_direction    TEXT
confidence            REAL
factors_json          TEXT              -- the exact factors dict decide_open_position was called with, incl. guardian_state
proposed_new_sl       TEXT              -- only for a hypothetical TIGHTEN_SL

-- Running, tick-updated (same shape as PP shadow's mfe/mae fields)
mfe                   TEXT NOT NULL DEFAULT '0'
mae                   TEXT NOT NULL DEFAULT '0'

-- Baseline = what ACTUALLY happened to the real, untouched position. Backfilled once, at real position close.
actual_exit_reason     TEXT
actual_pnl_usdt        TEXT
actual_closed_at       TEXT

-- Resolution (Task 8's own established semantics, reused verbatim — see "Resolution" below)
expectation_correct    INTEGER          -- 0/1/NULL, same SQLite-boolean convention as guardian_authority_decisions
prediction_error       REAL             -- Brier component, I3's own formula reused verbatim: (confidence - (1.0 if expectation_correct else 0.0))**2

run_id                TEXT NOT NULL
```

### `guardian_authority_shadow_pre_entry_observations` (pre-entry veto shadow — one row per CONFIRMED candidate)

Simpler: no per-tick concern (pre-entry is evaluated once, at the same call site `maybe_open_position_for_candidate` already hooks). No state machine — a single INSERT at confirm time, resolved once (if a real position was opened for the candidate) that position closes.

```
shadow_id            TEXT PRIMARY KEY   -- = candidate_id
candidate_id         TEXT NOT NULL
instrument           TEXT NOT NULL
created_at           TEXT NOT NULL

shadow_decision      TEXT NOT NULL      -- 'APPROVE' | 'PRE_ENTRY_VETO' — set once, at INSERT time, immutable
expected_outcome     TEXT NOT NULL
expected_direction   TEXT NOT NULL
confidence           REAL NOT NULL
factors_json         TEXT NOT NULL

status               TEXT NOT NULL DEFAULT 'PENDING'  -- 'PENDING' -> 'RESOLVED' (only if a real position was actually opened for this candidate) — a shadow PRE_ENTRY_VETO never blocks the real open, so a position always exists to resolve against unless the real Gate itself rejected the candidate for unrelated reasons
position_id          TEXT               -- filled in once known, NULL until then
actual_exit_reason   TEXT
actual_pnl_usdt      TEXT
actual_closed_at     TEXT
expectation_correct  INTEGER            -- same convention; NULL forever if shadow_decision='APPROVE' (no counterfactual to score — matches Task 8's own PRE_ENTRY_VETO ruling)

run_id               TEXT NOT NULL
```

### `guardian_authority_shadow_heuristics` (self-critique-from-shadow output — see "Self-critique from shadow data")

Schema-identical to `guardian_authority_heuristics` (same columns: `heuristic_id`, `description`, `condition_json`, `adjustment`, `confidence`, `sample_size`, `updated_at`). Deliberately a SEPARATE table, not a flag on the real one — see rationale below.

## "One row, not one row per tick" — resolving requirement 2 structurally

The tick-time shadow observation is **position-level**, exactly like `profit_protection_shadow_positions` is position-level (one row per shadowed position, `advance_shadow` updates it in place every tick). Every tick:
1. Compute `decide_open_position(factors, guardian_state, position.stop_loss, position.simulated_fill_entry, CURRENT_heuristics, tighten_threshold, close_threshold)` — the exact real function, zero modification, using whatever heuristics currently exist (possibly zero).
2. Update `mfe`/`mae` unconditionally (cheap, matches PP shadow's own running-max/min pattern).
3. **Only write `shadow_decision`/`decided_at`/`expected_outcome`/`expected_direction`/`confidence`/`factors_json`/`proposed_new_sl` if `status == 'OBSERVING'`** (i.e., only once, ever, per position) **AND** the hypothetical decision is `TIGHTEN_SL` or `CLOSE_EARLY` — transition to `status='DECIDED'`. Every later tick, even if the hypothetical decision would again be non-`NO_ACTION`, is evaluated (for MFE/MAE) but never overwrites the already-decided row — this is the exact mechanism that makes "GODFATHER's own I2 finding" (a single position re-triggering TIGHTEN_SL every tick) structurally impossible to reproduce in shadow data: only the FIRST hypothetical intervention per position is ever recorded.
4. If the position closes while `status` is still `'OBSERVING'` (hypothetical decision was `NO_ACTION` every tick of its life), write the summary as a `NO_ACTION` decision AT CLOSE TIME (one write, not one per tick) — `shadow_decision='NO_ACTION'`, `factors_json` = the LAST tick's factors, `expected_direction='neutral'`, `confidence=1.0` (matches the real engine's own "no signal matched" default) — then transition straight to `status='RESOLVED'` (no separate intervention to wait for; the baseline backfill happens in the same call since the position's real outcome is already known at this point).
5. Once `status='DECIDED'` (a genuine hypothetical intervention was registered) and the real position subsequently closes, backfill `actual_exit_reason`/`actual_pnl_usdt`/`actual_closed_at` from the real position (same `compute_pnl` reuse `profit_protection_experiment.py`'s own backfill already established) and transition to `'RESOLVED'`.

This gives exactly one shadow_decision per position, registered before the position's real outcome is known (immutable, matching requirement-10's own established discipline), covering both "GODFATHER would have intervened" and "GODFATHER would have done nothing" — satisfying requirement 2 exactly, and without needing `intervention_applied`-style filtering at all (there is nothing to filter: the row count already equals the position count).

## Resolution — reusing Task 8's established semantics, not inventing new ones

For a `DECIDED` (`TIGHTEN_SL`) row: `expectation_correct = (expected_direction == "favorable") == (actual_pnl_usdt > 0)`, the identical sign-comparison `resolve_pending_decisions` already uses, for the identical reason (TIGHTEN_SL's `expected_direction` is a documented constant, so this reduces to a real win/loss comparison). `prediction_error` (I3's Brier component, reused verbatim): `(confidence - (1.0 if expectation_correct else 0.0)) ** 2`.

For a `DECIDED` (`CLOSE_EARLY`) row: same as Task 8's own ruling — `expectation_correct` stays `NULL` forever (no counterfactual-of-inaction mechanism exists; scoring it would silently mis-score a correct early close as wrong). `prediction_error` also `NULL` (it is only defined where `expectation_correct` is meaningfully computed).

For a `NO_ACTION`-at-close row: `expectation_correct`/`prediction_error` stay `NULL` — there is no prediction to score (matches `expected_direction='neutral'`'s own established meaning: "no directional prediction is being made").

For pre-entry `APPROVE`: `expectation_correct` stays `NULL` forever (same as Task 8's PRE_ENTRY_VETO ruling — a veto's correctness is a counterfactual this system has no infrastructure to evaluate; APPROVE's "correctness" is symmetric and equally unscoreable here).
For pre-entry `PRE_ENTRY_VETO` (shadow only — the real open is NEVER actually blocked): resolves once the real position (which DID open, since shadow never vetoes for real) closes — `expectation_correct` stays `NULL` too, same reasoning as Task 8's real PRE_ENTRY_VETO ruling (still no counterfactual-of-non-entry to evaluate against; the real position's outcome tells us what happened WITH entry, not what would have happened without it — scoring against it would be exactly the mis-scoring Task 8's ruling already rejected once).

## Self-critique from shadow data — structurally isolated from production strategy (requirement 9)

`update_heuristics_from_resolved_decisions` (Task 9, already reviewed/hardened by I1) is reused **by adaptation, not by modification**: a new function with the identical grouping/threshold/adjustment logic, reading `guardian_authority_shadow_observations` (`status='RESOLVED'`, `expectation_correct IS NOT NULL` — i.e., only resolved `TIGHTEN_SL` shadow rows, same scope note as the real Task 9) instead of `guardian_authority_decisions`, and writing to `guardian_authority_shadow_heuristics` instead of `guardian_authority_heuristics`.

**This is the one place this spec deliberately does NOT reuse the real function directly** — a shared function writing to two different tables based on a flag is exactly the kind of "one bug away from leaking into production" surface the Global Constraints exist to rule out. Two separate, small, near-identical functions in two separate tables is more code but a strictly stronger guarantee: `evaluate_heuristics`/`decide_pre_entry`/`decide_open_position` structurally cannot read `guardian_authority_shadow_heuristics` (they only ever call `repo.find_guardian_authority_heuristics()`, never `find_guardian_authority_shadow_heuristics()` — a fact checkable by grep, not by trusting a conditional). Promoting a shadow-learned heuristic into real use is, and stays, a distinct, explicit, future, human action — not a code path that exists yet. That is the literal meaning of "no automatic change to production strategy or risk code."

## Wiring

- New `GuardianConfig.authority_shadow_enabled: bool = False` (`config/loader.py`, alongside the existing `authority_*` fields), documented in `guardian.yaml` the same way `authority_enabled` already is (present, set to `false`, explained).
- New module `crypto_trading/paper_trading/guardian_authority_shadow.py` (mirrors `profit_protection_experiment.py`'s shape: `seed_shadow_for_position`, `advance_shadow`, `_close_shadow`/resolve, `run_guardian_authority_shadow_tick(repo, open_positions, closed_positions, price_lookup, now, settings, run_id)`), called from the same place `run_profit_protection_experiment_tick` is called (monitoring loop, after `close_triggered_positions`), wrapped in its own try/except, gated by `settings.guardian.authority_shadow_enabled`.
- Pre-entry shadow hooks the exact same call site `maybe_open_position_for_candidate` already occupies (`replay.py`, `recovery_sweep.py`) — a new, tiny, always-safe sibling call (`maybe_record_pre_entry_shadow`, gated by `authority_shadow_enabled`, never by `authority_enabled`) that computes `decide_pre_entry` hypothetically and writes one row, then falls through to whatever `maybe_open_position_for_candidate` itself already decided (unchanged) — shadow observation is purely additive, never a gate.
- New `crypto_trading/performance/guardian_authority_shadow_report.py` (modeled on `profit_protection_report.py`/`guardian_authority_report.py`'s own established pattern): per-decision-type counts, resolved/pending, win rate + Brier score (reusing `guardian_authority_report.py`'s own already-reviewed computation shape) computed over shadow data, plus `active_shadow_heuristics_count`.

## Acceptance criteria

1. With `authority_shadow_enabled: true` and `authority_enabled: false` (the state this phase activates), real PAPER positions are byte-identically unaffected — no write to `positions`, no write to `guardian_authority_decisions`, no write to `guardian_authority_heuristics`, no `open_position_for_candidate`/`tighten_position_stop_loss`/`cancel_order`/`place_stop_loss_order`/`set_leverage` call anywhere in the new code (grep-provable).
2. LIVE (`authority_enabled`, `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED`, `live_execution.yaml`'s hard limits) is completely untouched — no file this feature touches overlaps LIVE's own files.
3. A position sitting in `OBSERVING` for its entire life, even with empty heuristics, produces exactly one shadow row (not zero, not N) once it closes.
4. A position whose hypothetical decision crosses `tighten_threshold`/`close_threshold` on tick 5 (say) produces exactly one `DECIDED` row (registered at tick 5, immutable), regardless of how many further ticks the position lives.
5. `guardian_authority_shadow_heuristics` can accumulate real entries once enough shadow data resolves — and `evaluate_heuristics`/`decide_pre_entry`/`decide_open_position` never read that table (grep-provable: the only two call sites of `find_guardian_authority_heuristics()` are inside `decide_pre_entry`'s and `decide_open_position`'s own callers in `authority.py`/`tick.py`, and neither ever calls a `_shadow_` variant).
6. `python -m crypto_trading.performance.guardian_authority_shadow_report` runs read-only and reports real counts once shadow data exists.
7. Full test suite green, TDD throughout, same 2 pre-existing/documented baseline failures only.
