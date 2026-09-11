# Profit Protection Experiment — Design Spec

Date: 2026-09-11
Status: Approved for planning (implementation not yet started)

## 1. Purpose

Test, in PAPER only, forward-looking, whether moving a position's stop-loss
to break-even once unrealized profit reaches a predefined threshold
(+1.0% or +1.5%) improves outcomes compared to the current exit logic
("Baseline"), without changing entry, target, original stop-loss, sizing,
or any other production behavior.

This is a **hypothesis test**, not a strategy change. Nothing in this
system may use the experiment's results to alter LIVE, Gate, Risk Agent,
Guardian, the screener, position sizing, or the real PAPER exit logic. The
experiment is read-only with respect to all of those.

## 2. Non-goals / Guardrails

These are binding constraints on the implementation, not aspirations:

- **G1 — No production impact.** The shadow experiment never writes to the
  `positions` table, never calls `repo.close_position_with_event`, never
  changes `stop_loss`/`target`/`size` on a real position, and never feeds
  back into `check_exit_trigger`. It is purely an additional, independent
  observer — the same category as `guardian_observations`,
  `demo_executions`, and `live_executions` (all "strictly additive
  parallel observers", never joined into or written from
  `position_opening.py`/`position_closing.py`).
- **G2 — LIVE is untouched.** No file under this design imports from or is
  imported by `live_execution_loop.py`, `paper_trading/live_execution.py`,
  or `connectors/bingx_live_trading.py`. LIVE positions are not tracked in
  the `positions` table at all (see `live_executions`) and this experiment
  never queries or references that table.
- **G3 — Entry/screener/Gate/Risk Agent/AI roles/sizing/leverage/AI budget/
  Discovery/capacity logic are not touched.** The experiment only reads
  already-decided `Position` rows (entry, original stop_loss, target,
  size, opened_at) — it never influences what gets opened or how much.
- **G4 — Guardian is not touched.** `guardian_observations` and
  `guardian/*` are not read or written by this experiment. Guardian-
  assisted exit (`guardian_state == "EXIT"`) is replicated *read-only* in
  the shadow state machine only insofar as it is required to keep the
  shadow's non-SL exit paths identical to baseline (see §5) — the
  experiment reads the same `find_latest_guardian_observation` data
  Guardian already publishes, exactly as `position_closing.py` does; it
  never calls into `guardian/deterministic.py` or changes Guardian's own
  state.
- **G5 — Existing open/closed PAPER positions are not retroactively
  changed.** No historical trade's `exit_reason`/`simulated_fill_exit`/
  fees/funding in the `positions` table is ever modified by this feature.
- **G6 — No historical position before the activation watermark is ever
  included.** A position is only seeded into the experiment if
  `position.opened_at >= activated_at` (see §4.1). This is enforced at
  the single seeding call site, and covered by a test that asserts an
  already-open position at activation time is never seeded.
- **G7 — No look-ahead.** The shadow state machine only ever consumes the
  same `(candle_low, candle_high, current_price)` tuple already fetched
  for the current tick, in the same forward, chronological, tick-by-tick
  order as the rest of the monitoring loop. It never queries future
  candles, never re-evaluates a past tick with knowledge from a later
  one.
- **G8 — Same-candle ambiguity is resolved conservatively.** See §5.2 for
  the exact rule and its proof.
- **G9 — +1.0% and +1.5% are frozen, pre-registered hypotheses.** They are
  hard-coded as the only two variants this experiment runs. No code path
  in this design selects, tunes, ranks, or auto-promotes a threshold based
  on observed results. The report (§7) explicitly labels both as
  "pre-registered hypotheses under test", never as "the winner" or "the
  recommended value". Changing which thresholds are tested (or promoting
  one to production) is an explicit, separate, future human decision —
  out of scope for this feature.
- **G10 — Isolation is proven, not assumed.** A test explicitly forces the
  experiment tick function to raise, and asserts (a) `close_triggered_positions`
  already ran and its result is returned unaffected, (b) the real position's
  row in `positions` is unchanged, (c) `run_monitoring_tick` itself does not
  raise. See §8.

## 3. Architecture

### 3.1 New files

- `crypto_trading/paper_trading/profit_protection_experiment.py` — the
  state machine and tick function. New code only; touches no existing
  exit function.
- `crypto_trading/performance/profit_protection_report.py` — read-only
  reporting script, `python -m
  crypto_trading.performance.profit_protection_report`, mirrors the
  existing `paper_track_report.py`/`live_track_report.py` pattern (never
  started by `run.py`, never writes to the DB).
- `tests/crypto_trading/paper_trading/test_profit_protection_experiment.py`
- `tests/crypto_trading/performance/test_profit_protection_report.py`
- Storage layer additions live in `storage/db.py` (new `CREATE TABLE`
  statements) and `storage/repository.py` (new, additive methods) — see
  §4.

### 3.2 The one change to existing code

`crypto_trading/monitoring_loop.py::run_monitoring_tick`:

1. The existing `for position in repo.find_open_positions(): ...` loop
   that builds `price_lookup` is changed to first materialize
   `open_positions = list(repo.find_open_positions())` and then iterate
   over `open_positions` instead of calling `find_open_positions()` a
   second time. This is a pure refactor (identical order, identical
   values) needed only so the position list is available to the
   experiment hook below without an extra DB query.
2. Immediately after the existing call to `close_triggered_positions(...)`
   (unchanged, still runs first, still the only thing that can close a
   real position), one new call is added:

   ```python
   try:
       run_profit_protection_experiment_tick(
           repo, open_positions, price_lookup, now, settings, run_id
       )
   except Exception as exc:
       log_event(
           run_id, event="profit_protection_experiment_tick_failed",
           error_type=type(exc).__name__, error=str(exc),
       )
   ```

   This mirrors the codebase's established "an unexpected exception in one
   subsystem never crashes/blocks another" convention (same shape as the
   outer `except Exception` in `run_monitoring_tick` itself). Because it
   runs strictly after `close_triggered_positions` and is wrapped in its
   own `try/except`, a bug in the experiment can delay or corrupt nothing
   about real position closing — proven by the G10 test.

No other line of `monitoring_loop.py` changes. `run.py`, `guardian_loop.py`,
`live_execution_loop.py`, `discovery_loop.py`, `demo_execution_loop.py`,
`gate/*`, `agents/*`, `screening/*` are not touched.

## 4. Data model

### 4.1 `profit_protection_activation`

One row, ever. Written once, on the first experiment tick where
`settings.profit_protection_experiment.enabled` is `True` and no row
exists yet.

```sql
CREATE TABLE IF NOT EXISTS profit_protection_activation (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    activated_at TEXT NOT NULL
);
```

Single-row-by-constraint pattern (same idea as `schema_meta`). Read via
`repo.get_profit_protection_activation() -> datetime | None`; written via
`repo.activate_profit_protection_experiment(now: datetime) -> datetime`
which is `INSERT OR IGNORE`-based so a concurrent/duplicate activation
attempt can never overwrite an already-set watermark (same idempotency
family as `claim_demo_execution`).

### 4.2 `profit_protection_shadow_positions`

One row per `(position_id, threshold_pct)` — for the two frozen
thresholds this means at most 2 rows per real position.

```sql
CREATE TABLE IF NOT EXISTS profit_protection_shadow_positions (
    shadow_id TEXT PRIMARY KEY,          -- f"{position_id}:{threshold_pct}"
    position_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    threshold_pct TEXT NOT NULL,         -- "0.010" | "0.015", frozen (G9)
    entry_price TEXT NOT NULL,           -- copy of theoretical_entry at seed time
    original_stop_loss TEXT NOT NULL,    -- copy at seed time, never re-read from positions
    target TEXT NOT NULL,                -- copy at seed time
    threshold_price TEXT NOT NULL,       -- entry_price * (1 + threshold_pct)
    opened_at TEXT NOT NULL,             -- copy of position.opened_at
    status TEXT NOT NULL,                -- "OPEN" | "CLOSED"
    threshold_reached INTEGER NOT NULL DEFAULT 0,
    threshold_reached_at TEXT,
    breakeven_stop_loss TEXT,            -- set to entry_price once activated
    mfe TEXT NOT NULL DEFAULT '0',       -- max favorable excursion, price terms, since entry
    mae TEXT NOT NULL DEFAULT '0',       -- max adverse excursion, price terms, since entry
    exit_reason TEXT,                    -- "stop_loss" | "target" | "time_limit" | "guardian_exit"
    theoretical_exit TEXT,
    simulated_fill_exit TEXT,
    fees TEXT,
    funding TEXT,
    closed_at TEXT,
    shadow_realized_pnl TEXT,            -- via compute_pnl(), see §5.4
    hypothetical_baseline_exit_reason TEXT,   -- backfilled when real position closes
    hypothetical_baseline_pnl TEXT,           -- backfilled when real position closes
    pnl_difference TEXT,                      -- shadow_realized_pnl - hypothetical_baseline_pnl,
                                               -- computed once both are known
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pp_shadow_position ON profit_protection_shadow_positions(position_id);
CREATE INDEX IF NOT EXISTS idx_pp_shadow_status ON profit_protection_shadow_positions(status);
```

Comment header in `db.py` will state explicitly (mirroring the existing
`demo_executions`/`live_executions`/`guardian_observations` comment
convention): *strictly additive shadow simulation of an already-open PAPER
position, NEVER joined-into or written-from
`position_opening.py`/`position_closing.py`, never read by
Gate/Risk/Guardian/LIVE.*

### 4.3 Repository methods (additive only)

- `seed_profit_protection_shadows(position: Position, thresholds: list[Decimal], now: datetime) -> None`
  — `INSERT OR IGNORE` one row per threshold, only ever called when
  `position.opened_at >= activated_at` (G6).
- `find_open_profit_protection_shadows() -> list[dict]`
- `update_profit_protection_shadow(shadow_id: str, **fields) -> None` —
  full-row update, `updated_at = now`.
- `close_profit_protection_shadow(shadow_id: str, ...) -> None`
- `backfill_profit_protection_baseline_outcome(position_id: str, exit_reason: str, baseline_pnl: Decimal) -> None`
  — called once, when the real position closes, updates every shadow row
  for that `position_id` (both thresholds) with
  `hypothetical_baseline_exit_reason`/`hypothetical_baseline_pnl`/
  `pnl_difference` (computed against whichever of `shadow_realized_pnl`/
  `NULL` is already present — if the shadow is still open at this point,
  `pnl_difference` stays `NULL` until the shadow itself later closes, at
  which point the shadow-close step also fills `pnl_difference` if the
  baseline outcome is already known. Whichever event happens second is
  responsible for computing `pnl_difference`).
- `find_all_profit_protection_shadows() -> list[dict]` — for the report
  (read-only).

None of these methods touch the `positions` table.

## 5. Algorithm

### 5.1 Seeding

On every experiment tick, for every `position` in `open_positions` (the
same list `run_monitoring_tick` already built) where
`position.opened_at >= activated_at`: if no shadow rows exist yet for
`position.position_id`, seed one row per frozen threshold (G6). Seeding
copies `entry_price`/`original_stop_loss`/`target`/`opened_at` from the
`Position` object at that instant — never re-reads them later, so a
(disallowed, hypothetical) future change to the real position can never
retroactively alter an already-seeded shadow's rules.

### 5.2 Per-tick advance (conservative ordering, G7/G8)

For every shadow row with `status == "OPEN"` whose `instrument` is in this
tick's `price_lookup` (always true while the real position is open — see
§5.3 for why this is sufficient):

1. `active_sl = breakeven_stop_loss if breakeven_stop_loss is not None else original_stop_loss`
2. Update `mfe = max(mfe, candle_high - entry_price)`,
   `mae = min(mae, candle_low - entry_price)` (both computed BEFORE any
   exit check, so MFE/MAE reflect the full candle even on the closing
   tick).
3. Exit checks, in this exact order — identical priority chain to
   `check_exit_trigger`, with only step (a)'s SL level swapped for the
   variant's own:
   - (a) `if candle_low <= active_sl:` → close `"stop_loss"`,
     `min(candle_low, active_sl)` (same conservative gap-fill formula as
     baseline).
   - (b) `elif candle_high >= target:` → close `"target"`,
     `min(candle_high, target)`.
   - (c) `elif compute_hold_hours(...) >= max_position_hold_hours:` →
     close `"time_limit"`.
   - (d) `elif guardian_assisted_exit_enabled and guardian_state == "EXIT":`
     → close `"guardian_exit"` (reads `find_latest_guardian_observation`
     and applies the same staleness-guard formula
     `position_closing.py::close_triggered_positions` already applies —
     read-only against `guardian_observations`, G4). This ~10-line check
     is **duplicated**, not extracted into a shared helper: the user's
     constraint that baseline exit logic (`position_closing.py`) stay
     completely untouched outweighs the usual reuse preference here. A
     unit test asserts the duplicate produces identical accept/reject
     results as `close_triggered_positions` for the same inputs, so the
     two can't silently drift.
4. Only **after** all of the above (i.e., never in the same candle a
   stop/target/time/guardian check just ran against the *old* SL level):
   if `threshold_reached` is still `False` and `candle_high >=
   threshold_price`, set `threshold_reached = True`,
   `threshold_reached_at = now`, `breakeven_stop_loss = entry_price`. This
   takes effect starting the **next** tick's step 1 — never the tick that
   just detected it.

This ordering is what makes G8 concrete: a candle that contains both a
threshold-touch and a stop-level-touch is always resolved as "the stop
fired first" (since the stop check in step 3 always runs against the
*not-yet-updated* SL), which is the same conservative convention the
codebase already applies to stop-loss vs. target (`check_exit_trigger`
always checks `stop_loss` before `target` regardless of true intra-candle
order).

### 5.3 Proof that no extra price-fetching is needed

Claim: a shadow row's `closed_at` tick is always `<=` the real baseline
position's own close tick, for the same `position_id`.

- Target/time_limit/guardian conditions depend only on `target`,
  `opened_at`, `now`, and the Guardian state — none of which differ
  between baseline and any shadow variant. So whenever baseline closes via
  one of these three reasons, every still-open shadow for that position
  necessarily satisfies the identical condition at the identical tick.
- The stop condition is `candle_low <= active_sl`. `active_sl` is either
  `original_stop_loss` (identical to baseline) or `entry_price`
  (breakeven). Because a LONG position's `entry_price > original_stop_loss`
  always holds (SL is below entry), `active_sl >= original_stop_loss`
  always. So whenever baseline's stop condition
  (`candle_low <= original_stop_loss`) is true, the shadow's stop
  condition (`candle_low <= active_sl`, and `active_sl >=
  original_stop_loss`) is also true at that same tick or an earlier one.

Therefore a shadow can never still be `OPEN` once the real position has
left `repo.find_open_positions()`. Consequently, the experiment only ever
needs the `price_lookup` dict `run_monitoring_tick` already builds for
real open positions — no independent connector calls, no risk of ever
needing (and therefore never being tempted to use) a future candle.

### 5.4 Closing a shadow

Uses the existing, unmodified `paper_trading/execution.py` helpers
(`compute_fill_price`, `compute_fees`, `compute_funding`) with the same
`risk_limits` (`spread_pct`/`slippage_pct`/`fee_pct`) as baseline, so fill
assumptions are identical. `shadow_realized_pnl` is computed by
constructing an ephemeral (never persisted as a real `Position`,
`status="CLOSED"`, arbitrary `candidate_id`/`fill_model_version` markers)
`Position` instance and calling the existing, already-tested
`compute_pnl()` — guaranteeing bit-for-bit parity with how every other
PnL number in this system is computed (single source of truth, per
`performance/metrics.py`'s own docstring).

### 5.5 Backfilling the baseline outcome

`monitoring_loop.py`'s existing call to `close_triggered_positions`
already returns the list of just-closed real positions. The experiment
hook (still inside the new `try/except`) additionally calls, for each
just-closed position,
`repo.backfill_profit_protection_baseline_outcome(position_id,
exit_reason, compute_pnl(position))` — read-only with respect to
`positions`, purely a write into the experiment's own table.

## 6. Config

`config/loader.py`:

```python
class ProfitProtectionExperimentConfig(BaseModel):
    enabled: bool = False
    thresholds_pct: list[Decimal] = Field(
        default_factory=lambda: [Decimal("0.010"), Decimal("0.015")]
    )
```

Opt-in, same as `GuardianConfig`/`DemoExecutionConfig` — default `enabled=False`
changes nothing for any existing deployment. `thresholds_pct` is documented
in the class docstring as **frozen for the duration of this experiment
(G9)** — not a tuning knob.

## 7. Reporting

`crypto_trading.performance.profit_protection_report`, read-only, prints
JSON (matching `paper_track_report.py`'s convention). For each of the two
threshold variants, and combined:

### 7.1 Per-trade rows

Every closed shadow row rendered as one comparison row containing,
explicitly separated (per explicit user requirement):
`baseline_actual_exit_reason`, `baseline_actual_pnl`,
`profit_protection_hypothetical_exit_reason` (the shadow's own exit —
labelled "hypothetical" from production's point of view, since it never
actually executed),
`profit_protection_hypothetical_pnl`, `pnl_difference`, and a categorical
`outcome_label` ∈ {`"protection_saved_a_loss"`, `"protection_clipped_a_winner"`,
`"protection_no_change"`, `"protection_improved_other"`,
`"protection_worsened_other"`} (see §7.3 for exact definitions).

### 7.2 Reach-classification (per user requirement #4)

Every shadow row is classified into exactly one of:

- `never_reached_threshold` — `threshold_reached == False`.
- `reached_threshold_baseline_loss` — reached, and
  `hypothetical_baseline_pnl < 0`.
- `reached_threshold_baseline_approx_breakeven` — reached, and
  `abs(hypothetical_baseline_pnl / position_size) <= 0.003` (0.3% of
  notional — documented constant, `_BREAKEVEN_BAND_PCT`, this is the one
  free judgment call in this spec and is called out as such).
  `position_size` here is the real position's own `size` field, read via
  `repo.get_position(position_id)` at report time (read-only, same
  pattern `paper_track_report.py` already uses for its own
  `pnl_pct = pnl / p.size`) — it is not duplicated into the shadow table.
- `reached_threshold_baseline_big_winner` — reached, and
  `hypothetical_baseline_exit_reason == "target"` (baseline actually ran
  all the way to the original target — an unambiguous, schema-native
  definition of "big winner", not an arbitrary percentage cutoff).
- `reached_threshold_baseline_moderate_gain` — reached, positive PnL,
  outside the breakeven band, but did not reach target (residual bucket,
  makes the classification exhaustive).

### 7.3 Improved / worsened / unchanged (per user requirement #3)

Only computed once both `shadow_realized_pnl` and
`hypothetical_baseline_pnl` are known:

- **"profit protection improved P/L"**: count and total $ where
  `pnl_difference > 0`.
- **"profit protection worsened P/L"**: count and total $ where
  `pnl_difference < 0`.
- **"protection_no_change"**: `pnl_difference == 0` (always true for
  `never_reached_threshold` rows, since an untouched SL means shadow and
  baseline are the exact same trade).
- **"loss saved" ("förlust som blev 0")**: `hypothetical_baseline_pnl < 0
  and shadow_realized_pnl >= 0`.
- **"large winner clipped" ("stor vinst som förlorades")**:
  `reached_threshold_baseline_big_winner` (§7.2) and
  `profit_protection_hypothetical_exit_reason != "target"` and
  `pnl_difference < 0`.

### 7.4 Aggregate stats (per threshold and combined)

Reusing `performance/metrics.py`'s existing functions
(`compute_win_rate`, `compute_expectancy`, `compute_profit_factor`,
`compute_drawdown`, `trade_pnls`-equivalent) against the shadow PnL series
and, separately, against the baseline PnL series for the *same subset of
positions* (so the comparison is apples-to-apples on identical trade
counts) — total P/L, expectancy, average, median, win rate, profit
factor, max drawdown, plus: count reached +1.0%, count reached +1.5%,
count that reached-then-reverted-to-loss-under-baseline, and
MFE-to-realized-gain conversion ratio (`shadow_realized_pnl / mfe` where
`mfe > 0`, averaged).

### 7.5 Chronological robustness split

Positions are ordered by `opened_at` and split into a first half / second
half (by count, not by date range, so both halves have comparable sample
size); §7.4's aggregate stats are computed separately for each half, per
threshold, so a threshold that looks good only in one half is visible as
such, per explicit user requirement.

### 7.6 Framing (G9)

The report's top-level JSON includes a fixed, non-conditional field:

```json
"note": "Pre-registered hypotheses under test: +1.0% and +1.5%. This report never selects a winner or recommends promotion to production — that is a separate, later, explicit human decision."
```

This string is not conditional on the data — it is always present, so the
report can never be read as a recommendation.

## 8. Testing plan

- State machine unit tests (no repo, no DB): threshold not reached never
  moves SL; threshold reached activates breakeven starting next tick only
  (never same tick); same-candle threshold+stop ambiguity resolves as
  stop-first (G8); same-candle threshold+target resolves as target
  fires only if stop didn't already fire that tick; a position that never
  reaches threshold produces `pnl_difference == 0` and
  `outcome_label == "protection_no_change"`; a position that reaches
  threshold and later falls back through breakeven closes at
  approximately zero PnL (minus fees/funding) even though baseline (same
  candles, unmodified SL) would have gone on to a full loss at the
  original, lower stop.
- Repository tests: seeding is idempotent (`INSERT OR IGNORE`); seeding
  never occurs for `opened_at < activated_at` (G6, explicit test);
  activation watermark is set exactly once even under concurrent/repeated
  calls; backfill correctly updates both threshold rows for a position.
- **Isolation test (G10, explicit user requirement #8)**: monkeypatch
  `run_profit_protection_experiment_tick` to raise inside
  `run_monitoring_tick`, assert (a) the function's return value (closed
  real positions) is unchanged from a run without the monkeypatch, (b) no
  exception propagates out of `run_monitoring_tick`, (c) the real
  position's row in `positions` is byte-identical before/after, (d) a
  `profit_protection_experiment_tick_failed` event is logged.
- Report tests: classification buckets (§7.2) are exhaustive and mutually
  exclusive over a synthetic set of shadow rows; improved/worsened/
  unchanged counts sum to the total closed-shadow count; the fixed G9
  `note` field is always present regardless of input data.

## 9. Post-implementation verification (per user requirement #10)

After implementation, before considering this feature done:

1. Run the full existing test suite — zero regressions.
2. Produce a **read-only diff** (`git diff` against the pre-feature
   commit, scoped explicitly) that is inspected line-by-line to confirm:
   - `guardian/`, `gate/`, `agents/`, `screening/`, `live_execution_loop.py`,
     `paper_trading/live_execution.py`,
     `connectors/bingx_live_trading.py`, `paper_trading/position_opening.py`,
     `paper_trading/position_sizing.py`,
     `paper_trading/monitoring.py` (`check_exit_trigger` itself) are
     **untouched** (no diff hunks in these files at all).
   - `paper_trading/position_closing.py` is **untouched**.
   - `monitoring_loop.py`'s diff contains only the two changes described
     in §3.2 (the `open_positions` list materialization and the new
     `try/except`-wrapped call) — nothing else.
   - `config/loader.py`'s diff contains only the additive
     `ProfitProtectionExperimentConfig` class (plus its wiring into
     `Settings`) — no existing field changed.
   - `storage/db.py`/`storage/repository.py` diffs contain only new,
     additive `CREATE TABLE IF NOT EXISTS` statements and new methods —
     no existing table or method body changed.

## 10. How to run (once implemented)

1. Set `enabled: true` and confirm `thresholds_pct` under
   `profit_protection_experiment` in the environment's config YAML (or via
   whatever mechanism `Settings` already uses for this project — no new
   env-var precedent needed beyond the existing config-loading path).
2. Restart the monitoring process. The first tick sets the activation
   watermark; only positions opened from that instant onward are ever
   seeded.
3. Let it run until `python -m
   crypto_trading.performance.profit_protection_report` shows at least
   30–50 closed shadow trades per threshold (per user's explicit
   requirement — the report itself surfaces the current closed-count so
   this is directly observable, no separate tooling needed).
4. Read the report. Per G9, do not use it to auto-select or promote a
   threshold — that is a separate, later, explicit decision.
