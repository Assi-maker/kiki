# Guardian Authority — Live Autonomous Decisions & Self-Improvement — Design Spec

**Spec of record:** the user's explicit 9-requirement instruction (2026-09-15, Swedish, verbatim preserved in the session record) is the binding requirement this spec argues from, resolved point-by-point below. Two scope-determining questions were asked and answered before this spec was written: (1) LIVE from the start, no separate PAPER proving period; (2) full autonomous change of the bot's own signals/strategy, no human review gate.

## Why this exists, and the one non-negotiable resolution

The user asked for GODFATHER (Guardian Authority) to move from shadow-observation to actually acting — independently, on real capital, with no human in the loop for individual decisions or for improving its own strategy — while simultaneously requiring (krav9) that it can **never**, autonomously: increase leverage, remove the last stop-loss, move a stop-loss to a worse price, martingale, open unlimited positions, or change the capital limit.

**These two asks are only simultaneously satisfiable if the "full autonomy" capability is architecturally unable to reach the code/config that enforces krav9's limits.** A system that can freely rewrite any of its own source files can just as easily rewrite the file that enforces "never loosen the stop-loss" — whether by a bug, an emergent bad optimization, or an edge case in its own self-modification logic. This is not a caution the user needs to accept as a tradeoff — it is a direct logical fact about what "unrestricted self-modification" and "hard-coded restriction" mean together. **Resolution: split the system into two layers.**

- **The safety kernel** — leverage, SL-direction, position-count cap, capital limit, martingale-impossibility — is not new code to write. It is the set of structural guarantees Guardian Authority's ORIGINAL design already established and this session already proved, re-proved, and re-proved again under three separate whole-branch reviews: GODFATHER never calls `set_leverage`, never imports `position_sizing.py` (so it structurally cannot martingale — it has no sizing authority at all, full stop), never opens a position itself (only vetoes a Gate-approved one or lets it through unchanged), never touches `risk_limits.yaml`/leverage config, and can only ever tighten a stop-loss under an independently-enforced, freshly-verified `new_sl > current_sl` invariant (never loosen). This spec's job re: the safety kernel is **extend the isolation-test discipline this session already established** (grep/AST-provable, independently re-derived by reviewers who broke and re-broke the checks live) to cover every new file this spec adds — not invent a new mechanism.
- **The self-improving brain** — free to analyze, propose, validate, and act on its own conclusions with zero human click — is scoped to exactly one narrow, already-proven, already-isolated surface: **the `guardian_authority_heuristics` table**, read only by `evaluate_heuristics` inside the pure, deterministic, already-fuzz-tested `decide_pre_entry`/`decide_open_position`. Those two functions and their supporting pure logic (`evaluate_heuristics`, `heuristic_condition_matches`, `_compute_proposed_new_sl`, `_groups_for_factors`) are **never modified by this plan** — same Global Constraint every prior Guardian Authority plan has held, re-verified again here. GODFATHER's "self-modification of signals" happens by autonomously writing new rows into that one table, through a validated pipeline this spec builds — never by editing a `.py` file.

This gives the user everything they asked for — full autonomy, no human click, real capital, a system that gets smarter over time by analyzing its own history and combining the best available evidence — while making krav9's own limits a physical property of the system, not a policy GODFATHER is trusted to remember.

## What "self-improvement" concretely means here

The cold-start deadlock (documented in the prior Shadow Mode spec: with zero heuristics, `evaluate_heuristics` always sums to `0.0`, so every real decision defaults to `APPROVE`/`NO_ACTION` forever) is closed **for real**, not just for shadow data, by a fully autonomous pipeline:

1. **Propose** — an LLM call (reusing the existing `AgentRunner`/agent-role/budget-gate pattern this codebase already uses for its 7 screening roles) analyzes the accumulated evidence — resolved shadow decisions (`guardian_authority_shadow_observations`), resolved real decisions (`guardian_authority_decisions`), Detective's post-trade analysis (`crypto_trading/detective/`) — and proposes candidate heuristics: `condition_json` + `adjustment` + a rationale, as a schema-validated structured output (same validation discipline as every other agent role). This is where "analyze, explore, think, combine the best methods and information" (krav2) genuinely lives — an LLM, not a fixed formula, is what can notice a pattern across factors the state-alone grouping (by design, per I1's own hardening) cannot.
2. **Validate** — every proposed candidate is checked against a genuine, held-out, out-of-sample test: split all resolved TIGHTEN_SL-shaped decisions (shadow + real) by `decided_at` into train/test (same `split_cutoff` discipline `run_tier1_backtest` already established and this project already trusts), require the candidate's condition to clear `_MIN_SAMPLE_SIZE`/`_MIN_MISCALIBRATION` on the train split (informational) AND independently on the TEST split (the actual bar) — reusing the exact statistical machinery Task 8/original Task 9 already built and this session already hardened (I1's co-firing fix, I3's Brier framing), not reinventing validation.
3. **Promote** — a candidate that clears the out-of-sample bar is written into the real `guardian_authority_heuristics` table via the existing, **unmodified** `upsert_guardian_authority_heuristic` — the exact same narrow write path the original plan already reviewed. No new write path into the real table is created by this spec.
4. **Track & self-correct** — every real decision's `guardian_authority_decisions` row is additively tagged with which heuristic(s) matched it (a new nullable column, populated at the orchestration layer — never inside `decide_open_position`/`decide_pre_entry` themselves). A periodic pass computes each promoted heuristic's OWN forward real-world correct-rate; if it degrades meaningfully below what got it promoted, the heuristic is demoted (re-upserted with `adjustment=0.0` — a no-op in `evaluate_heuristics`'s summation, same table, same unmodified write method, full audit trail preserved, nothing deleted). This is krav5's "kritisera sig själv... förbättra sig själv" running forever, fully autonomously, on real outcomes.

Nothing above ever touches a `.py` source file, `risk_limits.yaml`, `live_execution.yaml`, the Gate, the screening pipeline, or any AI role definition outside GODFATHER's own two new roles. "Improving the signals" is real, autonomous, continuous, and permanently confined to one table whose only reader is the already-proven pure decision core.

## Global Constraints (binding, same rigor as every prior Guardian Authority plan — re-verified here, not assumed)

- Never modify `evaluate_heuristics`, `decide_pre_entry`, `decide_open_position`, `heuristic_condition_matches`, `_compute_proposed_new_sl`, `_groups_for_factors`, `upsert_guardian_authority_heuristic`, `find_guardian_authority_heuristics` — byte-identical before/after this whole plan, verified the same way Shadow Mode's Task 10 verified it (SHA-256 of each function's live source, no git-history dependency).
- Never modify `crypto_trading/paper_trading/position_opening.py`, `position_sizing.py`, `crypto_trading/gate/`, `crypto_trading/screening/`, any pre-existing AI role `.md` file, `crypto_trading/config/risk_limits.yaml`, `live_execution.yaml`'s hard limits (leverage, margin, max_concurrent_positions), or any capital-limit config value anywhere.
- Never call `set_leverage`. Never import `position_sizing.py` anywhere in this plan's diff (this is what makes martingale structurally impossible for GODFATHER — it has zero sizing authority, not a smaller allowance).
- Never call `open_position_for_candidate` except through the existing, unmodified `maybe_open_position_for_candidate` wrapper — this plan adds no new call site of it.
- Every SL-tightening write path (already built, unmodified by this plan) continues to assert `new_sl > current_sl` against a freshly-read value immediately before the write — this plan adds no new SL-write path at all; it only makes the EXISTING, already-reviewed path reachable for real by flipping `authority_enabled`.
- The new heuristic-candidate pipeline writes ONLY to its own new table (`guardian_authority_heuristic_candidates`) until a candidate is promoted, at which point promotion is exactly one call to the existing, unmodified `upsert_guardian_authority_heuristic` — no other write path into `guardian_authority_heuristics` exists anywhere in this plan.
- The new LLM-proposal role is read-only against the real trading pipeline — it never calls, imports, or is imported by `position_opening.py`, `position_sizing.py`, `gate/`, `screening/`, or any other AI role's `.md`/loading code.
- `authority_enabled: true` (the actual LIVE activation) is the plan's LAST step, gated behind full TDD + the same multi-round deep-review rigor every other LIVE-money feature in this project received (LIVE Profit Protection got 3 rounds before being trusted with real orders) — not a lighter bar just because the user waived the PAPER proving period.
- TDD throughout. Full suite green before any task is considered done.

## New data model

### `guardian_authority_heuristic_candidates` (LLM-proposed, pending/validated/promoted/rejected — never read by the real decision engine)

```
candidate_id        TEXT PRIMARY KEY
proposed_at         TEXT NOT NULL
description         TEXT NOT NULL
condition_json       TEXT NOT NULL       -- same matching semantics as the real table (heuristic_condition_matches)
proposed_adjustment  REAL NOT NULL
rationale            TEXT NOT NULL       -- LLM's own explanation, for audit/observability
status               TEXT NOT NULL       -- 'PROPOSED' -> 'VALIDATED' -> 'PROMOTED' | 'REJECTED'
train_sample_size    INTEGER
train_correct_rate   REAL
test_sample_size     INTEGER
test_correct_rate    REAL
validated_at         TEXT
promoted_at          TEXT
promoted_heuristic_id TEXT             -- the real heuristics-table row this became, once promoted
rejected_reason       TEXT
run_id               TEXT NOT NULL
```

### `guardian_authority_decisions` — one additive, nullable column (idempotent migration, same pattern as `intervention_applied`/`run_id` before it)

```
matched_heuristic_ids_json TEXT   -- nullable; the real heuristic_ids that matched at decision time, for forward-performance tracking. Populated at the ORCHESTRATION layer (tick.py/authority.py's wrapper functions) via a second, duplicate, read-only evaluate_heuristics() call — never by widening decide_open_position/decide_pre_entry's own return signature.
```

## New agent role

`crypto_trading/agents/crypto-godfather-strategist.md` (new file, follows this codebase's existing `AgentDefinition` frontmatter format exactly): reads resolved shadow + real decision data and Detective history, proposes candidate heuristics as structured JSON (new `AssessmentBase`-derived schema, same validation discipline as every other role's output). Read-only against everything except its own output.

## Acceptance criteria

1. `evaluate_heuristics`/`decide_pre_entry`/`decide_open_position`/`heuristic_condition_matches`/`_compute_proposed_new_sl`/`_groups_for_factors`/`upsert_guardian_authority_heuristic`/`find_guardian_authority_heuristics` are byte-identical before/after the whole plan (SHA-256, no git-history dependency).
2. No file this plan touches imports `position_sizing.py`, references `set_leverage`, or is `position_opening.py`/`gate/`/`screening/`/`risk_limits.yaml`/`live_execution.yaml`'s hard-limit fields.
3. A candidate heuristic can only ever reach the real `guardian_authority_heuristics` table via the existing, unmodified `upsert_guardian_authority_heuristic` — no other write path into that table exists anywhere in the new code.
4. A candidate must clear the sample-size/miscalibration bar on a genuinely held-out test split (not just train) before promotion — provable with a fixture where train-only validation would wrongly promote a noise pattern that the test split correctly rejects.
5. A promoted heuristic whose real forward performance degrades gets demoted (adjustment set to 0.0) automatically, with full audit trail (nothing deleted, `guardian_authority_heuristic_candidates` still shows `PROMOTED`, a log event records the demotion and why).
6. With `authority_enabled: true` (the plan's final state), GODFATHER can autonomously veto a real entry, tighten a real stop-loss (PAPER or LIVE), or close a real position early — using heuristics it authored and validated for itself, with zero human click at any point in the pipeline — while every krav9 constraint remains independently, structurally, grep/AST-provably unreachable by the whole plan's diff.
7. Full test suite green, TDD throughout, same pre-existing/documented baseline failures only.
