# Guardian Authority ("Godfather") Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Guardian Authority exactly per the approved spec — an extension of Guardian that can veto a signal before entry, tighten (never loosen) a stop-loss on an open position, or close a position early, using its own self-maintained, prediction-calibrated memory, on both LIVE and PAPER, with zero changes to entry/Gate/Risk/AI/sizing/leverage/capital-limit logic, and zero autonomous modification of production strategy code.

**Architecture:** New module `crypto_trading/guardian/authority.py` (+ a small `crypto_trading/guardian/authority_live.py` for the LIVE-specific SL-tightening order mechanics, mirroring how `live_profit_protection.py` stayed separate from `paper_trading/live_execution.py`). Two new call sites (not new logic inside the files they call from): the pre-entry veto wraps `open_position_for_candidate` at its two existing call sites (`replay.py`, `recovery_sweep.py`); the tick-time decision runs inside `guardian/tick.py::process_one_position`, strictly after the existing deterministic state computation, never replacing it.

**Spec:** `docs/superpowers/specs/2026-09-14-guardian-authority-design.md` — read this in full before starting any task. It is the binding authority.

## Rulings on the spec's "Open questions for the implementation plan to resolve" (decided here, not deferred further)

- **Heuristics representation:** a small, deterministic, inspectable table (`guardian_authority_heuristics`) — never an opaque model. Each row is one named rule: a structured `condition` (reusing the EXACT factor vocabulary already established this session — `trigger_reasons`, `candidate_score` bucket, `rsi_30m` bucket, etc. — the same fields the manual historical analysis already used), a numeric `adjustment` (how much this rule should push the decision toward veto/tighten/close vs. no-action), a `confidence`, `sample_size`, and `last_updated`. Read fresh, in full, at the start of every decision. Updated ONLY by the self-critique step (Task 9), never by the decision-making step itself (read-only there).
- **Self-critique cadence:** opportunistic, not a new scheduler — runs as the last step of the SAME tick that just resolved at least one new decision (i.e., "after resolving N≥1 decisions this tick, re-derive heuristics from all resolved decisions so far"). No new background loop/thread; reuses whatever thread already calls the tick.
- **LLM involvement:** matches Guardian's own existing, already-reviewed pattern exactly — the decision itself (veto/tighten/close/no-action) is fully deterministic (heuristics table + existing Guardian factors), never LLM-decided. An LLM call is OPTIONAL and budget-gated (reuse `_budget_allows_one_more_call`'s exact pattern) purely to generate the human-readable `reasoning`/`expected_outcome` text for a row that's already been decided deterministically — identical division of labor to Guardian's existing `should_invoke_ai`/AI-narrates-a-settled-decision design. This keeps the actual decision boundary fully testable and deterministic.
- **CLOSE_EARLY mechanism:** does NOT need any new closing code. It constructs and saves a `GuardianObservation` with `state="EXIT"` via the existing, unmodified `repo.save_guardian_observation()` — the exact same downstream mechanism (`position_closing.py`'s guardian_exit handling, `live_execution.py::close_guardian_exit_positions`) already picks this up with zero changes, regardless of whether Guardian's own deterministic tick or Guardian Authority produced the EXIT observation. This is the cleanest possible reuse and is now reflected in Task 7 below.

## Global Constraints

- Never modify `crypto_trading/paper_trading/position_opening.py`, `position_sizing.py`, `crypto_trading/gate/`, `crypto_trading/screening/`, any AI role `.md` file, `crypto_trading/config/risk_limits.yaml`, or any leverage/capital-limit config value anywhere.
- Never call `BingXLiveTradingConnector.set_leverage`, never import `position_sizing.py`, never call any position-opening/claim function directly (only the wrapped wrapper may call `open_position_for_candidate`, and only to let it proceed unchanged on APPROVE).
- Every SL-tightening write path asserts `new_sl > current_sl` (LONG-only, matches this codebase's sole direction) immediately before the write, using a freshly-read current value — never a cached/assumed one. Violation refuses and logs, never clamps.
- `CLOSE_EARLY` only ever writes via `repo.save_guardian_observation()` with `state="EXIT"` — no new closing code, no direct calls to any close/cancel-order function.
- Default OFF behind a new config flag (`guardian.authority_enabled` — new field, default `False`) — landing this code changes zero runtime behavior until explicitly activated later.
- TDD throughout. Every task's tests must pass before commit; full suite green before the task is considered done.
- Reuses, never duplicates: Guardian's existing factor computation (`guardian/deterministic.py`), evidence fetching (`guardian/data.py`), AI budget-gating pattern (`guardian/tick.py::_budget_allows_one_more_call`), and LIVE Profit Protection's exact add-before-remove SL-replacement sequence and connector primitives (`place_stop_loss_order`/`cancel_order`).

---

### Task 1: `guardian_authority_decisions` table and repository CRUD

**Files:**
- Modify: `crypto_trading/storage/db.py`, `crypto_trading/storage/repository.py`
- Test: `tests/crypto_trading/storage/test_repository_guardian_authority.py`

**Interfaces:**
- Produces:
  - `save_guardian_authority_decision(decision_id, position_id, candidate_id, decision_type, decided_at, reasoning, expected_outcome, expected_direction, confidence, run_id, old_sl=None, new_sl=None) -> bool` (INSERT OR IGNORE on `decision_id` PK — idempotent, matches every other claim-style write in this codebase)
  - `get_guardian_authority_decision(decision_id) -> dict | None`
  - `find_pending_guardian_authority_decisions() -> list[dict]` (`outcome_status = 'PENDING'`, for the resolution pass)
  - `resolve_guardian_authority_decision(decision_id, actual_exit_reason, actual_pnl_usdt, expectation_correct, resolved_at) -> None` (sets `outcome_status = 'RESOLVED'` and the actual-outcome columns; must NEVER touch `expected_outcome`/`expected_direction`/`confidence`/`decided_at` — those are immutable once written, per spec requirement 10)

**Design:** Exact schema from the spec's "Memory / self-improvement" section. Mirror `live_profit_protection`'s table/CRUD style (plain sqlite3, `.isoformat()` datetimes, per-write commit, INSERT OR IGNORE for the claim-shaped insert).

- [ ] **Step 1: Write the failing tests**

Cover: (a) `save_...` is idempotent (second call for the same `decision_id` returns `False`, does not overwrite); (b) a fresh row has `outcome_status='PENDING'`; (c) `find_pending_...` returns only PENDING rows, not RESOLVED ones; (d) `resolve_...` sets the actual-outcome fields and flips status to RESOLVED; (e) **the specific, spec-mandated immutability test**: call `resolve_...`, then assert `expected_outcome`/`expected_direction`/`confidence`/`decided_at`/`reasoning` are byte-identical to what was written at `save_...` time — this is the single most important test in this task, it directly proves requirement 10.

- [ ] **Step 2: Run to verify failure**

- [ ] **Step 3: Implement**

Add the table (`CREATE TABLE IF NOT EXISTS guardian_authority_decisions (...)`, exact columns from the spec) to `db.py`, and the four methods above to both the `Repository` Protocol and `SQLiteRepository`.

- [ ] **Step 4: Run to verify pass**

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/storage/db.py crypto_trading/storage/repository.py tests/crypto_trading/storage/test_repository_guardian_authority.py
git commit -m "feat(crypto-trading): add guardian_authority_decisions table and repository CRUD"
```

---

### Task 2: `guardian_authority_heuristics` table and repository CRUD

**Files:**
- Modify: `crypto_trading/storage/db.py`, `crypto_trading/storage/repository.py`
- Test: `tests/crypto_trading/storage/test_repository_guardian_authority.py` (same file, add to it)

**Interfaces:**
- Produces:
  - `find_guardian_authority_heuristics() -> list[dict]` (all rows — the full, small heuristics set, read fresh every decision per the plan's ruling above)
  - `upsert_guardian_authority_heuristic(heuristic_id, description, condition_json, adjustment, confidence, sample_size, updated_at) -> None` (replace-by-id; the self-critique step's only write)

**Design:**

```sql
CREATE TABLE IF NOT EXISTS guardian_authority_heuristics (
    heuristic_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    condition_json TEXT NOT NULL,  -- structured match condition, e.g. {"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.1}
    adjustment REAL NOT NULL,      -- signed: positive pushes toward veto/tighten/close, negative pushes toward no-action
    confidence REAL NOT NULL,
    sample_size INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
```

- [ ] **Step 1: Write the failing tests**

`upsert_...` creates a new row; a second `upsert_...` with the same `heuristic_id` REPLACES it (not a second row — this is a deliberate `INSERT OR REPLACE`, unlike Task 1's `INSERT OR IGNORE`, since heuristics are meant to evolve, decisions are not); `find_...` returns all rows.

- [ ] **Step 2-5:** as Task 1's pattern. Commit message: `feat(crypto-trading): add guardian_authority_heuristics table and repository CRUD`.

---

### Task 3: Pure decision engine (no I/O)

**Files:**
- Create: `crypto_trading/guardian/authority.py` (this task starts the file)
- Test: `tests/crypto_trading/guardian/test_authority.py`

**Interfaces:**
- Produces:
  - `evaluate_heuristics(factors: dict, heuristics: list[dict]) -> tuple[float, list[str]]` — sums matching heuristics' `adjustment` values (a heuristic "matches" if the position/candidate's own factor values satisfy its `condition_json`), returns the total score plus which heuristic_ids matched (for the `reasoning` text later).
  - `decide_pre_entry(candidate_evidence: dict, heuristics: list[dict], veto_threshold: float) -> tuple[str, str, str, float]` — returns `(decision, expected_outcome_text, expected_direction, confidence)` where `decision` is `"APPROVE"` or `"PRE_ENTRY_VETO"`. Deterministic: `"PRE_ENTRY_VETO"` iff the summed heuristic score exceeds `veto_threshold`.
  - `decide_open_position(position_factors: dict, guardian_state: str, current_sl: Decimal, entry: Decimal, heuristics: list[dict], tighten_threshold: float, close_threshold: float) -> tuple[str, str, str, float, Decimal | None]` — returns `(decision, expected_outcome_text, expected_direction, confidence, proposed_new_sl_or_None)` where `decision` is `"NO_ACTION"`, `"TIGHTEN_SL"`, or `"CLOSE_EARLY"`. **`TIGHTEN_SL` may only be returned with a `proposed_new_sl` that is strictly greater than `current_sl` (LONG) — if the computed candidate value is not strictly greater, this function itself downgrades the decision to `"NO_ACTION"` rather than ever returning an invalid tightening. This is the first of two independent enforcement points (the second is the write-path assertion in Task 5/6) — belt and suspenders, per this codebase's own established double-check convention (see LIVE PP's own two independent verification reads).**

**Design:** This is the module's only genuinely novel logic, and it has zero I/O — every input is a plain value/dict, every output is a plain tuple. This makes it exhaustively unit-testable without any database or connector mocking at all.

- [ ] **Step 1: Write the failing tests**

Cover at minimum: heuristics matching (a condition matches/doesn't match a given factor set); scoring (multiple matching heuristics sum correctly); pre-entry APPROVE below threshold, VETO above; open-position NO_ACTION/TIGHTEN_SL/CLOSE_EARLY at their respective thresholds; **the critical safety test**: `decide_open_position` given inputs that would compute a `proposed_new_sl` LOWER than `current_sl` (a deliberately-adversarial heuristics fixture) returns `"NO_ACTION"`, never an invalid `"TIGHTEN_SL"` — this test must exist and must be adversarially constructed, not just a happy-path check.

- [ ] **Step 2-4:** as usual TDD cycle.

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/guardian/authority.py tests/crypto_trading/guardian/test_authority.py
git commit -m "feat(crypto-trading): add Guardian Authority pure decision engine"
```

---

### Task 4: PAPER stop-loss tightening (repository write path)

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Test: `tests/crypto_trading/storage/test_repository_guardian_authority.py`

**Interfaces:**
- Produces: `tighten_position_stop_loss(position_id: str, new_stop_loss: Decimal, updated_at: datetime) -> bool` — updates `positions.stop_loss` **only if** `new_stop_loss > positions.stop_loss` in the SAME atomic SQL statement (a `WHERE new_stop_loss > stop_loss`-shaped guard, not a read-then-write race), returns whether the update actually applied. This is a second, DB-level enforcement of the same invariant Task 3 already enforces in application code — belt and suspenders.

**Design:** New, small, single-purpose method. Never touches `target`, `size`, or any other column. This is the only place in the whole plan that writes a PAPER position's `stop_loss` after creation — grep the rest of the codebase to confirm no other method currently does this (expected: none does, PAPER positions' stop_loss is currently immutable after open).

- [ ] **Step 1: Write the failing tests**

A strictly-higher new stop_loss succeeds, row reflects it. An equal-or-lower new stop_loss is REFUSED (returns `False`, row unchanged) — tested explicitly, not assumed. A non-existent `position_id` returns `False`, no error.

- [ ] **Step 2-5:** as usual. Commit: `feat(crypto-trading): add DB-enforced PAPER stop-loss tightening (never-loosen guard)`.

---

### Task 5: LIVE stop-loss tightening (isolated module, reuses PP's connector primitives)

**Files:**
- Create: `crypto_trading/guardian/authority_live.py`
- Test: `tests/crypto_trading/guardian/test_authority_live.py`
- Modify: `crypto_trading/storage/db.py`, `crypto_trading/storage/repository.py` (new claim table, mirroring `live_profit_protection`'s exact shape)

**Interfaces:**
- New table `guardian_authority_live_sl_actions` — same columns and same idempotency/state-machine shape as `live_profit_protection` (position_id PK, status, old_sl_order_id, old_sl_price, new_sl_client_order_id, new_sl_order_id, claimed_at, updated_at, last_error) — a SEPARATE table, never shared with `live_profit_protection`, so the two mechanisms can never collide on the same row.
- Produces: `apply_live_sl_tightening(repo, connector, position_id, instrument, new_sl, run_id, now) -> None` — **reuses the EXACT same add-before-remove sequence as `live_profit_protection.py`'s `_run_claimed_sequence`/`_finalize_verified_active_new_sl`**: verify real position, identify exactly one existing STOP_MARKET, place new SL at `new_sl` (never at a hardcoded breakeven — this is the one parameter difference from PP), verify NEW/PENDING, re-verify position still open, only then cancel old. **Read `crypto_trading/paper_trading/live_profit_protection.py` in full before implementing this task — copy its proven sequence structure and failure-mode handling (`ABORTED_AMBIGUOUS_SL`, `UNCERTAIN_NEW_SL_STATUS`, `REPLACEMENT_PARTIAL`, the pre-cancel position re-check, the per-position isolation, the restart/crash recovery cases) rather than re-deriving it from scratch — this exact sequence has already been through 2 rounds of deep adversarial review and one Critical bug fix; do not introduce a fresh, unreviewed variant of the same logic.**

**Design:** This task is explicitly a **reuse-and-adapt**, not a redesign. The single semantic difference from `live_profit_protection.py`: the target SL price is a caller-supplied `new_sl` parameter instead of the position's own `avgPrice` (breakeven). Every safety property (add-before-remove, position-liveness re-check immediately before cancel, exhaustive failure-mode statuses, restart recovery) must be preserved identically.

- [ ] **Step 1: Write the failing tests**

Mirror `test_live_profit_protection.py`'s own test list as closely as possible: normal success (new SL strictly tighter, placed, verified, old cancelled); new SL rejected by exchange; old-cancel fails → `REPLACEMENT_PARTIAL`-equivalent status; new SL status unknown; zero/multiple existing SL found; position closes mid-sequence; restart recovery for a claimed-but-interrupted row. **Plus two new tests specific to this module**: (a) `apply_live_sl_tightening` is never called at all (by its own caller, Task 7) unless `new_sl > current_sl` was already confirmed by Task 3 — but as defense-in-depth, this module ALSO independently re-verifies `new_sl > <freshly-read current SL from the exchange>` before ever placing an order, and refuses (no write, status `ABORTED_INVALID_TIGHTENING` or similar) if that check fails, even if the caller somehow got it wrong; (b) **spec test item 5 — racing Profit Protection**: seed a position that ALREADY has an in-flight or completed `live_profit_protection` claim on the same `position_id`, then run `apply_live_sl_tightening` on it — confirm the two mechanisms' separate claim tables (`guardian_authority_live_sl_actions` vs `live_profit_protection`) never collide (each claims its own row independently), and confirm the SL-identification step (`get_open_orders` + `STOP_MARKET` filter) correctly finds whatever the CURRENT real SL is at that moment (whether PP or Guardian Authority put it there last) rather than assuming a stale value — i.e. tightening always ratchets from truth, never from an assumption about which mechanism acted most recently.

- [ ] **Step 2-5:** as usual, but budget more review time — this is the second most safety-critical task in the plan after Task 3's core engine, given it places real LIVE orders.

Commit: `feat(crypto-trading): add LIVE stop-loss tightening for Guardian Authority, reusing PP's add-before-remove sequence`.

---

### Task 6: Pre-entry veto wiring

**Files:**
- Modify: `crypto_trading/paper_trading/replay.py`, `crypto_trading/paper_trading/recovery_sweep.py`
- Test: `tests/crypto_trading/paper_trading/test_replay.py`, `tests/crypto_trading/paper_trading/test_recovery_sweep.py`

**Interfaces:**
- Consumes: Task 3's `decide_pre_entry`, Task 1's `save_guardian_authority_decision`, Task 2's `find_guardian_authority_heuristics`.
- Produces: a small wrapper in `guardian/authority.py`, e.g. `maybe_open_position_for_candidate(repo, candidate, risk_limits, reference_price, opened_at, run_id, settings) -> Position | None` — if `settings.guardian.authority_enabled` is `False`, calls `open_position_for_candidate` directly (byte-identical to today). If `True`: runs `decide_pre_entry`, logs the decision (VETO → save the decision row and return `None`, never calling `open_position_for_candidate` at all; APPROVE → proceed to call it exactly as before).

**Design:** `replay.py:213` and `recovery_sweep.py:58` each change their ONE call from `open_position_for_candidate(...)` to `maybe_open_position_for_candidate(...)` with the same arguments plus `settings`/`run_id`. Neither file's surrounding logic changes.

- [ ] **Step 1: Write the failing tests**

Flag `False` (default): behavior byte-identical to before (existing tests in both files must still pass unmodified — this IS the test). Flag `True`, heuristics approve: same. Flag `True`, heuristics veto: `open_position_for_candidate` is never called (spy/mock assertion), no `positions` row created, a `PRE_ENTRY_VETO` decision row exists with a non-null `expected_outcome`.

- [ ] **Step 2-5:** as usual. Commit: `feat(crypto-trading): wire Guardian Authority pre-entry veto into replay/recovery_sweep, default off`.

---

### Task 7: Tick-time decision wiring (TIGHTEN_SL / CLOSE_EARLY)

**Files:**
- Modify: `crypto_trading/guardian/tick.py`
- Test: `tests/crypto_trading/guardian/test_tick.py`

**Interfaces:**
- Consumes: Task 3's `decide_open_position`, Task 4's `tighten_position_stop_loss` (PAPER), Task 5's `apply_live_sl_tightening` (LIVE — called only when the position has an ACTIVE `live_executions` row, i.e. it's a real LIVE position, exactly the same check Profit Protection itself uses), Task 1's decision-saving, `repo.save_guardian_observation` for `CLOSE_EARLY` (per this plan's ruling above — no new closing code).

**Design:** Inside `process_one_position`, immediately after `new_state = classify_guardian_state(...)` (line ~86 of the current file) and BEFORE the existing `repo.save_guardian_observation(observation)` call at the end — insert, gated by `settings.guardian.authority_enabled`:

```python
if settings.guardian.authority_enabled:
    heuristics = repo.find_guardian_authority_heuristics()
    decision, expected_outcome, expected_direction, confidence, proposed_sl = decide_open_position(
        factors, new_state, position.stop_loss, position.simulated_fill_entry, heuristics,
        settings.guardian.authority_tighten_threshold, settings.guardian.authority_close_threshold,
    )
    if decision != "NO_ACTION":
        decision_id = f"ga:{position.position_id}:{now.isoformat()}"
        repo.save_guardian_authority_decision(
            decision_id, position.position_id, position.candidate_id, decision, now,
            reasoning=f"matched heuristics: {matched_ids}", expected_outcome=expected_outcome,
            expected_direction=expected_direction, confidence=confidence, run_id=run_id,
            old_sl=position.stop_loss, new_sl=proposed_sl,
        )
        if decision == "TIGHTEN_SL":
            live_row = repo.get_live_execution(position.position_id)
            if live_row is not None and live_row["phase"] == "ACTIVE":
                apply_live_sl_tightening(repo, live_connector, position.position_id, position.instrument, proposed_sl, run_id, now)
            else:
                repo.tighten_position_stop_loss(position.position_id, proposed_sl, now)
        elif decision == "CLOSE_EARLY":
            repo.save_guardian_observation(GuardianObservation(
                observation_id=f"ga-exit:{position.position_id}:{now.isoformat()}",
                position_id=position.position_id, observed_at=now, state="EXIT",
                decay_score=decay_score, progress_ratio=progress_ratio, unrealized_pnl=unrealized_pnl,
                factors={name: float(value) for name, value in factors.items()}, run_id=run_id,
            ))
```

(Exact variable names/threading of `live_connector` through this call chain is the implementer's call — `process_one_position`/`run_guardian_tick_body` currently take a `GuardianDataSource` for market data, not a LIVE order-placement connector; this task must decide how `apply_live_sl_tightening`'s `BingXLiveTradingConnector` reaches this call site, e.g. an additional optional parameter threaded through from `run.py`'s guardian thread construction, defaulting to `None` when LIVE isn't enabled. Document the choice in the task report.)

- [ ] **Step 1: Write the failing tests**

Flag off: byte-identical to today (all existing `guardian/tick.py` tests must still pass unmodified). Flag on, heuristics → NO_ACTION: unchanged behavior, existing `guardian_observations` write happens exactly as before, no `guardian_authority_decisions` row. Flag on → TIGHTEN_SL on a PAPER position: `tighten_position_stop_loss` called, position's stop_loss updated. Flag on → TIGHTEN_SL on an ACTIVE LIVE position: `apply_live_sl_tightening` called instead (not the PAPER path). Flag on → CLOSE_EARLY: a `state="EXIT"` `GuardianObservation` is saved via the existing method, decision row saved.

- [ ] **Step 2-5:** as usual. Commit: `feat(crypto-trading): wire Guardian Authority tick-time decisions into guardian/tick.py, default off`.

---

### Task 8: Resolution pass

**Files:**
- Modify: `crypto_trading/guardian/authority.py`
- Test: `tests/crypto_trading/guardian/test_authority.py`

**Interfaces:**
- Produces: `resolve_pending_decisions(repo, now) -> int` (returns count resolved) — for each `find_pending_guardian_authority_decisions()` row, looks up the position (`repo.get_position(position_id)`); if still open, skip (leave PENDING); if closed, compute `actual_exit_reason`/`actual_pnl_usdt` from the position's own stored fields (reuse `compute_pnl` from `paper_trading.execution`, exactly like every other PnL computation in this codebase — never a new formula), compute `expectation_correct` by comparing `expected_direction` to the sign of the actual P/L, call `repo.resolve_guardian_authority_decision(...)`.

- [ ] **Step 1: Write the failing tests**

A PENDING decision whose position is still open → untouched, stays PENDING. A PENDING decision whose position closed profitably with `expected_direction="favorable"` → resolved, `expectation_correct=True`. Same position, `expected_direction="unfavorable"` → resolved, `expectation_correct=False`. **Re-run the exact same immutability test from Task 1** at this integration level too: resolving never changes `expected_outcome`/`expected_direction`/`confidence`.

- [ ] **Step 2-5:** as usual. Commit: `feat(crypto-trading): add Guardian Authority decision resolution pass`.

---

### Task 9: Self-critique / heuristics update

**Files:**
- Modify: `crypto_trading/guardian/authority.py`, `crypto_trading/guardian/tick.py` (call site, after Task 8's resolution pass runs in the same tick)
- Test: `tests/crypto_trading/guardian/test_authority.py`

**Interfaces:**
- Produces: `update_heuristics_from_resolved_decisions(repo, now) -> int` (returns count of heuristics upserted) — reads ALL resolved decisions (not just newly-resolved ones — simplest correct approach, matches "opportunistic, re-derive from all resolved so far" per this plan's ruling), groups by the same factor vocabulary used in the manual historical analysis this session already did (trigger_reasons, candidate_score bucket, etc. — reuse those exact bucket boundaries, don't invent new ones), computes each group's `expectation_correct` rate, and upserts a heuristic row per group whose sample size and miscalibration are large enough to be worth encoding (implementer's call on the exact minimum-sample-size threshold — document the reasoning, keep it conservative given this session's own repeated lesson about small-n overfitting).

**Design:** This is deliberately the least novel task — it's the SAME kind of bucketed win-rate/expectancy analysis this conversation already did manually (and Detective already does per-batch), just automated and applied to Guardian Authority's own decision history instead of raw trade history.

- [ ] **Step 1: Write the failing tests**

A fixture of resolved decisions with an obvious miscalibration pattern (e.g. all `TIGHTEN_SL` decisions matching one condition were wrong) produces a heuristic row with a negative `adjustment` for that condition. Too-small sample size does NOT produce a heuristic update (test the threshold explicitly). Running twice with the same data is idempotent (same heuristic_id gets replaced, not duplicated — reuses Task 2's `upsert` semantics).

- [ ] **Step 2-5:** as usual. Commit: `feat(crypto-trading): add Guardian Authority self-critique heuristics update`.

---

### Task 10: Config flag, production-isolation tests, and final verification

**Files:**
- Modify: `crypto_trading/config/loader.py`, `crypto_trading/config/guardian.yaml`
- Test: `tests/crypto_trading/config/test_loader.py`, `tests/crypto_trading/guardian/test_authority.py` (isolation test)

**Interfaces:**
- Produces: `GuardianConfig.authority_enabled: bool` (default `False`), `.authority_veto_threshold: float`, `.authority_tighten_threshold: float`, `.authority_close_threshold: float` (defaults: implementer's reasonable choice, document it — these are NOT tuned/activated by this plan, only wired with safe, inert-by-default values).

- [ ] **Step 1: Write the failing tests**

Config defaults load correctly, `authority_enabled` is `False`. **Production-isolation tests** (same discipline as Tier 1/LIVE-PP): `crypto_trading/guardian/authority.py` and `authority_live.py` never import `position_sizing.py`, never reference `set_leverage`, never call `open_position_for_candidate` directly (only via the wrapper in Task 6), never import anything from `crypto_trading/backtest/`, and never reference any capital-limit field name (`starting_capital_usdt`, `max_total_exposure_pct`, `margin_per_trade_usdt`, `max_concurrent_positions`) anywhere in either file — the full Global Constraints/req-9 checklist, each item traced to an explicit assertion in this test, not just the leverage/sizing subset.

- [ ] **Step 2-5:** as usual. Commit: `feat(crypto-trading): add Guardian Authority config flag, default off`.

- [ ] **Step 6 (no code, verification only):** Full suite green. `git diff --stat` against this plan's base commit confirms only the files named across Tasks 1-10 changed — explicitly confirm `position_opening.py`, `position_sizing.py`, `crypto_trading/gate/`, `crypto_trading/screening/`, `risk_limits.yaml`, and `crypto_trading/backtest/` are ALL absent from the diff. Report to the user: files changed, test results, confirmation `authority_enabled` defaults to `False` end-to-end, and that this plan does NOT activate anything — activation is a separate, later, explicit decision, matching every other real-money-adjacent feature this session.
