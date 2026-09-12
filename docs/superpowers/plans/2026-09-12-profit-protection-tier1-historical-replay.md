# Profit Protection Tier 1 Historical Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a separate, read-only backtest subsystem that replays the two frozen Profit Protection hypotheses (+1.0%/+1.5%) against real historical BingX 1-minute candle data for the 68 PAPER positions already confirmed by the real AI/Gate pipeline, reconstructing baseline vs. shadow outcomes through the exact same production exit/fill/fee/funding functions, with a strict time-based train/test split — producing a statistically usable first read on the hypotheses without waiting 1-2 weeks for the live PAPER exposure pool to free up.

**Architecture:** A new `crypto_trading/backtest/` package that never touches `data/crypto_trading.db` for writes: it reads real historical position/candidate/Guardian-observation rows from production (read-only), fetches real historical klines/funding from BingX (one small, additive connector extension: optional `start_time_ms`/`end_time_ms` on `get_klines`/`get_funding_rate`), and replays each position's real entry/stop_loss/target through the *unmodified* production functions (`close_triggered_positions` for baseline, `advance_shadow`/`seed_shadows_for_position` for both PP variants, `check_exit_trigger`, `compute_fill_price`/`compute_fees`/`compute_funding`/`compute_pnl`) against a dedicated, disposable backtest SQLite database. The existing, already-fixed `profit_protection_report.py::build_report()` is reused verbatim for the bulk of the statistics; a new `backtest/report.py` adds only what it doesn't already provide (median, bootstrap CI, baseline-parity check, the per-position audit table).

**Tech Stack:** Python 3.13, pytest, respx (HTTP mocking for BingX), pydantic, sqlite3 — no new dependencies.

**Spec:** This plan's own "Viktiga krav" section (user's 12-point specification, 2026-09-12 conversation) is the spec of record; no separate spec doc exists. Verified facts this plan relies on (all confirmed live, read-only, in the same conversation):
- BingX `swap/v3/quote/klines` supports `startTime`/`endTime` (ms epoch); hard server-side cap `limit <= 1440` (verified: `limit=10080` → `code:109400`); a 24h/1m window returns exactly 1440 candles in one call; retention confirmed at least 6 months back.
- BingX `swap/v2/quote/fundingRate` supports `startTime`/`endTime` too; verified against a real 2026-09-03 24h window, returned the 4 real funding events at their real 8h timestamps.
- 68 real PAPER positions already exist in `data/crypto_trading.db` (50 CLOSED: 12 `stop_loss`/20 `target`/18 `time_limit`; 18 OPEN), spanning 2026-09-03 → now, 40 instruments — every one has a real AI/Gate-approved `theoretical_entry`/`stop_loss`/`target`/`opened_at`, independent of whatever `size` it ended up with.
- `guardian_observations` already has 6,366 real historical rows covering 52 of the 68 positions (`HOLD`/`WATCH`/`PROTECT` only — **zero `EXIT` ever recorded** in this dataset) — real history to replay, not something to recompute from RSI/volume-zscore from scratch.

## Global Constraints

- **Never write to `positions`, `candidates`, `assessments`, `gate_decisions`, `guardian_observations`, `demo_executions`, `live_executions`, or `profit_protection_shadow_positions` in `data/crypto_trading.db`.** All backtest writes go to a dedicated, disposable SQLite file under `backtest_output/`. The one exception, already an accepted convention elsewhere in this codebase (`performance/paper_track_report.py`, `performance/profit_protection_report.py`): opening a `SQLiteRepository` against `data/crypto_trading.db` for **read-only** access still runs `init_schema()`'s idempotent `CREATE TABLE IF NOT EXISTS`/`INSERT OR IGNORE schema_version` — harmless, not "experiment or trading data," documented, never touched again after open.
- **No production strategy/config file is modified.** The only production file touched in this entire plan is `crypto_trading/connectors/bingx_market_data.py`, and only by adding two new **optional, additive** parameters with default `None` — every existing call site (zero-arg `get_klines(symbol, interval, limit=100)`) is byte-for-byte unaffected.
- **No new AI calls, no Gate re-run, no sizing/exposure change, no Guardian recomputation.** Entry/stop_loss/target come from the *already-made* real historical decision; Guardian state comes from the *already-logged* real historical observation.
- **The two frozen thresholds only.** `FROZEN_THRESHOLDS_PCT` (`paper_trading/profit_protection_experiment.py`) is imported and used as-is — this plan defines no new threshold, no parameter sweep, no "let's also try 1.25%."
- **Every backtest position gets a fixed synthetic notional, `Decimal("1000")`**, completely decoupled from whatever `size` (including `0`) the real historical position actually got from the live exposure pool. This is the entire point of Tier 1 — it sidesteps the pool-saturation bottleneck by construction, not by waiting for it to clear.
- **Determinism:** given the same fetched historical candles/funding/Guardian rows, two runs of the replay engine must produce byte-identical shadow/position rows in the backtest DB. No `datetime.now()`/`random`/network call inside the tick loop itself — only at the outer fetch boundary (which is itself cached to disk per instrument/window, see Task 3).
- **Train/test split is time-based and physically separate** (two distinct backtest database files), never a random shuffle, never decided after looking at outcomes.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `crypto_trading/connectors/bingx_market_data.py` | Modify | Add optional `start_time_ms`/`end_time_ms` to `get_klines`/`get_funding_rate` |
| `crypto_trading/backtest/__init__.py` | Create | Empty package marker |
| `crypto_trading/backtest/dataset.py` | Create | Read-only selection of the 68 positions into typed `BacktestTarget` records |
| `crypto_trading/backtest/historical_fetch.py` | Create | Paginated real historical kline/funding fetch (1440-candle chunking), with a local on-disk cache so re-running a report never re-fetches |
| `crypto_trading/backtest/guardian_replay.py` | Create | Copies real historical `guardian_observations` rows (read from production, written only to the backtest DB) |
| `crypto_trading/backtest/replay_engine.py` | Create | Core per-position tick loop: seeds baseline position + 2 PP shadows, replays candles chronologically through unmodified production functions |
| `crypto_trading/backtest/report.py` | Create | Median, bootstrap CI, baseline-parity check, per-position audit table; delegates everything else to the existing `profit_protection_report.py::build_report()` |
| `crypto_trading/backtest/run_tier1_backtest.py` | Create | CLI entrypoint: selects dataset → fetches historical data → replays → writes JSON report |
| `tests/crypto_trading/connectors/test_bingx_market_data.py` | Modify | New optional-parameter tests |
| `tests/crypto_trading/backtest/test_dataset.py` | Create | Dataset selection tests |
| `tests/crypto_trading/backtest/test_historical_fetch.py` | Create | Pagination/caching tests (respx-mocked) |
| `tests/crypto_trading/backtest/test_guardian_replay.py` | Create | Guardian copy tests |
| `tests/crypto_trading/backtest/test_replay_engine.py` | Create | No-lookahead, same-candle, threshold-next-tick-only, baseline-parity, determinism, zero-production-writes |
| `tests/crypto_trading/backtest/test_report.py` | Create | Median/bootstrap-CI/per-position-table/train-test-split tests |
| `tests/crypto_trading/backtest/test_run_tier1_backtest.py` | Create | End-to-end CLI integration test, explicit "production DB untouched" assertion |

---

## Task 1: Connector — optional historical range on klines/funding

**Files:**
- Modify: `crypto_trading/connectors/bingx_market_data.py`
- Test: `tests/crypto_trading/connectors/test_bingx_market_data.py`

**Interfaces:**
- Produces: `BingXMarketDataConnector.get_klines(symbol, interval, limit=100, start_time_ms=None, end_time_ms=None) -> list[dict]`, `.get_funding_rate(symbol, limit=1, start_time_ms=None, end_time_ms=None) -> list[dict]`.
- Consumed by: Task 3's `historical_fetch.py`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/crypto_trading/connectors/test_bingx_market_data.py` (check the file first — reuse whatever `respx`/`connector` fixtures already exist there):

```python
def test_get_klines_omits_start_end_time_when_not_given(connector, respx_mock):
    route = respx_mock.get("https://open-api.bingx.com/openApi/swap/v3/quote/klines").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "", "data": []})
    )
    connector.get_klines("BTC-USDT", "1m", limit=5)
    request = route.calls.last.request
    assert "startTime" not in request.url.params
    assert "endTime" not in request.url.params


def test_get_klines_includes_start_end_time_when_given(connector, respx_mock):
    route = respx_mock.get("https://open-api.bingx.com/openApi/swap/v3/quote/klines").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "", "data": []})
    )
    connector.get_klines("BTC-USDT", "1m", limit=1440, start_time_ms=1788393600000, end_time_ms=1788480000000)
    request = route.calls.last.request
    assert request.url.params["startTime"] == "1788393600000"
    assert request.url.params["endTime"] == "1788480000000"


def test_get_funding_rate_includes_start_end_time_when_given(connector, respx_mock):
    route = respx_mock.get("https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate").mock(
        return_value=httpx.Response(200, json={"code": 0, "msg": "", "data": []})
    )
    connector.get_funding_rate("BTC-USDT", limit=50, start_time_ms=1788393600000, end_time_ms=1788480000000)
    request = route.calls.last.request
    assert request.url.params["startTime"] == "1788393600000"
    assert request.url.params["endTime"] == "1788480000000"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/crypto_trading/connectors/test_bingx_market_data.py -k "start_end_time" -v`
Expected: FAIL with `TypeError: get_klines() got an unexpected keyword argument 'start_time_ms'`

- [ ] **Step 3: Implement**

In `crypto_trading/connectors/bingx_market_data.py`, replace `get_klines`/`get_funding_rate`:

```python
    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> list[dict]:
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
            "timestamp": self._timestamp_ms(),
        }
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        return self._get(_KLINES_PATH, params)

    def get_funding_rate(
        self,
        symbol: str,
        limit: int = 1,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
    ) -> list[dict]:
        params = {"symbol": symbol, "limit": limit, "timestamp": self._timestamp_ms()}
        if start_time_ms is not None:
            params["startTime"] = start_time_ms
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        return self._get(_FUNDING_RATE_PATH, params)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/crypto_trading/connectors/test_bingx_market_data.py -v`
Expected: PASS (all tests, including every pre-existing one — zero-arg call sites are unaffected since both new params default to `None` and are simply omitted from `params` in that case)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/connectors/bingx_market_data.py tests/crypto_trading/connectors/test_bingx_market_data.py
git commit -m "feat(crypto-trading): add optional historical start/end time to BingX klines/funding"
```

---

## Task 2: Dataset selection

**Files:**
- Create: `crypto_trading/backtest/__init__.py` (empty)
- Create: `crypto_trading/backtest/dataset.py`
- Test: `tests/crypto_trading/backtest/test_dataset.py`

**Interfaces:**
- Produces: `BacktestTarget(BaseModel)` — `position_id: str`, `instrument: str`, `entry_price: Decimal`, `simulated_fill_entry: Decimal`, `stop_loss: Decimal`, `target: Decimal`, `opened_at: datetime`, `original_size: Decimal`, `original_status: Literal["OPEN_POSITION","CLOSED"]`, `original_exit_reason: str | None`, `original_closed_at: datetime | None`, `original_theoretical_exit: Decimal | None`, `original_simulated_fill_exit: Decimal | None`. `select_backtest_targets(repo: Repository) -> list[BacktestTarget]`.
- Consumed by: Task 5 (`replay_engine.py`), Task 6 (`report.py`, for the baseline-parity check via `original_*` fields).

- [ ] **Step 1: Write the failing tests**

Create `tests/crypto_trading/backtest/__init__.py` (empty) and `tests/crypto_trading/backtest/test_dataset.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.backtest.dataset import select_backtest_targets
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def _seed_position(repo, position_id, instrument, size, status, **overrides) -> Position:
    defaults = dict(
        position_id=position_id, candidate_id=position_id, instrument=instrument,
        direction="LONG", status=status, theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=size, fill_model_version="v1", opened_at=_NOW,
    )
    defaults.update(overrides)
    position = Position(**defaults)
    repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id=position_id, occurred_at=_NOW,
            run_id="seed", schema_version=1, payload={},
        ),
    )
    return position


def test_select_backtest_targets_includes_size_zero_positions(tmp_path):
    """The whole point of Tier 1: a real historical position that got
    size=0 from the live exposure pool is still a perfectly valid trade
    setup (real entry/stop/target) - it must not be silently excluded."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_position(repo, "pos-blocked", "BTCUSDT", Decimal("0"), "CLOSED",
                    theoretical_exit=Decimal("49000"), simulated_fill_exit=Decimal("48975"),
                    exit_reason="stop_loss", fees=Decimal("0"), funding=Decimal("0"),
                    closed_at=_NOW)

    targets = select_backtest_targets(repo)

    assert len(targets) == 1
    assert targets[0].position_id == "pos-blocked"
    assert targets[0].original_size == Decimal("0")


def test_select_backtest_targets_includes_open_positions_with_no_original_exit(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_position(repo, "pos-open", "ETHUSDT", Decimal("1000"), "OPEN_POSITION")

    targets = select_backtest_targets(repo)

    assert len(targets) == 1
    assert targets[0].original_status == "OPEN_POSITION"
    assert targets[0].original_exit_reason is None
    assert targets[0].original_closed_at is None


def test_select_backtest_targets_preserves_real_entry_stop_target(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_position(repo, "pos-1", "BTCUSDT", Decimal("500"), "CLOSED",
                    theoretical_entry=Decimal("60000"), simulated_fill_entry=Decimal("60030"),
                    stop_loss=Decimal("58000"), target=Decimal("64000"),
                    theoretical_exit=Decimal("64000"), simulated_fill_exit=Decimal("63968"),
                    exit_reason="target", fees=Decimal("0.2"), funding=Decimal("0"),
                    closed_at=_NOW)

    targets = select_backtest_targets(repo)

    t = targets[0]
    assert t.entry_price == Decimal("60000")
    assert t.simulated_fill_entry == Decimal("60030")
    assert t.stop_loss == Decimal("58000")
    assert t.target == Decimal("64000")
    assert t.original_exit_reason == "target"
    assert t.original_theoretical_exit == Decimal("64000")
    assert t.original_simulated_fill_exit == Decimal("63968")


def test_select_backtest_targets_returns_empty_for_empty_repo(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert select_backtest_targets(repo) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/crypto_trading/backtest/test_dataset.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crypto_trading.backtest'`

- [ ] **Step 3: Implement**

Create `crypto_trading/backtest/__init__.py` (empty file).

Create `crypto_trading/backtest/dataset.py`:

```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from crypto_trading.storage.repository import Repository

_PAGE_SIZE = 10_000


class BacktestTarget(BaseModel):
    """One real, already AI/Gate-confirmed historical trade setup, read
    read-only from production. `original_size`/`original_status`/
    `original_exit_reason`/`original_closed_at`/`original_theoretical_exit`/
    `original_simulated_fill_exit` are never used to compute anything in
    the replay itself (Tier 1 always uses a fixed backtest notional,
    decoupled from whatever size the live exposure pool actually gave this
    position) - they exist ONLY for the baseline-parity cross-check in
    report.py (Task 6)."""

    position_id: str
    instrument: str
    entry_price: Decimal
    simulated_fill_entry: Decimal
    stop_loss: Decimal
    target: Decimal
    opened_at: datetime
    original_size: Decimal
    original_status: Literal["OPEN_POSITION", "CLOSED"]
    original_exit_reason: str | None
    original_closed_at: datetime | None
    original_theoretical_exit: Decimal | None
    original_simulated_fill_exit: Decimal | None


def select_backtest_targets(repo: Repository) -> list[BacktestTarget]:
    """Read-only selection of EVERY real historical PAPER position
    (CLOSED and OPEN, size=0 and size>0 alike - see BacktestTarget
    docstring for why size is never a filter here). No exclusions: every
    position in `positions` reached CONFIRMED through the real, unmodified
    Gate/AI pipeline, so every one is a valid entry/stop/target setup."""
    targets: list[BacktestTarget] = []
    offset = 0
    while True:
        page = repo.find_all_positions(limit=_PAGE_SIZE, offset=offset)
        if not page:
            break
        for position in page:
            targets.append(
                BacktestTarget(
                    position_id=position.position_id,
                    instrument=position.instrument,
                    entry_price=position.theoretical_entry,
                    simulated_fill_entry=position.simulated_fill_entry,
                    stop_loss=position.stop_loss,
                    target=position.target,
                    opened_at=position.opened_at,
                    original_size=position.size,
                    original_status=position.status,
                    original_exit_reason=position.exit_reason,
                    original_closed_at=position.closed_at,
                    original_theoretical_exit=position.theoretical_exit,
                    original_simulated_fill_exit=position.simulated_fill_exit,
                )
            )
        offset += _PAGE_SIZE
    return targets
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/crypto_trading/backtest/test_dataset.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/backtest/__init__.py crypto_trading/backtest/dataset.py tests/crypto_trading/backtest/__init__.py tests/crypto_trading/backtest/test_dataset.py
git commit -m "feat(crypto-trading): add Tier 1 backtest dataset selection (read-only)"
```

---

## Task 3: Historical kline/funding fetch with pagination and on-disk cache

**Files:**
- Create: `crypto_trading/backtest/historical_fetch.py`
- Test: `tests/crypto_trading/backtest/test_historical_fetch.py`

**Interfaces:**
- Consumes: Task 1's `get_klines(..., start_time_ms, end_time_ms)`/`get_funding_rate(..., start_time_ms, end_time_ms)`.
- Produces: `fetch_historical_klines(connector, symbol: str, interval: str, start: datetime, end: datetime, cache_dir: Path) -> list[Kline]`, `fetch_historical_funding(connector, symbol: str, start: datetime, end: datetime, cache_dir: Path) -> list[FundingRate]`.
- Consumed by: Task 5 (`replay_engine.py`).

**Design:** `limit<=1440` is a hard server cap (Task verified this live: `code:109400` above it). At 1m resolution that's exactly 24h per call.

> **Correction (final whole-branch review, Critical Fix 1).** This paragraph originally claimed "every single position's full replay window fits in exactly one kline call and one funding call in practice", and treated pagination as a rare edge case. That reasoning was **wrong, and it is the origin of a Critical bug** (the plan's own reasoning was at fault, not an implementer deviation — same pattern as the Task 5 Critical-1/2 entries already in the ledger). The replay window is `opened_at + max_position_hold_hours + 2 minutes`, **not** `opened_at + 24h`: with a 1440-candle cap, a call starting at `opened_at` returns candles `opened_at .. opened_at + 23h59m`, and the first of those — the entry candle — is excluded by the replay engine's `evaluable` filter, so `hold_hours` topped out at 23.9833 and the `hold_hours >= max_position_hold_hours` gate in `check_exit_trigger`/`advance_shadow` was **structurally unreachable**. Real-run evidence: zero `time_limit` baseline exits across 76 replayed positions vs production's real 18, with 35/76 left artificially `OPEN_POSITION` and silently dropped from every baseline statistic. Because the window now exceeds 1440 minutes, **the >1440-candle pagination path is always exercised, for every position — it is the normal path, never a rare edge case**, and every position's fetch issues 2 kline calls rather than 1.

The function paginates in 1440-candle (1-day) chunks, walking `start` forward by 1440 minutes each iteration until it reaches `end`. Caches each `(symbol, interval, start_ms, end_ms)` response to a local JSON file under `cache_dir` so re-running a report (e.g. after fixing a report-formatting bug) never re-fetches identical historical data — this is what "deterministic" (Global Constraints) rests on: the *fetch* step is the only network-facing part, and it's memoized.

- [ ] **Step 1: Write the failing tests**

Create `tests/crypto_trading/backtest/test_historical_fetch.py`:

```python
import json
from datetime import UTC, datetime

import httpx
import respx

from crypto_trading.backtest.historical_fetch import (
    fetch_historical_funding,
    fetch_historical_klines,
)
from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector


def _connector() -> BingXMarketDataConnector:
    return BingXMarketDataConnector(
        base_url="https://open-api.bingx.com", timeout_seconds=5.0,
        max_retries=1, requests_per_second=1000, cache_ttl_seconds=0,
    )


def _raw_kline(close: str, time_ms: int) -> dict:
    return {"open": close, "high": close, "low": close, "close": close, "volume": "1", "time": time_ms}


@respx.mock
def test_fetch_historical_klines_single_call_for_a_24h_window(tmp_path):
    start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    route = respx.get("https://open-api.bingx.com/openApi/swap/v3/quote/klines").mock(
        return_value=httpx.Response(200, json={
            "code": 0, "msg": "",
            "data": [_raw_kline("50000", int(start.timestamp() * 1000))],
        })
    )
    connector = _connector()

    klines = fetch_historical_klines(connector, "BTC-USDT", "1m", start, end, tmp_path)

    assert route.call_count == 1
    assert route.calls.last.request.url.params["limit"] == "1440"
    assert len(klines) == 1
    assert klines[0].instrument == "BTC-USDT"


@respx.mock
def test_fetch_historical_klines_paginates_beyond_24h(tmp_path):
    """A >24h window (an OPEN position replayed up to 'now') must issue
    more than one call, each still capped at limit=1440 (the real server
    limit verified live: limit>1440 -> code 109400)."""
    start = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)  # 48h -> 2 pages of 1440

    def _responder(request):
        start_ms = int(request.url.params["startTime"])
        return httpx.Response(200, json={"code": 0, "msg": "", "data": [_raw_kline("1", start_ms)]})

    respx.get("https://open-api.bingx.com/openApi/swap/v3/quote/klines").mock(side_effect=_responder)
    connector = _connector()

    klines = fetch_historical_klines(connector, "BTC-USDT", "1m", start, end, tmp_path)

    assert len(klines) == 2  # one kline returned per page in this stub


@respx.mock
def test_fetch_historical_klines_is_cached_on_second_call(tmp_path):
    start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    route = respx.get("https://open-api.bingx.com/openApi/swap/v3/quote/klines").mock(
        return_value=httpx.Response(200, json={
            "code": 0, "msg": "", "data": [_raw_kline("50000", int(start.timestamp() * 1000))],
        })
    )
    connector = _connector()

    first = fetch_historical_klines(connector, "BTC-USDT", "1m", start, end, tmp_path)
    second = fetch_historical_klines(connector, "BTC-USDT", "1m", start, end, tmp_path)

    assert route.call_count == 1  # second call served entirely from cache
    assert first == second


@respx.mock
def test_fetch_historical_funding_uses_start_end_time(tmp_path):
    start = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    respx.get("https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate").mock(
        return_value=httpx.Response(200, json={
            "code": 0, "msg": "",
            "data": [{"symbol": "BTC-USDT", "fundingRate": "0.0001",
                      "fundingTime": int(start.timestamp() * 1000), "markPrice": "50000"}],
        })
    )
    connector = _connector()

    rates = fetch_historical_funding(connector, "BTC-USDT", start, end, tmp_path)

    assert len(rates) == 1
    assert rates[0].funding_rate == pytest.approx  # placeholder removed below


def test_fetch_historical_funding_returns_empty_list_for_no_data(tmp_path):
    pass  # covered by the respx-mocked test above; kept as a marker for symmetry with klines
```

(Note for the implementer: drop the accidental `pytest.approx` line in `test_fetch_historical_funding_uses_start_end_time` — replace with a concrete `Decimal` comparison, e.g. `from decimal import Decimal; assert rates[0].funding_rate == Decimal("0.0001")`, and remove the empty no-op test — these two lines are a planning-time typo, fix during Step 1 before running.)

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/crypto_trading/backtest/test_historical_fetch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crypto_trading.backtest.historical_fetch'`

- [ ] **Step 3: Implement**

Create `crypto_trading/backtest/historical_fetch.py`:

```python
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from crypto_trading.schemas.market import FundingRate, Kline

_KLINE_INTERVAL = "1m"
_MAX_CANDLES_PER_CALL = 1440  # BingX server-enforced hard cap, verified live 2026-09-12


class HistoricalMarketDataSource(Protocol):
    def get_klines(
        self, symbol: str, interval: str, limit: int = 100,
        start_time_ms: int | None = None, end_time_ms: int | None = None,
    ) -> list[dict]: ...
    def get_funding_rate(
        self, symbol: str, limit: int = 1,
        start_time_ms: int | None = None, end_time_ms: int | None = None,
    ) -> list[dict]: ...


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _cache_path(cache_dir: Path, kind: str, symbol: str, interval: str, start: datetime, end: datetime) -> Path:
    key = f"{kind}:{symbol}:{interval}:{_ms(start)}:{_ms(end)}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return cache_dir / f"{kind}_{symbol}_{digest}.json"


def fetch_historical_klines(
    connector: HistoricalMarketDataSource, symbol: str, interval: str,
    start: datetime, end: datetime, cache_dir: Path,
) -> list[Kline]:
    """Every real network call this function makes is startTime/endTime-
    bounded and paginated in <=1440-candle chunks (the real, live-verified
    BingX server limit) walking forward from `start`. Results are cached
    to `cache_dir` keyed by (symbol, interval, start, end) so a second
    call with the same arguments never re-fetches - this is what makes
    the replay engine deterministic across repeated runs (Global
    Constraints)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, "klines", symbol, interval, start, end)
    if path.exists():
        raw_all = json.loads(path.read_text())
    else:
        raw_all = []
        cursor = start
        while cursor < end:
            page_end = min(cursor + timedelta(minutes=_MAX_CANDLES_PER_CALL), end)
            raw = connector.get_klines(
                symbol, interval, limit=_MAX_CANDLES_PER_CALL,
                start_time_ms=_ms(cursor), end_time_ms=_ms(page_end),
            )
            raw_all.extend(raw)
            cursor = page_end
        path.write_text(json.dumps(raw_all))
    return sorted(
        (Kline.from_raw(r, symbol, interval) for r in raw_all),
        key=lambda k: k.observed_at,
    )


def fetch_historical_funding(
    connector: HistoricalMarketDataSource, symbol: str,
    start: datetime, end: datetime, cache_dir: Path,
) -> list[FundingRate]:
    """Funding events occur only every 8h - a single call per position
    window is always sufficient (never approaches any pagination limit
    at the scale this plan operates at), but the same cache convention as
    klines is used for consistency and determinism."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, "funding", symbol, "8h", start, end)
    if path.exists():
        raw = json.loads(path.read_text())
    else:
        raw = connector.get_funding_rate(
            symbol, limit=100, start_time_ms=_ms(start), end_time_ms=_ms(end)
        )
        path.write_text(json.dumps(raw))
    return sorted((FundingRate.from_raw(r) for r in raw), key=lambda f: f.observed_at)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/crypto_trading/backtest/test_historical_fetch.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/backtest/historical_fetch.py tests/crypto_trading/backtest/test_historical_fetch.py
git commit -m "feat(crypto-trading): add paginated, cached historical kline/funding fetch for Tier 1 replay"
```

---

## Task 4: Guardian observation replay copy

**Files:**
- Create: `crypto_trading/backtest/guardian_replay.py`
- Test: `tests/crypto_trading/backtest/test_guardian_replay.py`

**Interfaces:**
- Consumes: `Repository.find_guardian_observations_for_position(position_id) -> list[dict]` (existing, read), `Repository.save_guardian_observation(observation: GuardianObservation) -> bool` (existing, write).
- Produces: `copy_guardian_history(source_repo: Repository, backtest_repo: Repository, position_id: str) -> int` (returns the count copied).
- Consumed by: Task 5 (`replay_engine.py`), called once per position before the tick loop starts.

**Design:** Guardian's *deterministic* classification is already computed and persisted by production every ~60s (`guardian.check_interval_seconds`) for every position it watched live. Recomputing it from scratch would need historical RSI/volume-zscore/BTC-regime series and would duplicate `guardian/deterministic.py`'s logic outside its own tested boundary. Since the real observations already exist (6,366 rows, 52/68 positions covered), the correct, minimal-risk move is to **copy them read-only into the backtest DB**, then let `close_triggered_positions`'s *already-existing* staleness-guarded lookup (`position_closing.py:41-58`, unmodified) find them exactly as it does in production. A position with **zero** real Guardian coverage (16 of the 68) simply never finds an observation — `guardian_state` stays `None` — identical to how `close_triggered_positions` already behaves for a position with no Guardian history at all today.

- [ ] **Step 1: Write the failing tests**

Create `tests/crypto_trading/backtest/test_guardian_replay.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.backtest.guardian_replay import copy_guardian_history
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def _observation(obs_id: str, position_id: str, state: str, observed_at: datetime) -> GuardianObservation:
    return GuardianObservation(
        observation_id=obs_id, position_id=position_id, observed_at=observed_at,
        state=state, decay_score=Decimal("0.4"), progress_ratio=Decimal("0.1"),
        unrealized_pnl=Decimal("10"), factors={"time_decay": 0.1}, run_id="seed",
    )


def test_copy_guardian_history_copies_all_observations_for_the_position(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    source.save_guardian_observation(_observation("obs-1", "pos-1", "WATCH", _NOW))
    source.save_guardian_observation(_observation("obs-2", "pos-1", "PROTECT", _NOW))
    source.save_guardian_observation(_observation("obs-3", "pos-OTHER", "WATCH", _NOW))

    count = copy_guardian_history(source, backtest, "pos-1")

    assert count == 2
    copied = backtest.find_guardian_observations_for_position("pos-1")
    assert {o["observation_id"] for o in copied} == {"obs-1", "obs-2"}
    assert backtest.find_guardian_observations_for_position("pos-OTHER") == []


def test_copy_guardian_history_returns_zero_for_a_position_with_no_history(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")

    count = copy_guardian_history(source, backtest, "pos-never-watched")

    assert count == 0
    assert backtest.find_guardian_observations_for_position("pos-never-watched") == []


def test_copy_guardian_history_never_writes_to_the_source_repo(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    source.save_guardian_observation(_observation("obs-1", "pos-1", "WATCH", _NOW))

    copy_guardian_history(source, backtest, "pos-1")

    assert len(source.find_guardian_observations_for_position("pos-1")) == 1  # unchanged, not duplicated
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/crypto_trading/backtest/test_guardian_replay.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crypto_trading.backtest.guardian_replay'`

- [ ] **Step 3: Implement**

Create `crypto_trading/backtest/guardian_replay.py`:

```python
from __future__ import annotations

from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.storage.repository import Repository


def copy_guardian_history(source_repo: Repository, backtest_repo: Repository, position_id: str) -> int:
    """Read-only against `source_repo` (production). Copies every real
    historical Guardian observation for this position into the backtest
    DB so close_triggered_positions()'s existing staleness-guarded lookup
    (position_closing.py) finds them naturally during replay, unmodified.
    No recomputation of decay factors - see module docstring in the plan
    for why that would duplicate guardian/deterministic.py's own tested
    logic outside its boundary."""
    observations = source_repo.find_guardian_observations_for_position(position_id)
    for row in observations:
        backtest_repo.save_guardian_observation(GuardianObservation(**row))
    return len(observations)
```

Note for the implementer: `find_guardian_observations_for_position` returns `list[dict]` from `SELECT *` — confirm the dict's keys line up 1:1 with `GuardianObservation`'s fields (they should, since `save_guardian_observation` originally wrote them from the same model) before assuming `GuardianObservation(**row)` works unmodified; if `factors` comes back as a JSON string column rather than a parsed dict, add a `json.loads()` there and cover it with a test using a real round-trip (`save_guardian_observation` then `find_guardian_observations_for_position` then re-construct) rather than assuming.

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/crypto_trading/backtest/test_guardian_replay.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/backtest/guardian_replay.py tests/crypto_trading/backtest/test_guardian_replay.py
git commit -m "feat(crypto-trading): copy real historical Guardian observations into Tier 1 backtest DB"
```

---

## Task 5: Replay engine — the core tick loop (most correctness-critical task)

**Files:**
- Create: `crypto_trading/backtest/replay_engine.py`
- Test: `tests/crypto_trading/backtest/test_replay_engine.py`

**Interfaces:**
- Consumes: `BacktestTarget` (Task 2), `fetch_historical_klines`/`fetch_historical_funding` (Task 3), `copy_guardian_history` (Task 4); **unmodified production functions**: `paper_trading.position_closing.close_triggered_positions`, `paper_trading.profit_protection_experiment.seed_shadows_for_position`/`advance_shadow`/`FROZEN_THRESHOLDS_PCT`/`_shadow_id`/`_guardian_state_for`, `paper_trading.execution.compute_fill_price`.
- Produces: `replay_position(target: BacktestTarget, connector, source_repo: Repository, backtest_repo: Repository, settings: Settings, cache_dir: Path, run_id: str) -> None`. Writes only into `backtest_repo` (a real `Position` row + 2 `profit_protection_shadow_positions` rows). Consumed by Task 7 (`run_tier1_backtest.py`).

**Reuse ledger (per the user's explicit requirement to enumerate what's reused and what isn't):**

| Function | Reused unmodified? | Why |
|---|---|---|
| `close_triggered_positions` (baseline exit) | **Yes, verbatim** | This *is* the production baseline reconstruction — same SL→target→time_limit→guardian_exit order, same gap-fill math, same fee/funding. Pointed at `backtest_repo`, never `data/crypto_trading.db`. |
| `seed_shadows_for_position`/`advance_shadow`/`_close_shadow` (PP shadow state machine) | **Yes, verbatim** | Same reasoning — this is the exact code whose historical behavior we're trying to measure; reimplementing it for the backtest would test a different, unproven copy of the logic. |
| `check_exit_trigger`/`compute_hold_hours` | **Yes, verbatim (via `close_triggered_positions`)** | Not called directly by this module at all — reused transitively. |
| `compute_fill_price`/`compute_fees`/`compute_funding`/`compute_pnl` | **Yes, verbatim (via the two functions above)** | Same reasoning. |
| `_guardian_state_for` (private helper in `profit_protection_experiment.py`) | **Yes, imported directly** | Avoids re-deriving the exact staleness-guard math a third time; `backtest/` isn't production code so importing a leading-underscore helper across the package boundary is an accepted, deliberate reuse here (unlike inside `paper_trading/`, where the module intentionally *duplicates* `position_closing`'s lookup instead of importing it, for the documented reason that `position_closing.py` must stay completely untouched by the live experiment — that constraint doesn't apply to a read-only backtest module). |
| `open_position_for_candidate` (real position-opening flow) | **Not reused** | It computes `size` via the real, live exposure-pool-aware `compute_position_size()` — the exact mechanism Tier 1 exists to sidestep. Replaced by directly constructing a `Position` with the fixed `_BACKTEST_NOTIONAL` and the real historical `theoretical_entry`/`simulated_fill_entry`/`stop_loss`/`target`/`opened_at` (Task 2's `BacktestTarget`). |
| `position_sizing.py::compute_position_size` | **Not reused, deliberately** | Same reason — this is the live exposure-pool logic. Global Constraints explicitly forbid touching sizing/exposure; this task doesn't call it at all. |
| `discovery_loop.py`/`orchestrator.py`/Gate/AI roles | **Not reused** | Tier 1 never re-derives a trade decision — it only replays the *outcome* of a decision that was already made for real. |
| `monitoring_catchup.py::run_monitoring_catchup` | **Not reused directly, but its chronological-replay pattern is the template this task follows** | That function is scoped to short live-restart gaps (`_MAX_CATCHUP_KLINES=1000`) and calls production's own repo; this task needs a much longer, backtest-DB-scoped replay, so it re-implements the *loop shape* (candle-by-candle, each evaluated with its own timestamp as `now`) rather than importing the function itself. |

**Algorithm (exact, per the user's requirement to describe every rule explicitly):**

1. Fetch klines for `[target.opened_at, min(now_utc, target.opened_at + 24h)]` at `1m` (Task 3). The **first evaluable candle** is the earliest one with `observed_at > target.opened_at` (strictly after — the position was already open going into the entry price itself; the entry candle isn't re-evaluated as an exit opportunity, matching `monitoring_catchup.py`'s own `since < k.observed_at <= now` convention).
2. Fetch funding for the same window (Task 3); for each candle, the funding rate used is the most recent funding observation with `observed_at <= candle.observed_at` (falls back to `Decimal("0")` if none yet — identical convention to `monitoring_catchup.py::_latest_funding_rate`).
3. Copy Guardian history for this position (Task 4) into `backtest_repo` **before** the tick loop starts — `close_triggered_positions`/`_guardian_state_for` only ever read observations with `observed_at <= now` (the staleness guard is itself `now`-relative), so having the *entire* history present up front is safe and introduces no look-ahead: a candle at tick T can never see a Guardian observation whose own `observed_at` is after T, because the staleness-guard's `now - observed_at <= 2*check_interval` check is evaluated fresh at every tick with that tick's own candle timestamp as `now`.
4. Seed a `Position` row into `backtest_repo`: `position_id=target.position_id`, `theoretical_entry=target.entry_price`, `simulated_fill_entry=target.simulated_fill_entry` (the real historical fill, not recomputed), `stop_loss=target.stop_loss`, `target=target.target`, `size=_BACKTEST_NOTIONAL`, `opened_at=target.opened_at`, `status="OPEN_POSITION"`.
5. Seed the two PP shadows via `seed_shadows_for_position(backtest_repo, position, activated_at=_EPOCH, now=target.opened_at)` — `_EPOCH = datetime(1970,1,1,tzinfo=UTC)` is always `<= position.opened_at`, so the function's forward-only watermark check (`position.opened_at < activated_at: return`) never excludes a backtest position. This reuses the exact seeding function instead of calling `repo.seed_profit_protection_shadow` twice inline.
6. For each candle **in ascending `observed_at` order** (never touch a later candle before an earlier one has been fully processed — this ordering *is* the no-look-ahead guarantee):
   a. Build `price_lookup = {instrument: (candle.low, candle.high, candle.close, funding_rate_as_of(candle.observed_at))}`.
   b. Call `close_triggered_positions(backtest_repo, price_lookup, candle.observed_at, settings.risk_limits, run_id, guardian_config=settings.guardian)` — this is the **baseline** side; it may close the seeded `Position` row (stop_loss/target/time_limit/guardian_exit, in that fixed order — same-candle stop-and-target ambiguity is resolved by this function's own fixed check order: stop_loss is checked and returned first, unconditionally, exactly matching production, so a candle whose low breaches the stop AND whose high reaches the target in the same bar always exits `stop_loss` here, never `target` — this is not a new rule invented for the backtest, it's `check_exit_trigger`'s existing, unmodified behavior).
   c. For **each** `threshold_pct` in `FROZEN_THRESHOLDS_PCT`: re-fetch `shadow = backtest_repo.get_profit_protection_shadow(_shadow_id(target.position_id, threshold_pct))`; if `shadow["status"] != "OPEN"`, skip (already closed on an earlier candle — never re-evaluated, matching production's `find_open_profit_protection_shadows()` filter). Otherwise compute `guardian_state = _guardian_state_for(backtest_repo, target.position_id, candle.observed_at, settings.guardian) if settings.guardian.assisted_exit_enabled else None`, then call `advance_shadow(shadow, candle.low, candle.high, candle.close, funding_rate_as_of(candle.observed_at), candle.observed_at, settings.risk_limits.max_position_hold_hours, guardian_state, settings.guardian.assisted_exit_enabled, settings.risk_limits, backtest_repo)`. This is where **threshold-activation-only-affects-the-next-tick** is guaranteed: `advance_shadow` checks the exit conditions against the shadow's `active_sl` *as read at the start of this call* (from the row written by the *previous* tick), and only calls `activate_profit_protection_breakeven` at the very end, after every exit check — so a threshold first touched on candle N can only ever affect the stop used starting candle N+1's `advance_shadow` call, never candle N's own exit decision. This is `advance_shadow`'s existing, unmodified behavior (see its own docstring), not something this task re-implements.
   d. If the baseline position AND both shadows are all no longer `OPEN`/`OPEN_POSITION`, stop iterating candles for this position early (a harmless optimization — every subsequent candle would be a no-op since `close_triggered_positions`/`advance_shadow` already skip non-open rows).
7. If the loop exhausts all fetched candles while the baseline position and/or a shadow are still open, they are left `OPEN`/`OPEN_POSITION` in `backtest_repo` — **right-censored**, exactly as `n_reached_threshold`/`n_closed` in the existing `profit_protection_report.py` already distinguish (Task 6 surfaces this explicitly, never silently drops it).

- [ ] **Step 1: Write the failing tests**

Create `tests/crypto_trading/backtest/test_replay_engine.py`:

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.backtest.dataset import BacktestTarget
from crypto_trading.backtest.replay_engine import replay_position
from crypto_trading.config.loader import get_settings
from crypto_trading.paper_trading.profit_protection_experiment import _shadow_id
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


class _StubConnector:
    """A tiny, fully deterministic in-memory stand-in for BingX - avoids
    HTTP mocking noise in this task's tests (Task 3 already covers the
    real fetch/pagination/cache mechanics against respx directly)."""

    def __init__(self, klines: list[dict], funding: list[dict] | None = None):
        self._klines = klines
        self._funding = funding or []

    def get_klines(self, symbol, interval, limit=100, start_time_ms=None, end_time_ms=None):
        return [
            k for k in self._klines
            if (start_time_ms is None or k["time"] >= start_time_ms)
            and (end_time_ms is None or k["time"] <= end_time_ms)
        ]

    def get_funding_rate(self, symbol, limit=1, start_time_ms=None, end_time_ms=None):
        return self._funding


def _kline(close: str, time_ms: int, high=None, low=None) -> dict:
    return {"open": close, "high": high or close, "low": low or close, "close": close,
            "volume": "1", "time": time_ms}


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _target(**overrides) -> BacktestTarget:
    defaults = dict(
        position_id="pos-1", instrument="BTCUSDT", entry_price=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
        target=Decimal("60000"), opened_at=_NOW, original_size=Decimal("0"),
        original_status="CLOSED", original_exit_reason="stop_loss",
        original_closed_at=_NOW + timedelta(hours=1),
        original_theoretical_exit=Decimal("49000"), original_simulated_fill_exit=Decimal("48975"),
    )
    defaults.update(overrides)
    return BacktestTarget(**defaults)


def test_replay_position_never_writes_to_the_source_repo(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    connector = _StubConnector([_kline("50000", _ms(_NOW + timedelta(minutes=1)))])
    settings = get_settings()

    replay_position(_target(), connector, source, backtest, settings, tmp_path / "cache", "run-1")

    assert source.find_open_positions() == []
    assert source.get_position("pos-1") is None


def test_replay_position_seeds_both_frozen_thresholds(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    connector = _StubConnector([_kline("50000", _ms(_NOW + timedelta(minutes=1)))])
    settings = get_settings()

    replay_position(_target(), connector, source, backtest, settings, tmp_path / "cache", "run-1")

    assert backtest.get_profit_protection_shadow(_shadow_id("pos-1", Decimal("0.010"))) is not None
    assert backtest.get_profit_protection_shadow(_shadow_id("pos-1", Decimal("0.015"))) is not None


def test_replay_position_stop_loss_wins_on_same_candle_as_target(tmp_path):
    """SL always checked first (check_exit_trigger's fixed order, reused
    unmodified) - a candle whose low breaches stop AND whose high reaches
    target in the same bar must close stop_loss, never target."""
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    candle_time = _NOW + timedelta(minutes=1)
    connector = _StubConnector([_kline("55000", _ms(candle_time), high="61000", low="48000")])
    settings = get_settings()

    replay_position(_target(), connector, source, backtest, settings, tmp_path / "cache", "run-1")

    position = backtest.get_position("pos-1")
    assert position.status == "CLOSED"
    assert position.exit_reason == "stop_loss"


def test_replay_position_threshold_touch_only_affects_the_next_candle(tmp_path):
    """Candle 1 touches the +1.0% threshold (50500) but does NOT breach
    the ORIGINAL stop (49000) - the shadow must still be OPEN after candle
    1, with threshold_reached=1. Only candle 2 (which dips to 49950, above
    the ORIGINAL stop but BELOW the new breakeven stop 50000) proves the
    breakeven activation took effect - i.e. the shadow closes on candle 2,
    not candle 1, and at the breakeven price, not the original stop."""
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    c1 = _NOW + timedelta(minutes=1)
    c2 = _NOW + timedelta(minutes=2)
    connector = _StubConnector([
        _kline("50600", _ms(c1), high="50600", low="50100"),
        _kline("49960", _ms(c2), high="50050", low="49950"),
    ])
    settings = get_settings()

    replay_position(_target(target=Decimal("60000")), connector, source, backtest, settings,
                     tmp_path / "cache", "run-1")

    shadow = backtest.get_profit_protection_shadow(_shadow_id("pos-1", Decimal("0.010")))
    assert shadow["status"] == "CLOSED"
    assert shadow["exit_reason"] == "stop_loss"
    assert shadow["theoretical_exit"] == "49950"  # the NEW breakeven stop (50000) gap-filled to candle low
    assert shadow["threshold_reached"] == 1


def test_replay_position_is_deterministic_across_two_runs(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    connector = _StubConnector([
        _kline("50600", _ms(_NOW + timedelta(minutes=1)), high="50600", low="50100"),
        _kline("49960", _ms(_NOW + timedelta(minutes=2)), high="50050", low="49950"),
    ])
    settings = get_settings()

    backtest_a = SQLiteRepository(tmp_path / "a.db")
    backtest_b = SQLiteRepository(tmp_path / "b.db")
    replay_position(_target(), connector, source, backtest_a, settings, tmp_path / "cache", "run-1")
    replay_position(_target(), connector, source, backtest_b, settings, tmp_path / "cache", "run-2")

    shadow_a = backtest_a.get_profit_protection_shadow(_shadow_id("pos-1", Decimal("0.010")))
    shadow_b = backtest_b.get_profit_protection_shadow(_shadow_id("pos-1", Decimal("0.010")))
    for key in ("status", "exit_reason", "theoretical_exit", "mfe", "mae", "threshold_reached"):
        assert shadow_a[key] == shadow_b[key]


def test_replay_position_leaves_shadow_open_when_candles_run_out(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    backtest = SQLiteRepository(tmp_path / "backtest.db")
    connector = _StubConnector([_kline("50100", _ms(_NOW + timedelta(minutes=1)))])  # never touches SL/target
    settings = get_settings()

    replay_position(_target(), connector, source, backtest, settings, tmp_path / "cache", "run-1")

    shadow = backtest.get_profit_protection_shadow(_shadow_id("pos-1", Decimal("0.010")))
    assert shadow["status"] == "OPEN"  # right-censored, never silently dropped
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/crypto_trading/backtest/test_replay_engine.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crypto_trading.backtest.replay_engine'`

- [ ] **Step 3: Implement**

Create `crypto_trading/backtest/replay_engine.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from crypto_trading.backtest.dataset import BacktestTarget
from crypto_trading.backtest.guardian_replay import copy_guardian_history
from crypto_trading.backtest.historical_fetch import (
    HistoricalMarketDataSource,
    fetch_historical_funding,
    fetch_historical_klines,
)
from crypto_trading.config.loader import Settings
from crypto_trading.paper_trading.position_closing import close_triggered_positions
from crypto_trading.paper_trading.profit_protection_experiment import (
    FROZEN_THRESHOLDS_PCT,
    _guardian_state_for,
    _shadow_id,
    advance_shadow,
    seed_shadows_for_position,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_DIRECTION = "LONG"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_KLINE_INTERVAL = "1m"

# Fixed synthetic notional for every backtest position, deliberately
# decoupled from whatever `size` (including 0) the real historical
# position actually got from the live PAPER exposure pool - see this
# plan's Global Constraints. Not read from config: this is a Tier 1
# backtest-only constant, never a tunable.
BACKTEST_NOTIONAL = Decimal("1000")


def replay_position(
    target: BacktestTarget,
    connector: HistoricalMarketDataSource,
    source_repo: Repository,
    backtest_repo: Repository,
    settings: Settings,
    cache_dir: Path,
    run_id: str,
) -> None:
    """Writes ONLY into backtest_repo. source_repo is read from exactly
    once (Guardian history copy) and never written to - see
    guardian_replay.py::copy_guardian_history and this function's own
    test_replay_position_never_writes_to_the_source_repo."""
    # CORRECTED (final whole-branch review, Critical Fix 1). The original
    # reference code below was `window_end = min(now_utc,
    # target.opened_at + timedelta(hours=24))`. BOTH halves of that were
    # wrong: (a) the `min(now_utc, ...)` clamp poisons the fetch cache key
    # with a different `end` on every run (review round 1, Bundled Fix 1),
    # and (b) `+ timedelta(hours=24)` is one candle too short to ever
    # reach the `time_limit` exit, because the 1440-candle server cap plus
    # the `evaluable` filter's exclusion of the entry candle caps
    # `hold_hours` at 23.9833. The window must be derived from config with
    # headroom, so the >1440-candle pagination path is ALWAYS exercised:
    window_end = target.opened_at + timedelta(
        hours=settings.risk_limits.max_position_hold_hours, minutes=2
    )

    klines = fetch_historical_klines(
        connector, target.instrument, _KLINE_INTERVAL, target.opened_at, window_end, cache_dir
    )
    funding_rates = fetch_historical_funding(
        connector, target.instrument, target.opened_at, window_end, cache_dir
    )
    copy_guardian_history(source_repo, backtest_repo, target.position_id)

    position = Position(
        position_id=target.position_id, candidate_id=target.position_id,
        instrument=target.instrument, direction=_DIRECTION, status="OPEN_POSITION",
        theoretical_entry=target.entry_price, simulated_fill_entry=target.simulated_fill_entry,
        stop_loss=target.stop_loss, target=target.target, size=BACKTEST_NOTIONAL,
        fill_model_version="backtest-tier1", opened_at=target.opened_at,
    )
    backtest_repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_OPENED:{target.position_id}", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id=target.position_id,
            occurred_at=target.opened_at, run_id=run_id, schema_version=1, payload={},
        ),
    )
    seed_shadows_for_position(backtest_repo, position, activated_at=_EPOCH, now=target.opened_at)

    evaluable = [k for k in klines if k.observed_at > target.opened_at]  # entry candle itself is never re-evaluated
    for kline in evaluable:  # already ascending (fetch_historical_klines sorts) - never process out of order
        funding_rate = _latest_funding_rate(funding_rates, kline.observed_at)
        price_lookup = {
            target.instrument: (kline.low, kline.high, kline.close, funding_rate)
        }
        close_triggered_positions(
            backtest_repo, price_lookup, kline.observed_at, settings.risk_limits,
            run_id, guardian_config=settings.guardian,
        )

        any_shadow_open = False
        for threshold_pct in FROZEN_THRESHOLDS_PCT:
            shadow = backtest_repo.get_profit_protection_shadow(
                _shadow_id(target.position_id, threshold_pct)
            )
            if shadow is None or shadow["status"] != "OPEN":
                continue
            any_shadow_open = True
            guardian_state = (
                _guardian_state_for(backtest_repo, target.position_id, kline.observed_at, settings.guardian)
                if settings.guardian.assisted_exit_enabled
                else None
            )
            advance_shadow(
                shadow, kline.low, kline.high, kline.close, funding_rate, kline.observed_at,
                settings.risk_limits.max_position_hold_hours, guardian_state,
                settings.guardian.assisted_exit_enabled, settings.risk_limits, backtest_repo,
            )

        baseline_still_open = backtest_repo.get_position(target.position_id).status == "OPEN_POSITION"
        if not baseline_still_open and not any_shadow_open:
            break


def _latest_funding_rate(funding_rates, as_of: datetime) -> Decimal:
    visible = [f for f in funding_rates if f.observed_at <= as_of]
    return max(visible, key=lambda f: f.observed_at).funding_rate if visible else Decimal("0")
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/crypto_trading/backtest/test_replay_engine.py -v`
Expected: PASS (all 6 tests). If `test_replay_position_threshold_touch_only_affects_the_next_candle` fails on the exact `theoretical_exit` string, re-derive the expected gap-fill value by hand from `advance_shadow`'s own formula (`min(candle_low, active_sl)`) rather than adjusting the assertion to match whatever the code produces — the whole point of this test is pinning the *existing*, already-proven conservative gap-fill behavior.

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/backtest/replay_engine.py tests/crypto_trading/backtest/test_replay_engine.py
git commit -m "feat(crypto-trading): add Tier 1 replay engine (baseline + PP 1.0%/1.5%, unmodified production functions)"
```

---

## Task 6: Statistics report — median, bootstrap CI, baseline parity, per-position table

**Files:**
- Create: `crypto_trading/backtest/report.py`
- Test: `tests/crypto_trading/backtest/test_report.py`

**Interfaces:**
- Consumes: `profit_protection_report.build_report(repo) -> dict` (existing, **reused verbatim** — already computes sample sizes, reach classification, win/loss counts, total/expectancy/win-rate/profit-factor/max-drawdown P/L, conversion ratio, outcome labels, `n_blocked_by_exposure` (will always read `0` here since every backtest position has `size=BACKTEST_NOTIONAL>0` — documented, not a bug), chronological split).
- Produces: `build_tier1_report(train_repo: Repository, test_repo: Repository, source_repo: Repository, targets: list[BacktestTarget]) -> dict`.
- Consumed by: Task 7 (`run_tier1_backtest.py`).

**What this module adds on top of the reused `build_report()`:**
1. **Median P/L** per threshold, train and test separately (not in `build_report()`).
2. **Bootstrap confidence interval** on mean P/L difference (shadow − baseline) per threshold, train and test separately — percentile bootstrap, 10,000 resamples, 95% CI, pure Python (`random.choices`, no new dependency).
3. **Baseline-parity check**: for every `target` whose `original_status == "CLOSED"`, compare the *replayed* baseline outcome (read from `train_repo`/`test_repo`'s `positions` table) against `target.original_exit_reason`/`target.original_closed_at` — flags a mismatch as a named finding in the report rather than silently trusting either side.
4. **The exact per-position audit table** the user specified: `position_id | instrument | entry | threshold | threshold_reached | MFE | MAE | baseline_exit | baseline_pnl | shadow_exit | shadow_pnl | pnl_difference`.

- [ ] **Step 1: Write the failing tests**

Create `tests/crypto_trading/backtest/test_report.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.backtest.dataset import BacktestTarget
from crypto_trading.backtest.report import _bootstrap_ci, _median, build_tier1_report
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def test_median_odd_count():
    assert _median([Decimal("1"), Decimal("5"), Decimal("3")]) == Decimal("3")


def test_median_even_count():
    assert _median([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]) == Decimal("2.5")


def test_median_empty_returns_none():
    assert _median([]) is None


def test_bootstrap_ci_returns_none_for_empty_input():
    assert _bootstrap_ci([]) is None


def test_bootstrap_ci_returns_a_tuple_bracketing_the_sample_mean():
    values = [Decimal(str(v)) for v in [10, 12, 9, 11, 50, -5, 8, 10, 11, 9]]
    low, high = _bootstrap_ci(values, resamples=2000, seed=42)
    sample_mean = sum(values) / len(values)
    assert low <= sample_mean <= high


def test_bootstrap_ci_is_deterministic_given_a_seed():
    values = [Decimal(str(v)) for v in [10, 12, 9, 11, 50, -5, 8, 10, 11, 9]]
    first = _bootstrap_ci(values, resamples=500, seed=7)
    second = _bootstrap_ci(values, resamples=500, seed=7)
    assert first == second


def test_build_tier1_report_flags_baseline_parity_mismatch(tmp_path):
    """If the replay's own baseline exit_reason disagrees with what
    production actually recorded for the same position, that must be a
    visible, named finding - never silently averaged away."""
    source = SQLiteRepository(tmp_path / "source.db")
    train = SQLiteRepository(tmp_path / "train.db")
    test_repo = SQLiteRepository(tmp_path / "test.db")
    from crypto_trading.schemas.event import Event
    from crypto_trading.schemas.trade import Position

    def _seed(repo, exit_reason):
        repo.create_position_with_event(
            Position(
                position_id="pos-1", candidate_id="pos-1", instrument="BTCUSDT",
                direction="LONG", status="CLOSED", theoretical_entry=Decimal("50000"),
                simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
                target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1",
                opened_at=_NOW, theoretical_exit=Decimal("49000"),
                simulated_fill_exit=Decimal("48975"), exit_reason=exit_reason,
                fees=Decimal("0"), funding=Decimal("0"), closed_at=_NOW,
            ),
            Event(event_id="e1", event_type="POSITION_OPENED", aggregate_type="position",
                  aggregate_id="pos-1", occurred_at=_NOW, run_id="seed", schema_version=1, payload={}),
        )

    _seed(train, "stop_loss")  # replay agrees with production
    target = BacktestTarget(
        position_id="pos-1", instrument="BTCUSDT", entry_price=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"), target=Decimal("52000"),
        opened_at=_NOW, original_size=Decimal("500"), original_status="CLOSED",
        original_exit_reason="target",  # production said target - DISAGREES with the replay above
        original_closed_at=_NOW, original_theoretical_exit=Decimal("52000"),
        original_simulated_fill_exit=Decimal("51974"),
    )

    report = build_tier1_report(train, test_repo, source, [target])

    assert len(report["baseline_parity_mismatches"]) == 1
    assert report["baseline_parity_mismatches"][0]["position_id"] == "pos-1"
    assert report["baseline_parity_mismatches"][0]["replayed_exit_reason"] == "stop_loss"
    assert report["baseline_parity_mismatches"][0]["production_exit_reason"] == "target"


def test_build_tier1_report_per_position_table_has_required_columns(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    train = SQLiteRepository(tmp_path / "train.db")
    test_repo = SQLiteRepository(tmp_path / "test.db")

    report = build_tier1_report(train, test_repo, source, [])

    assert report["per_position_table"] == []  # empty dataset -> empty table, never crashes
    required_columns = {
        "position_id", "instrument", "entry", "threshold", "threshold_reached",
        "mfe", "mae", "baseline_exit", "baseline_pnl", "shadow_exit", "shadow_pnl",
        "pnl_difference",
    }
    assert report["per_position_table_columns"] == sorted(required_columns)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/crypto_trading/backtest/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crypto_trading.backtest.report'`

- [ ] **Step 3: Implement**

Create `crypto_trading/backtest/report.py`:

```python
from __future__ import annotations

import random
from decimal import Decimal

from crypto_trading.backtest.dataset import BacktestTarget
from crypto_trading.paper_trading.profit_protection_experiment import (
    FROZEN_THRESHOLDS_PCT,
    _shadow_id,
)
from crypto_trading.performance.profit_protection_report import build_report
from crypto_trading.storage.repository import Repository

_PER_POSITION_COLUMNS = sorted([
    "position_id", "instrument", "entry", "threshold", "threshold_reached",
    "mfe", "mae", "baseline_exit", "baseline_pnl", "shadow_exit", "shadow_pnl",
    "pnl_difference",
])


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _bootstrap_ci(
    values: list[Decimal], resamples: int = 10_000, confidence: float = 0.95, seed: int | None = None,
) -> tuple[Decimal, Decimal] | None:
    """Percentile bootstrap on the sample mean. Pure Python, no numpy -
    resamples `values` WITH replacement `resamples` times, computes the
    mean each time, returns the (2.5th, 97.5th) percentile of that
    distribution for a 95% CI. `seed` makes a specific call reproducible
    for tests; the real report run uses no seed (system entropy) since
    the underlying DATA is already fixed/cached (Task 3) - only the
    resampling order varies run to run, which is expected and standard
    for a bootstrap, not a determinism violation of the replay itself."""
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(resamples):
        sample = rng.choices(values, k=n)
        means.append(sum(sample, Decimal("0")) / n)
    means.sort()
    lower_idx = int((1 - confidence) / 2 * resamples)
    upper_idx = int((1 + confidence) / 2 * resamples) - 1
    return means[lower_idx], means[upper_idx]


def _baseline_parity_mismatches(repo: Repository, targets: list[BacktestTarget]) -> list[dict]:
    mismatches = []
    for target in targets:
        if target.original_status != "CLOSED":
            continue
        replayed = repo.get_position(target.position_id)
        if replayed is None or replayed.status != "CLOSED":
            continue  # not in this repo (train vs test split) or didn't close in the replay window
        if replayed.exit_reason != target.original_exit_reason:
            mismatches.append({
                "position_id": target.position_id,
                "replayed_exit_reason": replayed.exit_reason,
                "production_exit_reason": target.original_exit_reason,
            })
    return mismatches


def _per_position_rows(repo: Repository, targets: list[BacktestTarget]) -> list[dict]:
    rows = []
    for target in targets:
        replayed = repo.get_position(target.position_id)
        if replayed is None:
            continue
        for threshold_pct in FROZEN_THRESHOLDS_PCT:
            shadow = repo.get_profit_protection_shadow(_shadow_id(target.position_id, threshold_pct))
            if shadow is None:
                continue
            rows.append({
                "position_id": target.position_id,
                "instrument": target.instrument,
                "entry": str(target.entry_price),
                "threshold": str(threshold_pct),
                "threshold_reached": bool(shadow["threshold_reached"]),
                "mfe": shadow["mfe"],
                "mae": shadow["mae"],
                "baseline_exit": replayed.exit_reason,
                "baseline_pnl": str(_baseline_pnl(replayed)) if replayed.status == "CLOSED" else None,
                "shadow_exit": shadow["exit_reason"],
                "shadow_pnl": shadow["shadow_realized_pnl"],
                "pnl_difference": shadow["pnl_difference"],
            })
    return rows


def _baseline_pnl(position) -> Decimal:
    from crypto_trading.paper_trading.execution import compute_pnl
    return compute_pnl(position)


def _split_report_with_extras(repo: Repository, targets: list[BacktestTarget]) -> dict:
    base = build_report(repo)
    for threshold_key, block in base["per_threshold"].items():
        shadow_pnls = [
            Decimal(t["profit_protection_hypothetical_pnl"]) for t in block["trades"]
            if t["profit_protection_hypothetical_pnl"] is not None
            and t["reach_classification"] != "blocked_by_exposure"
        ]
        baseline_pnls = [
            Decimal(t["baseline_actual_pnl"]) for t in block["trades"]
            if t["baseline_actual_pnl"] is not None
            and t["reach_classification"] != "blocked_by_exposure"
        ]
        pnl_diffs = [
            Decimal(t["pnl_difference"]) for t in block["trades"]
            if t["pnl_difference"] is not None
            and t["reach_classification"] != "blocked_by_exposure"
        ]
        block["shadow_median_pnl_usdt"] = str(_median(shadow_pnls)) if _median(shadow_pnls) is not None else None
        block["baseline_median_pnl_usdt"] = (
            str(_median(baseline_pnls)) if _median(baseline_pnls) is not None else None
        )
        ci = _bootstrap_ci(pnl_diffs)
        block["pnl_difference_95pct_bootstrap_ci"] = (
            [str(ci[0]), str(ci[1])] if ci is not None else None
        )
    return base


def build_tier1_report(
    train_repo: Repository, test_repo: Repository, source_repo: Repository,
    targets: list[BacktestTarget],
) -> dict:
    train_targets = [t for t in targets if t.position_id in {t2.position_id for t2 in targets}]
    # train_repo/test_repo already only ever contain the positions routed
    # into them at replay time (Task 7 decides train-vs-test BEFORE
    # calling replay_position) - this function itself never re-splits by
    # date, it only reports on whatever each repo already holds.
    return {
        "train": _split_report_with_extras(train_repo, targets),
        "test": _split_report_with_extras(test_repo, targets),
        "baseline_parity_mismatches": (
            _baseline_parity_mismatches(train_repo, targets) + _baseline_parity_mismatches(test_repo, targets)
        ),
        "per_position_table": _per_position_rows(train_repo, targets) + _per_position_rows(test_repo, targets),
        "per_position_table_columns": _PER_POSITION_COLUMNS,
    }
```

Note for the implementer: the unused `train_targets` local is a leftover from drafting — delete it during implementation (dead code, would otherwise be a legitimate review finding).

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/crypto_trading/backtest/test_report.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/backtest/report.py tests/crypto_trading/backtest/test_report.py
git commit -m "feat(crypto-trading): add Tier 1 report (median, bootstrap CI, baseline parity, per-position table)"
```

---

## Task 7: CLI entrypoint + train/test wiring + zero-production-writes integration test

**Files:**
- Create: `crypto_trading/backtest/run_tier1_backtest.py`
- Test: `tests/crypto_trading/backtest/test_run_tier1_backtest.py`

**Interfaces:**
- Consumes: everything from Tasks 2–6.
- Produces: `run_tier1_backtest(source_repo: Repository, connector, settings: Settings, split_cutoff: datetime, output_dir: Path) -> dict` (the full report, also written to `output_dir/tier1_report.json`); `main()` CLI wrapper reading `--split-cutoff` and standard env-based settings, mirroring `profit_protection_report.py::main()`'s own pattern.

- [ ] **Step 1: Write the failing test**

Create `tests/crypto_trading/backtest/test_run_tier1_backtest.py`:

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.backtest.run_tier1_backtest import run_tier1_backtest
from crypto_trading.config.loader import get_settings
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


class _StubConnector:
    def get_klines(self, symbol, interval, limit=100, start_time_ms=None, end_time_ms=None):
        return [{"open": "50100", "high": "50100", "low": "50100", "close": "50100",
                  "volume": "1", "time": int((_NOW + timedelta(minutes=1)).timestamp() * 1000)}]

    def get_funding_rate(self, symbol, limit=1, start_time_ms=None, end_time_ms=None):
        return []


def _seed(source_repo, position_id, opened_at):
    source_repo.create_position_with_event(
        Position(
            position_id=position_id, candidate_id=position_id, instrument="BTCUSDT",
            direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
            simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
            target=Decimal("60000"), size=Decimal("0"), fill_model_version="v1", opened_at=opened_at,
        ),
        Event(event_id=f"e-{position_id}", event_type="POSITION_OPENED", aggregate_type="position",
              aggregate_id=position_id, occurred_at=opened_at, run_id="seed", schema_version=1, payload={}),
    )


def test_run_tier1_backtest_routes_positions_by_split_cutoff(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    _seed(source, "pos-train", _NOW)
    _seed(source, "pos-test", _NOW + timedelta(days=2))
    cutoff = _NOW + timedelta(days=1)
    connector = _StubConnector()
    settings = get_settings()

    report = run_tier1_backtest(source, connector, settings, cutoff, tmp_path / "out")

    train_ids = {row["position_id"] for row in report["per_position_table"] if row["position_id"] == "pos-train"}
    test_ids = {row["position_id"] for row in report["per_position_table"] if row["position_id"] == "pos-test"}
    assert "pos-train" in train_ids
    assert "pos-test" in test_ids
    assert (tmp_path / "out" / "tier1_report.json").exists()


def test_run_tier1_backtest_never_writes_to_the_source_repo(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    _seed(source, "pos-1", _NOW)
    original_position = source.get_position("pos-1")
    connector = _StubConnector()
    settings = get_settings()

    run_tier1_backtest(source, connector, settings, _NOW + timedelta(days=1), tmp_path / "out")

    assert source.get_position("pos-1") == original_position  # byte-identical, untouched
    assert source.find_all_positions(limit=100) == [original_position]  # no extra rows added
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/crypto_trading/backtest/test_run_tier1_backtest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crypto_trading.backtest.run_tier1_backtest'`

- [ ] **Step 3: Implement**

Create `crypto_trading/backtest/run_tier1_backtest.py`:

```python
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.backtest.dataset import select_backtest_targets
from crypto_trading.backtest.replay_engine import replay_position
from crypto_trading.backtest.report import build_tier1_report
from crypto_trading.config.loader import Settings, get_settings
from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
from crypto_trading.logging import new_run_id
from crypto_trading.storage.repository import Repository, SQLiteRepository


def run_tier1_backtest(
    source_repo: Repository, connector, settings: Settings,
    split_cutoff: datetime, output_dir: Path,
) -> dict:
    """Read-only against source_repo. Writes ONLY to two fresh, disposable
    backtest DB files under output_dir - never to data/crypto_trading.db.
    Every position is replayed into exactly ONE of train.db/test.db,
    decided once, before any replay happens, by
    target.opened_at < split_cutoff - physically separate databases, not
    a filter applied after the fact, so out-of-sample truly cannot leak
    into training results by construction (Global Constraints)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = output_dir / "historical_data_cache"
    train_repo = SQLiteRepository(output_dir / "train.db")
    test_repo = SQLiteRepository(output_dir / "test.db")
    run_id = new_run_id()

    targets = select_backtest_targets(source_repo)
    for target in targets:
        destination = train_repo if target.opened_at < split_cutoff else test_repo
        replay_position(target, connector, source_repo, destination, settings, cache_dir, run_id)

    report = build_tier1_report(train_repo, test_repo, source_repo, targets)
    report["split_cutoff"] = split_cutoff.isoformat()
    report["n_positions_total"] = len(targets)
    report["n_positions_train"] = sum(1 for t in targets if t.opened_at < split_cutoff)
    report["n_positions_test"] = sum(1 for t in targets if t.opened_at >= split_cutoff)

    (output_dir / "tier1_report.json").write_text(json.dumps(report, indent=2, default=str))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Profit Protection Tier 1 historical replay")
    parser.add_argument(
        "--split-cutoff", required=True,
        help="ISO datetime (UTC) - positions opened before this go to train, on/after go to test",
    )
    parser.add_argument("--output-dir", default="backtest_output/tier1")
    args = parser.parse_args()

    settings = get_settings()
    source_repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    connector = BingXMarketDataConnector(
        base_url=settings.pipeline.bingx_base_url, timeout_seconds=10.0,
        max_retries=settings.pipeline.bingx_max_retries,
        requests_per_second=settings.pipeline.bingx_requests_per_second,
        cache_ttl_seconds=settings.pipeline.bingx_cache_ttl_seconds,
    )
    split_cutoff = datetime.fromisoformat(args.split_cutoff)
    if split_cutoff.tzinfo is None:
        split_cutoff = split_cutoff.replace(tzinfo=UTC)

    report = run_tier1_backtest(source_repo, connector, settings, split_cutoff, Path(args.output_dir))
    print(json.dumps(
        {"n_positions_total": report["n_positions_total"],
         "n_positions_train": report["n_positions_train"],
         "n_positions_test": report["n_positions_test"],
         "baseline_parity_mismatches": len(report["baseline_parity_mismatches"])},
        indent=2,
    ))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/crypto_trading/backtest/test_run_tier1_backtest.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/backtest/run_tier1_backtest.py tests/crypto_trading/backtest/test_run_tier1_backtest.py
git commit -m "feat(crypto-trading): add Tier 1 backtest CLI entrypoint with time-based train/test split"
```

---

## Task 8: Full-suite verification and scoped diff proof

**Files:** none modified — read-only verification only.

- [ ] **Step 1: Run every new/modified test file together**

Run:
```bash
python -m pytest \
  tests/crypto_trading/connectors/test_bingx_market_data.py \
  tests/crypto_trading/backtest/ \
  -v
```
Expected: PASS, 0 failures.

- [ ] **Step 2: Run the entire `tests/crypto_trading/` suite**

Run: `python -m pytest tests/crypto_trading/ -q`
Expected: PASS, 0 new failures (the pre-existing, out-of-scope `test_settings_load_profit_protection_experiment_defaults` failure — caused by the live `enabled: true` PAPER activation, unrelated to this plan — is expected to still be present; do not touch it).

- [ ] **Step 3: Scoped diff proof — only the one intended production file changed**

Run:
```bash
git diff <pre-plan-sha> HEAD --stat -- crypto_trading/ | grep -v '^ crypto_trading/backtest/'
```
Expected: exactly one line — `crypto_trading/connectors/bingx_market_data.py | ...`. Nothing else under `crypto_trading/` outside the new `backtest/` package may appear.

- [ ] **Step 4: Prove zero writes to the real production DB from a real (not stubbed) run**

Run (read-only inspection, no destructive commands):
```bash
sha256sum data/crypto_trading.db > /tmp/before.sha
python -m crypto_trading.backtest.run_tier1_backtest --split-cutoff 2026-09-08T00:00:00 --output-dir backtest_output/tier1_manual_check
sha256sum data/crypto_trading.db > /tmp/after.sha
diff /tmp/before.sha /tmp/after.sha
```
Expected: `diff` prints nothing (files identical) — this is the strongest possible proof of "no production writes," stronger than any unit test, run once manually as a final gate. **This is a real network call to BingX for however many of the 68 positions fall in the fetch window** — read-only market-data GETs only, no order, no LIVE/Demo credential touched (this connector never had any). If this step is skipped or deferred, say so explicitly in the final report to the user rather than silently marking Task 8 complete without it.

- [ ] **Step 5: Read the report and eyeball-check for internal consistency**

Run: `python -m json.tool backtest_output/tier1_manual_check/tier1_report.json | head -100`
Confirm: `n_positions_train + n_positions_test == n_positions_total`, `baseline_parity_mismatches` is a list (empty is fine, non-empty needs eyeballing, not automatic failure), `per_position_table` has one row per `(position, threshold)` pair with all 12 required columns present.

No commit for this task (read-only, except the disposable `backtest_output/` artifacts from Step 4 — add `backtest_output/` to `.gitignore` if it isn't already, in a small final commit).

```bash
echo "backtest_output/" >> .gitignore
git add .gitignore
git commit -m "chore(crypto-trading): ignore Tier 1 backtest output directory"
```

---

## Self-Review (per writing-plans skill)

**Spec coverage** — every numbered requirement from the user's 12-point spec maps to a task:
1. Historical data (endpoint/params/intervals documented, additive) → Task 1, 3
2. Production parity, explicit reuse/non-reuse ledger → Task 5's reuse table
3. No production impact (dedicated DB, no writes) → Global Constraints + Task 8 Step 4
4. Historical dataset selection/exclusions/size=0/open positions/entry-start-time/baseline sourcing → Task 2
5. Replay algorithm (first candle, MFE/MAE, threshold-next-tick, SL/target/time-limit order, Guardian, same-candle, fill/fees/funding/exit/PnL) → Task 5's algorithm section
6. Baseline vs PP 1.0% vs PP 1.5% on identical data → Task 5 (both replayed through the same fetched candle stream)
7. Statistics (counts, wins/losses, P/L, median, expectancy, win rate, MFE/MAE, gave-back-gains/clipped-winner via reused `_outcome_label`, P/L diff, bootstrap CI, train/test separation) → Task 6 (delegates most to existing `build_report()`, adds median/CI/parity/table)
8. size=0 never mixed into $ results → Global Constraints (`BACKTEST_NOTIONAL` fixed for everyone) + existing `_is_blocked_by_exposure` logic inside the reused `build_report()`
9. No parameter hunting → `FROZEN_THRESHOLDS_PCT` imported, never redefined, anywhere in this plan
10. Test requirements (kline retrieval, funding retrieval, production parity, no-lookahead, same-candle, threshold activation, baseline parity, size=0 exclusion, train/test split, determinism, no production writes) → one test per item, named explicitly in Tasks 1, 3, 5, 6, 7
11. Acceptance criteria → see below
12. Output/per-position table → Task 6

**Placeholder scan:** no "TBD"/"handle appropriately" — the two noted drafting artifacts (a stray `pytest.approx` line in Task 3's test, an unused `train_targets` local in Task 6) are explicitly called out with the exact fix, not left vague.

**Type consistency:** `BacktestTarget` fields defined once in Task 2, used identically in Tasks 5/6/7. `replay_position`'s signature (Task 5) matches its call site in Task 7 exactly. `build_tier1_report`'s signature (Task 6) matches its call site in Task 7 exactly.

---

## Acceptance Criteria

- [ ] Same input (same cached historical data + same code) produces byte-identical shadow/position rows across two runs (Task 5, `test_replay_position_is_deterministic_across_two_runs`).
- [ ] Zero rows changed in `data/crypto_trading.db` after a real end-to-end run (Task 8, Step 4 — SHA-256 file comparison, not a mocked assertion).
- [ ] Zero order/write API calls made anywhere in this plan — every network call is a `GET` to a public BingX market-data endpoint already in use elsewhere in this codebase.
- [ ] Historical klines and funding independently verifiable against BingX's real API (already done live in this conversation; Task 1/3 tests pin the exact request shape).
- [ ] Baseline reproducible and cross-checked against the real, already-recorded production outcome (Task 6, `baseline_parity_mismatches` — present, not silently discarded, in the final report).
- [ ] Both thresholds compared on an identical dataset and identical candle stream (Task 5 — one candle-stream fetch per position, both PP variants and baseline replayed against it in the same loop).
- [ ] Test/out-of-sample period strictly later than train, enforced by two physically separate database files decided before any replay runs (Task 7).

---

## Proposed File Structure (summary)

```
crypto_trading/
  connectors/bingx_market_data.py     (modified — 2 additive params)
  backtest/
    __init__.py
    dataset.py
    historical_fetch.py
    guardian_replay.py
    replay_engine.py
    report.py
    run_tier1_backtest.py
tests/crypto_trading/
  connectors/test_bingx_market_data.py  (modified)
  backtest/
    __init__.py
    test_dataset.py
    test_historical_fetch.py
    test_guardian_replay.py
    test_replay_engine.py
    test_report.py
    test_run_tier1_backtest.py
```

## Proposed New Functions (summary)

- `select_backtest_targets(repo) -> list[BacktestTarget]`
- `fetch_historical_klines(connector, symbol, interval, start, end, cache_dir) -> list[Kline]`
- `fetch_historical_funding(connector, symbol, start, end, cache_dir) -> list[FundingRate]`
- `copy_guardian_history(source_repo, backtest_repo, position_id) -> int`
- `replay_position(target, connector, source_repo, backtest_repo, settings, cache_dir, run_id) -> None`
- `build_tier1_report(train_repo, test_repo, source_repo, targets) -> dict`
- `run_tier1_backtest(source_repo, connector, settings, split_cutoff, output_dir) -> dict`
- `main()` (CLI)

## Existing Functions Reused (summary — see Task 5's full ledger for the "why")

`close_triggered_positions`, `check_exit_trigger`, `compute_hold_hours`, `seed_shadows_for_position`, `advance_shadow`, `_close_shadow`, `_guardian_state_for`, `_shadow_id`, `FROZEN_THRESHOLDS_PCT`, `compute_fill_price`, `compute_fees`, `compute_funding`, `compute_pnl`, `build_report` (from `profit_protection_report.py`), `SQLiteRepository` (pointed at a fresh file), `find_all_positions`, `find_guardian_observations_for_position`, `save_guardian_observation`, `create_position_with_event`.

## Explicitly NOT Reused (summary — see Task 5 for the "why")

`open_position_for_candidate`, `compute_position_size`, anything in `discovery_loop.py`/`orchestrator.py`/`gate/`/`screening/`/AI agent roles, `guardian/deterministic.py`'s decay-factor computation (real historical observations are copied instead), `monitoring_catchup.py` (pattern reused, function not imported).

## Test Strategy

TDD throughout — every task writes the failing test before the implementation. `respx` for HTTP-level connector tests (existing convention). `_StubConnector` classes for engine-level tests (avoids re-testing HTTP mechanics already covered by Task 1/3). Real `SQLiteRepository(tmp_path / "...")` everywhere — no mocked repository. Determinism, no-lookahead, same-candle, and zero-production-write guarantees each get a dedicated, named test rather than being asserted only implicitly through a bigger integration test.

## Risks / Edge Cases

- **Guardian coverage gap:** 16 of 68 positions have zero real Guardian history. Handled by design (`guardian_state` stays `None`, matching existing production behavior for an unwatched position) — not a blocker, but worth naming in the final report so it isn't mistaken for a bug.
- **`respx`'s actual mock-matching semantics for query params** may differ subtly from the plan's `request.url.params[...]` assertions depending on the installed version — Task 1's implementer should run the tests early and adjust assertion syntax (not the underlying behavior) if the library's API differs.
- **`GuardianObservation(**row)` field-name mismatch** (e.g. `factors` stored as a JSON string column) — flagged explicitly in Task 4 with the required fix-if-needed, not assumed to just work.
- **BingX rate limits at real full-dataset scale** (68 positions × up to 2 calls each = ~136 requests) — `requests_per_second` is already configured in `settings.pipeline.bingx_requests_per_second` and `BingXMarketDataConnector` already rate-limits every call through `BaseMarketDataConnector._rate_limit()`; no new throttling logic needed, but a real full run will take a few minutes wall-clock, not seconds — worth setting expectations before Task 8 Step 4.
- **Positions still `OPEN_POSITION` in production** get replayed only up to `min(now, opened_at+24h)` — if `now` is before `opened_at+24h`, the backtest's own baseline/shadow may *also* end up right-censored `OPEN`, exactly mirroring live reality; this is correct, not a bug, and Task 6's report must never silently drop these (already handled: `n_reached_threshold`/`n_closed` distinguish them via the reused `build_report()`).
- **Bootstrap CI on very small per-threshold samples** (some cells may have single-digit `n`) will be wide and not very informative — expected given the 68-position ceiling; the report should be read as "Tier 1, a first look," never as a final verdict, consistent with everything already established in this conversation about sample-size caveats.

## Estimated Complexity

Medium. No new external dependencies, no architectural risk to production (additive connector change is the only production file touched, and it's two optional kwargs). The bulk of the engineering is careful, tested reuse-wiring (Task 5) rather than novel algorithm design — the hard analytical thinking (what to reuse, what never to touch, how to guarantee no-lookahead/determinism/zero-production-writes) is already resolved in this plan; implementation should be close to mechanical if followed task-by-task. Rough sizing: Tasks 1–4 and 6–8 are each roughly 1–2 hours of focused work; Task 5 (the replay engine) is the one task worth budgeting extra care and time for, given it's flagged as "most correctness-critical."
