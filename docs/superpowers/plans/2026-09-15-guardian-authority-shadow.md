# Guardian Authority Shadow/Observation Mode — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Let Guardian Authority observe and log hypothetical PAPER decisions — including hypothetical `NO_ACTION` — even with zero learned heuristics, without ever touching real position/order state, closing the cold-start deadlock documented in the spec below. `authority_enabled` (real interventions, PAPER+LIVE) and everything LIVE stays exactly as-is throughout.

**Spec:** `docs/superpowers/specs/2026-09-15-guardian-authority-shadow-design.md` — read this in full before starting any task. It is the binding authority; read it once, not per-task (it explains WHY each schema/wiring choice was made — task briefs below give WHAT to build).

**Architecture:** Direct adaptation of the already-proven `paper_trading/profit_protection_experiment.py` + `profit_protection_shadow_positions` shadow-tracking pattern (read that file in full before Task 4 — it is your reference implementation, not merely inspiration). Two new observation tables (tick-time + pre-entry), a new module mirroring `profit_protection_experiment.py`'s shape, a new self-critique-from-shadow function writing to a THIRD, separate table the real decision engine never reads, and a new read-only report.

## Global Constraints

(Reproduced from the spec — copy these into every task reviewer's dispatch verbatim.)

- Never modify `crypto_trading/paper_trading/position_opening.py`, `position_sizing.py`, `crypto_trading/gate/`, `crypto_trading/screening/`, any AI role `.md` file, `crypto_trading/config/risk_limits.yaml`, or any leverage/capital-limit config value anywhere.
- Never modify `crypto_trading/guardian/authority.py`'s pure decision engine (`decide_pre_entry`, `decide_open_position`, `evaluate_heuristics`, `heuristic_condition_matches`, `_compute_proposed_new_sl`) or `authority_live.py` — reuse unmodified, same import, same call signature.
- Never call `open_position_for_candidate`, `tighten_position_stop_loss`, `apply_live_sl_tightening`, `place_stop_loss_order`, `cancel_order`, `set_leverage`, or `repo.save_guardian_observation(..., state="EXIT")` from any file this plan creates or touches.
- `authority_enabled` stays untouched, `false`, throughout. This plan's new flag (`authority_shadow_enabled`) never gates anything `authority_enabled` also gates, and vice versa.
- Shadow mode only ever reads/writes PAPER positions (`open_positions`/`closed_positions`, same lists `profit_protection_experiment.py` already receives) — never LIVE.
- `guardian_authority_shadow_heuristics` (Task 9) is a separate table from `guardian_authority_heuristics`. `evaluate_heuristics`/`decide_pre_entry`/`decide_open_position` must never be given a reason to read it — no shared read path, no flag-based branch in a shared function.
- TDD throughout. Full suite green before each task's commit. The 2 pre-existing/documented `profit_protection_enabled` baseline failures are not this plan's concern.

## File Structure

- Create: `crypto_trading/storage/db.py` migrations for 3 new tables (shadow_observations, shadow_pre_entry_observations, shadow_heuristics).
- Modify: `crypto_trading/storage/repository.py` — CRUD for all 3 tables (Protocol + `SQLiteRepository`).
- Modify: `crypto_trading/config/loader.py`, `crypto_trading/config/guardian.yaml` — new `authority_shadow_enabled` flag.
- Create: `crypto_trading/paper_trading/guardian_authority_shadow.py` — tick-time shadow seed/advance/resolve.
- Modify: `crypto_trading/monitoring_loop.py` — wire the tick-time shadow call.
- Modify: `crypto_trading/guardian/authority.py` — add `maybe_record_pre_entry_shadow` (pre-entry shadow, additive, own function).
- Modify: `crypto_trading/paper_trading/replay.py`, `crypto_trading/paper_trading/recovery_sweep.py` — call the new pre-entry shadow hook alongside the existing `maybe_open_position_for_candidate` call.
- Create: `crypto_trading/performance/guardian_authority_shadow_report.py`.
- Tests: one new test file per new module/table, plus production-isolation tests (Task 11).

---

## Task 1: `guardian_authority_shadow_observations` table + repository CRUD

**Files:** `crypto_trading/storage/db.py`, `crypto_trading/storage/repository.py`, `tests/crypto_trading/storage/test_repository_guardian_authority_shadow.py`

**Interfaces (produces):**
- `seed_guardian_authority_shadow(shadow_id, position_id, candidate_id, instrument, opened_at, created_at) -> bool` — INSERT OR IGNORE, `status='OBSERVING'`, `mfe='0'`, `mae='0'`, all decision fields NULL.
- `get_guardian_authority_shadow(shadow_id) -> dict | None`
- `find_open_guardian_authority_shadows() -> list[dict]` — `status IN ('OBSERVING', 'DECIDED')`
- `record_guardian_authority_shadow_tick(shadow_id, mfe: Decimal, mae: Decimal, updated_at) -> None` — UPDATE mfe/mae only, `WHERE shadow_id = ? AND status IN ('OBSERVING','DECIDED')` (same open-only guard PP's `record_profit_protection_tick` uses).
- `decide_guardian_authority_shadow(shadow_id, decision, decided_at, expected_outcome, expected_direction, confidence: float, factors_json, proposed_new_sl: Decimal | None, updated_at) -> bool` — the ONE write that transitions `OBSERVING -> DECIDED`, sets all 7 immutable decision fields. `WHERE shadow_id = ? AND status = 'OBSERVING'` (so a second call is structurally a no-op — returns `False`, never overwrites). Use exact numeric CAST-safe patterns already established (this table stores no stop-loss comparison guard itself, but store Decimals as `str()` throughout, same convention as every other table in this codebase).
- `resolve_guardian_authority_shadow_no_action(shadow_id, factors_json, actual_exit_reason, actual_pnl_usdt: Decimal, actual_closed_at, updated_at) -> bool` — the position closed while still `status='OBSERVING'` (never decided). One write: sets `shadow_decision='NO_ACTION'`, `expected_direction='neutral'`, `confidence=1.0`, `factors_json` (last-known), AND the baseline fields AND `status='RESOLVED'`, all at once. `WHERE shadow_id = ? AND status = 'OBSERVING'`.
- `resolve_guardian_authority_shadow_decided(shadow_id, actual_exit_reason, actual_pnl_usdt: Decimal, actual_closed_at, expectation_correct: bool | None, prediction_error: float | None, updated_at) -> bool` — the position closed after a shadow decision was already registered. `WHERE shadow_id = ? AND status = 'DECIDED'`, sets baseline fields + `expectation_correct` + `prediction_error` + `status='RESOLVED'`.
- `abandon_guardian_authority_shadow(shadow_id, abandoned_at) -> None` — same orphan-handling semantics as `abandon_profit_protection_shadow` (`WHERE shadow_id = ? AND status IN ('OBSERVING','DECIDED')`, sets `status='ABANDONED'`).
- `find_resolved_guardian_authority_shadows() -> list[dict]` — `status = 'RESOLVED'` (consumed by Task 9 and Task 10).

**Tests (write first):** mirror `test_repository_guardian_authority.py`'s and `profit_protection_experiment.py`'s own repository test shapes: idempotent seed (second seed with different data is a no-op — INSERT OR IGNORE), `decide_...` is a one-time transition (second call after DECIDED returns False, leaves original values byte-identical — same style as the immutability proof `mark_guardian_authority_decision_intervention_applied`'s sibling test used), `resolve_..._no_action` only fires from OBSERVING, `resolve_..._decided` only fires from DECIDED, `abandon_...` only fires from an open status, `record_..._tick` is a no-op once RESOLVED/ABANDONED (mfe/mae frozen).

**Commit:** `feat(crypto-trading): add guardian_authority_shadow_observations table and repository CRUD`

---

## Task 2: `guardian_authority_shadow_pre_entry_observations` table + repository CRUD

**Files:** `crypto_trading/storage/db.py`, `crypto_trading/storage/repository.py`, `tests/crypto_trading/storage/test_repository_guardian_authority_shadow.py`

**Interfaces (produces):**
- `save_guardian_authority_pre_entry_shadow(shadow_id, candidate_id, instrument, shadow_decision, expected_outcome, expected_direction, confidence: float, factors_json, run_id, created_at) -> bool` — INSERT OR IGNORE, single write (no separate "decide" step — pre-entry is single-shot per the spec), `status='PENDING'`.
- `get_guardian_authority_pre_entry_shadow(shadow_id) -> dict | None`
- `link_guardian_authority_pre_entry_shadow_to_position(candidate_id, position_id, updated_at) -> None` — fills `position_id` once the real position is known (candidate_id is 1:1 with position_id per existing convention elsewhere in this codebase — verify by grepping `candidate_id` usage in `position_opening.py`/`Position` schema before assuming).
- `find_pending_guardian_authority_pre_entry_shadows() -> list[dict]` — `status='PENDING' AND position_id IS NOT NULL` (nothing to resolve until the position_id link exists).
- `resolve_guardian_authority_pre_entry_shadow(shadow_id, actual_exit_reason, actual_pnl_usdt: Decimal, actual_closed_at, updated_at) -> bool` — `WHERE shadow_id = ? AND status = 'PENDING'`, sets baseline fields, `status='RESOLVED'`. `expectation_correct` is NEVER set here (per spec: always NULL for both APPROVE and PRE_ENTRY_VETO shadow rows — this method has no such parameter, do not add one).
- `find_resolved_guardian_authority_pre_entry_shadows() -> list[dict]` (consumed by Task 10's report only — Task 9's self-critique is TIGHTEN_SL-only, same scope note as the real Task 9).

**Tests:** same TDD idempotency/one-time-transition shapes as Task 1.

**Commit:** `feat(crypto-trading): add guardian_authority_shadow_pre_entry_observations table and repository CRUD`

---

## Task 3: `authority_shadow_enabled` config flag

**Files:** `crypto_trading/config/loader.py`, `crypto_trading/config/guardian.yaml`, `tests/crypto_trading/config/test_loader.py`

**Interfaces (produces):** `GuardianConfig.authority_shadow_enabled: bool = False` — new field, alongside the existing `authority_*` fields. Document in `guardian.yaml` the same way `authority_enabled` is already documented (present, explicit `false`, a short comment naming this plan/spec and pointing out it gates ONLY the new shadow-observation code paths, never anything `authority_enabled` also gates).

**Tests:** default-value test (mirrors the existing `authority_enabled` default test), a config-loads test asserting the yaml round-trips correctly.

**Commit:** `feat(crypto-trading): add Guardian Authority shadow-mode config flag, default off`

---

## Task 4: Tick-time shadow module (`paper_trading/guardian_authority_shadow.py`)

**Files:** Create `crypto_trading/paper_trading/guardian_authority_shadow.py`, `tests/crypto_trading/paper_trading/test_guardian_authority_shadow.py`

**Read first:** `crypto_trading/paper_trading/profit_protection_experiment.py` in full — this is your reference implementation. Your `seed_shadow_for_position`/`advance_shadow`/`run_guardian_authority_shadow_tick` should be recognizably the same shape as that file's `seed_shadows_for_position`/`advance_shadow`/`run_profit_protection_experiment_tick`, adapted to this table/decision vocabulary — not a fresh design.

**Interfaces (produces):**
- `seed_shadow_for_position(repo, position: Position, now) -> None` — unconditional (no activation-watermark gate needed; unlike PP, this has no historical-orphan-auto-open risk since it never acts on anything — seed for every currently-open position not yet seeded). Idempotent via `seed_guardian_authority_shadow`'s own INSERT OR IGNORE.
- `advance_shadow(shadow: dict, position: Position, guardian_state: str, tighten_threshold: float, close_threshold: float, current_price: Decimal, candle_high: Decimal, candle_low: Decimal, now, repo) -> None`:
  1. `record_guardian_authority_shadow_tick` (mfe/mae, unconditional — reuse the exact `max(mfe, candle_high-entry)`/`min(mae, candle_low-entry)` running-extremum formula from `profit_protection_experiment.py::advance_shadow`).
  2. If `shadow["status"] == 'OBSERVING'`: build `factors` the SAME way `guardian/tick.py`'s real call site does (position's own decay factors + `{"guardian_state": guardian_state}` merge), call `decide_open_position(factors, guardian_state, position.stop_loss, position.simulated_fill_entry, repo.find_guardian_authority_heuristics(), tighten_threshold, close_threshold)` — the real function, unmodified. If the returned decision is not `NO_ACTION`, call `decide_guardian_authority_shadow(...)` with its outputs.
  3. Never write to `positions`, never call any close/open/order function — this function's only writes are the two repo calls above.
- `_resolve_on_close(repo, shadow: dict, position: Position, now) -> None` — called for a position present in `closed_positions` this tick. Computes `baseline_pnl = compute_pnl(position)` (reuse, same as PP's backfill). If `shadow["status"] == 'OBSERVING'`: call `resolve_guardian_authority_shadow_no_action` (with the shadow's last-known factors_json — read from the shadow row itself, since OBSERVING rows never got a `factors_json` write in step 2 above... **note to implementer:** `advance_shadow` must ALSO update a scratch `last_factors_json` field on every tick while `status='OBSERVING'` even though the immutable decision fields aren't written yet, so `_resolve_on_close` has something to snapshot — add this as a small additive column if the Task 1 schema doesn't already cover it; flag this in your report if you find the Task 1 schema needs a one-column addition here, this is a legitimate cross-task gap the controller will rule on, not a blocker). If `shadow["status"] == 'DECIDED'`: compute `expectation_correct`/`prediction_error` per the spec's "Resolution" section (TIGHTEN_SL only gets a real value; CLOSE_EARLY gets both NULL) and call `resolve_guardian_authority_shadow_decided`.
- `run_guardian_authority_shadow_tick(repo, open_positions: list[Position], closed_positions: list[Position], price_lookup: dict, now, settings: Settings, run_id: str) -> None` — top-level orchestrator, mirroring `run_profit_protection_experiment_tick`'s exact structure: (1) seed every open position not yet seeded, (2) advance every open shadow whose position is still open and has a candle this tick, abandoning any whose position vanished from `open_positions` (same stranded-shadow handling as PP's own `abandon_profit_protection_shadow` call, same per-shadow try/except isolation, same `log_event` on failure), (3) resolve every shadow (OBSERVING or DECIDED) whose position appears in `closed_positions` this tick. `if not settings.guardian.authority_shadow_enabled: return` at the very top.

**Tests:** TDD, covering: a position with empty heuristics stays OBSERVING its whole life and resolves NO_ACTION at close (the core cold-start proof); a position whose factors cross `tighten_threshold` on tick N transitions to DECIDED on tick N and stays DECIDED (unchanged) through tick N+1..M even if factors would again qualify; a stranded shadow (position vanished from open_positions) gets ABANDONED; a per-shadow advance failure doesn't abort the batch; flag-off is a no-op (no rows seeded at all).

**Commit:** `feat(crypto-trading): add Guardian Authority tick-time shadow observation (cold-start fix, PAPER-only, no real writes)`

---

## Task 5: Wire tick-time shadow into `monitoring_loop.py`

**Files:** `crypto_trading/monitoring_loop.py`, `tests/crypto_trading/test_monitoring_loop.py`

**Interfaces:** no new public interface. In `run_monitoring_tick` (or wherever `run_profit_protection_experiment_tick` is currently called — read the live file, it's right after `close_triggered_positions`, `crypto_trading/monitoring_loop.py` around line 94), add an identically-shaped call to `run_guardian_authority_shadow_tick(repo, open_positions, closed, price_lookup, now, settings, run_id)`, in its OWN try/except (never share the PP experiment's try/except block — one experiment's failure must never be attributable to, or mask, the other's), logging `guardian_authority_shadow_tick_failed` on exception, exactly mirroring the existing `profit_protection_experiment_tick_failed` pattern immediately above/below it.

**Tests:** flag-off byte-identical behavior (no new DB writes, no new function calls — spy/count assertion); flag-on calls `run_guardian_authority_shadow_tick` exactly once per tick with the right arguments; an exception inside it doesn't abort the tick or prevent `repo.complete_run` from being reached.

**Commit:** `feat(crypto-trading): wire Guardian Authority shadow tick into monitoring_loop, default off`

---

## Task 6: Pre-entry shadow hook

**Files:** `crypto_trading/guardian/authority.py`, `crypto_trading/paper_trading/replay.py`, `crypto_trading/paper_trading/recovery_sweep.py`, `tests/crypto_trading/guardian/test_authority.py`, `tests/crypto_trading/paper_trading/test_replay.py`, `tests/crypto_trading/paper_trading/test_recovery_sweep.py`

**Interfaces (produces):** `maybe_record_pre_entry_shadow(candidate: Candidate, repo: Repository, settings: Settings, run_id: str, now: datetime) -> None`, in `crypto_trading/guardian/authority.py` (additive function, does not touch `maybe_open_position_for_candidate` or any of its logic). `if not settings.guardian.authority_shadow_enabled: return` first line. Otherwise: build the SAME `_pre_entry_factors(candidate)` the real veto path already uses, call `decide_pre_entry(candidate_evidence, repo.find_guardian_authority_heuristics(), settings.guardian.authority_veto_threshold)` — the real function, unmodified — and call `save_guardian_authority_pre_entry_shadow(...)` with its outputs. Never raises (wrap internally, `log_event` on failure — this must never be able to affect whether the real candidate opens).

Wire into `replay.py` and `recovery_sweep.py` at the exact call sites that already call `maybe_open_position_for_candidate` — add `maybe_record_pre_entry_shadow(candidate, repo, settings, run_id, now)` as a sibling call, BEFORE or AFTER `maybe_open_position_for_candidate` (implementer's choice, document which and why — either is safe since shadow recording never influences the real decision), never inside a branch that could skip it when the real function proceeds.

**Tests:** flag-off no-op; flag-on records a shadow row whose `shadow_decision` matches what `decide_pre_entry` would independently compute for the same inputs; a shadow-level exception never prevents the real `maybe_open_position_for_candidate` call from running or its result from being used.

**Commit:** `feat(crypto-trading): add Guardian Authority pre-entry shadow observation, default off`

---

## Task 7: Link pre-entry shadow to its real position + resolve

**Files:** `crypto_trading/guardian/authority.py` (or a new small function near Task 6's), `crypto_trading/paper_trading/replay.py` (wherever the real `position_id` becomes known right after `open_position_for_candidate` succeeds), `crypto_trading/monitoring_loop.py` (resolution, alongside Task 5's own resolution step), tests in the corresponding files.

**Interfaces:**
- Immediately after a real position opens for a candidate that has a pending pre-entry shadow row, call `link_guardian_authority_pre_entry_shadow_to_position(candidate.candidate_id, position.position_id, now)`.
- In `monitoring_loop.py`'s own resolution step (alongside Task 5's `closed_positions` loop, or inside `run_guardian_authority_shadow_tick` itself if the implementer judges that a cleaner single entry point — controller ruling deferred to implementer, document the choice): for each closed position, check `find_pending_guardian_authority_pre_entry_shadows()` for a match on `position_id` and call `resolve_guardian_authority_pre_entry_shadow(...)` with the real `compute_pnl`/`exit_reason`.

**Tests:** a candidate with a shadow row that never opens a real position (Gate rejected it for unrelated reasons) stays unlinked forever, never crashes anything; a linked shadow resolves correctly when its position closes; resolution is idempotent (second attempt is a no-op, `WHERE status='PENDING'`).

**Commit:** `feat(crypto-trading): link and resolve Guardian Authority pre-entry shadow observations against real position outcomes`

---

## Task 8: `guardian_authority_shadow_heuristics` table + self-critique-from-shadow

**Files:** `crypto_trading/storage/db.py`, `crypto_trading/storage/repository.py`, new function in `crypto_trading/paper_trading/guardian_authority_shadow.py` (or a new small module — implementer's call, document it), tests.

**Read first:** `crypto_trading/guardian/authority.py`'s `update_heuristics_from_resolved_decisions` and its supporting `_groups_for_factors`/grouping section (post-I1: state-alone grouping only) — your new function has the IDENTICAL grouping/threshold/adjustment/confidence logic, adapted only to read `find_resolved_guardian_authority_shadows()` instead of `find_resolved_guardian_authority_decisions()` and write `upsert_guardian_authority_shadow_heuristic(...)` (new repo method, same shape as `upsert_guardian_authority_heuristic` but targeting the new table) instead of the real one. Filter: `decision_type` is implicitly always the tick-time table's own rows (no `decision_type` column needed there unlike the real table, since this table only ever holds tick-time TIGHTEN_SL/CLOSE_EARLY/NO_ACTION rows — but the self-critique function still only tallies rows where `shadow_decision == 'TIGHTEN_SL'` and `expectation_correct IS NOT NULL`, same scope note as the real Task 9).

**Interfaces (produces):**
- `Repository.upsert_guardian_authority_shadow_heuristic(heuristic_id, description, condition_json, adjustment: float, confidence: float, sample_size: int, updated_at) -> None`, `find_guardian_authority_shadow_heuristics() -> list[dict]`.
- `update_shadow_heuristics_from_resolved_shadow_observations(repo, now) -> int` — same signature shape, same `_MIN_SAMPLE_SIZE`/`_MIN_MISCALIBRATION`/`_ADJUSTMENT_SCALE` constants (import/reuse the real module's constants directly, do not redefine — if that's not importable cleanly, duplicate the three literal values with a comment pointing at the source of truth, controller ruling: duplication of 3 named constants is acceptable here, duplication of the grouping/threshold LOGIC is not).

**Wiring:** call once per monitoring tick, after Task 5's resolution step, gated by `authority_shadow_enabled`, same "only run if something new resolved this tick" opportunistic cadence the real Task 9 uses (count resolved-this-tick, call only if >=1) — or simpler or every-tick-unconditionally if the implementer judges the cost negligible at this data scale; document the choice.

**Verification (critical, both the implementer's own review and the task reviewer must independently confirm, not just read the code):** grep the whole diff and `crypto_trading/guardian/authority.py` for any call to `find_guardian_authority_shadow_heuristics` or `upsert_guardian_authority_shadow_heuristic` OUTSIDE this new function/module — there must be none. `evaluate_heuristics`/`decide_pre_entry`/`decide_open_position` must remain byte-identical (diff them against `master`, zero hunks).

**Commit:** `feat(crypto-trading): add Guardian Authority self-critique-from-shadow-data, isolated from the real heuristics table`

---

## Task 9: Read-only shadow report

**Files:** Create `crypto_trading/performance/guardian_authority_shadow_report.py`, `tests/crypto_trading/performance/test_guardian_authority_shadow_report.py`

**Read first:** `crypto_trading/performance/guardian_authority_report.py` in full — same `build_report(repo) -> dict` + `main()` pattern, same win_rate/brier_score/brier_score_note shape reused for the tick-time shadow's `TIGHTEN_SL` entries (identical formulas, same `intervention_applied`-equivalent concept doesn't apply here since shadow rows are already 1-per-position by construction — no filter needed, see Task 4).

**Interfaces (produces):** `build_report(repo) -> dict` with sections for tick-time shadow (`NO_ACTION`/`TIGHTEN_SL`/`CLOSE_EARLY` counts, resolved/pending, win_rate+brier_score+brier_score_note for TIGHTEN_SL same as the real report), pre-entry shadow (`APPROVE`/`PRE_ENTRY_VETO` counts, resolved/pending), `active_shadow_heuristics_count`. Only `find_*` calls — never write, never import/be imported by `authority.py`/`authority_live.py`/`tick.py`/`guardian_authority_shadow.py`.

**Commit:** `feat(crypto-trading): add read-only Guardian Authority shadow report`

---

## Task 10: Production-isolation tests (req-9-style checklist)

**Files:** new tests in `tests/crypto_trading/paper_trading/test_guardian_authority_shadow.py` and/or a new `tests/crypto_trading/guardian/test_authority_shadow_isolation.py`.

**Read first:** Task 10 of the original Guardian Authority plan (`docs/superpowers/plans/2026-09-14-guardian-authority.md`) and its corresponding tests in `test_authority.py`/`test_authority_live.py` — same AST-based-where-practical, source-text-scan-otherwise discipline, covering every file this plan touched.

**Checklist to encode as tests (traced 1:1 to this plan's Global Constraints):**
1. No file this plan created/touched calls `open_position_for_candidate`, `tighten_position_stop_loss`, `apply_live_sl_tightening`, `place_stop_loss_order`, `cancel_order`, `set_leverage`, or `save_guardian_observation` with `state="EXIT"`.
2. No file this plan created/touched imports `position_sizing.py`.
3. `evaluate_heuristics`/`decide_pre_entry`/`decide_open_position`/`heuristic_condition_matches`/`_compute_proposed_new_sl` in `authority.py` are byte-identical to their state at this plan's base commit (direct diff, zero hunks).
4. `find_guardian_authority_shadow_heuristics`/`upsert_guardian_authority_shadow_heuristic` are called ONLY from Task 8's own function — nowhere in `authority.py`'s real decision-path functions.
5. `authority_shadow_enabled` defaults `false`; `authority_enabled` is unreferenced by every file this plan created.
6. Whole-plan `git diff --stat` against base, independently re-run and read (not trusted from any implementer's own claim), confirms every one of the 6 forbidden paths in the Global Constraints is genuinely absent from the FULL plan's diff.
7. Full suite green, same 2 pre-existing/documented baseline failures only.

**Commit:** `test(crypto-trading): add production-isolation tests for Guardian Authority shadow mode`

---

## Task 11: Final regression, safety review, and activation

Not a code task — the plan's own closing step, executed by the controller (not dispatched as an implementer task):

1. Full repo test suite, fresh run, on the whole plan's diff.
2. Dispatch a final whole-branch review (most capable model), same rigor as the original Guardian Authority plan's own final review — point it at this plan's Global Constraints and the spec's Acceptance Criteria section, ask it to independently re-derive (not trust) every isolation claim above.
3. Only after that review is clean (or its findings are fixed/parked per the standard SDD fix-loop): set `authority_shadow_enabled: true` in `crypto_trading/config/guardian.yaml` (a plain config commit, `feat(crypto-trading): activate Guardian Authority shadow/PAPER observation mode`) — this is the ONLY activation this plan performs. `authority_enabled` and everything LIVE remain exactly as they were before this plan started.
