# LIVE Profit Protection (+1.0% → break-even) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the +1.0% → break-even LIVE stop-loss replacement mechanism exactly as designed in the spec, behind a default-off flag, with zero change to entry/Gate/Risk/AI/sizing/leverage/PAPER/backtest/the PAPER Profit Protection experiment.

**Architecture:** A new, isolated module (`live_profit_protection.py`) that reads real exchange state before and after every write, uses a new local table purely for idempotency/audit (never as source of truth for the real position/orders), and is wired into the existing LIVE tick as one new step between `reconcile_active_executions` and `close_time_limit_positions`.

**Tech Stack:** Python, SQLite (existing `storage/db.py`/`repository.py` pattern), existing `BingXLiveTradingConnector`, existing `log_event`.

**Spec:** `docs/superpowers/specs/2026-09-13-live-profit-protection-design.md` — read this in full before starting any task. It is the binding authority; this plan argues from it.

## Global Constraints

- Never modify `crypto_trading/paper_trading/position_opening.py`, `position_sizing.py`, `position_closing.py`, `profit_protection_experiment.py`, `crypto_trading/gate/`, `crypto_trading/detective/`, anything under `crypto_trading/backtest/`, or `crypto_trading/config/risk_limits.yaml`.
- Never modify the entry side of `crypto_trading/paper_trading/live_execution.py` (`process_pending_positions`, `_submit_entry_order`, `_resolve_uncertain_entry`, `has_sufficient_live_capacity`, `reconcile_active_executions`, `close_guardian_exit_positions`, `close_time_limit_positions`) — only ADD one new call site inside `run_live_execution_tick` (in `live_execution_loop.py`, not `live_execution.py` itself).
- The new feature is OFF by default (`profit_protection_enabled: false` in `live_execution.yaml`) — landing this plan changes zero runtime behavior until a separate, later, explicit activation.
- Every write to the real exchange must be preceded and followed by a fresh read of real exchange state (`get_position`/`get_open_orders`) — never act on cached/assumed state.
- Add-before-remove only. No code path may ever cancel the old SL before the new SL is verified `NEW`/`PENDING` on the exchange.
- No code path may ever place a third order for a position already in `REPLACEMENT_PARTIAL`, or retry a position already in any terminal/blocked status (see spec's status table).
- TP is never read, computed, or written by any new code in this plan.
- LONG-only (matches `_DIRECTION = "LONG"` used everywhere else in this codebase for PAPER/LIVE).
- TDD throughout: failing test first, watch it fail for the right reason, minimal implementation, watch it pass, commit.

---

### Task 1: Two new BingX connector methods (place standalone SL, cancel one order)

**Files:**
- Modify: `crypto_trading/connectors/bingx_live_trading.py`
- Test: `tests/crypto_trading/connectors/test_bingx_live_trading.py`

**Interfaces:**
- Produces: `place_stop_loss_order(symbol: str, quantity: str, stop_price: str, client_order_id: str) -> dict`, `cancel_order(symbol: str, order_id: str) -> dict`
- Consumed by: Task 5's `live_profit_protection.py`

**Design:** Both reuse `self._request`/`_unwrap_order`/`_guard_host` exactly like every existing method — no new transport code. `place_stop_loss_order` sends a standalone `STOP_MARKET` order (`side=SELL`, `positionSide=LONG`, `workingType=MARK_PRICE`, matching the original SL's shape from `place_entry_order_with_sl_tp`'s `stopLoss` sub-object) to the SAME `_ORDER_PATH` used for entry, and raises `OrderRejectedError` on a structured rejection — identical pattern to `place_entry_order_with_sl_tp`. `cancel_order` issues `DELETE` to the same `_ORDER_PATH` with `{"symbol": symbol, "orderId": order_id}` — verified against CCXT's documented BingX linear-swap `cancel_order` implementation (same path, same params, only the verb differs from the existing `GET`-based `get_order_status`).

- [ ] **Step 1: Write the failing tests**

```python
import json

import httpx
import respx
import pytest

from crypto_trading.connectors.bingx_live_trading import (
    BingXLiveTradingConnector,
    OrderRejectedError,
)


def _connector() -> BingXLiveTradingConnector:
    return BingXLiveTradingConnector(api_key="k", api_secret="s", max_retries=1)


@respx.mock
def test_place_stop_loss_order_sends_stop_market_sell_long():
    route = respx.post("https://open-api.bingx.com/openApi/swap/v2/trade/order").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "", "data": {"order": {"orderId": "999"}}})
    )
    connector = _connector()

    result = connector.place_stop_loss_order(
        "BTC-USDT", quantity="0.01", stop_price="50000", client_order_id="lvabc123pp"
    )

    assert result["orderId"] == "999"
    sent = dict(p.split("=") for p in route.calls.last.request.content.decode().split("&") if "=" in p)
    assert sent["side"] == "SELL"
    assert sent["positionSide"] == "LONG"
    assert sent["type"] == "STOP_MARKET"
    assert sent["clientOrderID"] == "lvabc123pp"


@respx.mock
def test_place_stop_loss_order_raises_order_rejected_on_structured_error():
    respx.post("https://open-api.bingx.com/openApi/swap/v2/trade/order").mock(
        return_value=httpx.Response(200, json={"code": 80001, "msg": "duplicate stop order"})
    )
    connector = _connector()

    with pytest.raises(OrderRejectedError):
        connector.place_stop_loss_order("BTC-USDT", "0.01", "50000", "lvabc123pp")


@respx.mock
def test_cancel_order_sends_delete_with_symbol_and_order_id():
    route = respx.delete("https://open-api.bingx.com/openApi/swap/v2/trade/order").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "", "data": {"orderId": "999", "status": "CANCELED"}})
    )
    connector = _connector()

    result = connector.cancel_order("BTC-USDT", "999")

    assert result["status"] == "CANCELED"
    params = dict(p.split("=") for p in str(route.calls.last.request.url.params).split("&"))
```

(Note for the implementer: adjust the two "parse the sent params" assertions above to however `respx`/`httpx` actually exposes a signed, `&`-joined POST body / DELETE query string in this codebase's existing tests — look at `tests/crypto_trading/connectors/test_bingx_live_trading.py`'s existing tests for `place_entry_order_with_sl_tp`/`set_leverage` for the established assertion style and reuse it exactly, rather than the placeholder split-string parsing above.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/crypto_trading/connectors/test_bingx_live_trading.py -k "stop_loss_order or cancel_order" -v`
Expected: FAIL with `AttributeError: 'BingXLiveTradingConnector' object has no attribute 'place_stop_loss_order'`

- [ ] **Step 3: Implement**

Add to `crypto_trading/connectors/bingx_live_trading.py`, following the exact existing style of `place_entry_order_with_sl_tp`/`get_order_status`:

```python
def place_stop_loss_order(
    self, symbol: str, quantity: str, stop_price: str, client_order_id: str
) -> dict:
    """Standalone SL replacement for LIVE Profit Protection (2026-09-13) -
    NEVER touches TP, NEVER used at entry. Same STOP_MARKET/MARK_PRICE
    shape as the SL sub-order inside place_entry_order_with_sl_tp's
    combined placement, just issued alone against the same order
    endpoint. Raises OrderRejectedError on a structured rejection -
    identical, already-safety-audited semantics as
    place_entry_order_with_sl_tp (a synchronous rejection here means zero
    fill guaranteed, nothing to look up)."""
    params = {
        "symbol": symbol,
        "side": "SELL",
        "positionSide": "LONG",
        "type": "STOP_MARKET",
        "quantity": quantity,
        "stopPrice": stop_price,
        "clientOrderID": client_order_id,
        "workingType": "MARK_PRICE",
    }
    try:
        return _unwrap_order(self._request("POST", _ORDER_PATH, params))
    except _ApiCodeError as exc:
        raise OrderRejectedError(str(exc)) from exc

def cancel_order(self, symbol: str, order_id: str) -> dict:
    """Cancels exactly one order by orderId - never the whole-symbol
    cancel_all_open_orders(), which would also remove TP. Same
    _ORDER_PATH as get_order_status()/place_entry_order_with_sl_tp(),
    verified against CCXT's documented BingX linear-swap cancelOrder
    implementation (DELETE, {symbol, orderId})."""
    return self._request("DELETE", _ORDER_PATH, {"symbol": symbol, "orderId": order_id}) or {}
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/crypto_trading/connectors/test_bingx_live_trading.py -v`
Expected: PASS (all tests, including pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/connectors/bingx_live_trading.py tests/crypto_trading/connectors/test_bingx_live_trading.py
git commit -m "feat(crypto-trading): add standalone SL placement and single-order cancel to LIVE connector"
```

---

### Task 2: `live_profit_protection` table and repository CRUD

**Files:**
- Modify: `crypto_trading/storage/db.py`, `crypto_trading/storage/repository.py`
- Test: `tests/crypto_trading/storage/test_repository.py` (or a new `test_live_profit_protection_repository.py` if the existing file is already large — implementer's call, follow whatever this codebase's own convention is for a new table's tests)

**Interfaces:**
- Produces (on `Repository` Protocol + `SQLiteRepository`):
  - `claim_live_profit_protection(position_id: str, threshold_pct: str, trigger_mark_price: str, breakeven_price: str, new_sl_client_order_id: str, claimed_at: datetime) -> bool` (INSERT OR IGNORE, same race-defense pattern as `claim_live_execution` — but this claim does NOT need the `WHERE EXISTS position OPEN_POSITION` guard since Task 5 already verifies the live position itself before ever calling this; a plain `INSERT OR IGNORE ... position_id` primary-key uniqueness is the idempotency gate here. `old_sl_order_id`/`old_sl_price` are deliberately NOT claim-time parameters — they aren't known until sequence step 4, after the claim already exists; see `update_live_profit_protection_old_sl` below)
  - `get_live_profit_protection(position_id: str) -> dict | None`
  - `find_claimed_live_profit_protection() -> list[dict]` (status == `'CLAIMED'`, for restart recovery)
  - `update_live_profit_protection_old_sl(position_id: str, old_sl_order_id: str, old_sl_price: str, updated_at: datetime) -> None` (called at sequence step 4, once the single existing SL has been identified)
  - `update_live_profit_protection_new_sl(position_id: str, new_sl_order_id: str, updated_at: datetime) -> None` (called at sequence step 6, once the new SL's real order id is known)
  - `set_live_profit_protection_status(position_id: str, status: str, updated_at: datetime, last_error: str | None = None) -> None`
- Consumed by: Task 5/6's `live_profit_protection.py`

**Design:** Mirror `live_executions`' own table/CRUD conventions exactly (see `storage/db.py` lines ~186-206 and `repository.py`'s `claim_live_execution`/`mark_live_execution_failed` for the established style: plain `sqlite3`, ISO-format datetimes, explicit commit per write). Table columns exactly as specified in the spec's Data Model section.

- [ ] **Step 1: Write the failing tests**

Write tests asserting: `claim_live_profit_protection` returns `True` on first call, `False` on a second call for the same `position_id` (idempotency — this IS the "PP only activates once per position" gate); `get_live_profit_protection` returns `None` before any claim and the full row after; `find_claimed_live_profit_protection` returns only rows with `status == 'CLAIMED'`, not rows already moved to a terminal status; `update_live_profit_protection_new_sl` and `set_live_profit_protection_status` each update only their own fields (a status change must never clobber a previously-recorded `new_sl_order_id`, and vice versa).

- [ ] **Step 2: Run to verify failure**

Expected: FAIL with `AttributeError`/`OperationalError: no such table: live_profit_protection`.

- [ ] **Step 3: Implement**

Add the `CREATE TABLE IF NOT EXISTS live_profit_protection (...)` (exact columns from the spec's Data Model section) to `storage/db.py`'s schema init, and the five methods above to both the `Repository` Protocol and `SQLiteRepository` in `repository.py`, following the exact style (parameterized SQL, `.isoformat()` datetimes, `self._conn.commit()` after every write) already used by every neighboring method in that file.

- [ ] **Step 4: Run to verify pass**

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/storage/db.py crypto_trading/storage/repository.py tests/crypto_trading/storage/
git commit -m "feat(crypto-trading): add live_profit_protection table and repository CRUD"
```

---

### Task 3: Config flag

**Files:**
- Modify: `crypto_trading/config/live_execution.yaml`, `crypto_trading/config/loader.py`
- Test: `tests/crypto_trading/config/test_loader.py`

**Interfaces:**
- Produces: `LiveExecutionConfig.profit_protection_enabled: bool` (default `False`), `LiveExecutionConfig.profit_protection_threshold_pct: Decimal` (default `Decimal("0.01")`, i.e. 1.0% — a config value, not a hardcoded literal in the logic module, so a later, SEPARATE change can retune it without touching code, per the spec's explicit "the +1.0% threshold itself is not tuned in this change")
- Consumed by: Task 7's wiring into `live_execution_loop.py`

- [ ] **Step 1: Write the failing test**

```python
def test_get_settings_loads_live_profit_protection_defaults():
    settings = get_settings()
    assert settings.live_execution.profit_protection_enabled is False
    assert settings.live_execution.profit_protection_threshold_pct == Decimal("0.01")
```

- [ ] **Step 2: Run to verify failure**

- [ ] **Step 3: Implement**

Add to `live_execution.yaml` (with a comment explaining the default-off/separate-later-activation rationale, matching this file's existing comment style):
```yaml
profit_protection_enabled: false
profit_protection_threshold_pct: "0.01"
```
Add matching fields to `LiveExecutionConfig` in `loader.py`: `profit_protection_enabled: bool = Field(default=False)`, `profit_protection_threshold_pct: Decimal = Field(default=Decimal("0.01"), gt=0, le=1)`.

- [ ] **Step 4: Run to verify pass**

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/config/live_execution.yaml crypto_trading/config/loader.py tests/crypto_trading/config/test_loader.py
git commit -m "feat(crypto-trading): add default-off LIVE profit protection config flag"
```

---

### Task 4: Threshold-check helper (pure function, no I/O)

**Files:**
- Create: `crypto_trading/paper_trading/live_profit_protection.py` (this task starts the file; Task 5 adds to it)
- Test: `tests/crypto_trading/paper_trading/test_live_profit_protection.py` (this task starts the file; Task 5/6 add to it)

**Interfaces:**
- Produces: `unrealized_profit_pct(avg_price: Decimal, mark_price: Decimal) -> Decimal` — LONG-only (matches this codebase's sole supported direction everywhere else): `(mark_price - avg_price) / avg_price`. A negative result means unrealized loss, correctly excluded by the caller's `>= threshold_pct` check in Task 5.
- Consumed by: Task 5's main sequence function

**Design:** Deliberately the simplest possible pure function, isolated so it can be unit-tested against hand-calculated values without any connector/repo mocking at all (spec test case 12).

- [ ] **Step 1: Write the failing tests**

```python
from decimal import Decimal

from crypto_trading.paper_trading.live_profit_protection import unrealized_profit_pct


def test_unrealized_profit_pct_positive_when_mark_above_entry():
    assert unrealized_profit_pct(Decimal("100"), Decimal("101")) == Decimal("0.01")


def test_unrealized_profit_pct_negative_when_mark_below_entry():
    assert unrealized_profit_pct(Decimal("100"), Decimal("98")) == Decimal("-0.02")


def test_unrealized_profit_pct_zero_at_entry():
    assert unrealized_profit_pct(Decimal("100"), Decimal("100")) == Decimal("0")
```

- [ ] **Step 2: Run to verify failure**

- [ ] **Step 3: Implement**

```python
from __future__ import annotations

from decimal import Decimal


def unrealized_profit_pct(avg_price: Decimal, mark_price: Decimal) -> Decimal:
    """LONG-only (this codebase's sole supported direction, see
    paper_trading/position_opening.py::_DIRECTION). avg_price is the
    real exchange fill entry (get_position()'s own 'avgPrice' field -
    exchange state, never this module's locally-stored data), mark_price
    is the real exchange mark price (get_position()'s 'markPrice' -
    consistent with this project's SL/TP orders, which already trigger
    on workingType=MARK_PRICE, not last-traded price)."""
    return (mark_price - avg_price) / avg_price
```

- [ ] **Step 4: Run to verify pass**

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/paper_trading/live_profit_protection.py tests/crypto_trading/paper_trading/test_live_profit_protection.py
git commit -m "feat(crypto-trading): add LONG-only unrealized profit % helper for LIVE profit protection"
```

---

### Task 5: Core sequence — normal path, ambiguous-SL, rejected, uncertain-status branches

**Files:**
- Modify: `crypto_trading/paper_trading/live_profit_protection.py`
- Test: `tests/crypto_trading/paper_trading/test_live_profit_protection.py`

**Interfaces:**
- Consumes: Task 1's `place_stop_loss_order`/`cancel_order`, Task 2's repository methods, Task 4's `unrealized_profit_pct`, existing `connector.get_position`/`get_open_orders`/`get_order_by_client_order_id`, existing `_client_order_id`-style deterministic ID pattern (import or replicate the exact helper from `live_execution.py` — implementer's call, but the ID scheme must be equally deterministic and restart-safe)
- Produces: `run_live_profit_protection_tick(repo: Repository, connector: BingXLiveTradingConnector, threshold_pct: Decimal, run_id: str, now: datetime) -> None`
- Consumed by: Task 7's wiring

**Algorithm** (this is the spec's 7-step sequence — read `docs/superpowers/specs/2026-09-13-live-profit-protection-design.md` in full before implementing, it is the binding source, this is a summary):

For each `ACTIVE`-phase row from `repo.find_active_live_executions()` whose `position_id` has NO existing `live_profit_protection` row yet (skip entirely if one exists — that position already has a terminal outcome or is `CLAIMED`, handled by Task 6):

1. `position = connector.get_position(instrument)`. `None` → this position's real exchange counterpart is already gone; nothing to protect, skip (do NOT create a `live_profit_protection` row for a position that was never eligible — only Task 6's restart recovery deals with rows that already exist).
2. Compute `unrealized_profit_pct(Decimal(position["avgPrice"]), Decimal(position["markPrice"]))`. Below `threshold_pct` → skip, no row created (this position simply hasn't reached the trigger yet; it will be re-evaluated next tick).
3. At/above threshold → NOW claim: `repo.claim_live_profit_protection(...)` with `threshold_pct`, `trigger_mark_price=position["markPrice"]`, `breakeven_price=position["avgPrice"]`, and a freshly-generated deterministic `new_sl_client_order_id`. If the claim returns `False` (another thread/restart already claimed it — should be rare/impossible in this single-threaded tick design, but the DB is the race defense regardless), skip.
4. `orders = connector.get_open_orders(instrument)`, filter `type == "STOP_MARKET"`. Exactly 1 → `repo.update_live_profit_protection_old_sl(position_id, old_sl_order_id=<its orderId>, old_sl_price=<its stopPrice>, updated_at=now)` and continue. 0 or 2+ → `set_live_profit_protection_status(..., "ABORTED_AMBIGUOUS_SL", ...)`, stop, no write attempted.
5. `connector.place_stop_loss_order(instrument, quantity=<from repo.get_live_execution(position_id)["entry_quantity"]>, stop_price=str(breakeven_price), client_order_id=new_sl_client_order_id)`. Raises `OrderRejectedError` → `set_live_profit_protection_status(..., "ABORTED_NEW_SL_REJECTED", ..., last_error=str(exc))`, stop. Old SL untouched.
6. Look up the new SL by `new_sl_client_order_id` (via `connector.get_order_by_client_order_id`, exactly like `live_execution.py`'s own `_resolve_uncertain_entry` does for entry orders — reuse that exact lookup-then-classify discipline, do not invent a new one). Status `NEW`/`PENDING` → `repo.update_live_profit_protection_new_sl(position_id, new_sl_order_id=<its orderId>, updated_at=now)`, continue to step 7. Status `FILLED` → the position closed at ~breakeven during this operation; re-check `connector.get_position(instrument)`; if flat, `set_live_profit_protection_status(..., "SL_REPLACED", ...)` with a note this closed during verification (no order operation follows); if somehow still open (shouldn't happen, log it), treat conservatively as uncertain. Anything else (not found, lookup error, unrecognized status) → `set_live_profit_protection_status(..., "UNCERTAIN_NEW_SL_STATUS", ...)`, stop. **Do not cancel the old SL in this branch.**
7. `connector.cancel_order(instrument, old_sl_order_id)`. Success → re-read `get_open_orders(instrument)` once more (log what's actually there), `set_live_profit_protection_status(..., "SL_REPLACED", ...)`. Failure → `set_live_profit_protection_status(..., "REPLACEMENT_PARTIAL", ...)` (both order IDs already recorded from steps 4/6).

Every transition calls `log_event` with `position_id`, `instrument`, old/new SL price and order ID (whichever are known at that point), and the resulting status — per the spec's logging requirement.

- [ ] **Step 1: Write the failing tests**

Cover spec test cases 1-6 and 9-11 (normal success; rejected; old-cancel-fails→`REPLACEMENT_PARTIAL`; new-SL-status-unknown; zero existing SL; two+ existing SL; TP untouched in every scenario — assert on a fake/stub connector that TP-related methods are simply never called; position closed at step 1; position closes between step 1 and verification). Use a stub/fake `BingXLiveTradingConnector`-shaped object (this codebase's established pattern — check `tests/crypto_trading/paper_trading/test_live_execution.py` for its existing stub connector and reuse/extend that style, do not invent a parallel mocking approach) and a real `SQLiteRepository` against an in-memory/tmp DB (matches this codebase's existing repository-layer test convention, e.g. `test_position_opening.py`).

- [ ] **Step 2: Run to verify failure**

- [ ] **Step 3: Implement**

Implement `run_live_profit_protection_tick` per the algorithm above.

- [ ] **Step 4: Run to verify pass**

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/paper_trading/live_profit_protection.py tests/crypto_trading/paper_trading/test_live_profit_protection.py
git commit -m "feat(crypto-trading): implement LIVE profit protection break-even SL sequence"
```

---

### Task 6: Restart/crash recovery

**Files:**
- Modify: `crypto_trading/paper_trading/live_profit_protection.py`
- Test: `tests/crypto_trading/paper_trading/test_live_profit_protection.py`

**Interfaces:**
- Consumes: Task 2's `find_claimed_live_profit_protection`, Task 5's connector calls
- Produces: a recovery step run at the START of `run_live_profit_protection_tick` (before scanning for newly-eligible positions), resolving every `CLAIMED` row per the spec's "Restart / crash recovery" section

**Algorithm:** For each row from `repo.find_claimed_live_profit_protection()`:
1. `get_position` flat → `set_live_profit_protection_status(..., "POSITION_CLOSED_BEFORE_PP", ...)`.
2. Else `get_open_orders`, filter `STOP_MARKET`: exactly 1 matching `old_sl_order_id` → new SL never confirmed placed; look up `new_sl_client_order_id` via `get_order_by_client_order_id` — found as `NEW`/`PENDING`/`FILLED` → resume at Task 5 step 6/7 accordingly; not found/rejected → resume at Task 5 step 5 (retry placement under the SAME stored `new_sl_client_order_id` — never generate a new one on resume, that's what makes the lookup-first retry safe).
3. Exactly 2 `STOP_MARKET` orders (old + new) → attempt `cancel_order(old_sl_order_id)` exactly once; success → `SL_REPLACED`; failure → `REPLACEMENT_PARTIAL`.
4. 0 `STOP_MARKET` orders, position still open → `set_live_profit_protection_status(..., "ANOMALY_NO_PROTECTIVE_ORDER_FOUND", ...)`, log at error level, no auto-heal.

- [ ] **Step 1: Write the failing tests**

Cover spec test case 7: one test per recovery branch above (mid-step-3 not-found → retry; mid-step-3 found-NEW → resume at cancel; mid-step-4/5 both-orders-exist → cancel succeeds → `SL_REPLACED`; both-orders-exist → cancel fails again → `REPLACEMENT_PARTIAL`; zero-orders anomaly). Also cover spec test case 8 (a position already at any terminal status is never re-examined by either the main scan or recovery).

- [ ] **Step 2: Run to verify failure**

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run to verify pass**

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/paper_trading/live_profit_protection.py tests/crypto_trading/paper_trading/test_live_profit_protection.py
git commit -m "feat(crypto-trading): add restart/crash recovery to LIVE profit protection"
```

---

### Task 7: Wire into the LIVE tick, behind the flag; production-isolation test

**Files:**
- Modify: `crypto_trading/live_execution_loop.py`
- Test: `tests/crypto_trading/test_live_execution_loop.py`, plus a new isolation test in `tests/crypto_trading/paper_trading/test_live_profit_protection.py`

**Interfaces:**
- Consumes: Task 5/6's `run_live_profit_protection_tick`, `settings.live_execution.profit_protection_enabled`/`profit_protection_threshold_pct`

**Design:** One new call, gated by the flag, inserted exactly where the spec says (after `reconcile_active_executions`, before `close_time_limit_positions`):

```python
reconcile_active_executions(repo, connector, market_data_connector, run_id, now)
if settings.live_execution.profit_protection_enabled:
    run_live_profit_protection_tick(
        repo, connector, settings.live_execution.profit_protection_threshold_pct, run_id, now,
    )
close_guardian_exit_positions(repo, connector, run_id, now)
close_time_limit_positions(...)
process_pending_positions(...)
```

- [ ] **Step 1: Write the failing tests**

One test: with the flag `False` (default), `run_live_profit_protection_tick` is never called (patch/spy it). One test: with the flag `True`, it IS called, in the right order relative to `reconcile_active_executions`/`close_time_limit_positions`. One isolation test (spec test case 13): a static scan (e.g. `inspect.getsource`/`ast`, or simply a grep-style substring assertion — follow whatever lightweight pattern this codebase already uses for its Tier 1 production-isolation tests, if any exist, otherwise the simplest correct check) confirming `live_profit_protection.py` never imports from `paper_trading.position_closing`, `paper_trading.profit_protection_experiment`, or `crypto_trading.backtest`.

- [ ] **Step 2: Run to verify failure**

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run to verify pass**

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/live_execution_loop.py tests/crypto_trading/test_live_execution_loop.py tests/crypto_trading/paper_trading/test_live_profit_protection.py
git commit -m "feat(crypto-trading): wire LIVE profit protection into the live execution tick, default off"
```

---

### Task 8: Final verification (no code changes)

Per the spec's "Post-implementation verification" section:

- [ ] **Step 1:** Run the full project test suite: `python -m pytest tests/crypto_trading/ -q`. Expected: all green except the pre-existing, known, unrelated `test_settings_load_profit_protection_experiment_defaults` failure (caused by the live PAPER experiment's own `enabled: true` config state, unrelated to this plan).
- [ ] **Step 2:** `git diff --stat <plan-start-commit> HEAD` — confirm the file list matches exactly the Global Constraints' allow-list (Task 1-7's files only). Any other file appearing is a stop-and-investigate condition, not something to wave through.
- [ ] **Step 3:** Read `crypto_trading/config/live_execution.yaml` and confirm every pre-existing field (`max_concurrent_positions`, `margin_per_trade_usdt`, `leverage`, `max_position_hold_hours`, `signal_ttl_seconds`, `check_interval_seconds`, `claim_stale_after_seconds`, `max_retries`, `margin_safety_buffer_usdt`) is byte-identical to before this plan started; only the two new `profit_protection_*` keys are new.
- [ ] **Step 4:** Read-only check against the real BingX account: `get_balance()` and `get_all_positions()` via the real `BingXLiveTradingConnector` (same as this session's earlier verification call) — confirm the connector still works exactly as before, no regression from the two additive methods.
- [ ] **Step 5:** Do NOT place any real order, manually or otherwise, as a "test" of the new feature. It stays behind `profit_protection_enabled: false`. Activation is a separate, later, explicit decision.
- [ ] **Step 6:** Report to the user: exact files changed (from Step 2's diff), exactly how PP is wired into the tick (the Task 7 snippet, with real line numbers), full test results, full `git diff --stat`, and explicit confirmation that existing LIVE entry/SL/TP/reconciliation code (`live_execution.py`'s existing functions) has zero diff.
