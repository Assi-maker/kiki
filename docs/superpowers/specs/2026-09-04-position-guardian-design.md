# Post-Entry Position Guardian — Design Spec

Date: 2026-09-04
Status: Approved by user (direction), pending implementation plan.

## 1. Motivation

A candidate is judged once, at entry: seven AI roles plus a deterministic
Gate decide a trade is worth taking. After entry, the market keeps moving,
but nothing in the system re-asks "does this trade's original edge still
hold?" — PAPER/Demo only ever check the hard boundaries (stop_loss/target/
time_limit). Guardian is a **separate, post-entry-only observer** that
continuously re-evaluates each already-open position's original thesis
strength, so a later (out of scope for v1) active mode could protect an
achieved gain when edge is fading, instead of always waiting passively for
a hard stop or target.

## 2. Non-goals / hard boundaries (user-mandated, v1)

1. **Shadow mode only.** Guardian may not close, modify, or otherwise affect
   any position, in PAPER or Demo. No code path capable of that exists in
   this version — not a runtime check that could be bypassed, an absent
   capability.
2. **State is 100% deterministic.** `HOLD`/`WATCH`/`PROTECT`/`EXIT` is
   computed by a pure function of deterministic inputs. AI may only attach
   interpretive `reasoning` text to an already-decided state; it has no
   parameter, field, or code path that can set or change the state.
3. **AI is called only when the deterministic layer already flagged a state
   transition** — never every tick, never speculatively.
4. **Shares the existing daily AI budget** with the seven roles and
   Detective (`settings.budget_limits.max_ai_calls_per_day`/
   `max_daily_ai_cost_usd`). Never bypassed, never a fallback call when
   exhausted — the deterministic observation is still recorded either way.
5. **`guardian_observations` is append-only** (DB triggers reject UPDATE/
   DELETE, same pattern as the existing `events` table) and is the *only*
   table this subsystem writes to. No other trading state is ever mutated.
6. **Full restart-safety**, achieved by construction: every write is a new,
   independently-idempotent INSERT of an observation row; nothing is
   "claimed" or requires crash-recovery logic (see §8).
7. **Detective integration is additive only** — extra context for its
   existing read-only, hypothesis-only analysis. Detective's own hard
   boundary (never acts, never changes strategy) is unchanged.
8. **Zero changes** to entry strategy, quant screener, Opportunity Screener,
   the seven AI roles, the Gate, Risk Agent, position sizing, SL/TP logic,
   or the BingX Demo execution built earlier today.

## 3. Architecture

New, self-contained package `crypto_trading/guardian/` + a top-level
`guardian_loop.py`, wired into `run.py` as an **eighth, opt-in daemon
thread** (`CRYPTO_TRADING_GUARDIAN_ENABLED`, default off — same pattern as
`CRYPTO_TRADING_DEMO_EXECUTION_ENABLED`). Guardian:

- reads `positions` (PAPER is the system of record for "is this trade still
  open" — unaffected by Demo's independent lifecycle) — **never writes to
  it**
- reads `candidates.evidence_record` (the entry-time evidence already
  persisted by the quant screener) — never writes to `candidates`
- writes exclusively to the new `guardian_observations` table

```
crypto_trading/guardian/
    __init__.py
    deterministic.py   # pure factor computations, decay_score, state classification
    data.py             # fresh evidence fetching (klines/funding -> CandidateEvidenceRecord, BTC regime)
    ai_context.py       # AI invocation gating + context building
    tick.py              # orchestrates one tick across all open positions
crypto_trading/guardian_loop.py       # run_guardian_tick()/run_forever(), same shape as demo_execution_loop.py
crypto_trading/schemas/guardian.py    # GuardianState, GuardianObservation, GuardianAssessment
crypto_trading/config/guardian.yaml + GuardianConfig (config/loader.py)
.claude/agents/crypto-guardian.md
```

## 4. Deterministic factors

All factors are pure functions, bounded to `[0, 1]` where **0 = no decay
observed, 1 = maximum decay** — the same "value vs. its own reference,
clipped to [0,1]" transparency `screening/quant_screener.py::
_compute_candidate_score()` already uses, not a black box. Each reuses
`quant_screener.py`'s existing, already-tested evidence-builder functions
run again against **freshly fetched** data — no new indicator math.

| Factor | Formula | Reference data |
|---|---|---|
| `time_decay_factor` | `clip(elapsed_hours / max_position_hold_hours, 0, 1)` | `risk_limits.max_position_hold_hours` (existing config) |
| `momentum_decay_factor` | `clip((rsi_entry - rsi_now) / (rsi_entry - 50), 0, 1)` if `rsi_entry > 50`, else `0` | `candidate.evidence_record.momentum_breakout_evidence.value` vs. fresh `build_momentum_breakout_evidence()` |
| `volume_decay_factor` | `clip((zscore_entry - zscore_now) / zscore_entry, 0, 1)` if `zscore_entry > 0`, else `0` | `candidate.evidence_record.volume_evidence.value` vs. fresh `build_volume_evidence()` |
| `funding_decay_factor` | `clip((funding_mag_entry - funding_mag_now) / funding_mag_entry, 0, 1)` if `funding_mag_entry > 0`, else `0` | `candidate.evidence_record.funding_oi_evidence.value` vs. fresh `build_funding_oi_evidence()` (magnitude-only, matching what's already stored — see §4.1) |
| `secondary_confirmation_lost_factor` | `1.0` if entry's `secondary_timeframe_evidence` had ≥1 evidence triggered AND the freshly-recomputed one (same recorded secondary timeframe) has 0 triggered; else `0.0` | `candidate.evidence_record.secondary_timeframe_evidence` |
| `market_regime_against_position_factor` | `clip((50 - btc_rsi_now) / 50, 0, 1)` | Fresh `build_momentum_breakout_evidence()` on BTC-USDT (LONG-only system, so a bearish/neutral market-wide RSI is a headwind) |

**`decay_score`** = weighted mean of the six factors above. Weights default
to `1/6` each in `guardian.yaml` (`GuardianConfig.factor_weights`), not
hardcoded in Python — explicitly so they can be re-tuned after Detective's
calibration (§9) without a code change.

**`progress_ratio`** (kept separate from `decay_score` — a directional
measure, not a decay measure): `(current_price - entry_price) / (target_price
- entry_price)` for the LONG-only system. ~0 at entry, ~1.0 near target,
negative moving toward/through stop.

**`unrealized_pnl`**: same gross formula already used by
`performance/paper_track_report.py::_unrealized_pnl()` (`position.size *
(current_price - position.simulated_fill_entry) / position.simulated_fill_entry`),
reimplemented as a small pure function in `guardian/deterministic.py` (no
import of the `performance` reporting module into a live loop).

### 4.1 Known limitation, stated explicitly

`funding_oi_evidence.value` is a magnitude (`abs(funding_rate) * 100`), not
a signed rate — `quant_screener.py` never stored the sign. `funding_decay_factor`
is therefore direction-agnostic ("has funding pressure calmed down since
entry", not "has it flipped against the position"). Flagged here rather
than silently treated as more precise than it is; a future iteration could
add a signed-funding factor if Detective's calibration shows this one is
too weak.

## 5. State classification (pure function)

```python
def classify_guardian_state(
    decay_score: Decimal, unrealized_pnl_positive: bool, config: GuardianConfig,
) -> GuardianState:
    if decay_score < config.watch_decay_threshold:
        return "HOLD"
    if decay_score < config.protect_decay_threshold:
        return "WATCH"
    if decay_score < config.exit_decay_threshold:
        # PROTECT implies protecting an *achieved* gain - with no profit yet,
        # closer monitoring (WATCH) is the honest label, not PROTECT.
        return "PROTECT" if unrealized_pnl_positive else "WATCH"
    return "EXIT"  # thesis sufficiently broken regardless of PnL sign -
                    # "don't wait passively for the hard stop" (user's own framing)
```

Default thresholds (`guardian.yaml`, explicitly labeled as unvalidated
starting points, not tuned): `watch_decay_threshold=0.35`,
`protect_decay_threshold=0.55`, `exit_decay_threshold=0.75`.

This directly implements the user's framing: *keep the winner while edge
holds (HOLD), protect the gain once edge visibly fades (PROTECT, only when
there is a gain to protect), don't wait passively for the stop once the
thesis is clearly broken (EXIT, independent of PnL sign).*

## 6. When AI is called

`ai_context.py::should_invoke_ai(previous_observation, new_state) -> bool`:

```python
return new_state != "HOLD" and (
    previous_observation is None or previous_observation["state"] != new_state
)
```

AI fires only on a **transition into, or between, non-HOLD states** (first
observation landing outside HOLD counts as a transition too) — never
repeatedly while a position sits unchanged in the same state, never for a
transition back down to HOLD (nothing to explain). This is checked against
the **last persisted observation** for that position — restart-safe by
construction (§8), no separate claim/lock needed.

When AI is warranted: a new, small role `crypto-guardian` (same
`.claude/agents/*.md` + `AgentRunner.run()` machinery as every other role,
**not** part of `gate/risk_signal_gate.py::_REQUIRED_ROLES`, never in the
Gate's decision path) receives the six deterministic factors, `decay_score`,
`progress_ratio`, `unrealized_pnl`, and the position's original
`bull_thesis`/`risk`/`forecast` assessment text (already stored on the
candidate) as context, and returns a `GuardianAssessment(AssessmentBase)`
with a single `reasoning: str` field — no state field exists on this model
for AI to set.

## 7. Budget (shared, never bypassed)

Before every AI-warranted call, the exact same check `detective/batch.py`
already performs: `daily_count_at_start + 1 > max_ai_calls_per_day` or
`daily_cost_at_start + _WORST_CASE_COST_PER_CALL_USD > max_daily_ai_cost_usd`
→ skip the AI call, log `guardian_ai_deferred_budget`, **still persist the
deterministic observation** (state/decay_score/factors) with
`ai_reasoning=None`. The deterministic layer never depends on AI succeeding.
Guardian's call is a single small role (like one of the seven), so it
reuses `orchestrator.py`'s existing `_WORST_CASE_COST_PER_CALL_USD =
Decimal("0.20")` constant directly rather than inventing a new figure.

## 8. Restart-safety

Every `guardian_observations` row is `INSERT OR IGNORE`, PK =
`f"{position_id}:{observed_at.isoformat()}"` — a duplicate tick (e.g. a
restart re-observing at a coincidentally identical timestamp) is naturally
absorbed, never a duplicate row or an error. AI-invocation gating (§6)
reads the **last persisted** observation fresh each tick, so a crash
between computing the deterministic state and (optionally) calling AI just
means: next tick recomputes from scratch, sees no new persisted transition
yet, and correctly re-evaluates — never a double AI bill, never a lost
observation beyond the one in-flight tick.

## 9. Detective integration (additive, calibration-only)

`detective/context.py::build_position_analysis_context()` gains an
additional, optional field: when `guardian_observations` exist for a
position being analyzed, its full state-trajectory (`observed_at`, `state`,
`decay_score`) is included, chronological. New repository method
`find_guardian_observations_for_position(position_id) -> list[dict]`.
`crypto-detective.md`'s prompt gains one additional instruction: when this
trajectory is present, comment (as a hypothesis, same discipline as
everything else Detective says) on whether WARNING/WEAKENING/EXIT-style
Guardian states preceded the trade's actual outcome. Detective's existing
absolute limits (§2 of its own prompt: never a parameter change, never an
action, never a certainty) are unchanged and unaffected by this addition.

## 10. `guardian_observations` schema

```sql
CREATE TABLE IF NOT EXISTS guardian_observations (
    observation_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    state TEXT NOT NULL,
    decay_score TEXT NOT NULL,
    progress_ratio TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    factors TEXT NOT NULL,
    ai_reasoning TEXT,
    ai_cost_usd TEXT,
    run_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_guardian_observations_position
    ON guardian_observations(position_id, observed_at);
CREATE TRIGGER IF NOT EXISTS guardian_observations_no_update
BEFORE UPDATE ON guardian_observations
BEGIN SELECT RAISE(ABORT, 'guardian_observations is append-only: UPDATE is not permitted'); END;
CREATE TRIGGER IF NOT EXISTS guardian_observations_no_delete
BEFORE DELETE ON guardian_observations
BEGIN SELECT RAISE(ABORT, 'guardian_observations is append-only: DELETE is not permitted'); END;
```

## 11. Guardian tick (`guardian_loop.py`)

For each row in `repo.find_open_positions()` (PAPER, existing method, no
new query needed):

1. Fetch fresh klines+funding for the position's instrument via the
   existing `BingXMarketDataConnector`; on any data-quality failure, skip
   this position this tick (log, no observation written — same fail-safe
   principle as the rest of the system, never a guess).
2. Recompute evidence via `quant_screener.py`'s existing functions; also
   fetch/recompute BTC-USDT's momentum evidence once per tick (shared
   across all positions checked that tick, not re-fetched per position).
3. Compute the six decay factors, `decay_score`, `progress_ratio`,
   `unrealized_pnl`, and classify the state (§4/§5).
4. Check AI-invocation gating (§6) against the last persisted observation.
5. If warranted and budget allows (§7): call `crypto-guardian`, attach
   `reasoning`.
6. `INSERT OR IGNORE` the observation row (§10).

`run_guardian_tick()`/`run_forever()` follow the exact same outer
fail-safe shape as `monitoring_loop.py`/`demo_execution_loop.py`: an
unexpected exception is caught, logged, and never crashes the thread.

## 12. Testing strategy

- `guardian/deterministic.py` — pure functions, direct unit tests with
  simple `Position`/evidence fixtures, no mocks: each factor's boundary
  behavior (0, 1, and the entry-value-is-zero edge cases), the weighted
  mean, and every state-classification branch including the
  PROTECT-requires-profit rule.
- `guardian/data.py` — respx/stub-connector tests matching
  `test_monitoring_loop.py`'s `_MonitoringStubConnector` pattern; explicit
  test for the empty/invalid-data skip path (same class of bug just fixed
  in `monitoring_loop.py` today — a deliberately targeted regression test
  here too).
- `guardian/ai_context.py` — `should_invoke_ai()` truth table (first
  observation, no transition, transition to non-HOLD, transition between
  non-HOLD states, transition back to HOLD).
- `guardian/tick.py` — budget-exhausted path (deterministic observation
  still persisted, `ai_reasoning=None`, no AI call attempted); a fake
  `AgentRunner` for the AI-warranted path.
- Append-only enforcement: a direct test that `UPDATE`/`DELETE` against
  `guardian_observations` raises, mirroring the existing `events` table
  tests.
- Restart-safety: duplicate-tick test proving `INSERT OR IGNORE` absorbs a
  repeated observation without error or a second row.
- Isolation: explicit test proving a full guardian tick never mutates
  `positions`, `demo_executions`, or `candidates` (same style as the
  BingX Demo isolation tests already in the suite).
- Detective integration: `build_position_analysis_context()` test proving
  the guardian trajectory is included when present and simply absent
  (never an error) when not.
- Full suite green before anything leaves shadow mode, per the user's
  explicit instruction.

## 13. Out of scope (deferred, not forgotten)

- Any active mode (Guardian actually closing/reducing a position) — a
  separate, later, explicitly-requested decision after real shadow-mode
  data has been collected and validated via Detective.
- A signed-funding-rate decay factor (§4.1).
- Guardian-driven Telegram notifications (could reuse `notify_loop.py`'s
  pattern later; not needed for shadow-mode data collection).
