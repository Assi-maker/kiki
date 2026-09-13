# LIVE Profit Protection (+1.0% → break-even) — Design Spec

**Status:** Approved for implementation (2026-09-13, explicit user decision after 3 rounds of correction).

**Goal:** When a real LIVE BingX position reaches +1.0% unrealized profit (vs. its real exchange fill entry, mark-price basis), move its real exchange stop-loss order to break-even (entry price). Take-profit is never touched. This is the first LIVE version; initial SL distance and the +1.0% threshold itself are NOT tuned in this change — that is an explicit, separate, later decision (user's own words: "blanda inte ihop de två förändringarna i samma kodändring").

**Non-goals (explicitly out of scope for this change):** entry logic, Gate, Risk Agent, AI, screening, sizing, leverage, PAPER, backtest, the PAPER Profit Protection experiment, historical replay, any change to the initial SL/target values themselves.

## Why this needs care (read before implementing)

This module places and cancels real orders against a real BingX account with real capital. Every failure mode below was chosen because leaving a position genuinely unprotected, even briefly, is unacceptable — full stop, no exceptions, no "probably fine." When a step's outcome cannot be verified from the exchange, the code must treat it as unsafe and stop, never guess and never blindly retry a write.

## Verified BingX API facts (not assumptions)

Investigated via CCXT's production-tested BingX connector source and its documented real API response examples (`ccxt/ccxt` `python/ccxt/bingx.py`, `swap/v2` linear endpoints — the exact host/path family this codebase's `BingXLiveTradingConnector` already uses):

- **For linear (USDT-margined) swap — our exact case** — `GET .../swap/v2/trade/openOrders` returns each order (including SL/TP orders created via the combined entry+SL+TP placement) as its own flat entry in the `orders` array, each with its own `orderId`, `type` (`STOP_MARKET`/`TAKE_PROFIT_MARKET`/etc.), `stopPrice`, `status`. This is confirmed distinct from *inverse* swap, where the same endpoint nests SL/TP as sub-objects without their own visible orderId — we are never on that code path (all instruments in this codebase are `-USDT` linear swap).
- **Cancel-one-order** — CCXT's `cancel_order` for linear swap calls `DELETE .../swap/v2/trade/order` with `{symbol, orderId}` (or `clientOrderID`) — the exact same path our connector already uses for `GET` order lookups (`_ORDER_PATH`), just a different verb. No new endpoint family, only a new verb on an already-used path.
- **Order status values** — `NEW`/`PENDING` = open/not yet triggered, `FILLED` = executed, `CANCELED`/`CANCELLED`/`FAILED` = terminated without effect. A freshly-placed, accepted `STOP_MARKET` order that hasn't triggered yet is `NEW`/`PENDING`.
- **Real position fields** (`GET .../swap/v2/user/positions`, already wrapped by `get_position()`/`get_all_positions()`) include `avgPrice` (real exchange fill entry — the authoritative entry price, never our own locally-stored `exchange_fill_entry` column, per "exchange state is source of truth"), `markPrice` (used for the +1.0% calculation, consistent with our own SL/TP's `workingType: MARK_PRICE`), `positionAmt`.

**Not found in documentation, deliberately not assumed:** whether BingX accepts a second, simultaneous `STOP_MARKET` order on a position that already has one. The design below does not need to know this in advance — placing the new order and reading the exchange's own accept/reject response IS the verification, and every possible answer has a pre-defined, safe outcome (see Step 3 below and the state table).

## Data model

New table, `live_profit_protection`, one row per LIVE position, created (claimed) the moment a PP attempt starts — never before:

```sql
CREATE TABLE IF NOT EXISTS live_profit_protection (
    position_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    threshold_pct TEXT NOT NULL,           -- fixed "0.01" (1.0%), never a second value in this change
    trigger_mark_price TEXT,               -- mark price that satisfied the threshold, captured at claim time
    breakeven_price TEXT,                  -- = avgPrice at claim time, the target new SL price
    old_sl_order_id TEXT,
    old_sl_price TEXT,
    new_sl_client_order_id TEXT NOT NULL,  -- deterministic, same pattern as _client_order_id() in live_execution.py
    new_sl_order_id TEXT,
    last_error TEXT,
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

`status` values (each is a stable, documented outcome — never an intermediate value left lying around across ticks except `CLAIMED`, which restart-recovery always resolves forward, never resumes blindly):

| status | Meaning | Can PP retry this position later? |
|---|---|---|
| `CLAIMED` | Attempt in progress or interrupted by a crash/restart. Recovery logic (below) always resolves this to one of the other statuses by reading exchange state — never left as `CLAIMED` after a tick observes it. | N/A — transient |
| `SL_REPLACED` | Full success: new break-even SL verified `NEW`/`PENDING`, old SL confirmed cancelled, final state verified. | No — terminal success |
| `REPLACEMENT_PARTIAL` | New SL verified active, but the old SL could not be cancelled (both order IDs retained). Position is protected (arguably double-protected) but in an exceptional state. | **No** — blocked permanently, human/reconciliation review only |
| `ABORTED_AMBIGUOUS_SL` | Step 2 found 0 or 2+ existing `STOP_MARKET` orders — could not identify a single original SL. No write was attempted. | No — blocked (see "Why no retry" below) |
| `ABORTED_NEW_SL_REJECTED` | Exchange definitively rejected placing the new SL (structured rejection, zero fill guaranteed). Old SL untouched. This is also our empirical answer to "does BingX allow 2 simultaneous SL orders" — logged clearly for human review. | No — blocked |
| `UNCERTAIN_NEW_SL_STATUS` | New SL placement's outcome could not be determined (lookup error/timeout/unrecognized status). Old SL was **never** cancelled. | No — blocked, needs reconciliation/human review |
| `POSITION_CLOSED_BEFORE_PP` | Position was already gone (closed/flat on the exchange) when checked. Informational, not a failure. | No — position is closed, nothing to protect |
| `ANOMALY_NO_PROTECTIVE_ORDER_FOUND` | Restart-recovery only: position still open, but zero `STOP_MARKET` orders exist at all — a state this code should never itself produce (add-before-remove guarantees ≥1 protective order at every self-caused transition). Logged at error level; no auto-heal. | No — blocked, human review required |

**Why no automatic retry on any aborted/blocked status:** every one of these represents either an ambiguous state (don't act on ambiguity) or a confirmed hard constraint (a rejection tells us something true and stable about this account/position — retrying the identical operation every tick forever would just repeat the same rejection while spamming the real exchange). A human reviewing logs is the correct next step for all of these, not blind automated retry. This directly satisfies "PP ska bara kunna aktiveras en gång per LIVE-position" read as "at most one terminal outcome per position, ever."

## The sequence (normal success path)

1. **Verify LIVE position.** `connector.get_position(symbol)`. If `None` → position already closed on the exchange → write `POSITION_CLOSED_BEFORE_PP`, stop. No write ever attempted against a position we can't currently see.
2. **Verify exactly one active SL.** `connector.get_open_orders(symbol)`, filter `type == "STOP_MARKET"`. Exactly 1 → continue, record its `orderId`/`stopPrice` into the claimed row. 0 or 2+ → `ABORTED_AMBIGUOUS_SL`, stop. No write.
3. **Place break-even SL.** New connector method `place_stop_loss_order(symbol, quantity, stop_price, client_order_id)` — a standalone `STOP_MARKET` order, `side=SELL`, `positionSide=LONG` (mirrors `close_position_market`'s existing LONG-only safety pattern), `workingType=MARK_PRICE` (matches the original SL/TP). `client_order_id` is deterministic (`_client_order_id(position_id, "pp")`-style), so a restart can always look it up rather than guess. Exchange rejects it (structured, synchronous — same `OrderRejectedError` pattern already safety-audited in `_submit_entry_order`) → `ABORTED_NEW_SL_REJECTED`, stop. Old SL is never touched at this point.
4. **Verify the new SL is genuinely active.** Look it up by its deterministic client order id. Status `NEW`/`PENDING` → continue to step 5. Anything else (not found, transport error, `UNKNOWN`-shaped response, or even a surprising immediate `FILLED` — price already moved past break-even between step 1 and step 3, a real possibility handled explicitly, see below) → `UNCERTAIN_NEW_SL_STATUS`, stop. **Old SL is never cancelled** in this branch — this is the single most important invariant in the whole design.
5. **Cancel the old SL.** New connector method `cancel_order(symbol, order_id)` (`DELETE` on the existing `_ORDER_PATH`, verified above). Only reached once step 4 has positively confirmed the new SL is live.
6. **Cancel failed?** → `REPLACEMENT_PARTIAL` (per the corrected rule): keep both order IDs, log both, block all further PP for this position, never attempt a third order, never touch TP, never auto-cancel either surviving order without a human/reconciliation-driven, exchange-state-verified action. This is a safe terminal state — the position has ≥1 real protective order the whole time, arguably belt-and-braces.
7. **Verify final state.** Re-read `get_open_orders(symbol)` one more time and log what's actually there (should be: new SL only, no old SL, TP unchanged). Write `SL_REPLACED`.

**The "price already hit break-even between check and placement" case:** if step 4 finds the new SL already `FILLED`, the position closed at (approximately) break-even — a valid, safe outcome of a market order type, not a bug. Treat as: do not proceed to "cancel old SL" (it's likely already gone too, since the position is flat) — instead re-check `get_position()`; if flat, write `SL_REPLACED` with a note that closure happened during verification, matching "stängd position under operationen ska hanteras utan blind orderoperation" (no order operation is attempted once we know the position is flat).

## Restart / crash recovery

A `CLAIMED` row found at the start of any tick (before attempting a *new* claim on any position) is always resolved by re-deriving truth from the exchange — never assumed, never blindly resumed mid-recipe:

1. `get_position(symbol)` — if flat, position closed during the interrupted attempt → check `get_open_orders` for cleanup visibility only (nothing to cancel if flat — the exchange already closed everything), write `POSITION_CLOSED_BEFORE_PP`.
2. Else, `get_open_orders(symbol)`: if exactly one `STOP_MARKET` order remains and it matches `old_sl_order_id` (recorded at claim time) → the new SL was never confirmed placed. Look up `new_sl_client_order_id` via `get_order_by_client_order_id` (deterministic, safe, same pattern as `_resolve_uncertain_entry`): `NEW`/`PENDING`/`FILLED` found → we're actually further along than the row shows, resume from step 4/5 accordingly; not found/rejected → safe to retry step 3 (placing under the same deterministic client order id is itself the safety check against double-placement).
3. If **two** `STOP_MARKET` orders exist (old + new) → new SL succeeded, old cancel never completed. Attempt the cancel exactly once more; success → `SL_REPLACED`; failure → `REPLACEMENT_PARTIAL`. (This finite, evidence-based single retry is not "blind" — we have direct proof from exchange state that a cancel is the only remaining, safe action.)
4. If **zero** `STOP_MARKET` orders exist and the position is still open → an anomaly this code should never itself produce (add-before-remove guarantees ≥1 at every self-caused transition) → do not auto-heal; write a distinct `status` (`ANOMALY_NO_PROTECTIVE_ORDER_FOUND`) and log at error level for human review. Never silently re-place a guessed SL here.

## Integration point

New, standalone module `crypto_trading/paper_trading/live_profit_protection.py` — never imports from or modifies `live_execution.py`, `position_closing.py`, `profit_protection_experiment.py`, or anything under `crypto_trading/backtest/`. One public function:

```python
def run_live_profit_protection_tick(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    threshold_pct: Decimal,
    run_id: str,
    now: datetime,
) -> None: ...
```

Called from `live_execution_loop.py::run_live_execution_tick`, inserted **after** `reconcile_active_executions` (so the ACTIVE-position list is freshly reconciled against the real exchange before PP ever looks at it) and **before** `close_time_limit_positions`/`process_pending_positions` (so PP never races a same-tick close or a new-entry capacity check). `close_guardian_exit_positions` may run before or after — order relative to it does not matter since PP only touches SL price, never triggers a close itself.

Gated by a new flag in `live_execution.yaml`, `profit_protection_enabled: false` by default (mirrors PAPER's own PP experiment `enabled` watermark pattern) — landing this code changes nothing about running behavior until explicitly flipped on in a later, separate step.

## Safety invariants (traceability to user's requirements)

- Never a larger risk than the original SL: the new SL is always exactly `avgPrice` (break-even) or better — target/TP is never touched, so max loss can only shrink, never grow.
- Never moves SL the wrong way: no code path computes a new SL price other than `avgPrice` (a hardcoded break-even, not a formula that could invert on bad input).
- Never duplicates an SL to `SL_REPLACED`: reaching `SL_REPLACED` requires cancel of the old one to have been confirmed; `REPLACEMENT_PARTIAL` exists precisely for when that isn't true, and it is a distinct, blocked status.
- Always verifies real exchange position before any write (step 1, and again in restart recovery).
- Idempotent at restart (see above).
- Never assumes a failed/uncertain API result means the operation failed — never blind-retries a write whose outcome is unknown (steps 4/7, `UNCERTAIN_NEW_SL_STATUS`).
- Logs before/after SL price, position, trigger price, and every exchange order ID at every transition (`log_event`, same convention as the rest of `live_execution.py`).
- A failed PP operation always leaves the position with its existing protective SL intact (steps 3/4's failure branches never touch the old SL).
- `reconcile_active_executions`/exchange state remains the sole source of truth for the real position and its real orders; `live_profit_protection` rows exist only for idempotency, audit, and blocking re-attempts — never as a substitute for reading the exchange.

## New connector methods (the only change to `bingx_live_trading.py`)

```python
def place_stop_loss_order(self, symbol: str, quantity: str, stop_price: str, client_order_id: str) -> dict: ...
def cancel_order(self, symbol: str, order_id: str) -> dict: ...
```

Both additive, both reuse `_request`/`_unwrap_order`/`_guard_host` exactly as every existing method does. `place_stop_loss_order` raises `OrderRejectedError` on a structured rejection, mirroring `place_entry_order_with_sl_tp` exactly.

## Test plan (all required before the write-path code is considered done)

1. Normal path: add → verify NEW/PENDING → remove old → verify final state → `SL_REPLACED`.
2. New SL placement rejected → `ABORTED_NEW_SL_REJECTED`, old SL untouched, no cancel attempted.
3. New SL placed, old-SL cancel fails → `REPLACEMENT_PARTIAL`, both order IDs logged, TP untouched, no third order.
4. New SL status unknown/lookup fails → `UNCERTAIN_NEW_SL_STATUS`, old SL never cancelled.
5. Zero existing SL found → `ABORTED_AMBIGUOUS_SL`, no write attempted.
6. Two+ existing SL found → `ABORTED_AMBIGUOUS_SL`, no write attempted.
7. Restart recovery from each of: mid-step-3 (only old SL exists, new client-order-id not found → retry placement), mid-step-4 (both SL exist → resume at cancel), mid-step-5 (both SL exist, one more cancel attempt → success and failure sub-cases), the zero-SL anomaly case.
8. PP already `SL_REPLACED`/any blocked status for a position → tick skips it entirely, no exchange write attempted.
9. TP order identity/price asserted unchanged across every scenario above.
10. Position found closed at step 1 → `POSITION_CLOSED_BEFORE_PP`, no order operation.
11. Position closes between step 1 and step 4 (new SL comes back `FILLED`) → handled as a valid closure, not an error, no further order operation.
12. +1.0% computed correctly against `avgPrice` and direction (LONG-only, matches existing `_DIRECTION` invariant elsewhere in the codebase) — a fixture-based unit test on the threshold-check helper alone, independent of the order-sequence tests above.
13. `run_live_profit_protection_tick` never imports/calls anything from `paper_trading/position_closing.py`, `paper_trading/profit_protection_experiment.py`, or `crypto_trading/backtest/` (a static/import-scan test, same discipline as the Tier 1 plan's own production-file-isolation tests).

## Post-implementation verification (before any activation)

1. All new targeted tests green.
2. Full project test suite green (or only the pre-existing, known, unrelated PP-experiment-config failure).
3. `git diff --stat` against the pre-change commit shows only: `crypto_trading/connectors/bingx_live_trading.py` (additive), `crypto_trading/paper_trading/live_execution.py` (one new call site), `crypto_trading/live_execution_loop.py` (one new call site), `crypto_trading/config/live_execution.yaml` (one new flag, default off), `crypto_trading/config/loader.py` (one new field), `crypto_trading/storage/db.py`/`repository.py` (new table + its CRUD), the new `live_profit_protection.py` module, and its tests. Nothing under `paper_trading/position_opening.py`, `position_sizing.py`, `position_closing.py`, `profit_protection_experiment.py`, `crypto_trading/backtest/`, `crypto_trading/gate/`, `crypto_trading/detective/`, or any `risk_limits.yaml`/PAPER config.
4. `live_execution.yaml`'s existing fields (`max_concurrent_positions`, `margin_per_trade_usdt`, `leverage`, `max_position_hold_hours`, `signal_ttl_seconds`) unchanged in value; only the new flag added.
5. A read-only check against the real BingX account (`get_balance`/`get_all_positions`) confirming the connector still works exactly as before.
6. No manually-triggered real PP order as a "test" — the feature stays behind `profit_protection_enabled: false` until a separate, explicit activation decision.
