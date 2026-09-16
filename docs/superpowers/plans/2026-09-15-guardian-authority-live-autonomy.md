# Guardian Authority — Live Autonomous Decisions & Self-Improvement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Close the real (not just shadow) cold-start deadlock so GODFATHER can autonomously veto entries, tighten stop-losses, and close positions early on real capital, using heuristics it proposes, validates out-of-sample, promotes, and self-corrects entirely on its own — while making every one of the user's hard constraints (never increase leverage, never loosen a stop-loss, no martingale, no unlimited positions, no capital-limit change) a structural, grep/AST-provable property of the code, not a policy.

**Spec:** `docs/superpowers/specs/2026-09-15-guardian-authority-live-autonomy-design.md` — read this in full before starting any task. It is the binding authority; it explains WHY each isolation boundary exists.

**Architecture:** Nine additive tasks, all confined to two new tables, one new nullable column, one new agent role, and one new orchestration module — the pure decision core (`evaluate_heuristics`/`decide_pre_entry`/`decide_open_position`/`heuristic_condition_matches`/`_compute_proposed_new_sl`/`_groups_for_factors`) and the real heuristics table's write path (`upsert_guardian_authority_heuristic`) are never modified, only reused. Every task after this plan's Task 3 reuses statistical/isolation machinery this session already built and hardened (I1/I2/I3, Shadow Mode's Task 10) rather than inventing new mechanisms.

## Global Constraints

(Reproduced from the spec — copy verbatim into every task reviewer's dispatch.)

- Never modify `evaluate_heuristics`, `decide_pre_entry`, `decide_open_position`, `heuristic_condition_matches`, `_compute_proposed_new_sl`, `_groups_for_factors`, `upsert_guardian_authority_heuristic`, `find_guardian_authority_heuristics` — byte-identical before/after this whole plan.
- Never modify `crypto_trading/paper_trading/position_opening.py`, `position_sizing.py`, `crypto_trading/gate/`, `crypto_trading/screening/`, any pre-existing AI role `.md` file, `crypto_trading/config/risk_limits.yaml`, or `live_execution.yaml`'s hard limits (leverage, margin, max_concurrent_positions).
- Never call `set_leverage`. Never import `position_sizing.py` anywhere in this plan's diff.
- Never call `open_position_for_candidate` except through the existing, unmodified `maybe_open_position_for_candidate` wrapper — no new call site.
- A candidate heuristic reaches the real `guardian_authority_heuristics` table ONLY via the existing, unmodified `upsert_guardian_authority_heuristic` — no other write path into that table anywhere in this plan.
- `matched_heuristic_ids_json` is populated at the orchestration layer only (tick.py's `process_one_position`, authority.py's `maybe_open_position_for_candidate`'s caller) via a second, duplicate, read-only `evaluate_heuristics()`/`decide_pre_entry`'s own internal scoring call — never by widening `decide_open_position`/`decide_pre_entry`'s return signature.
- `authority_enabled: true` is the plan's LAST commit, gated behind full TDD and a final deep review pass at least as rigorous as LIVE Profit Protection's (3 rounds before being trusted with real orders).
- TDD throughout. Full suite green before each task's commit.

## File Structure

- Modify: `crypto_trading/storage/db.py`, `crypto_trading/storage/repository.py` — 1 new table (heuristic candidates) + CRUD; 1 additive nullable column on `guardian_authority_decisions` + widened `save_guardian_authority_decision`.
- Modify: `crypto_trading/guardian/tick.py`, `crypto_trading/guardian/authority.py` — additive `matched_heuristic_ids_json` wiring at the two real decision call sites (orchestration layer only).
- Create: `.claude/agents/crypto-godfather-strategist.md` — new agent role.
- Modify: `crypto_trading/schemas/assessments.py` — new `GodfatherStrategistAssessment` schema.
- Create: `crypto_trading/guardian/self_improvement.py` — propose / validate / promote / track-and-demote pipeline + orchestrator.
- Modify: wherever this plan's implementer judges the periodic tick best wired (likely `crypto_trading/monitoring_loop.py` or `crypto_trading/discovery_loop.py`, gated + budget-limited to at most once/day for the AI-costing proposal step).
- Modify: `crypto_trading/config/guardian.yaml` — `authority_enabled: true` (final task only).
- Tests: one new file per new module/table, plus a whole-plan isolation test suite mirroring Shadow Mode's Task 10.

---

## Task 1: `guardian_authority_heuristic_candidates` table + repository CRUD

**Files:** `crypto_trading/storage/db.py`, `crypto_trading/storage/repository.py`, `tests/crypto_trading/storage/test_repository_guardian_authority_heuristic_candidates.py`

**Interfaces (produces):**
```python
def save_guardian_authority_heuristic_candidate(
    self, candidate_id: str, description: str, condition_json: str,
    proposed_adjustment: float, rationale: str, run_id: str, proposed_at: datetime,
) -> bool  # INSERT OR IGNORE, status='PROPOSED'
def get_guardian_authority_heuristic_candidate(self, candidate_id: str) -> dict | None
def find_proposed_guardian_authority_heuristic_candidates(self) -> list[dict]  # status='PROPOSED'
def find_promoted_guardian_authority_heuristic_candidates(self) -> list[dict]  # status='PROMOTED'
def record_guardian_authority_heuristic_candidate_validation(
    self, candidate_id: str, status: str,  # 'VALIDATED' | 'REJECTED'
    train_sample_size: int, train_correct_rate: float,
    test_sample_size: int, test_correct_rate: float,
    validated_at: datetime, rejected_reason: str | None = None,
) -> bool  # WHERE status='PROPOSED' only, one-time transition
def promote_guardian_authority_heuristic_candidate(
    self, candidate_id: str, promoted_heuristic_id: str, promoted_at: datetime,
) -> bool  # WHERE status='VALIDATED' only
def mark_guardian_authority_heuristic_candidate_demoted(
    self, candidate_id: str, demoted_at: datetime, demotion_reason: str,
) -> bool  # WHERE status='PROMOTED' only, sets a nullable demoted_at/demotion_reason column, status stays 'PROMOTED' (audit trail - never deleted, never silently reverted to a prior state)
```

**Schema** (exact, per the design spec's data model section):
```sql
CREATE TABLE IF NOT EXISTS guardian_authority_heuristic_candidates (
    candidate_id TEXT PRIMARY KEY,
    proposed_at TEXT NOT NULL,
    description TEXT NOT NULL,
    condition_json TEXT NOT NULL,
    proposed_adjustment REAL NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL,
    train_sample_size INTEGER,
    train_correct_rate REAL,
    test_sample_size INTEGER,
    test_correct_rate REAL,
    validated_at TEXT,
    promoted_at TEXT,
    promoted_heuristic_id TEXT,
    rejected_reason TEXT,
    demoted_at TEXT,
    demotion_reason TEXT,
    run_id TEXT NOT NULL
);
```

**Tests (write first):** mirror Shadow Mode's Task 1/2's own idempotency/one-time-transition test shapes exactly (seed idempotent via INSERT OR IGNORE; `record_..._validation` only fires from PROPOSED, a second call is a structural no-op with a whole-row byte-identity proof; `promote_...` only fires from VALIDATED; `mark_..._demoted` only fires from PROMOTED and leaves `status='PROMOTED'` unchanged, only setting the two new nullable fields).

**Commit:** `feat(crypto-trading): add guardian_authority_heuristic_candidates table and repository CRUD`

---

## Task 2: `matched_heuristic_ids_json` tracking column + orchestration wiring

**Files:** `crypto_trading/storage/db.py`, `crypto_trading/storage/repository.py`, `crypto_trading/guardian/tick.py`, `crypto_trading/guardian/authority.py`, corresponding tests.

**Interfaces:**
- Migration-only additive column: `guardian_authority_decisions.matched_heuristic_ids_json TEXT` (nullable, idempotent `_add_column_idempotent` pattern, same as `intervention_applied`/every prior addition to this table — NOT in the `CREATE TABLE` string).
- Widen `save_guardian_authority_decision(..., matched_heuristic_ids_json: str | None = None)` — additive keyword param, no existing caller breaks.
- At BOTH real call sites — `tick.py::process_one_position` (the TIGHTEN_SL/CLOSE_EARLY save) and `authority.py::maybe_open_position_for_candidate` (the PRE_ENTRY_VETO save) — add a SECOND, duplicate call to the already-imported, unmodified `evaluate_heuristics(factors_or_candidate_evidence, heuristics)` purely to capture `matched_ids` (the function already computes and returns this — the orchestration layer just wasn't keeping it before), then `json.dumps(matched_ids)` into the new param. **Do not modify `decide_open_position`/`decide_pre_entry`'s own signatures or bodies at all** — they already call `evaluate_heuristics` internally and already discard `matched_ids` after using it to build `_matched_heuristics`/text; that's fine, untouched. This task's second call is purely additive, at the orchestration layer, and costs one extra cheap pure-function call per decision (same "read-only duplicate" pattern this codebase already uses elsewhere, e.g. `profit_protection_experiment.py::_guardian_state_for`).

**Tests:** a real decision (both TIGHTEN_SL/CLOSE_EARLY via `tick.py` and PRE_ENTRY_VETO via `authority.py`) with 2+ matched heuristics correctly records both ids in `matched_heuristic_ids_json`; a decision with zero matched heuristics (shouldn't normally happen since only non-`NO_ACTION`/`APPROVE` decisions are saved, but confirm gracefully) records an empty list, not a crash; migration test mirroring the established idempotent-column-add pattern; confirm via diff that `decide_open_position`/`decide_pre_entry` have zero hunks.

**Commit:** `feat(crypto-trading): track matched heuristic ids on real Guardian Authority decisions`

---

## Task 3: LLM-driven heuristic-candidate proposal (`crypto_trading/guardian/self_improvement.py`, propose step)

**Files:** Create `.claude/agents/crypto-godfather-strategist.md`, modify `crypto_trading/schemas/assessments.py`, create `crypto_trading/guardian/self_improvement.py` + its test file.

**This is the plan's most novel task — read carefully before implementing, and expect a deeper review pass.**

**Read first:** `.claude/agents/crypto-guardian.md` (the format to match exactly: YAML frontmatter `name`/`description`/`tools`, then the system-prompt body in Swedish, matching this project's own established voice), `crypto_trading/agents/runner.py` (`AgentRunner`/`RealClaudeRunner`/`MockAgentRunner` — reuse unmodified), `crypto_trading/guardian/tick.py`'s `_budget_allows_one_more_call` (reuse the EXACT pattern, do not reinvent budget-gating), `crypto_trading/schemas/assessments.py` (the `AssessmentBase` shape every role's output already extends).

**New schema** (`crypto_trading/schemas/assessments.py`, additive):
```python
class ProposedHeuristic(BaseModel):
    description: str
    condition: dict  # same matching semantics as heuristic_condition_matches expects
    adjustment: float
    rationale: str

class GodfatherStrategistAssessment(AssessmentBase):
    proposed_heuristics: list[ProposedHeuristic]
```

**New agent role** (`.claude/agents/crypto-godfather-strategist.md`): reads (a) resolved shadow TIGHTEN_SL decisions (`repo.find_resolved_guardian_authority_shadows()`, already built by the Shadow Mode plan), (b) resolved real TIGHTEN_SL decisions (`repo.find_resolved_guardian_authority_decisions()`), (c) Detective's post-trade analysis where available (`crypto_trading/detective/stats.py`'s `compute_breakdown_by_signal_type`/`compute_guardian_exit_effectiveness` — read-only, reuse unmodified). Its system prompt must explicitly state the SAME "condition-matching semantics" (`_max`/`_min` numeric-bound suffixes, list-membership, equality — copy verbatim from `authority.py`'s own module docstring "Condition-matching semantics" section) so its JSON output is genuinely usable by the unmodified `heuristic_condition_matches`. Its system prompt must ALSO explicitly state it is proposing CANDIDATES for validation, never a live decision, and that its output has zero effect on real trading until Task 4/5's independent, out-of-sample validation clears it.

**Function:**
```python
def propose_candidate_heuristics(
    repo: Repository, runner: AgentRunner, settings: Settings, run_id: str, now: datetime,
) -> int  # count of candidates saved this call
```
Gated by `_budget_allows_one_more_call` (imported/reused, not forked) AND a new once-per-UTC-day watermark (mirroring `_utc_day_start`'s own existing pattern in this same codebase — proposing candidates is not a per-tick action, it's expensive/slow-changing). Builds context from the three sources above, calls `runner.run(agent_def, context, GodfatherStrategistAssessment)`, and for each `ProposedHeuristic` in a successful response, calls `save_guardian_authority_heuristic_candidate` with a deterministic `candidate_id` (e.g. `f"llm:{run_id}:{index}"`). Never raises — wrap in try/except, `log_event` on failure, matching every other AI-call-site's fail-safe discipline in this codebase.

**Tests:** using `MockAgentRunner` (never a real API call in tests) — a successful proposal saves the right number of candidate rows with the right fields; budget-exhausted skips the call entirely (spy/count assertion, zero candidates saved, zero AI cost incurred); the once-per-day watermark prevents a second proposal call on the same UTC day even if budget allows; a malformed/failed agent response never crashes, never saves a partial candidate.

**Commit:** `feat(crypto-trading): add LLM-driven Guardian Authority heuristic-candidate proposal, budget-gated, default off`

---

## Task 4: Out-of-sample validation gate

**Files:** `crypto_trading/guardian/self_improvement.py`, its test file.

**Read first:** `crypto_trading/guardian/authority.py`'s `_groups_for_factors`/`update_heuristics_from_resolved_decisions` (the statistical machinery to reuse — `_MIN_SAMPLE_SIZE`, `_MIN_MISCALIBRATION`, import these constants directly, do not redefine) and `heuristic_condition_matches` (import unmodified — this is what actually evaluates whether a historical decision's factors match a candidate's proposed `condition_json`).

**Function:**
```python
def validate_pending_heuristic_candidates(repo: Repository, now: datetime) -> int  # count validated+rejected this call
```
For each row from `find_proposed_guardian_authority_heuristic_candidates()`:
1. Gather the SAME pool Task 8's shadow self-critique and the real Task 9 self-critique already use: resolved TIGHTEN_SL rows (shadow + real) with a non-null `expectation_correct`, each with its reconstructable factors dict (shadow rows: `factors_json` directly on the row; real rows: reuse the existing `_reconstruct_tighten_sl_factors` — import unmodified, do not fork).
2. Split this pool into train/test by `decided_at`: sort chronologically, first 70% -> train, last 30% -> test (a simple, deterministic, documented split — matches `run_tier1_backtest`'s own "physically separate before any evaluation, so out-of-sample cannot leak into training results by construction" principle in spirit, simpler in mechanism since this is a row-level split, not a DB-level one).
3. For each split, filter to rows where `heuristic_condition_matches(json.loads(candidate["condition_json"]), factors)` is true, compute `sample_size`/`correct_rate`/`deviation` exactly as the existing self-critique functions do.
4. **Promote-eligible only if BOTH splits clear `_MIN_SAMPLE_SIZE`/`_MIN_MISCALIBRATION` AND agree in sign** (train deviation and test deviation both positive, or both negative) — this is the genuine out-of-sample bar; train-only agreement is not sufficient (Acceptance Criterion 4's own required proof). Call `record_guardian_authority_heuristic_candidate_validation` with `status='VALIDATED'` and both splits' numbers.
5. Otherwise `status='REJECTED'` with a `rejected_reason` naming which check failed (too few train samples / too few test samples / miscalibration below floor on either split / sign disagreement between splits).

**Tests:** a fixture where a genuine, consistent pattern exists in BOTH train and test splits validates; a fixture where the SAME pattern exists on train (by chance) but reverses on test correctly rejects (the specific proof Acceptance Criterion 4 requires — construct this deliberately, e.g. two differently-signed clusters split unevenly by the 70/30 cutoff); insufficient total sample size (even before splitting) rejects with the right reason; `heuristic_condition_matches` is called with the candidate's own `condition_json`, never a hand-modified copy (verify via a condition using the `_min`/`_max` suffix convention specifically, to prove real reuse of the existing matching semantics, not a simplified stand-in).

**Commit:** `feat(crypto-trading): add out-of-sample validation gate for Guardian Authority heuristic candidates`

---

## Task 4B: Widen validation to PRE_ENTRY_VETO-shaped candidates via a real closed-position counterfactual pool (2026-09-16 addendum, closes R2)

**Why this task exists:** see `docs/superpowers/specs/2026-09-15-guardian-authority-live-autonomy-design.md`'s "Addendum (2026-09-16)" section in full before starting — it is the binding authority for this task, resolving a real gap the post-Task-4 reuse audit found and the user ruled on directly: Task 4's TIGHTEN_SL-only validation pool is empty by construction at cold start (no real/shadow TIGHTEN_SL decision can exist without a heuristic already existing), so no candidate could ever validate, ever, as originally scoped. The fix is NOT a seeded heuristic and NOT a fabricated outcome — it is a second, independent validation pool built entirely from real closed positions' real entry evidence and real realized PnL, which exist completely independent of whether any Guardian Authority heuristic has ever existed.

**Files:** `crypto_trading/schemas/assessments.py`, `.claude/agents/crypto-godfather-strategist.md`, `crypto_trading/storage/db.py`, `crypto_trading/storage/repository.py`, `crypto_trading/guardian/self_improvement.py`, its test file(s) (extend `tests/crypto_trading/guardian/test_self_improvement_validation.py` for the dual-pool routing tests; a new test file for the pool builder itself is fine — your call, document which you chose and why).

**Read first:** the addendum (above), `crypto_trading/guardian/authority.py`'s `_pre_entry_factors` (the exact factor vocabulary `decide_pre_entry` itself is evaluated against — reuse unmodified, do not restate), `crypto_trading/guardian/self_improvement.py`'s current `_tighten_sl_evidence_pool`/`_split_pool_chronologically`/`_split_stats`/`_validation_outcome`/`validate_pending_heuristic_candidates` (Task 4, already committed — the last three are ALREADY generic over `list[tuple[str, dict, bool]]`, containing zero TIGHTEN_SL-specific logic; you are adding a second pool-builder of the identical shape, not forking the statistical machinery), `crypto_trading/paper_trading/execution.py::compute_pnl` (existing, unmodified — the only way PnL is ever computed anywhere in this codebase).

**Schema addition** (`crypto_trading/schemas/assessments.py`, additive):
```python
class ProposedHeuristic(BaseModel):
    description: str
    condition: dict
    adjustment: float
    rationale: str
    target_decision_type: Literal["TIGHTEN_SL", "PRE_ENTRY_VETO"]  # NEW
```

**New column** (idempotent `_add_column_idempotent` pattern, same as `matched_heuristic_ids_json`/`intervention_applied` before it): `guardian_authority_heuristic_candidates.target_decision_type TEXT` — nullable. Widen `save_guardian_authority_heuristic_candidate(..., target_decision_type: str | None = None)` — additive keyword param with a default, so Task 1's own existing tests keep passing unchanged. `propose_candidate_heuristics` always passes the LLM-declared value now that the schema requires it; a `NULL` row (there are none in production — this feature has never been activated) is treated as `"TIGHTEN_SL"` wherever validation reads it back, for backward compatibility, not because NULL is a valid ongoing state going forward.

**Agent role update** (`.claude/agents/crypto-godfather-strategist.md`): add a section explaining the SECOND decision type it may now propose for, `PRE_ENTRY_VETO`, its OWN distinct factor vocabulary (`instrument`, `candidate_score`, `trigger_reasons` — copy `_pre_entry_factors`'s own field list verbatim, the same discipline the existing TIGHTEN_SL section already uses for `guardian_state`-shaped conditions), and require every proposal to declare `target_decision_type` and use ONLY that type's own vocabulary in its `condition` — a `PRE_ENTRY_VETO` proposal conditioning on `guardian_state` (a TIGHTEN_SL-only factor) is a role-prompt violation the implementer should explicitly warn against, mirroring the existing "only observed_factor_names" discipline already in this file. Check whether `_build_context`'s existing evidence (it already includes bounded, most-recent closed-position data per Task 3's `_MAX_EVIDENCE_ROWS` window) gives the strategist enough real entry-factor/outcome visibility to reason about pre-entry patterns; if genuinely insufficient, extend it minimally (still bounded, still read-only, still reusing `_pre_entry_factors`) — but the required deliverable is the dual-pool validation below, not a context-builder redesign.

**New pool builder** (`crypto_trading/guardian/self_improvement.py`, same shape as `_tighten_sl_evidence_pool`):
```python
def _pre_entry_veto_evidence_pool(repo: Repository) -> list[tuple[str, dict, bool]]:
```
For every row in `repo.find_closed_positions()` (existing, unmodified): skip if `closed_at` is `None` (defensive) or `repo.get_candidate(position.candidate_id)` returns `None` (corrupt/missing candidate record — skip, never guess). Otherwise: `factors = _pre_entry_factors(candidate)` (imported unmodified from `authority.py`), `would_veto_have_been_correct = compute_pnl(position) <= Decimal("0")` (same "zero counts as NOT favorable" convention `resolve_pending_decisions` already uses — a veto is "correct" iff the real position actually lost money or exactly broke even). Append `(position.closed_at.isoformat(), factors, would_veto_have_been_correct)`.

**Wire into `validate_pending_heuristic_candidates`:** compute BOTH pools once per call (`_tighten_sl_evidence_pool`, `_pre_entry_veto_evidence_pool`), chronologically split each ONCE (two independent `_split_pool_chronologically` calls — the two pools have different timestamp domains and must never be merged or share a split boundary), then for each candidate select the already-split train/test pair matching `candidate.get("target_decision_type") or "TIGHTEN_SL"` before calling the existing, unmodified `_split_stats`/`_validation_outcome`. No changes to `_split_stats`, `_validation_outcome`, `_split_pool_chronologically`, or the sample-size/miscalibration/sign-agreement bar itself — this task adds a second pool and a routing decision, nothing else to the statistical core.

**Tests:**
- `_pre_entry_veto_evidence_pool` builds correct `(closed_at, factors, correct)` tuples from real closed positions + their real candidates + real PnL; skips a position whose candidate record is missing; a position with PnL exactly `0` is scored `True` (veto would have been correct), a position with positive PnL is scored `False`.
- A `PRE_ENTRY_VETO`-targeted candidate is validated ONLY against the new pool — construct a fixture where the SAME condition would validate against the TIGHTEN_SL pool but not the PRE_ENTRY_VETO pool (or vice versa) and assert the candidate's `target_decision_type` alone determines which result it gets. This is the concrete proof against cross-contamination between the two pools.
- A `NULL`/legacy `target_decision_type` candidate is validated against the TIGHTEN_SL pool (backward-compat default), asserted explicitly.
- A genuine "reverses on test" fixture for the NEW pool specifically, mirroring Task 4's own Acceptance-Criterion-4 proof — two differently-signed clusters of closed positions split unevenly by the 70/30 cutoff, proving the out-of-sample bar is real for this pool too, not just inherited by assumption.
- **The concrete proof R2 is actually closed:** a cold-start fixture — zero real/shadow Guardian Authority decisions, zero real heuristics, but a realistic number of real closed positions in history — where a `PRE_ENTRY_VETO`-targeted candidate reaches `VALIDATED`. This is the test that matters most for this task.
- A decision type whose real matching history is genuinely too small stays `REJECTED` with the standard `too few train/test samples` reason — never silently promoted, never treated as evidence by lowering a bar.
- Isolation: confirm (state explicitly in your report) that this task adds no new write path anywhere near `guardian_authority_heuristics`, and that `heuristic_condition_matches`/`_pre_entry_factors`/`compute_pnl`/`find_closed_positions`/`get_candidate` are all called via existing imports, never restated.

**Commit:** `feat(crypto-trading): widen Guardian Authority validation to PRE_ENTRY_VETO-shaped candidates via a real closed-position counterfactual pool`

---

## Task 5: Promotion pipeline

**Files:** `crypto_trading/guardian/self_improvement.py`, its test file.

**Function:**
```python
def promote_validated_heuristic_candidates(repo: Repository, now: datetime) -> int  # count promoted this call
```
For every row from a new, small `find_validated_guardian_authority_heuristic_candidates()` (add this one additional read method to Task 1's repository surface if not already present — `status='VALIDATED'`): compute `adjustment`/`confidence` using the candidate's TEST-split numbers (never train — the held-out numbers are what earned promotion, and are what should describe the heuristic's real strength) with the SAME formulas `update_heuristics_from_resolved_decisions` already uses (`deviation = test_correct_rate - 0.5`, `adjustment = deviation * _ADJUSTMENT_SCALE`, `confidence = abs(deviation) * 2.0` — import the constant, don't redefine), a deterministic `heuristic_id = f"ga-llm:{candidate_id}"`, then call the existing, **unmodified** `repo.upsert_guardian_authority_heuristic(...)`. On success, call `promote_guardian_authority_heuristic_candidate(candidate_id, heuristic_id, now)`.

**Verification you must do yourself and state explicitly in your report:** grep the whole diff for every call site of `upsert_guardian_authority_heuristic` — confirm this promotion function is the ONLY new call site this plan adds (the original, already-reviewed Task 9 self-critique call site is the only pre-existing one; both are legitimate).

**R3, co-firing cap (2026-09-16 addendum — read the design spec's "Addendum" section, R3, before implementing this part):** the `ga-llm:*` family has no natural 1:1-with-a-single-factor structure the way `ga-hc:state:*` does (`_groups_for_factors` provably caps that family at one match per decision — see its own I1 hardening comment), so an unbounded number of promoted `ga-llm:*` heuristics could co-fire on the same decision and sum their adjustments in `evaluate_heuristics`, reopening the exact multi-heuristic escalation I1 closed for the self-critique family. The user's explicit instruction: resolve this conservatively so multiple heuristics can never escalate a decision merely by co-firing. Choose ONE of the following (or an equally conservative alternative), implement it, and document the choice and its mechanism explicitly in this task's commit message and report — do not leave it as an open question past this task:
- **Cap A (promotion-time):** enforce a hard cap on the number of simultaneously-`PROMOTED` (non-demoted) `ga-llm:*` heuristics per `target_decision_type` — promotion of a new candidate that would exceed the cap is refused (log the refusal, the candidate stays `VALIDATED`, eligible again later if an existing promoted one is demoted and frees a slot).
- **Cap B (evaluation-time contribution cap):** without changing `evaluate_heuristics` itself (frozen, Global Constraint), ensure — by construction of what gets promoted and how `adjustment` is chosen — that the SUM of every simultaneously-matchable `ga-llm:*` heuristic's `adjustment` for any realistic factors dict can never exceed roughly one heuristic's own worth (e.g. by scaling each promoted `ga-llm:*` heuristic's stored `adjustment` down by the count of currently-promoted heuristics targeting an overlapping condition space at promotion time, re-scaling as the promoted set changes).
Whichever is chosen must be provable from the diff alone (grep/count-based, not "trust the LLM's proposals stay disjoint") and covered by a test that actually constructs 2+ simultaneously-matching promoted `ga-llm:*` heuristics and proves the cap holds (Cap A: the second promotion is refused; Cap B: the summed contribution to a real `evaluate_heuristics` call is bounded as documented).

**Tests:** a validated candidate is promoted with the correct `heuristic_id`/`adjustment`/`confidence` derived from its TEST numbers (assert the exact computed values against a hand-checked fixture, not just "a row exists"); the promoted real heuristic is then genuinely visible to (and only to) `repo.find_guardian_authority_heuristics()` — confirm by reading it back through that exact, unmodified method; a second promotion attempt on an already-PROMOTED candidate is a structural no-op (Task 1's own `WHERE status='VALIDATED'` guard).

**Commit:** `feat(crypto-trading): promote validated Guardian Authority heuristic candidates via the existing, unmodified real heuristics write path`

---

## Task 6: Forward-performance tracking + auto-demotion

**Files:** `crypto_trading/guardian/self_improvement.py`, its test file.

**Function:**
```python
def track_and_demote_underperforming_heuristics(repo: Repository, now: datetime) -> int  # count demoted this call
```
For every row from `find_promoted_guardian_authority_heuristic_candidates()` (skip any already carrying a non-null `demoted_at`), branch on `promoted["target_decision_type"] or "TIGHTEN_SL"` (Task 4B's field):

**TIGHTEN_SL track (as originally specified, PLUS the R1 fix — read the design spec's "Addendum" section, R1, before implementing):** gather real resolved `guardian_authority_decisions` rows where `decision_type == "TIGHTEN_SL"`, `expectation_correct is not None`, **`bool(row.get("intervention_applied"))` is True** (the fix — without it, one losing position sitting above the tighten threshold for many ticks re-saves the same decision every tick, all resolving to the same outcome, and a single trade could clear the forward sample-size bar and trigger demotion on the evidence of one position; this is the SAME filter `update_heuristics_from_resolved_decisions` and Task 4B's own `_tighten_sl_evidence_pool` already apply — reuse the identical `bool(...)`-not-`is True` form, same reasoning: SQLite round-trips a stored `True` as the int `1`), `decided_at > promoted_at` (forward-only — a heuristic's OWN real track record, never counting decisions from before it existed), and `json.loads(row["matched_heuristic_ids_json"] or "[]")` contains this candidate's `promoted_heuristic_id`. Compute forward `correct_rate`.

**PRE_ENTRY_VETO track (new, 2026-09-16 addendum — closes the gap the audit found: `resolve_pending_decisions` permanently skips resolving any real `PRE_ENTRY_VETO` decision's counterfactual, by the ORIGINAL Guardian Authority plan's own deliberate, documented scope limit — "this task has no market-data infrastructure to evaluate it... Not a bug, not a TODO" — so a promoted `PRE_ENTRY_VETO` heuristic's real fired decisions can NEVER resolve, and without this track it would have zero forward-performance safety net once it starts acting on real capital):** reuse Task 4B's own `_pre_entry_veto_evidence_pool(repo)` (unmodified — the same real closed-position counterfactual mechanism, not a new one), filtered to rows whose `closed_at` (the pool's own timestamp field) is `> promoted_at`, and further filtered to rows whose `factors` genuinely match `heuristic_condition_matches(json.loads(get_guardian_authority_heuristic_candidate(candidate_id)["condition_json"]), factors)` — the promoted heuristic's OWN condition, re-applied forward to real closed positions since promotion, exactly mirroring what validation already did retrospectively. Compute forward `correct_rate` from the pool's own `would_veto_have_been_correct` values among matches.

**Both tracks converge on the same demotion rule:** Demote (`mark_guardian_authority_heuristic_candidate_demoted` + `upsert_guardian_authority_heuristic` with `adjustment=0.0, confidence=0.0, sample_size=<forward_sample_size>`, same `heuristic_id`, keeping the row on file — never deleted) when forward `sample_size >= 15` (a smaller, explicitly-named "canary" threshold than `_MIN_SAMPLE_SIZE=30` — document why: forward real data accumulates far slower than the historical pool used for validation, and per Acceptance Criterion 5 this must be able to catch real degradation before it accumulates 30 more real decisions/closed positions) AND forward `correct_rate < 0.4` (meaningfully below the uninformative 0.5 baseline, not merely "not perfect" — pick a magnitude symmetric with `_MIN_MISCALIBRATION`'s own established floor, document the choice). Same threshold constants for both tracks — no reason to hold the two decision types to different bars.

**Binding write ordering for demotion (2026-09-16 addendum — found during Task 5's review, controller-ruled, read before implementing):** every demotion MUST call `mark_guardian_authority_heuristic_candidate_demoted` BEFORE the zeroing `upsert_guardian_authority_heuristic(adjustment=0.0, ...)` call — never the reverse, and never one without the other in the same pass. Why this order specifically: Task 5's promotion function rescales every currently-live (`demoted_at IS NULL`) `ga-llm:*` heuristic's stored `adjustment` on every promotion pass, filtering out demoted rows by checking `demoted_at IS NOT NULL`. Both this task's two writes are separately committed (no shared transaction — same as every other write pair in this table). If the zeroing upsert ran BEFORE `mark_..._demoted`, a promotion pass that happens to interleave between the two writes would see `demoted_at IS NULL` (not yet set), treat the row as still-live, and rescale it back to a nonzero value — which `mark_..._demoted` would then mark demoted anyway, leaving a permanently-live, permanently-non-rescalable nonzero heuristic acting on real capital. Demoting first closes this window: once `demoted_at` is set, Task 5's own filter excludes the row from any concurrent or subsequent rescale, regardless of what the zeroing upsert's own timing is. Add a dedicated test that seeds this exact interleaving (a promotion-triggered rescale pass running between the two demotion writes) and proves the demoted row's `adjustment` stays `0.0` / is never resurrected.

**Binding cardinality cap on the TIGHTEN_SL track (2026-09-16 addendum — found during Task 5's review, controller-ruled, read before implementing):** Task 5's Cap B rescaling (`adjustment = raw / family_size` across all live `ga-llm:*` heuristics) bounds co-firing escalation correctly, but creates a liveness trap specific to the TIGHTEN_SL track: a TIGHTEN_SL-targeted heuristic diluted below `authority_tighten_threshold` by the family's own growth can never fire again, therefore can never accumulate an `intervention_applied=True` forward sample, therefore can never be demoted via this task's own TIGHTEN_SL track above — an absorbing state that only grows as more TIGHTEN_SL candidates get promoted (fails safe, but defeats self-improvement for that half of the family over time). `PRE_ENTRY_VETO` members don't have this problem (their forward-tracking above doesn't require firing). Resolution: this task must ALSO enforce a hard cap on the number of simultaneously-live (non-demoted) TIGHTEN_SL-track `ga-llm:*` heuristics, checked at promotion time (this is naturally Task 5's own promotion function, not this task's — if Task 5 is already committed by the time you read this, this may mean a small follow-up change to `promote_validated_heuristic_candidates` itself rather than to this task's own functions; use your judgment on which function should own the check, but the cap must exist before this task's own commit lands, and must be tested) — query `find_promoted_guardian_authority_heuristic_candidates()` filtered to `target_decision_type='TIGHTEN_SL'` (or the legacy-NULL default) and `demoted_at IS NULL` (the same data Task 5's own rescale logic already reads), and refuse a new TIGHTEN_SL promotion once the cap is reached (log the refusal; the candidate stays `VALIDATED`, eligible again once an existing one is demoted and frees a slot — same refusal-not-failure pattern as Task 5's own R3 Cap-A alternative). Pick a specific cap value and document why (a small integer, conservative — this codebase's existing precedent is `_MIN_SAMPLE_SIZE = 30`'s own "deliberately conservative" framing; the cap here should similarly favor "too few TIGHTEN_SL heuristics can ever be promoted" over "unbounded accumulation of stuck ones," since the former is merely a missed opportunity and the latter degrades every other heuristic's own signal permanently). Cover with a test that fills the cap and proves the next TIGHTEN_SL promotion is refused (and that PRE_ENTRY_VETO promotions are unaffected by the TIGHTEN_SL cap, and vice versa).

**Verification you must do yourself and state explicitly in your report:** grep for every call site of `upsert_guardian_authority_heuristic` after this task — should now be exactly 3 total in the whole codebase (original Task 9 self-critique, Task 5's promotion, this task's demotion) — enumerate all three in your report. Also confirm `_pre_entry_veto_evidence_pool` gains no new call sites beyond Task 4B's own and this task's (still zero write paths near it).

**Tests:** a promoted TIGHTEN_SL heuristic with a poor forward record (sample_size>=15, correct_rate<0.4, all `intervention_applied`) gets demoted, `adjustment` becomes exactly `0.0` in the real table (confirm `evaluate_heuristics` genuinely treats this as a no-op); a fixture where excluding non-`intervention_applied` rows is the ONLY thing standing between "demoted" and "not demoted" (the R1 proof — construct many same-position repeated-tick rows without `intervention_applied` and confirm they do NOT count); a promoted PRE_ENTRY_VETO heuristic with a poor forward record (from real closed positions after `promoted_at`) also gets demoted, via the pool mechanism, not the resolved-decisions mechanism; a promoted heuristic with a good or merely-insufficient-sample forward record (either track) is untouched; decisions/closed-positions from BEFORE `promoted_at` are never counted on either track (construct a fixture where including them would flip the verdict); an already-demoted candidate is never re-processed; the demote-before-zero write-ordering test described above; the TIGHTEN_SL cardinality-cap test described above.

**Commit:** `feat(crypto-trading): add forward-performance tracking and auto-demotion for promoted Guardian Authority heuristics`

---

## Task 7: Wire the self-improvement pipeline into a periodic tick

**Files:** wherever the implementer judges cleanest (read `monitoring_loop.py` and `discovery_loop.py` first and pick one, document the choice and why — likely `discovery_loop.py` given proposal is AI-cost-gated and once/day, closer in spirit to the existing discovery cadence than the tighter monitoring tick), corresponding tests.

**Orchestrator function** (in `self_improvement.py`):
```python
def run_godfather_self_improvement_tick(
    repo: Repository, runner: AgentRunner, settings: Settings, run_id: str, now: datetime,
) -> None
```
Calls, in order, each in its own try/except with its own `log_event` name (mirroring every other multi-step orchestrator in this codebase — Shadow Mode's own `run_guardian_authority_shadow_tick`, the original Task 9's tick wiring): `propose_candidate_heuristics` (Task 3, self-gated on budget+once/day, safe to call every tick), `validate_pending_heuristic_candidates` (Task 4), `promote_validated_heuristic_candidates` (Task 5), `track_and_demote_underperforming_heuristics` (Task 6). Wire this WHOLE function into the chosen loop, in ITS OWN try/except (never share another feature's), gated by `settings.guardian.authority_enabled` (NOT `authority_shadow_enabled` — this pipeline feeds the REAL heuristics table, so it should be active whenever the real Guardian Authority feature is; while `authority_enabled` is still `false`, per this plan's own final-task ordering, this whole pipeline is correctly a no-op — nothing to gate on `authority_shadow_enabled` here, that flag governs a completely separate, already-shipped concern).

**Tests:** flag-off (still `authority_enabled: false` at this point in the plan) is a complete no-op — zero AI calls, zero writes anywhere, spy/count assertion; a failure in one pipeline step (e.g. propose) doesn't prevent the later steps (validate/promote/track) from running that same tick; full ordering is respected (a candidate proposed this same tick is NOT validated in the same call unless the implementer deliberately re-queries after propose — decide and test whichever behavior is chosen, document it).

**Commit:** `feat(crypto-trading): wire Guardian Authority self-improvement pipeline into <chosen loop>, gated by authority_enabled`

---

## Task 8: Whole-plan production-isolation test suite

**Files:** new test file, mirroring Shadow Mode's `test_authority_shadow_isolation.py` exactly in structure and rigor (read it first — it is your reference implementation, including its OWN final-review-fixed mistakes: hardcoded file list, not git-diff-derived; SHA-256 hash comparison against live source, not `git show` against a base SHA — both lessons already paid for once this session, do not reintroduce either problem).

**Checklist to encode as tests (traced 1:1 to this plan's Global Constraints):**
1. `evaluate_heuristics`, `decide_pre_entry`, `decide_open_position`, `heuristic_condition_matches`, `_compute_proposed_new_sl`, `_groups_for_factors`, `upsert_guardian_authority_heuristic`, `find_guardian_authority_heuristics` — SHA-256 of live source, hardcoded expected hashes, zero git-history dependency.
2. No file this plan touches imports `position_sizing.py`, references `set_leverage`, or is `position_opening.py`/`gate/`/`screening/`/`risk_limits.yaml`/`live_execution.yaml`'s hard-limit fields — hardcoded file list (not git-diff-derived), AST-based import scan + line-restricted textual scan for the forbidden call names, same discipline as Shadow Mode's own Task 10.
3. `upsert_guardian_authority_heuristic` has EXACTLY 3 call sites in the whole codebase (original Task 9, this plan's Task 5 promotion, this plan's Task 6 demotion) — enumerate and name all three in the test's own assertion/comment, so a 4th appearing anywhere is caught.
4. `guardian_authority_heuristic_candidates` is never read by `evaluate_heuristics`/`decide_pre_entry`/`decide_open_position` (grep-provable — these three functions never call `find_proposed_...`/`find_validated_...`/`find_promoted_guardian_authority_heuristic_candidates` at all).
5. `matched_heuristic_ids_json` is written only at the two orchestration call sites named in Task 2, never read by `decide_open_position`/`decide_pre_entry` (it is a forward-looking audit field, populated after the decision, not an input to it).
6. `authority_enabled` still defaults `false` until this plan's own final task changes it — this test suite itself must be written and pass BEFORE Task 9 flips the flag, so it's a real pre-activation gate, not written after the fact.
7. Full suite green, TDD throughout, same pre-existing/documented baseline failures only.

**Commit:** `test(crypto-trading): add production-isolation tests for Guardian Authority live autonomy pipeline`

---

## Task 9: Final regression, deep review, and `authority_enabled: true` activation

Not a code task — the plan's own closing step, executed by the controller:

1. Full repo test suite, fresh run, on the whole plan's diff.
2. Dispatch a final whole-branch review on the most capable available model, at least as deep as Shadow Mode's own final review (which found and required fixing 3 real cross-task issues no single-task review caught) — this plan is materially higher-stakes (it is what makes GODFATHER act for real, using heuristics it wrote for itself), so budget for AT LEAST one additional, dedicated review pass specifically re-deriving (not trusting) every krav9 safety claim from the diff, matching the rigor LIVE Profit Protection received (3 rounds) before this plan's own final activation.
3. Only after that review is clean (or its findings are fixed/parked per the standard SDD fix-loop): set `authority_enabled: true` in `crypto_trading/config/guardian.yaml` (a plain config commit) — this is the ONLY activation this plan performs. Document in the commit message and in the ledger, explicitly, that this makes GODFATHER's real interventions live on BOTH PAPER and LIVE (per the user's own explicit choice, no separate PAPER-only proving stage) — and re-state, in that same commit message, the exact grep/AST-provable guarantees that hold regardless (never touches leverage/sizing/position-opening-outside-the-existing-wrapper/capital-limits/screening/gate).
