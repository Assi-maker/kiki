# Guardian Authority ("Godfather") — Design Spec

**Status:** Approved for spec-writing 2026-09-14, after an explicit user decision to proceed despite the tension with this project's own "never change trading logic without narrow, explicit approval" norm — resolved by hard, code-enforced guardrails (this spec's "Safety architecture" section) rather than by removing the norm.

**Goal:** An autonomous decision-making extension of the existing Guardian role that can (a) decline a Gate-CONFIRMED signal before a position ever opens, (b) tighten (never loosen) a stop-loss on an already-open position, and (c) close a position early — using its own growing, self-maintained memory of past decisions, their *pre-decision expectations*, and their *actual outcomes*, to improve its judgment over time. It never modifies the bot's production strategy code, never touches leverage/sizing/capital limits, and can only ever reduce risk exposure, never increase it.

**Spec of record:** This document + the user's own numbered requirements (krav 1–9) from the 2026-09-14 conversation, reproduced in "Requirements traceability" below.

## Why this exists (read before implementing)

The user explicitly wants an agent that acts independently, without per-decision human sign-off — a real departure from every other piece of this codebase, which the user themselves built around narrow, explicitly-approved, deeply-reviewed changes (see the LIVE Profit Protection work earlier this session: 2 real Critical bugs found and fixed across 6 review rounds, all before real capital was ever put at risk). The resolution the user chose is **not** "trust the AI's judgment unsupervised" — it is "give the AI real decision latitude *within hard, structural guardrails that make the worst outcomes impossible by construction*, and require the same rigorous, reviewed engineering process to BUILD it that every other real-money change in this codebase has required." Implementers and reviewers must hold both halves of that at once: real autonomy in the decision loop, zero autonomy over the guardrails themselves.

## Requirements traceability (verbatim intent, from the user)

1. Analyze everything, decide independently what's best for the given signal/position.
2. Analyze/explore/think, including historical data, to improve decisions — identify and combine the best methods/information for the specific situation.
3. Not overcomplicated — smart, decides on its own, does not consult the user per-decision.
4. Primary goal: make every trade/position more efficient — increase profit, decrease loss.
5. Can criticize and improve itself, and — the user's own words — "ändra och förbättra signaler också på den nuvarande boten." **Resolved** (explicit user choice in this conversation): it improves its own internal heuristics/memory only; it never writes to the bot's production strategy code. This satisfies the spirit (the bot's *effective* behavior improves, via Guardian Authority's decisions) without the literal, most dangerous reading (autonomous code self-modification).
6. Protect the user's capital; never expose them to large risk.
7. Effective, reliable, profitable, smart, continuously evolving, learns from mistakes, adapts to the situation.
8. Understand what is "brus" (noise) vs. "edge," and adapt accordingly.
9. **Hard limits, never overridable by its own judgment:** never increases leverage; never removes the last stop-loss; never moves a stop-loss further away; never martingales; never opens unlimited/additional new positions; never changes the capital limit.
10. (This conversation, added after design approval) **Must record its expectation BEFORE a decision plays out, not only the outcome afterward** — every decision is logged with an explicit, immutable, timestamped prediction at decision time, so later self-critique compares "was I right," not just "did it go well."

## Non-goals / Global Constraints

- Never modifies `crypto_trading/paper_trading/position_opening.py`, `position_sizing.py`, `crypto_trading/gate/`, `crypto_trading/screening/`, any AI role definition, `crypto_trading/config/risk_limits.yaml`, `crypto_trading/config/live_execution.yaml`'s existing fields, or any leverage/capital-limit value anywhere.
- Never calls `BingXLiveTradingConnector.set_leverage`, never imports `position_sizing.py`'s `compute_position_size`, never opens a new position (no code path creates a `positions` row or a `live_executions`/`demo_executions` claim).
- Never modifies PAPER, Demo, LIVE entry logic, Gate, Risk, AI roles, or the historical replay/backtest package (`crypto_trading/backtest/`).
- Never loosens or removes a stop-loss — every SL-tightening write path has a **hard runtime assertion** that the new SL is strictly closer to entry than the current one (for LONG: `new_sl > current_sl`); a violation refuses the write and logs an error, it never clamps or "helps."
- Reuses existing, already-reviewed close/order-modification mechanisms wherever they exist — never invents a second, parallel way to close a position or move a stop-loss. Specifically: an early close reuses the exact existing `guardian_exit` mechanism (`position_closing.py`'s handling of a `state == "EXIT"` Guardian observation, and — for LIVE — `live_execution.py::close_guardian_exit_positions`, both unmodified); an SL tightening on LIVE reuses the exact connector primitives already built and deeply reviewed for LIVE Profit Protection (`place_stop_loss_order`/`cancel_order` on `BingXLiveTradingConnector`) via the same add-before-remove sequence discipline (place new, verify active, only then cancel old — never the reverse).
- LIVE Profit Protection (already live, +1.0% → break-even) is completely unaffected — Guardian Authority is a separate mechanism that may ALSO tighten a stop-loss, but the two must never race each other on the same position (see "Interaction with Profit Protection" below).
- Scope: LIVE and PAPER. Demo is out of scope (already disabled per the user's own recent decision).
- No new AI-role definition changes; if this uses the LLM at all, it is additive, following the exact same budget/cost-guard pattern already established (`_budget_allows_one_more_call` in `guardian/tick.py`), never a new unbounded spend surface.

## Architecture

New module(s) under `crypto_trading/guardian/`, e.g. `crypto_trading/guardian/authority.py` (+ its own data/memory helpers) — **reuses**, never duplicates, Guardian's existing factor computation and evidence-fetching (`guardian/deterministic.py`, `guardian/data.py`, `guardian/ai_context.py`). Two integration points:

### 1. Pre-entry hook

Runs once per Gate-CONFIRMED candidate, **after** Gate/Risk/AI/QA have already independently confirmed it (never instead of them — this is one more NO available after every existing YES, never a new way to say YES), **before** `open_position_for_candidate` is called. Consults Guardian Authority's own memory (below) for whether this candidate's evidence pattern resembles past decisions with poor outcomes. Two possible results: `APPROVE` (default; the existing flow proceeds unchanged) or `VETO` (the position is never opened — same "skip, log, continue" pattern already used elsewhere in this codebase for a candidate that can't proceed, e.g. `position_opening.py`'s non-numeric-risk-value skip). A veto is always logged with its expectation (see "Memory" below) at decision time.

### 2. Open-position tick

Extends `guardian/tick.py::process_one_position` — runs immediately **after** the existing deterministic decay-score/state computation (completely unmodified: `classify_guardian_state` etc. keep computing HOLD/WATCH/PROTECT/EXIT exactly as today). Guardian Authority then makes one additional, independent decision using that state plus its own memory: `NO_ACTION` (default), `TIGHTEN_SL` (propose a new, strictly-closer stop-loss), or `CLOSE_EARLY` (equivalent to the existing EXIT state's effect, but decided by Guardian Authority's own judgment rather than only the fixed decay-score formula — implemented by reusing the exact same downstream close mechanism, never a second one).

### Interaction with Profit Protection

Both Guardian Authority (`TIGHTEN_SL`) and Profit Protection (fixed +1.0% → break-even) can move the same LIVE position's stop-loss. They must never race: Guardian Authority's `TIGHTEN_SL` path reuses the *exact* LIVE Profit Protection idempotency/claim discipline (a position-scoped claim/lock before any exchange write, exactly like `live_profit_protection`'s table), and — critically — **only ever tightens further than whatever the current real SL already is**, read fresh from the exchange immediately before acting (same "exchange state is source of truth" principle as Profit Protection). If Profit Protection already moved the SL to break-even and Guardian Authority separately judges a tighter stop is warranted, that is still a valid, safe tightening (strictly closer than break-even) — never a conflict, only ever a ratchet in one direction.

## Memory / self-improvement

New table, e.g. `guardian_authority_decisions`:

```sql
CREATE TABLE IF NOT EXISTS guardian_authority_decisions (
    decision_id TEXT PRIMARY KEY,
    position_id TEXT,              -- NULL for a pre-entry veto (no position exists yet)
    candidate_id TEXT NOT NULL,
    decision_type TEXT NOT NULL,   -- PRE_ENTRY_VETO | TIGHTEN_SL | CLOSE_EARLY
    decided_at TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    -- Requirement 10 (this conversation): the expectation is written in the
    -- SAME transaction as the decision, at decided_at, and is NEVER updated
    -- afterward - it is the record of what Guardian Authority believed
    -- BEFORE the outcome was known, immutable by construction.
    expected_outcome TEXT NOT NULL,        -- free text: what it predicts will happen
    expected_direction TEXT NOT NULL,      -- 'favorable' | 'unfavorable' | 'neutral' (the position's expected P/L direction if left alone, or the expected benefit of the action taken)
    confidence REAL,                       -- optional, 0-1, self-reported
    -- Filled in later, once genuinely known - never guessed, never backfilled early:
    outcome_status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | RESOLVED
    actual_exit_reason TEXT,
    actual_pnl_usdt TEXT,
    expectation_correct BOOLEAN,           -- computed at resolution time by comparing expected_direction to actual outcome
    resolved_at TEXT,
    -- SL_TIGHTEN-specific:
    old_sl TEXT,
    new_sl TEXT,
    -- audit
    run_id TEXT NOT NULL
);
```

**Only actual interventions get a row.** The default outcome of both hooks — pre-entry `APPROVE` and tick-time `NO_ACTION` — is NOT logged as a `guardian_authority_decisions` row; only `PRE_ENTRY_VETO`, `TIGHTEN_SL`, and `CLOSE_EARLY` are. This keeps the table bounded to genuine decisions (Guardian's own existing `guardian_observations` table already records every tick's deterministic state for every open position — Guardian Authority's table is deliberately a smaller, decision-only log layered on top, not a duplicate of that).

A **resolution pass** (part of the same tick loop, or a small dedicated step) finds `outcome_status = 'PENDING'` rows whose position has since closed (any reason), fills in the actual outcome fields, and computes `expectation_correct`. This is the raw material for self-improvement: Guardian Authority's own "how well-calibrated am I" signal, per-decision-type, not just an aggregate win rate.

**Self-critique step:** periodically (or opportunistically, e.g. once N new resolutions have accumulated) re-reads its own resolved decisions, looking specifically for patterns where `expectation_correct = False` clusters around some identifiable feature (an instrument class, a signal type, a market-regime bucket — reusing the exact same kind of factor breakdown this conversation's manual historical analysis already demonstrated). The output is an update to Guardian Authority's **own internal heuristics representation** — a data structure (not code) it reads fresh at the start of every future decision. This is architecturally the same pattern Detective already uses (`detective_analyses`, an AI role reasoning over its own accumulated history), extended to be prediction/calibration-focused (comparing pre-registered expectations to outcomes) rather than purely retrospective batch narrative.

## Safety architecture (structural, not policy — traces to requirement 9)

| Guardrail | How it's structurally guaranteed |
|---|---|
| Never increases leverage | No function in this module ever calls `set_leverage` or reads/writes any leverage config field. Verified by an import-scan test (same pattern as the Tier 1/LIVE-PP "production isolation" tests). |
| Never removes/loosens a stop-loss | Every SL-write path asserts `new_sl` is strictly closer to entry than the *freshly re-read* current real SL, immediately before the write; violation refuses and logs, never clamps. |
| Never martingales / changes sizing | No function in this module ever calls or imports `position_sizing.py`. |
| Never opens unlimited/additional positions | This module has no code path that calls `open_position_for_candidate`, `create_position_with_event`, or any LIVE/PAPER/Demo entry-claim method — its only two possible actions on a not-yet-open candidate are "do nothing" (implicit APPROVE) or `PRE_ENTRY_VETO`. |
| Never changes the capital limit | No function in this module reads or writes `starting_capital_usdt`, `max_total_exposure_pct`, `margin_per_trade_usdt`, or any other capital-defining config field. |
| Never a second, parallel close/order-modification mechanism | `CLOSE_EARLY` reuses the existing `guardian_exit` state/close path unmodified; `TIGHTEN_SL` reuses the existing, already-reviewed LIVE `place_stop_loss_order`/`cancel_order` primitives via the identical add-before-remove discipline already proven safe for Profit Protection. |
| Never silently modifies production strategy | This module never writes to any `.py` or `.yaml` file under `crypto_trading/` — its only writes are to its own new table and (via the reused, unmodified mechanisms above) to position/order state, exactly like every other role in this codebase. |

## Test plan (required before any write-path code is considered done)

1. Pre-entry veto: a candidate matching a known-poor historical pattern is vetoed; no position row is ever created; the expectation is recorded before the veto decision is finalized.
2. Pre-entry approve (default): a candidate with no adverse pattern proceeds exactly as today, byte-identical to the pre-Guardian-Authority flow.
3. `TIGHTEN_SL` on LIVE: new SL strictly closer than current — succeeds, add-before-remove verified (reuse the exact test techniques already proven for LIVE Profit Protection: a stub connector, explicit call-order assertions).
4. `TIGHTEN_SL` attempted with a computed new SL that is NOT strictly closer (an internal bug simulation) — the hard assertion refuses the write; old SL is untouched; this is tested explicitly, not just trusted.
5. `TIGHTEN_SL` racing an already-in-flight Profit Protection SL move on the same position — both resolve safely, no double-write, no lost update (reuse of PP's claim/idempotency discipline verified in this new context).
6. `CLOSE_EARLY` reuses the exact existing `guardian_exit` path — verified by asserting the SAME function/code path is invoked, not a new one.
7. Expectation is recorded at decision time and is provably never mutated afterward (a test asserts the `expected_outcome`/`expected_direction`/`confidence` columns are identical before and after the resolution pass runs).
8. Resolution pass correctly computes `expectation_correct` for both a correct and an incorrect prediction, only once the position has genuinely closed (never early/guessed).
9. Import-scan tests proving zero references to `set_leverage`, `position_sizing.py`, any position-opening/claim function, and any capital-limit config field anywhere in this module — same discipline as Tier 1/LIVE-PP's production-isolation tests.
10. Self-critique step produces a heuristics update from a fixture of resolved decisions with a known miscalibration pattern, and that update is provably only ever read by *future* decisions, never applied retroactively to already-resolved ones.

## Acceptance criteria

- All tests above pass; full existing project suite remains green.
- `git diff --stat` against the pre-implementation commit touches only new files plus the two integration seams (the pre-entry call site and `guardian/tick.py`'s post-state-computation hook) — `position_opening.py`'s and `position_sizing.py`'s own bodies are unchanged (only a new call site is added where the pre-entry hook is invoked, not a change to their internal logic).
- Default-off behind a config flag, matching every other real-money-adjacent feature this session (`profit_protection_enabled`'s own precedent) — landing the code changes nothing about live behavior until explicitly activated.
- A read-only report (or reuse of the existing dashboard/Detective infrastructure) can show: how many decisions of each type, how many resolved, calibration accuracy (`expectation_correct` rate) per decision type — this is what makes "blir smarter" *verifiable*, not just claimed.

## Open questions for the implementation plan to resolve explicitly

These are plan-level, not spec-level, decisions — flagged here so the plan doesn't silently guess:
- Exact heuristics-representation format (a scored rule list? a small structured JSON the pre-entry/tick hooks read? must be simple enough to reason about and test deterministically).
- Exact self-critique cadence (every tick? every N resolutions? a daily batch like Detective's own?).
- Exact LLM involvement, if any, in the decision itself (vs. the existing Guardian pattern where the AI only narrates an already-deterministic decision) — and if the LLM is involved in decision-making itself (not just narration), how its cost is bounded, consistent with `_budget_allows_one_more_call`.
