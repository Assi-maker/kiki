# BingX Live Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror Gate-approved trades as small, tightly bounded real orders on the user's actual BingX Futures account (max 4 concurrent positions, 10 USDT margin / 10x leverage each) — without ever touching PAPER's simulation, without ever exceeding the hard caps even under a race, and without activating anything in this plan.

**Architecture:** A third, fully independent parallel observer of the same `POSITION_OPENED` events PAPER and BingX Demo already read — modeled directly on the already-shipped, already-proven BingX Demo (VST) execution layer (`connectors/bingx_demo_trading.py`, `paper_trading/demo_execution.py`, `demo_execution_loop.py`). LIVE adds two things Demo never needed: a two-layer, reconciliation-based capacity/margin gate (Demo mirrors unconditionally, LIVE must not), and fixed 10 USDT/10x sizing independent of PAPER's dynamic sizing. Writes exclusively to a new `live_executions` table.

**Tech Stack:** Python, httpx + tenacity (existing pattern from `connectors/base.py`), pydantic (config), SQLite (existing `storage/db.py`/`repository.py`), pytest + respx (existing test conventions).

**Spec:** `docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md`

## Global Constraints

- `open-api.bingx.com` is the **only** host this code may ever contact — hardcoded class constant, never a constructor/env/settings value, checked by exact hostname match (not substring) immediately before every mutating HTTP call.
- Credentials read **only** from `BINGX_API_KEY`/`BINGX_API_SECRET` (confirmed with the user: the dedicated, already-configured, currently-unused LIVE keys in `.env`) — never logged, never printed, never included in an exception message.
- LIVE code may never create, modify, or close a row in the `positions` table. It only reads `positions` and writes `live_executions`. It never touches `demo_executions` or Demo's code paths.
- Fixed sizing: `margin_per_trade_usdt=10`, `leverage=10` (~100 USDT notional), independent of PAPER's `position.size`/1000 USDT cap — never derived from them.
- Hard cap: max 4 concurrent LIVE positions, enforced twice — once coarsely before Discovery spends any AI budget, once authoritatively immediately before order placement — both counts based on **reconciled** exchange state, never local DB state alone.
- Balance check: a new trade may only be sent if `get_balance()` reports available margin ≥ 11 USDT (`margin_per_trade_usdt` 10 + `margin_safety_buffer_usdt` 1, locked). The buffer never inflates order size — quantity is always computed from exactly 10 USDT × 10x.
- LIVE's own hard time limit is 6 hours (`live_execution.yaml::max_position_hold_hours`), completely independent of PAPER's unchanged 24-hour limit.
- `gate/risk_signal_gate.py`, `orchestrator.py`, the 7 AI roles, Guardian's classification logic, and every PAPER/Demo parameter are **not modified** anywhere in this plan.
- New thread gated by `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED`, **default off**. This env var is never set by this plan — activation is a separate, later, explicit decision.
- Zero real network calls in the automated test suite — everything hits `respx`-mocked `https://open-api.bingx.com`.
- **No task in this plan ever places a real order.** The only real, authenticated calls this plan permits anywhere are read-only (`get_balance`/`get_all_positions`/`get_contracts` against the real account) in the final task, and even those require the user's separate, explicit go-ahead in a live conversation before they run.

---

## Task 1: SPEC_CRYPTO.md amendment

**Files:**
- Modify: `SPEC_CRYPTO.md` (§1 "Det här är INTE" list, §19, §20 self-review table)

**Interfaces:** None (documentation only).

- [ ] **Step 1: Amend §1**

Find the paragraph added by the BingX Demo amendment (the one starting "**Explicit, avsiktligt undantag (2026-09-04, ...**" under the "Ett tradingsystem i verklig mening mot ett riktigt (live) konto" bullet) and add, directly after its bullet list (before the closing "Paper trading ... förblir uteslutande lokal, simulerad bokföring" sentence):

```
  **Andra, striktare undantag (2026-09-06, se
  `docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md`):**
  `crypto_trading/connectors/bingx_live_trading.py` får placera/avbryta
  ordrar mot användarens **riktiga** BingX-konto, men uteslutande inom ett
  hårt, kodnivå-säkrat kontrollerat produktionstest:
  - `_base_url` är en hårdkodad modulkonstant (`open-api.bingx.com`), aldrig
    en constructor-/env-/settings-parameter.
  - Ett exakt host-guard körs omedelbart före varje order-läggande/ändrande/
    avbrytande anrop.
  - Credentials läses uteslutande från `BINGX_API_KEY`/`BINGX_API_SECRET`,
    aldrig delade med `CRYPTO_TRADING_BINGX_DEMO_API_KEY/_SECRET`.
  - Tråden är avstängd som standard (`CRYPTO_TRADING_LIVE_EXECUTION_ENABLED`,
    opt-in) — och förblir avstängd genom hela detta plan-dokument.
  - Max 4 samtidiga positioner, 10 USDT margin/10x leverage (~100 USDT
    notional) per position, en egen 6-timmars hard time-limit, och en
    tvålagers (koarst + auktoritativt, båda reconciliation-baserade)
    kapacitets-/marginalspärr — se designspecen för den fulla motiveringen.
  - Denna kod får **aldrig** skapa/ändra/stänga en rad i `positions`-
    tabellen — PAPER, BingX Demo och BingX Live är tre oberoende, parallella
    observatörer av samma redan Gate-godkända trade.
```

- [ ] **Step 2: Amend §19**

Find the sentence "Explicit, avsiktligt undantag: `connectors/bingx_demo_trading.py` mot BingX Demo (VST) uteslutande, se §1 och `docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md`." and extend it:

```
Explicit, avsiktligt undantag: `connectors/bingx_demo_trading.py` mot BingX
Demo (VST) uteslutande, och `connectors/bingx_live_trading.py` mot det
riktiga kontot inom det hårt begränsade kontrollerade produktionstestet
(max 4 positioner, 10 USDT margin/10x), se §1 och
`docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md` /
`docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md`.
```

- [ ] **Step 3: Amend §20 self-review table**

Find the row `| Kan riktig (LIVE-konto) handel ske av misstag? | Nej — ... |` and replace its answer text with:

```
| Kan riktig (LIVE-konto) handel ske av misstag? | Nej — `connectors/bingx_live_trading.py` har en hårdkodad `_base_url`-konstant (`open-api.bingx.com`, aldrig en parameter), ett exakt host-guard före varje mutating anrop, dedikerade `BINGX_API_KEY/_SECRET`-variabler, tråden är avstängd som standard, en tvålagers reconciliation-baserad kapacitets-/marginalspärr (max 4 positioner, ≥11 USDT tillgänglig marginal), och ett fast 10 USDT/10x-tak oberoende av PAPER:s sizing (§1, §19, `docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md`). |
| Kan BingX Live-exekveringen ändra en PAPER- eller Demo-position? | Nej — den skriver uteslutande till `live_executions`, aldrig till `positions` eller `demo_executions`. |
```

- [ ] **Step 4: Commit**

```bash
git add SPEC_CRYPTO.md
git commit -m "$(cat <<'EOF'
docs(crypto-trading): amend SPEC for BingX Live execution exception

Narrows the broker-connection boundary further to explicitly permit a
tightly bounded real-money test (max 4 positions, 10 USDT margin/10x
leverage) through the new connectors/bingx_live_trading.py, gated by
a hardcoded exact-host guard, dedicated BINGX_API_KEY/_SECRET
credentials, a default-off arm flag, and a two-layer reconciled
capacity/margin gate. See
docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HaqP9r9eK2JEkBVwPyRym8
EOF
)"
```

---

## Task 2: Live execution config

**Files:**
- Create: `crypto_trading/config/live_execution.yaml`
- Modify: `crypto_trading/config/loader.py`
- Test: `tests/crypto_trading/config/test_live_execution_config.py`

**Interfaces:**
- Produces: `LiveExecutionConfig` (pydantic model: `check_interval_seconds: int`, `claim_stale_after_seconds: int`, `max_retries: int`, `max_concurrent_positions: int`, `margin_per_trade_usdt: Decimal`, `leverage: int`, `max_position_hold_hours: int`, `margin_safety_buffer_usdt: Decimal`), `Settings.live_execution: LiveExecutionConfig`, module function `is_live_execution_enabled() -> bool` reading `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED`.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/config/test_live_execution_config.py
from crypto_trading.config.loader import get_settings, is_live_execution_enabled


def test_settings_load_live_execution_defaults():
    settings = get_settings()
    live = settings.live_execution
    assert live.check_interval_seconds > 0
    assert live.claim_stale_after_seconds > 0
    assert live.max_retries > 0
    assert live.max_concurrent_positions == 4
    assert live.margin_per_trade_usdt == 10
    assert live.leverage == 10
    assert live.max_position_hold_hours == 6
    assert live.margin_safety_buffer_usdt == 1


def test_is_live_execution_enabled_reads_env_flag(monkeypatch):
    monkeypatch.delenv("CRYPTO_TRADING_LIVE_EXECUTION_ENABLED", raising=False)
    assert is_live_execution_enabled() is False
    monkeypatch.setenv("CRYPTO_TRADING_LIVE_EXECUTION_ENABLED", "1")
    assert is_live_execution_enabled() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/crypto_trading/config/test_live_execution_config.py -v`
Expected: FAIL — `live_execution.yaml` missing / `LiveExecutionConfig`/`is_live_execution_enabled` not defined.

- [ ] **Step 3: Create the YAML file**

```yaml
# crypto_trading/config/live_execution.yaml
# BingX Live execution (2026-09-06) - a third, independent parallel
# observer of PAPER's Gate-approved trades, see
# docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md. Whether
# the thread runs at all is an env-var arm flag
# (CRYPTO_TRADING_LIVE_EXECUTION_ENABLED), not this file. max_concurrent_positions,
# margin_per_trade_usdt, and leverage are hard user-mandated caps - never
# raise them without a fresh, explicit user decision.
check_interval_seconds: 30
claim_stale_after_seconds: 30
max_retries: 3
max_concurrent_positions: 4
margin_per_trade_usdt: "10"
leverage: 10
max_position_hold_hours: 6
margin_safety_buffer_usdt: "1.00"
```

- [ ] **Step 4: Add `LiveExecutionConfig` and wire it into `Settings`**

In `crypto_trading/config/loader.py`, add after `class GuardianConfig(BaseModel): ...`:

```python
class LiveExecutionConfig(BaseModel):
    # BingX Live execution (2026-09-06) - a tightly bounded, real-money
    # controlled test, see
    # docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md.
    # max_concurrent_positions/margin_per_trade_usdt/leverage/
    # max_position_hold_hours are hard user-mandated caps - completely
    # independent of PAPER's own risk_limits.yaml (max_concurrent_positions,
    # max_position_notional_usdt, max_position_hold_hours), never derived
    # from them. margin_safety_buffer_usdt is a separate fail-safe
    # threshold on the balance CHECK only - it must never inflate the
    # order's own size, which stays fixed at margin_per_trade_usdt.
    check_interval_seconds: int = Field(gt=0, default=30)
    claim_stale_after_seconds: int = Field(gt=0, default=30)
    max_retries: int = Field(gt=0, default=3)
    max_concurrent_positions: int = Field(gt=0, default=4)
    margin_per_trade_usdt: Decimal = Field(gt=0, default=Decimal("10"))
    leverage: int = Field(gt=0, default=10)
    max_position_hold_hours: int = Field(gt=0, default=6)
    margin_safety_buffer_usdt: Decimal = Field(ge=0, default=Decimal("1.00"))
```

In `class Settings(BaseModel):`, add:
```python
    live_execution: LiveExecutionConfig = Field(default_factory=LiveExecutionConfig)
```

In `get_settings()`, add to the `Settings(...)` call:
```python
        live_execution=_load_yaml_model(_CONFIG_DIR / "live_execution.yaml", LiveExecutionConfig),
```

At the bottom of the file, add:
```python
def is_live_execution_enabled() -> bool:
    """Opt-in arm flag for the BingX Live execution thread - same pattern as
    is_demo_execution_enabled()/is_guardian_enabled(). Stays unset/False for
    the entire implementation plan; activation is a separate, later,
    explicit decision (see design spec §16)."""
    return bool(os.environ.get("CRYPTO_TRADING_LIVE_EXECUTION_ENABLED"))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/crypto_trading/config/test_live_execution_config.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add crypto_trading/config/live_execution.yaml crypto_trading/config/loader.py tests/crypto_trading/config/test_live_execution_config.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add live execution config and opt-in arm flag

max_concurrent_positions=4, margin_per_trade_usdt=10, leverage=10,
max_position_hold_hours=6, margin_safety_buffer_usdt=1.00 - all hard
user-mandated caps, independent of PAPER's own risk_limits.yaml.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HaqP9r9eK2JEkBVwPyRym8
EOF
)"
```

---

## Task 3: `live_executions` table

**Files:**
- Modify: `crypto_trading/storage/db.py`
- Test: `tests/crypto_trading/storage/test_db.py`

**Interfaces:**
- Produces: table `live_executions` with columns `position_id TEXT PRIMARY KEY, phase TEXT NOT NULL, entry_client_order_id TEXT, entry_exchange_order_id TEXT, entry_quantity TEXT, sl_exchange_order_id TEXT, tp_exchange_order_id TEXT, exit_reason TEXT, exchange_fill_entry TEXT, exchange_fill_exit TEXT, last_error TEXT, margin_usdt TEXT, notional_usdt TEXT, leverage TEXT, realized_fees_usdt TEXT, realized_funding_usdt TEXT, claimed_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/crypto_trading/storage/test_db.py
def test_live_executions_table_exists(tmp_path):
    from crypto_trading.storage.db import get_connection

    conn = get_connection(tmp_path / "t.db")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(live_executions)").fetchall()}
    assert columns == {
        "position_id", "phase", "entry_client_order_id", "entry_exchange_order_id",
        "entry_quantity", "sl_exchange_order_id", "tp_exchange_order_id", "exit_reason",
        "exchange_fill_entry", "exchange_fill_exit", "last_error", "margin_usdt",
        "notional_usdt", "leverage", "realized_fees_usdt", "realized_funding_usdt",
        "claimed_at", "updated_at", "closed_at",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/crypto_trading/storage/test_db.py::test_live_executions_table_exists -v`
Expected: FAIL — table does not exist.

- [ ] **Step 3: Add the table to `_SCHEMA`**

In `crypto_trading/storage/db.py`, append inside the `_SCHEMA` string, after the `demo_executions` table definition:

```sql
-- BingX Live execution (2026-09-06): strictly additive third parallel
-- observer of an already-Gate-approved PAPER position (alongside PAPER's
-- own `positions` and BingX Demo's `demo_executions`) - never joined-into
-- or written-from position_opening.py/position_closing.py or
-- demo_execution.py, see
-- docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md.
-- phase: CLAIMED -> ENTRY_SUBMITTED -> ACTIVE -> CLOSED / FAILED / SKIPPED
-- (SKIPPED: capacity/margin/exchange-minimum prevented this position from
-- ever getting a live order - a safe, expected outcome, not an error).
-- margin_usdt/notional_usdt/leverage are recorded per-row even though
-- currently constant (10/100/10) - audit trail if the fixed values ever
-- change. realized_fees_usdt/realized_funding_usdt are populated at close
-- from BingX's own income/commission data, NULL when not yet known/N/A.
CREATE TABLE IF NOT EXISTS live_executions (
    position_id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    entry_client_order_id TEXT,
    entry_exchange_order_id TEXT,
    entry_quantity TEXT,
    sl_exchange_order_id TEXT,
    tp_exchange_order_id TEXT,
    exit_reason TEXT,
    exchange_fill_entry TEXT,
    exchange_fill_exit TEXT,
    last_error TEXT,
    margin_usdt TEXT,
    notional_usdt TEXT,
    leverage TEXT,
    realized_fees_usdt TEXT,
    realized_funding_usdt TEXT,
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/crypto_trading/storage/test_db.py::test_live_executions_table_exists -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/storage/db.py tests/crypto_trading/storage/test_db.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add live_executions table

Additive-only table, third independent observer alongside positions
and demo_executions. Never written from position_opening.py,
position_closing.py, or demo_execution.py.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HaqP9r9eK2JEkBVwPyRym8
EOF
)"
```

---

## Task 4: Repository methods for live execution

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Test: `tests/crypto_trading/storage/test_repository_live_execution.py`

**Interfaces:**
- Consumes: `Position` (existing), same `create_position_with_event` seeding pattern already used by `test_repository_demo_execution.py`.
- Produces (added to both the `Repository` Protocol and `SQLiteRepository`):
  - `claim_live_execution(position_id: str, claimed_at: datetime, margin_usdt: str, notional_usdt: str, leverage: str) -> bool`
  - `get_live_execution(position_id: str) -> dict | None`
  - `find_positions_pending_live_execution(limit: int) -> list[Position]`
  - `find_active_live_executions() -> list[dict]` (phase in `CLAIMED`/`ENTRY_SUBMITTED`/`ACTIVE`)
  - `find_stale_claimed_live_executions(older_than: datetime) -> list[dict]`
  - `update_live_execution_submitted(position_id: str, entry_client_order_id: str, entry_exchange_order_id: str, entry_quantity: str, exchange_fill_entry: str, sl_exchange_order_id: str | None, tp_exchange_order_id: str | None, updated_at: datetime) -> None`
  - `close_live_execution(position_id: str, exit_reason: str, exchange_fill_exit: str, closed_at: datetime, realized_fees_usdt: str | None = None, realized_funding_usdt: str | None = None) -> None`
  - `mark_live_execution_failed(position_id: str, last_error: str, updated_at: datetime) -> None`
  - `mark_live_execution_skipped(position_id: str, reason: str, updated_at: datetime) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto_trading/storage/test_repository_live_execution.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _open_position(repo: SQLiteRepository, position_id: str = "pos-1") -> Position:
    position = Position(
        position_id=position_id,
        candidate_id=position_id,
        instrument="BTC-USDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"),
        stop_loss=Decimal("49000"),
        target=Decimal("52000"),
        size=Decimal("1000"),
        fill_model_version="v1",
        opened_at=_NOW,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id=position_id,
        occurred_at=_NOW,
        run_id="seed",
        schema_version=1,
        payload={},
    )
    repo.create_position_with_event(position, event)
    return position


def test_claim_live_execution_is_idempotent_and_records_sizing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)

    first = repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    second = repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    assert first is True
    assert second is False
    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLAIMED"
    assert row["margin_usdt"] == "10"
    assert row["notional_usdt"] == "100"
    assert row["leverage"] == "10"


def test_find_positions_pending_live_execution_excludes_claimed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-1")
    _open_position(repo, "pos-2")
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    pending = repo.find_positions_pending_live_execution(limit=10)

    assert [p.position_id for p in pending] == ["pos-2"]


def test_update_live_execution_submitted_then_close(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    repo.update_live_execution_submitted(
        "pos-1",
        entry_client_order_id="cid-1",
        entry_exchange_order_id="ex-1",
        entry_quantity="0.002",
        exchange_fill_entry="50030",
        sl_exchange_order_id=None,
        tp_exchange_order_id=None,
        updated_at=_NOW,
    )
    active = repo.find_active_live_executions()
    assert len(active) == 1
    assert active[0]["phase"] == "ACTIVE"

    repo.close_live_execution(
        "pos-1", "target", "52100", _NOW + timedelta(hours=1),
        realized_fees_usdt="0.08", realized_funding_usdt="-0.01",
    )
    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "target"
    assert row["realized_fees_usdt"] == "0.08"
    assert row["realized_funding_usdt"] == "-0.01"
    assert repo.find_active_live_executions() == []


def test_mark_live_execution_failed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    repo.mark_live_execution_failed("pos-1", "ConnectorUnavailableError: boom", _NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "boom" in row["last_error"]


def test_mark_live_execution_skipped(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    repo.mark_live_execution_skipped("pos-1", "below_exchange_minimum", _NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "SKIPPED"
    assert row["last_error"] == "below_exchange_minimum"
    # SKIPPED is terminal and must never be retried:
    assert repo.find_positions_pending_live_execution(limit=10) == []


def test_find_stale_claimed_live_executions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    not_yet_stale = repo.find_stale_claimed_live_executions(_NOW - timedelta(seconds=1))
    stale = repo.find_stale_claimed_live_executions(_NOW + timedelta(seconds=31))

    assert not_yet_stale == []
    assert len(stale) == 1
    assert stale[0]["position_id"] == "pos-1"


def test_live_execution_never_writes_to_positions_table(tmp_path):
    """Isolation guarantee (spec §3): every repository method touching
    live_executions must leave the positions row exactly as it was."""
    repo = SQLiteRepository(tmp_path / "t.db")
    before = _open_position(repo)

    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50030", None, None, _NOW
    )
    repo.close_live_execution("pos-1", "target", "52100", _NOW)

    after = repo.get_position("pos-1")
    assert after == before
    assert after.status == "OPEN_POSITION"  # untouched by live_execution close
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/storage/test_repository_live_execution.py -v`
Expected: FAIL — methods don't exist yet.

- [ ] **Step 3: Add methods to the `Repository` Protocol**

In `crypto_trading/storage/repository.py`, inside `class Repository(Protocol):`, add:

```python
    def claim_live_execution(
        self, position_id: str, claimed_at: datetime, margin_usdt: str,
        notional_usdt: str, leverage: str,
    ) -> bool: ...
    def get_live_execution(self, position_id: str) -> dict | None: ...
    def find_positions_pending_live_execution(self, limit: int) -> list[Position]: ...
    def find_active_live_executions(self) -> list[dict]: ...
    def find_stale_claimed_live_executions(self, older_than: datetime) -> list[dict]: ...
    def update_live_execution_submitted(
        self,
        position_id: str,
        entry_client_order_id: str,
        entry_exchange_order_id: str,
        entry_quantity: str,
        exchange_fill_entry: str,
        sl_exchange_order_id: str | None,
        tp_exchange_order_id: str | None,
        updated_at: datetime,
    ) -> None: ...
    def close_live_execution(
        self,
        position_id: str,
        exit_reason: str,
        exchange_fill_exit: str,
        closed_at: datetime,
        realized_fees_usdt: str | None = None,
        realized_funding_usdt: str | None = None,
    ) -> None: ...
    def mark_live_execution_failed(
        self, position_id: str, last_error: str, updated_at: datetime
    ) -> None: ...
    def mark_live_execution_skipped(
        self, position_id: str, reason: str, updated_at: datetime
    ) -> None: ...
```

- [ ] **Step 4: Implement on `SQLiteRepository`**

Add to `class SQLiteRepository:` (anywhere after the `demo_execution` methods):

```python
    def claim_live_execution(
        self, position_id: str, claimed_at: datetime, margin_usdt: str,
        notional_usdt: str, leverage: str,
    ) -> bool:
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO live_executions "
                "(position_id, phase, margin_usdt, notional_usdt, leverage, "
                "claimed_at, updated_at) VALUES (?, 'CLAIMED', ?, ?, ?, ?, ?)",
                (
                    position_id, margin_usdt, notional_usdt, leverage,
                    claimed_at.isoformat(), claimed_at.isoformat(),
                ),
            )
            claimed = cur.rowcount > 0
            self._conn.commit()
            return claimed
        except Exception:
            self._conn.rollback()
            raise

    def get_live_execution(self, position_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM live_executions WHERE position_id = ?", (position_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def find_positions_pending_live_execution(self, limit: int) -> list[Position]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN_POSITION' "
            "AND position_id NOT IN (SELECT position_id FROM live_executions) "
            "ORDER BY opened_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def find_active_live_executions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM live_executions "
            "WHERE phase IN ('CLAIMED', 'ENTRY_SUBMITTED', 'ACTIVE')"
        ).fetchall()
        return [dict(row) for row in rows]

    def find_stale_claimed_live_executions(self, older_than: datetime) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM live_executions WHERE phase = 'CLAIMED' AND claimed_at < ?",
            (older_than.isoformat(),),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_live_execution_submitted(
        self,
        position_id: str,
        entry_client_order_id: str,
        entry_exchange_order_id: str,
        entry_quantity: str,
        exchange_fill_entry: str,
        sl_exchange_order_id: str | None,
        tp_exchange_order_id: str | None,
        updated_at: datetime,
    ) -> None:
        self._conn.execute(
            "UPDATE live_executions SET phase = 'ACTIVE', entry_client_order_id = ?, "
            "entry_exchange_order_id = ?, entry_quantity = ?, exchange_fill_entry = ?, "
            "sl_exchange_order_id = ?, tp_exchange_order_id = ?, updated_at = ? "
            "WHERE position_id = ?",
            (
                entry_client_order_id,
                entry_exchange_order_id,
                entry_quantity,
                exchange_fill_entry,
                sl_exchange_order_id,
                tp_exchange_order_id,
                updated_at.isoformat(),
                position_id,
            ),
        )
        self._conn.commit()

    def close_live_execution(
        self,
        position_id: str,
        exit_reason: str,
        exchange_fill_exit: str,
        closed_at: datetime,
        realized_fees_usdt: str | None = None,
        realized_funding_usdt: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE live_executions SET phase = 'CLOSED', exit_reason = ?, "
            "exchange_fill_exit = ?, realized_fees_usdt = ?, realized_funding_usdt = ?, "
            "closed_at = ?, updated_at = ? WHERE position_id = ?",
            (
                exit_reason, exchange_fill_exit, realized_fees_usdt, realized_funding_usdt,
                closed_at.isoformat(), closed_at.isoformat(), position_id,
            ),
        )
        self._conn.commit()

    def mark_live_execution_failed(
        self, position_id: str, last_error: str, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE live_executions SET phase = 'FAILED', last_error = ?, updated_at = ? "
            "WHERE position_id = ?",
            (last_error, updated_at.isoformat(), position_id),
        )
        self._conn.commit()

    def mark_live_execution_skipped(
        self, position_id: str, reason: str, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE live_executions SET phase = 'SKIPPED', last_error = ?, updated_at = ? "
            "WHERE position_id = ?",
            (reason, updated_at.isoformat(), position_id),
        )
        self._conn.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/storage/test_repository_live_execution.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 6: Commit**

```bash
git add crypto_trading/storage/repository.py tests/crypto_trading/storage/test_repository_live_execution.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add repository methods for live execution

Claim-before-place idempotency identical to demo_execution's pattern,
plus a SKIPPED terminal phase (capacity/margin/exchange-minimum safely
declined a trade - never retried) and margin/notional/leverage
recorded at claim time. Isolation test proves these methods never
mutate the positions table.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HaqP9r9eK2JEkBVwPyRym8
EOF
)"
```

---

## Task 5: `BingXLiveTradingConnector`

**Files:**
- Create: `crypto_trading/connectors/bingx_live_trading.py`
- Test: `tests/crypto_trading/connectors/test_bingx_live_trading.py`

**Interfaces:**
- Consumes: `crypto_trading.connectors.exceptions.ConnectorUnavailableError` (existing).
- Produces: `LiveExecutionGuardError` (new exception), `BingXLiveTradingConnector(api_key: str, api_secret: str, timeout_seconds: float = 10.0, max_retries: int = 3)` with methods `set_leverage(symbol, leverage=10, side="LONG") -> dict`, `place_entry_order_with_sl_tp(symbol, quantity, client_order_id, stop_loss_price, target_price) -> dict`, `get_order_by_client_order_id(symbol, client_order_id) -> dict | None`, `get_order_status(symbol, order_id) -> dict | None`, `get_all_positions() -> list[dict]`, `get_position(symbol) -> dict | None`, `get_open_orders(symbol) -> list[dict]`, `cancel_all_open_orders(symbol) -> dict`, `close_position_market(symbol, quantity, client_order_id) -> dict`, `get_balance() -> dict`.

**Note:** this connector's order/position/cancel mechanics are a direct, host-only-swap of the already-shipped, already-live-verified `BingXDemoTradingConnector` (same signing scheme, same body-vs-query-string quirk, same JSON-attached SL/TP with required `quantity`+`price`, same no-`reduceOnly`-on-hedge-mode finding). `get_balance()` is new and its exact real response field names are **not yet live-verified** — the code reads them defensively (`.get(...)` with a `"0"` fallback) and this must be confirmed against the real account before Task 11's final gate (read-only only, per that task).

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto_trading/connectors/test_bingx_live_trading.py
import json
from urllib.parse import parse_qs

import pytest
import respx
from httpx import Response

from crypto_trading.connectors.bingx_live_trading import (
    BingXLiveTradingConnector,
    LiveExecutionGuardError,
)
from crypto_trading.connectors.exceptions import ConnectorUnavailableError

_LIVE_BASE = "https://open-api.bingx.com"


def _connector(**overrides) -> BingXLiveTradingConnector:
    defaults = dict(api_key="k", api_secret="s", timeout_seconds=5, max_retries=2)
    defaults.update(overrides)
    return BingXLiveTradingConnector(**defaults)


@respx.mock
def test_place_entry_order_with_sl_tp_hits_live_host_with_10x_leverage_fields():
    route = respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(
            200,
            json={"code": 0, "msg": "", "data": {"order": {"orderId": "ex-1", "avgPrice": "50030"}}},
        )
    )

    result = _connector().place_entry_order_with_sl_tp(
        symbol="BTC-USDT", quantity="0.002", client_order_id="lv-cid-1",
        stop_loss_price="49000", target_price="52000",
    )

    assert result == {"orderId": "ex-1", "avgPrice": "50030"}
    body = route.calls[0].request.content.decode("utf-8")
    params = parse_qs(body)
    assert params["symbol"] == ["BTC-USDT"]
    assert params["clientOrderID"] == ["lv-cid-1"]
    assert "signature" in params
    stop_loss = json.loads(params["stopLoss"][0])
    assert stop_loss == {
        "type": "STOP_MARKET", "quantity": 0.002, "stopPrice": 49000.0,
        "price": 49000.0, "workingType": "MARK_PRICE",
    }


@respx.mock
def test_set_leverage_defaults_to_10x():
    route = respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/leverage").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": {"leverage": 10}})
    )

    _connector().set_leverage("BTC-USDT")

    body = route.calls[0].request.content.decode("utf-8")
    params = parse_qs(body)
    assert params["leverage"] == ["10"]


@respx.mock
def test_place_entry_order_raises_on_api_error_code():
    respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 80001, "msg": "insufficient balance", "data": {}})
    )

    with pytest.raises(ConnectorUnavailableError, match="insufficient balance"):
        _connector().place_entry_order_with_sl_tp(
            symbol="BTC-USDT", quantity="0.002", client_order_id="lv-cid-1",
            stop_loss_price="49000", target_price="52000",
        )


def test_refuses_to_place_order_against_a_non_live_host():
    connector = _connector()
    connector._base_url = "https://open-api-vst.bingx.com"  # simulate a mutated instance

    with pytest.raises(LiveExecutionGuardError):
        connector.place_entry_order_with_sl_tp(
            symbol="BTC-USDT", quantity="0.002", client_order_id="lv-cid-1",
            stop_loss_price="49000", target_price="52000",
        )


def test_refuses_a_lookalike_host():
    """A subdomain/near-miss host must never pass the guard (exact match
    only, no substring check - same discipline as the Demo connector)."""
    connector = _connector()
    connector._base_url = "https://open-api.bingx.com.evil.example"

    with pytest.raises(LiveExecutionGuardError):
        connector.cancel_all_open_orders("BTC-USDT")


@respx.mock
def test_get_all_positions_filters_out_flat_positions():
    respx.get(f"{_LIVE_BASE}/openApi/swap/v2/user/positions").mock(
        return_value=Response(
            200,
            json={
                "code": 0, "msg": "",
                "data": [
                    {"symbol": "ETH-USDT", "positionSide": "LONG", "positionAmt": "0"},
                    {"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.002"},
                ],
            },
        )
    )

    result = _connector().get_all_positions()

    assert result == [{"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.002"}]


@respx.mock
def test_get_position_filters_by_symbol_from_get_all_positions():
    respx.get(f"{_LIVE_BASE}/openApi/swap/v2/user/positions").mock(
        return_value=Response(
            200,
            json={
                "code": 0, "msg": "",
                "data": [
                    {"symbol": "ETH-USDT", "positionSide": "LONG", "positionAmt": "1.0"},
                    {"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.002"},
                ],
            },
        )
    )

    result = _connector().get_position("BTC-USDT")

    assert result == {"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.002"}


@respx.mock
def test_get_position_returns_none_for_a_symbol_not_in_the_account():
    respx.get(f"{_LIVE_BASE}/openApi/swap/v2/user/positions").mock(
        return_value=Response(
            200,
            json={
                "code": 0, "msg": "",
                "data": [{"symbol": "BTC-USDT", "positionSide": "LONG", "positionAmt": "0.002"}],
            },
        )
    )

    result = _connector().get_position("SOL-USDT")

    assert result is None


@respx.mock
def test_get_balance_returns_the_unwrapped_balance_object():
    respx.get(f"{_LIVE_BASE}/openApi/swap/v2/user/balance").mock(
        return_value=Response(
            200,
            json={
                "code": 0, "msg": "",
                "data": {"balance": {"asset": "USDT", "availableMargin": "123.45"}},
            },
        )
    )

    result = _connector().get_balance()

    assert result == {"asset": "USDT", "availableMargin": "123.45"}


@respx.mock
def test_close_position_market_omits_reduce_only():
    route = respx.post(f"{_LIVE_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": {"order": {"avgPrice": "49500"}}})
    )

    result = _connector().close_position_market("BTC-USDT", "0.002", "lv-close-1")

    assert result == {"avgPrice": "49500"}
    body = route.calls[0].request.content.decode("utf-8")
    params = parse_qs(body)
    assert "reduceOnly" not in params
    assert params["side"] == ["SELL"]
    assert params["positionSide"] == ["LONG"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/connectors/test_bingx_live_trading.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement the connector**

```python
# crypto_trading/connectors/bingx_live_trading.py
from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from crypto_trading.connectors.exceptions import ConnectorUnavailableError

_LIVE_HOST = "open-api.bingx.com"
_ORDER_PATH = "/openApi/swap/v2/trade/order"
_ALL_OPEN_ORDERS_PATH = "/openApi/swap/v2/trade/allOpenOrders"
_LEVERAGE_PATH = "/openApi/swap/v2/trade/leverage"
_POSITIONS_PATH = "/openApi/swap/v2/user/positions"
_OPEN_ORDERS_PATH = "/openApi/swap/v2/trade/openOrders"
_BALANCE_PATH = "/openApi/swap/v2/user/balance"


def _unwrap_order(data: dict | None) -> dict:
    """Same nesting quirk BingXDemoTradingConnector already found live
    (2026-09-04): the order endpoint nests actual fields one level down
    under "order". Centralized here so every caller gets a flat dict."""
    if not data:
        return {}
    return data.get("order", data)


class LiveExecutionGuardError(Exception):
    """Raised whenever this connector would otherwise send a mutating
    request to anything other than the exact real BingX host. Refuses to
    proceed rather than risk placing an order somewhere unintended - see
    docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md."""


class BingXLiveTradingConnector:
    """Order placement/cancel/query against the user's REAL BingX account.
    `_base_url` is a hardcoded class constant, never a constructor parameter
    or settings/env value - there is no code path that can point this
    connector at the Demo/VST host or anywhere else. A separate class from
    BingXDemoTradingConnector on purpose: no shared mutable state, no risk
    that a base-class change silently affects both hosts."""

    _base_url = f"https://{_LIVE_HOST}"

    def __init__(
        self, api_key: str, api_secret: str, timeout_seconds: float = 10.0, max_retries: int = 3
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    def _guard_host(self) -> None:
        parsed = urlparse(self._base_url)
        if parsed.scheme != "https" or parsed.hostname != _LIVE_HOST:
            raise LiveExecutionGuardError(
                f"refuses to trade against host={parsed.hostname!r}, "
                f"only {_LIVE_HOST!r} is permitted"
            )

    def _sign_query(self, params: dict) -> str:
        """Same signing/transport discipline already live-verified for
        BingX Demo (2026-09-04): plain, never percent-encoded key=value
        join, HMAC-SHA256 over that exact string, POST body (not URL query
        string) to avoid a CloudFront-level WAF rejection of JSON-valued
        params in the URL."""
        query = "&".join(f"{key}={value}" for key, value in sorted(params.items()))
        signature = hmac.new(
            self._api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return f"{query}&signature={signature}"

    def _request(self, method: str, path: str, params: dict) -> dict | None:
        self._guard_host()
        full_params = {**params, "timestamp": int(time.time() * 1000)}
        query_string = self._sign_query(full_params)

        @retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=0.5, max=5),
            retry=retry_if_exception_type(httpx.TransportError),
            reraise=True,
        )
        def _do() -> dict | None:
            self._guard_host()  # re-checked immediately before the network call itself
            headers = {"X-BX-APIKEY": self._api_key}
            with httpx.Client(timeout=self._timeout_seconds) as client:
                if method == "POST":
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    response = client.request(
                        method, f"{self._base_url}{path}", content=query_string, headers=headers
                    )
                else:
                    response = client.request(
                        method, f"{self._base_url}{path}?{query_string}", headers=headers
                    )
            try:
                body = response.json()
            except ValueError:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise ConnectorUnavailableError(
                        f"BingX Live HTTP error: {path} ({exc})"
                    ) from exc
                raise ConnectorUnavailableError(
                    f"BingX Live: non-JSON response from {path} (status {response.status_code})"
                )
            if body.get("code") != 0:
                raise ConnectorUnavailableError(
                    f"BingX Live API error {body.get('code')}: {body.get('msg')} ({path})"
                )
            return body.get("data")

        return _do()

    def set_leverage(self, symbol: str, leverage: int = 10, side: str = "LONG") -> dict:
        return self._request(
            "POST", _LEVERAGE_PATH, {"symbol": symbol, "side": side, "leverage": leverage}
        ) or {}

    def place_entry_order_with_sl_tp(
        self,
        symbol: str,
        quantity: str,
        client_order_id: str,
        stop_loss_price: str,
        target_price: str,
    ) -> dict:
        params = {
            "symbol": symbol,
            "side": "BUY",
            "positionSide": "LONG",
            "type": "MARKET",
            "quantity": quantity,
            "clientOrderID": client_order_id,
            "stopLoss": json.dumps(
                {
                    "type": "STOP_MARKET",
                    "quantity": float(quantity),
                    "stopPrice": float(stop_loss_price),
                    "price": float(stop_loss_price),
                    "workingType": "MARK_PRICE",
                },
                separators=(",", ":"),
            ),
            "takeProfit": json.dumps(
                {
                    "type": "TAKE_PROFIT_MARKET",
                    "quantity": float(quantity),
                    "stopPrice": float(target_price),
                    "price": float(target_price),
                    "workingType": "MARK_PRICE",
                },
                separators=(",", ":"),
            ),
        }
        return _unwrap_order(self._request("POST", _ORDER_PATH, params))

    def get_order_by_client_order_id(self, symbol: str, client_order_id: str) -> dict | None:
        try:
            data = self._request(
                "GET", _ORDER_PATH, {"symbol": symbol, "clientOrderID": client_order_id}
            )
        except ConnectorUnavailableError:
            return None
        return _unwrap_order(data) or None

    def get_order_status(self, symbol: str, order_id: str) -> dict | None:
        try:
            data = self._request("GET", _ORDER_PATH, {"symbol": symbol, "orderId": order_id})
        except ConnectorUnavailableError:
            return None
        return _unwrap_order(data) or None

    def get_all_positions(self) -> list[dict]:
        """Read-only. Account-wide open-position list in one call - used by
        the reconciled capacity count (live_execution.py) so the two-layer
        gate never trusts local DB state alone."""
        positions = self._request("GET", _POSITIONS_PATH, {}) or []
        return [p for p in positions if Decimal(str(p.get("positionAmt", "0"))) != 0]

    def get_position(self, symbol: str) -> dict | None:
        for position in self.get_all_positions():
            if position.get("symbol") == symbol:
                return position
        return None

    def get_open_orders(self, symbol: str) -> list[dict]:
        data = self._request("GET", _OPEN_ORDERS_PATH, {"symbol": symbol}) or {}
        if isinstance(data, list):
            return data
        return data.get("orders", [])

    def cancel_all_open_orders(self, symbol: str) -> dict:
        return self._request("DELETE", _ALL_OPEN_ORDERS_PATH, {"symbol": symbol}) or {}

    def close_position_market(self, symbol: str, quantity: str, client_order_id: str) -> dict:
        """LONG-only close: side=SELL against positionSide=LONG. No
        reduceOnly - confirmed live on the Demo account (2026-09-04) that a
        hedge-mode account rejects it outright; positionSide=LONG already
        provides the same safety property (a SELL order pinned to the LONG
        bucket can only reduce/close it, never flip/increase). Not yet
        independently re-verified against the LIVE account (Task 11, read-
        only checks only - this call itself is never made by this plan)."""
        return _unwrap_order(
            self._request(
                "POST",
                _ORDER_PATH,
                {
                    "symbol": symbol,
                    "side": "SELL",
                    "positionSide": "LONG",
                    "type": "MARKET",
                    "quantity": quantity,
                    "clientOrderID": client_order_id,
                },
            )
        )

    def get_balance(self) -> dict:
        """Read-only. Real USDT-margin account balance. Exact field names
        (e.g. `availableMargin`) are NOT YET live-verified against the real
        account - Task 11's read-only verification step confirms them
        before this is ever relied upon for a real balance check. Every
        caller reads fields defensively via `.get(..., "0")`."""
        data = self._request("GET", _BALANCE_PATH, {}) or {}
        return data.get("balance", data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/connectors/test_bingx_live_trading.py -v`
Expected: PASS (all 10 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/connectors/bingx_live_trading.py tests/crypto_trading/connectors/test_bingx_live_trading.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add BingXLiveTradingConnector with exact-host guard

Hardcoded open-api.bingx.com base URL (never a constructor/env
parameter), exact-hostname guard re-checked before every mutating
call, 10x-leverage default, new read-only get_balance()/
get_all_positions(). Zero real network calls in tests (respx-mocked).
get_balance()'s real field names still need live verification (Task
11, read-only only).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HaqP9r9eK2JEkBVwPyRym8
EOF
)"
```

---

## Task 6: `paper_trading/live_execution.py` orchestration

**Files:**
- Create: `crypto_trading/paper_trading/live_execution.py`
- Test: `tests/crypto_trading/paper_trading/test_live_execution.py`

**Interfaces:**
- Consumes: `BingXLiveTradingConnector`/`LiveExecutionGuardError` (Task 5), `Repository` live-execution methods (Task 4), `compute_hold_hours` (existing, `paper_trading/monitoring.py`), `Position` (existing).
- Produces:
  - `reconcile_active_executions(repo, connector, market_data_connector, run_id, now) -> int` — reconciles every locally-active row against the exchange's real position list, closes out any that have gone flat, **returns the resulting reconciled active count**.
  - `has_sufficient_live_capacity(repo, connector, market_data_connector, max_concurrent_positions, required_margin_usdt, run_id, now) -> bool` — the shared gate function both `discovery_loop.py` (Layer 1) and this module's own `process_pending_positions` (Layer 2) call.
  - `process_pending_positions(repo, connector, market_data_connector, quantity_precision_by_symbol, min_notional_by_symbol, settings, run_id, now) -> None`
  - `recover_stale_claims(repo, connector, quantity_precision_by_symbol, min_notional_by_symbol, settings, run_id, now) -> None`
  - `close_guardian_exit_positions(repo, connector, run_id, now) -> None`
  - `close_time_limit_positions(repo, connector, max_position_hold_hours, run_id, now) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto_trading/paper_trading/test_live_execution.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import get_settings
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.paper_trading.live_execution import (
    close_guardian_exit_positions,
    close_time_limit_positions,
    has_sufficient_live_capacity,
    process_pending_positions,
    reconcile_active_executions,
    recover_stale_claims,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _open_position(repo, position_id="pos-1", opened_at=_NOW, entry=Decimal("50000")) -> Position:
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTC-USDT",
        direction="LONG", status="OPEN_POSITION", theoretical_entry=entry,
        simulated_fill_entry=entry, stop_loss=Decimal("49000"), target=Decimal("52000"),
        size=Decimal("1000"), fill_model_version="v1", opened_at=opened_at,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
        aggregate_type="position", aggregate_id=position_id, occurred_at=opened_at,
        run_id="seed", schema_version=1, payload={},
    )
    repo.create_position_with_event(position, event)
    return position


class _SpyConnector:
    def __init__(self, balance="123.45", all_positions=None, order_status="FILLED"):
        self.calls = []
        self._balance = balance
        self._all_positions = all_positions if all_positions is not None else []
        self._order_status = order_status
        self.leverage_calls = []

    def set_leverage(self, symbol, leverage=10, side="LONG"):
        self.leverage_calls.append((symbol, leverage))
        return {}

    def place_entry_order_with_sl_tp(self, **kwargs):
        self.calls.append(kwargs)
        return {"orderId": "ex-1", "avgPrice": kwargs.get("stop_loss_price", "0")}

    def get_order_by_client_order_id(self, symbol, client_order_id):
        return {
            "orderId": "ex-1", "status": self._order_status,
            "executedQty": "0.002", "avgPrice": "50030",
        }

    def get_all_positions(self):
        return self._all_positions

    def get_position(self, symbol):
        for p in self._all_positions:
            if p.get("symbol") == symbol:
                return p
        return None

    def get_balance(self):
        return {"availableMargin": self._balance}

    def cancel_all_open_orders(self, symbol):
        return {}

    def close_position_market(self, symbol, quantity, client_order_id):
        return {"avgPrice": "0"}


class _SpyMarketDataConnector:
    def get_ticker(self, symbol):
        return {"lastPrice": "50000"}


def test_has_sufficient_live_capacity_true_when_room_and_margin(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _SpyConnector(balance="50.00", all_positions=[])

    assert has_sufficient_live_capacity(
        repo, connector, _SpyMarketDataConnector(), max_concurrent_positions=4,
        required_margin_usdt=Decimal("11"), run_id="r1", now=_NOW,
    ) is True


def test_has_sufficient_live_capacity_false_when_margin_short(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _SpyConnector(balance="5.00", all_positions=[])

    assert has_sufficient_live_capacity(
        repo, connector, _SpyMarketDataConnector(), max_concurrent_positions=4,
        required_margin_usdt=Decimal("11"), run_id="r1", now=_NOW,
    ) is False


def test_has_sufficient_live_capacity_false_when_reconciled_count_at_cap(tmp_path):
    """Local DB says only 3 active, but the exchange's real position list
    (via reconcile_active_executions) confirms all 4 are still genuinely
    open - the reconciled count, not the raw local phase, is authoritative."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(4):
        pid = f"pos-{i}"
        _open_position(repo, pid)
        repo.claim_live_execution(pid, _NOW, "10", "100", "10")
        repo.update_live_execution_submitted(
            pid, f"cid-{i}", f"ex-{i}", "0.002", "50000", None, None, _NOW
        )
    connector = _SpyConnector(
        balance="100.00",
        all_positions=[{"symbol": "BTC-USDT", "positionAmt": "0.002"}] * 4,
    )

    assert has_sufficient_live_capacity(
        repo, connector, _SpyMarketDataConnector(), max_concurrent_positions=4,
        required_margin_usdt=Decimal("11"), run_id="r1", now=_NOW,
    ) is False


def test_reconcile_active_executions_closes_out_positions_gone_flat_on_exchange(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-1")
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50000", None, None, _NOW
    )
    connector = _SpyConnector(all_positions=[])  # exchange shows nothing open - closed

    count = reconcile_active_executions(repo, connector, _SpyMarketDataConnector(), "r1", _NOW)

    assert count == 0
    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLOSED"


def test_process_pending_positions_claims_and_submits_when_capacity_available(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    assert connector.leverage_calls == [("BTC-USDT", 10)]
    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"
    assert row["margin_usdt"] == "10"
    assert row["notional_usdt"] == "100"


def test_process_pending_positions_skips_when_capacity_full(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-existing")
    repo.claim_live_execution("pos-existing", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-existing", "cid-0", "ex-0", "0.002", "50000", None, None, _NOW
    )
    _open_position(repo, "pos-new")
    connector = _SpyConnector(
        balance="100.00",
        all_positions=[{"symbol": "BTC-USDT", "positionAmt": "0.002"}],
    )
    settings = get_settings()
    # force max_concurrent_positions=1 for this test via a settings copy
    settings = settings.model_copy(
        update={"live_execution": settings.live_execution.model_copy(
            update={"max_concurrent_positions": 1}
        )}
    )

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    assert repo.get_live_execution("pos-new") is None  # never claimed
    assert connector.calls == []  # never even attempted an order


def test_process_pending_positions_skips_safely_below_exchange_minimum(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(balance="100.00", all_positions=[])
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("1000")},  # exchange minimum notional far above 100 USDT
        settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "SKIPPED"
    assert row["last_error"] == "below_exchange_minimum"
    assert connector.calls == []


def test_process_pending_positions_marks_failed_on_unfilled_order(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(balance="100.00", all_positions=[], order_status="CANCELED")
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "not filled" in row["last_error"].lower() or "CANCELED" in row["last_error"]


def test_process_pending_positions_marks_failed_on_connector_error(tmp_path):
    class _RaisingConnector(_SpyConnector):
        def place_entry_order_with_sl_tp(self, **kwargs):
            raise ConnectorUnavailableError("boom")

    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _RaisingConnector(balance="100.00", all_positions=[])
    settings = get_settings()

    process_pending_positions(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, settings, "r1", _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "boom" in row["last_error"]


def test_close_guardian_exit_positions_mirrors_only_after_paper_already_closed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50000", None, None, _NOW
    )
    connector = _SpyConnector()

    close_guardian_exit_positions(repo, connector, "r1", _NOW)
    assert repo.get_live_execution("pos-1")["phase"] == "ACTIVE"  # untouched, PAPER not closed yet

    repo.close_position_with_event(
        position_id="pos-1", theoretical_exit=Decimal("49500"),
        simulated_fill_exit=Decimal("49500"), exit_reason="guardian_exit",
        fees=Decimal("0"), funding=Decimal("0"), closed_at=_NOW,
        event=Event(event_id="POSITION_CLOSED:pos-1", event_type="POSITION_CLOSED",
                    aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
                    run_id="seed", schema_version=1, payload={}),
    )
    close_guardian_exit_positions(repo, connector, "r1", _NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "GUARDIAN_EXIT"


def test_close_time_limit_positions_uses_the_passed_in_hold_hours(tmp_path):
    """LIVE's own 6h limit, independent of whatever PAPER's is - proven by
    passing a value (2h) that would never trigger under PAPER's 24h."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, opened_at=_NOW - timedelta(hours=3))
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50000", None, None, _NOW
    )
    connector = _SpyConnector()

    close_time_limit_positions(repo, connector, max_position_hold_hours=2, run_id="r1", now=_NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "TIME_LIMIT"


def test_recover_stale_claims_looks_up_before_resubmitting(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW - timedelta(seconds=60), "10", "100", "10")
    connector = _SpyConnector(balance="100.00")
    settings = get_settings()

    recover_stale_claims(
        repo, connector, {"BTC-USDT": 3}, {"BTC-USDT": Decimal("0")}, settings,
        "r1", _NOW, stale_after_seconds=30,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/paper_trading/test_live_execution.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `paper_trading/live_execution.py`**

```python
# crypto_trading/paper_trading/live_execution.py
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from crypto_trading.config.loader import Settings
from crypto_trading.connectors.bingx_live_trading import (
    BingXLiveTradingConnector,
    LiveExecutionGuardError,
)
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.monitoring import compute_hold_hours
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_GUARDED_ERRORS = (ConnectorUnavailableError, LiveExecutionGuardError)


def _client_order_id(position_id: str, suffix: str) -> str:
    return f"lv{position_id[:24]}{suffix}"[:32]


def _quantity_for_live(entry_price: Decimal, margin_usdt: Decimal, leverage: int, precision: int) -> Decimal:
    """Fixed sizing, independent of PAPER's dynamic position.size (spec §4/§8):
    quantity = (margin * leverage) / entry_price, rounded down."""
    notional = margin_usdt * leverage
    raw_quantity = notional / entry_price
    quantum = Decimal(1).scaleb(-precision)
    return raw_quantity.quantize(quantum, rounding=ROUND_DOWN)


def reconcile_active_executions(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    market_data_connector: object,
    run_id: str,
    now: datetime,
) -> int:
    """Closes out any locally-ACTIVE row the exchange has already gone flat
    on (same "exchange going flat is the proof" principle as Demo's own
    reconcile_active_executions), then returns the resulting reconciled
    active count - the ONLY authoritative source for capacity checks
    (user-mandated: never trust local DB phase alone, spec §7)."""
    active_count = 0
    for row in repo.find_active_live_executions():
        if row["phase"] != "ACTIVE":
            active_count += 1  # CLAIMED/ENTRY_SUBMITTED: in-flight, still reserves a slot
            continue
        position = repo.get_position(row["position_id"])
        if position is None:
            continue
        if connector.get_position(position.instrument) is not None:
            active_count += 1  # still genuinely open on the exchange
            continue
        exit_price = Decimal(str(market_data_connector.get_ticker(position.instrument)["lastPrice"]))
        distance_to_stop = abs(exit_price - position.stop_loss)
        distance_to_target = abs(exit_price - position.target)
        exit_reason = "stop_loss" if distance_to_stop <= distance_to_target else "target"
        repo.close_live_execution(position.position_id, exit_reason, str(exit_price), now)
        log_event(
            run_id, event="live_position_closed", position_id=position.position_id,
            exit_reason=exit_reason,
        )
    return active_count


def has_sufficient_live_capacity(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    market_data_connector: object,
    max_concurrent_positions: int,
    required_margin_usdt: Decimal,
    run_id: str,
    now: datetime,
) -> bool:
    """The single shared gate both discovery_loop.py (coarse, pre-AI-cost)
    and this module's own process_pending_positions (authoritative,
    immediately pre-order) call - two call sites, one implementation, per
    spec §7. Reconciliation runs first so the count is never based on
    stale local state alone."""
    try:
        active_count = reconcile_active_executions(repo, connector, market_data_connector, run_id, now)
        if active_count >= max_concurrent_positions:
            log_event(run_id, event="live_capacity_full", active_count=active_count)
            return False
        balance = connector.get_balance()
        available_margin = Decimal(str(balance.get("availableMargin", "0")))
        if available_margin < required_margin_usdt:
            log_event(
                run_id, event="live_margin_insufficient",
                available_margin=str(available_margin), required=str(required_margin_usdt),
            )
            return False
        return True
    except _GUARDED_ERRORS as exc:
        log_event(
            run_id, event="live_capacity_check_failed",
            error_type=type(exc).__name__, error=str(exc),
        )
        return False  # fail-closed: never open a position when capacity/balance is unknown


def _confirm_fill(
    connector: BingXLiveTradingConnector, symbol: str, client_order_id: str, requested_quantity: str
) -> tuple[bool, str, str]:
    """Fail-safe fill confirmation (spec §8, user-mandated): local state must
    never show ACTIVE for a position that isn't genuinely, fully filled on
    the exchange. Returns (filled, executed_qty, avg_price)."""
    order = connector.get_order_by_client_order_id(symbol, client_order_id)
    if order is None:
        return False, "0", "0"
    status = order.get("status", "")
    executed_qty = str(order.get("executedQty", "0"))
    if status != "FILLED":
        return False, executed_qty, str(order.get("avgPrice", "0"))
    return True, executed_qty, str(order.get("avgPrice", "0"))


def _submit_entry_order(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    position: Position,
    quantity: Decimal,
    margin_usdt: Decimal,
    notional_usdt: Decimal,
    leverage: int,
    run_id: str,
    now: datetime,
) -> None:
    client_order_id = _client_order_id(position.position_id, "e")
    try:
        connector.set_leverage(position.instrument, leverage=leverage)
        connector.place_entry_order_with_sl_tp(
            symbol=position.instrument,
            quantity=str(quantity),
            client_order_id=client_order_id,
            stop_loss_price=str(position.stop_loss),
            target_price=str(position.target),
        )
        filled, executed_qty, avg_price = _confirm_fill(
            connector, position.instrument, client_order_id, str(quantity)
        )
        if not filled:
            repo.mark_live_execution_failed(
                position.position_id, f"entry order not filled (status check): {executed_qty}", now
            )
            log_event(
                run_id, event="live_order_not_filled", position_id=position.position_id,
                instrument=position.instrument,
            )
            return
        repo.update_live_execution_submitted(
            position.position_id,
            entry_client_order_id=client_order_id,
            entry_exchange_order_id=client_order_id,
            entry_quantity=executed_qty,  # exchange-confirmed, never the requested quantity
            exchange_fill_entry=avg_price,
            sl_exchange_order_id=None,
            tp_exchange_order_id=None,
            updated_at=now,
        )
        log_event(
            run_id, event="live_order_submitted", position_id=position.position_id,
            instrument=position.instrument, margin_usdt=str(margin_usdt),
            notional_usdt=str(notional_usdt), leverage=str(leverage),
        )
    except _GUARDED_ERRORS as exc:
        repo.mark_live_execution_failed(position.position_id, f"{type(exc).__name__}: {exc}", now)
        log_event(
            run_id, event="live_order_failed", position_id=position.position_id,
            error_type=type(exc).__name__, error=str(exc),
        )


def process_pending_positions(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    market_data_connector: object,
    quantity_precision_by_symbol: dict[str, int],
    min_notional_by_symbol: dict[str, Decimal],
    settings: Settings,
    run_id: str,
    now: datetime,
    limit: int = 10,
) -> None:
    """Layer 2, authoritative gate (spec §7): re-checks capacity/margin
    immediately before EACH claim, not once for the whole batch - this is
    what closes the race Gate can create by confirming multiple candidates
    in one discovery cycle. The moment capacity/margin is exhausted, this
    stops entirely for the rest of the tick; unclaimed positions are
    retried next tick, picked up automatically once a slot frees."""
    cfg = settings.live_execution
    for position in repo.find_positions_pending_live_execution(limit):
        if not has_sufficient_live_capacity(
            repo, connector, market_data_connector, cfg.max_concurrent_positions,
            cfg.margin_per_trade_usdt + cfg.margin_safety_buffer_usdt, run_id, now,
        ):
            break  # stop trying more this tick; PAPER's leg is unaffected
        precision = quantity_precision_by_symbol.get(position.instrument, 0)
        quantity = _quantity_for_live(
            position.simulated_fill_entry, cfg.margin_per_trade_usdt, cfg.leverage, precision
        )
        min_notional = min_notional_by_symbol.get(position.instrument, Decimal("0"))
        notional = quantity * position.simulated_fill_entry
        if quantity <= 0 or notional < min_notional:
            if not repo.claim_live_execution(
                position.position_id, now, str(cfg.margin_per_trade_usdt),
                str(cfg.margin_per_trade_usdt * cfg.leverage), str(cfg.leverage),
            ):
                continue
            repo.mark_live_execution_skipped(position.position_id, "below_exchange_minimum", now)
            log_event(
                run_id, event="live_skipped_below_minimum", position_id=position.position_id,
                instrument=position.instrument,
            )
            continue
        if not repo.claim_live_execution(
            position.position_id, now, str(cfg.margin_per_trade_usdt),
            str(cfg.margin_per_trade_usdt * cfg.leverage), str(cfg.leverage),
        ):
            continue  # another run/duplicate observation already claimed it
        _submit_entry_order(
            repo, connector, position, quantity, cfg.margin_per_trade_usdt,
            cfg.margin_per_trade_usdt * cfg.leverage, cfg.leverage, run_id, now,
        )


def recover_stale_claims(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    quantity_precision_by_symbol: dict[str, int],
    min_notional_by_symbol: dict[str, Decimal],
    settings: Settings,
    run_id: str,
    now: datetime,
    stale_after_seconds: int,
) -> None:
    """Crash recovery: a row stuck in CLAIMED past the grace window means
    the process died between claiming and confirming submission. Looks the
    order up by its deterministic clientOrderID BEFORE ever resubmitting -
    never a blind retry, same discipline as demo_execution.py."""
    cfg = settings.live_execution
    stale_before = now - timedelta(seconds=stale_after_seconds)
    for row in repo.find_stale_claimed_live_executions(stale_before):
        position = repo.get_position(row["position_id"])
        if position is None:
            continue
        client_order_id = _client_order_id(position.position_id, "e")
        existing = connector.get_order_by_client_order_id(position.instrument, client_order_id)
        if existing is not None and existing.get("status") == "FILLED":
            repo.update_live_execution_submitted(
                position.position_id,
                entry_client_order_id=client_order_id,
                entry_exchange_order_id=client_order_id,
                entry_quantity=str(existing.get("executedQty", "")),
                exchange_fill_entry=str(existing.get("avgPrice", "")),
                sl_exchange_order_id=None,
                tp_exchange_order_id=None,
                updated_at=now,
            )
            continue
        precision = quantity_precision_by_symbol.get(position.instrument, 0)
        quantity = _quantity_for_live(
            position.simulated_fill_entry, cfg.margin_per_trade_usdt, cfg.leverage, precision
        )
        _submit_entry_order(
            repo, connector, position, quantity, cfg.margin_per_trade_usdt,
            cfg.margin_per_trade_usdt * cfg.leverage, cfg.leverage, run_id, now,
        )


def close_guardian_exit_positions(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    run_id: str,
    now: datetime,
) -> None:
    """LIVE's equivalent of demo_execution.py's close_guardian_exit_positions:
    never re-runs Guardian's classification (zero extra AI cost, zero
    divergence risk), only mirrors a PAPER position ALREADY closed with
    exit_reason='guardian_exit'. Guardian can close a LIVE position earlier
    than the 6h limit; it structurally cannot extend past it, because
    close_time_limit_positions() runs before process_pending_positions()
    ever considers a NEW claim, and this function only ever fires on a
    position that already exists as ACTIVE - PAPER's own 6h-independent
    guardian_exit decision is the only trigger, never a live re-evaluation."""
    for row in repo.find_active_live_executions():
        if row["phase"] != "ACTIVE":
            continue
        position = repo.get_position(row["position_id"])
        if position is None or position.status != "CLOSED" or position.exit_reason != "guardian_exit":
            continue
        try:
            connector.cancel_all_open_orders(position.instrument)
            client_order_id = _client_order_id(position.position_id, "g")
            result = connector.close_position_market(
                position.instrument, quantity=row.get("entry_quantity") or "0",
                client_order_id=client_order_id,
            )
            repo.close_live_execution(
                position.position_id, "GUARDIAN_EXIT", str(result.get("avgPrice", "")), now
            )
            log_event(run_id, event="live_guardian_exit_closed", position_id=position.position_id)
        except _GUARDED_ERRORS as exc:
            log_event(
                run_id, event="live_guardian_exit_close_failed", position_id=position.position_id,
                error_type=type(exc).__name__, error=str(exc),
            )


def close_time_limit_positions(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    max_position_hold_hours: int,
    run_id: str,
    now: datetime,
) -> None:
    """LIVE's own hard time limit (default 6h, live_execution.yaml),
    completely independent of PAPER's 24h - the caller passes
    settings.live_execution.max_position_hold_hours, never
    settings.risk_limits.max_position_hold_hours. Reuses compute_hold_hours
    so PAPER/Demo/Live never disagree on elapsed time for the SAME
    position, only on the threshold each applies to it."""
    for row in repo.find_active_live_executions():
        if row["phase"] != "ACTIVE":
            continue
        position = repo.get_position(row["position_id"])
        if position is None or position.status != "OPEN_POSITION":
            continue
        if compute_hold_hours(position, now) < max_position_hold_hours:
            continue
        try:
            connector.cancel_all_open_orders(position.instrument)
            client_order_id = _client_order_id(position.position_id, "x")
            result = connector.close_position_market(
                position.instrument, quantity=row.get("entry_quantity") or "0",
                client_order_id=client_order_id,
            )
            repo.close_live_execution(
                position.position_id, "TIME_LIMIT", str(result.get("avgPrice", "")), now
            )
            log_event(run_id, event="live_time_limit_closed", position_id=position.position_id)
        except _GUARDED_ERRORS as exc:
            log_event(
                run_id, event="live_time_limit_close_failed", position_id=position.position_id,
                error_type=type(exc).__name__, error=str(exc),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/paper_trading/test_live_execution.py -v`
Expected: PASS (all 13 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/paper_trading/live_execution.py tests/crypto_trading/paper_trading/test_live_execution.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add live_execution.py orchestration

Two-layer capacity/margin gate (has_sufficient_live_capacity, shared
by discovery's coarse check and this module's own authoritative
pre-claim check), reconciliation-first tick ordering, fixed 10 USDT/
10x sizing independent of PAPER, fill-confirmation before ACTIVE,
below-exchange-minimum safe skip, and LIVE's own 6h time limit reusing
compute_hold_hours. Zero writes to positions/demo_executions.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HaqP9r9eK2JEkBVwPyRym8
EOF
)"
```

---

## Task 7: `live_execution_loop.py`

**Files:**
- Create: `crypto_trading/live_execution_loop.py`
- Test: `tests/crypto_trading/test_live_execution_loop.py`

**Interfaces:**
- Consumes: everything from Task 6, `Settings` (existing).
- Produces: `run_live_execution_tick(repo, connector, market_data_connector, quantity_precision_by_symbol, min_notional_by_symbol, settings, now) -> None`, `run_forever(repo, connector, market_data_connector, quantity_precision_by_symbol, min_notional_by_symbol, settings) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/test_live_execution_loop.py
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.config.loader import get_settings
from crypto_trading.live_execution_loop import run_live_execution_tick
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class _SpyConnector:
    def set_leverage(self, symbol, leverage=10, side="LONG"):
        return {}

    def place_entry_order_with_sl_tp(self, **kwargs):
        return {"orderId": "ex-1", "avgPrice": "50010"}

    def get_order_by_client_order_id(self, symbol, client_order_id):
        return {"orderId": "ex-1", "status": "FILLED", "executedQty": "0.002", "avgPrice": "50010"}

    def get_all_positions(self):
        return []

    def get_position(self, symbol):
        return None

    def get_balance(self):
        return {"availableMargin": "100.00"}

    def cancel_all_open_orders(self, symbol):
        return {}

    def close_position_market(self, symbol, quantity, client_order_id):
        return {"avgPrice": "0"}


class _SpyMarketDataConnector:
    def get_ticker(self, symbol):
        return {"lastPrice": "50000"}


def _seed_open_position(repo, position_id="pos-1"):
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTC-USDT",
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50000"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1", opened_at=_NOW,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
        aggregate_type="position", aggregate_id=position_id, occurred_at=_NOW,
        run_id="seed", schema_version=1, payload={},
    )
    repo.create_position_with_event(position, event)


def test_run_live_execution_tick_processes_pending_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)
    connector = _SpyConnector()

    run_live_execution_tick(
        repo, connector, _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, get_settings(), _NOW,
    )

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ACTIVE"


def test_run_live_execution_tick_never_crashes_the_caller_on_unexpected_error(tmp_path):
    class _ExplodingConnector(_SpyConnector):
        def get_balance(self):
            # get_balance() is on the real call path (has_sufficient_live_capacity,
            # called from process_pending_positions) - unlike get_all_positions,
            # which nothing in live_execution.py calls directly.
            raise RuntimeError("simulated crash")

    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)

    # must not raise
    run_live_execution_tick(
        repo, _ExplodingConnector(), _SpyMarketDataConnector(), {"BTC-USDT": 3},
        {"BTC-USDT": Decimal("0")}, get_settings(), _NOW,
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/crypto_trading/test_live_execution_loop.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `live_execution_loop.py`**

```python
# crypto_trading/live_execution_loop.py
from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.config.loader import Settings
from crypto_trading.connectors.bingx_live_trading import BingXLiveTradingConnector
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.paper_trading.live_execution import (
    close_guardian_exit_positions,
    close_time_limit_positions,
    process_pending_positions,
    reconcile_active_executions,
    recover_stale_claims,
)
from crypto_trading.storage.repository import Repository


def run_live_execution_tick(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    market_data_connector: object,
    quantity_precision_by_symbol: dict[str, int],
    min_notional_by_symbol: dict[str, Decimal],
    settings: Settings,
    now: datetime,
) -> None:
    """One live-execution tick. Reconciliation-first ordering (spec §7,
    the reverse of demo_execution_loop's order): recover_stale_claims ->
    reconcile_active_executions -> close_guardian_exit_positions ->
    close_time_limit_positions -> process_pending_positions LAST, so any
    new claim's capacity check already reflects this tick's own fresh
    reconciliation. Same outer fail-safe principle as every other loop in
    this codebase: an unexpected exception never crashes run_forever()."""
    run_id = new_run_id()
    repo.start_run(run_id, "live_execution", now)
    try:
        recover_stale_claims(
            repo, connector, quantity_precision_by_symbol, min_notional_by_symbol, settings,
            run_id, now, stale_after_seconds=settings.live_execution.claim_stale_after_seconds,
        )
        reconcile_active_executions(repo, connector, market_data_connector, run_id, now)
        close_guardian_exit_positions(repo, connector, run_id, now)
        close_time_limit_positions(
            repo, connector, settings.live_execution.max_position_hold_hours, run_id, now
        )
        process_pending_positions(
            repo, connector, market_data_connector, quantity_precision_by_symbol,
            min_notional_by_symbol, settings, run_id, now,
        )
        repo.complete_run(run_id, datetime.now(UTC), "ok", [])
    except Exception as exc:
        log_event(
            run_id, event="live_execution_tick_failed",
            error_type=type(exc).__name__, error=str(exc),
        )
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])


def run_forever(
    repo: Repository,
    connector: BingXLiveTradingConnector,
    market_data_connector: object,
    quantity_precision_by_symbol: dict[str, int],
    min_notional_by_symbol: dict[str, Decimal],
    settings: Settings,
) -> None:
    while True:
        run_live_execution_tick(
            repo, connector, market_data_connector, quantity_precision_by_symbol,
            min_notional_by_symbol, settings, datetime.now(UTC),
        )
        time.sleep(settings.live_execution.check_interval_seconds)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/test_live_execution_loop.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/live_execution_loop.py tests/crypto_trading/test_live_execution_loop.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add live_execution_loop with reconcile-first tick order

Reconciliation and all closing steps run before process_pending_positions,
so a new claim's capacity check reflects this tick's own fresh
reconciled state - deliberately the reverse of demo_execution_loop's
order.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HaqP9r9eK2JEkBVwPyRym8
EOF
)"
```

---

## Task 8: Discovery-loop capacity gate (Layer 1)

**Files:**
- Modify: `crypto_trading/discovery_loop.py`
- Test: `tests/crypto_trading/test_discovery_loop.py`

**Interfaces:**
- Consumes: `has_sufficient_live_capacity` (Task 6), `BingXLiveTradingConnector` (Task 5).
- Produces: `run_discovery_tick(..., live_connector=None, live_market_data_connector=None)` and `run_forever(..., live_connector=None, live_market_data_connector=None)` — both existing functions gain two new, optional, default-`None` keyword parameters. When `live_connector is None` (the default, and the state throughout this whole plan since `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED` is never set), behavior is byte-for-byte unchanged from today.

- [ ] **Step 1: Write the failing tests**

Add to `tests/crypto_trading/test_discovery_loop.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_LIVE_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _seed_active_live_position(repo, position_id: str) -> None:
    """Seeds a real OPEN_POSITION + a matching ACTIVE live_executions row -
    the reconciled-capacity check (has_sufficient_live_capacity) counts
    THESE rows, never a mocked connector method alone, so a test proving
    the capacity gate must actually populate them."""
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTC-USDT",
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50000"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1",
        opened_at=_LIVE_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
              aggregate_type="position", aggregate_id=position_id, occurred_at=_LIVE_NOW,
              run_id="seed", schema_version=1, payload={}),
    )
    repo.claim_live_execution(position_id, _LIVE_NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        position_id, f"cid-{position_id}", f"ex-{position_id}", "0.002", "50000",
        None, None, _LIVE_NOW,
    )


class _LiveConnectorStub:
    """Confirms every seeded ACTIVE row is still genuinely open on the
    exchange (reconciliation finds nothing stale to close), and reports
    ample balance - so the ONLY thing that can make capacity read "full"
    is the number of seeded rows the test itself set up, not the mock."""

    def get_position(self, symbol):
        return {"symbol": symbol, "positionAmt": "0.002"}

    def get_balance(self):
        return {"availableMargin": "100.00"}


def test_run_discovery_tick_skips_entirely_when_live_capacity_full(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(4):  # settings' default live_execution.max_concurrent_positions == 4
        _seed_active_live_position(repo, f"live-pos-{i}")
    settings = _settings()
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    connector = _stub_connector_with_one_healthy_symbol()

    positions = run_discovery_tick(
        connector, repo, runner, settings,
        live_connector=_LiveConnectorStub(), live_market_data_connector=connector,
    )

    assert positions == []
    assert repo.find_candidates_by_status("CANDIDATE") == []  # never even discovered
    run_rows = repo._conn.execute("SELECT status FROM runs ORDER BY started_at DESC LIMIT 1").fetchall()
    assert run_rows[0]["status"] == "ok"  # a clean, logged no-op, not an error


def test_run_discovery_tick_proceeds_normally_when_live_disabled(tmp_path):
    """live_connector=None (the default) - today's exact behavior,
    unaffected by anything in this plan. Seeds the same 4 active LIVE rows
    as the "full" test above to prove it's live_connector=None, not an
    empty DB, that short-circuits the gate."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(4):
        _seed_active_live_position(repo, f"live-pos-{i}")
    settings = _settings()
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    connector = _stub_connector_with_one_healthy_symbol()

    positions = run_discovery_tick(connector, repo, runner, settings)

    assert isinstance(positions, list)  # completes normally, gate never even runs


def test_run_discovery_tick_proceeds_when_live_capacity_available(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):  # one slot free under the default cap of 4
        _seed_active_live_position(repo, f"live-pos-{i}")
    settings = _settings()
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    connector = _stub_connector_with_one_healthy_symbol()

    positions = run_discovery_tick(
        connector, repo, runner, settings,
        live_connector=_LiveConnectorStub(), live_market_data_connector=connector,
    )

    assert isinstance(positions, list)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/test_discovery_loop.py -v`
Expected: FAIL — `run_discovery_tick` doesn't accept `live_connector`/`live_market_data_connector` yet.

- [ ] **Step 3: Add the gate to `discovery_loop.py`**

In `crypto_trading/discovery_loop.py`, add the import:

```python
from crypto_trading.paper_trading.live_execution import has_sufficient_live_capacity
```

Change the `run_discovery_tick` signature and add the pre-tick check as its first statement inside the function body (before `run_id = new_run_id()`... actually the run_id/start_run/log-as-ok pattern needs to stay, so the check goes right after `repo.start_run` and short-circuits before `build_live_snapshot`):

```python
def run_discovery_tick(
    connector: LiveMarketDataSource,
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    news_connector: object | None = None,
    external_data_connector: object | None = None,
    screener_runner: AgentRunner | None = None,
    live_connector: object | None = None,
    live_market_data_connector: object | None = None,
) -> list[Position]:
    """En periodisk discovery-tick (SPEC §7, PLAN_CRYPTO_PHASE5.md Task 7):
    bygger en live `MarketSnapshot` (Task 6) och kör den genom exakt samma
    `run_single_cycle()`-pipeline som `replay.py` (Task 5/Beslut 1) - ingen
    duplicerad pipeline-logik, ingen skillnad mellan replay och live utöver
    varifrån snapshoten kommer. Det dagliga AI-anropstaket och
    `ANALYSIS_INTERRUPTED`-återupptagningen (Task 4) körs oförändrat inuti
    `run_single_cycle -> run_discovery_cycle`, aldrig kringgått här.

    Fail-safe på loop-nivå (Global Constraints, SPEC §8.3): ett oväntat
    undantag - connector nere, ett programmeringsfel mitt i en candidates
    analys, vad som helst - kraschar aldrig anroparen (`run_forever`).
    Det fångas, loggas och skrivs till `runs.errors`; en candidate som redan
    hann bli `UNDER_AI_ANALYSIS` innan kraschen läks av nästa ticks
    `sweep_interrupted_analyses` + återupptagningspolicy (Task 4) - ingen ny
    recovery-mekanism behövs här, den är redan komponerad av de tidigare
    tasken.

    `clock=lambda: datetime.now(UTC)` (bugfix 2026-08-31, bekräftad mot en
    riktig live-körning): `build_live_snapshot()`s staleness-kontroll för
    varje hämtad post bedöms mot en färsk tidpunkt tagen direkt efter just
    den postens nätverksanrop, inte mot detta `now` (fånget här, före hela
    den sekventiella hämtningsloopen). Utan detta blev varje instrument som
    hämtades mer än några sekunder in i en flera-minuter-lång live-hämtning
    felaktigt `data_quality_invalid` - se market_snapshot.py::
    build_live_snapshot() för full förklaring.

    Layer 1 capacity/cost gate (2026-09-06, spec:
    docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md §7):
    when live_connector is not None (only true once LIVE is armed in
    run.py), a reconciled live-capacity/margin check runs BEFORE the
    snapshot/candidate pipeline. If live is full or under-margined, this
    tick is skipped entirely - no snapshot fetch, no candidate search, no
    AI calls - logged as a clean 'ok' run, not an error. live_connector is
    None (the default) everywhere in this codebase today, so this is a
    zero-behavior-change no-op until that thread exists and is armed."""
    run_id = new_run_id()
    now = datetime.now(UTC)
    repo.start_run(run_id, "discovery", now)
    if live_connector is not None:
        required_margin = (
            settings.live_execution.margin_per_trade_usdt
            + settings.live_execution.margin_safety_buffer_usdt
        )
        if not has_sufficient_live_capacity(
            repo, live_connector, live_market_data_connector or connector,
            settings.live_execution.max_concurrent_positions, required_margin, run_id, now,
        ):
            log_event(run_id, event="discovery_suppressed_live_capacity")
            repo.complete_run(run_id, datetime.now(UTC), "ok", [], instruments_scanned=0)
            return []
    try:
        snapshot = build_live_snapshot(
            connector, settings, now, clock=lambda: datetime.now(UTC), run_id=run_id
        )
        positions = run_single_cycle(
            snapshot,
            repo,
            runner,
            settings,
            run_id,
            news_connector=news_connector,
            external_data_connector=external_data_connector,
            screener_runner=screener_runner,
        )
        repo.complete_run(
            run_id, datetime.now(UTC), "ok", [], instruments_scanned=len(snapshot.instruments)
        )
        return positions
    except Exception as exc:
        log_event(
            run_id, event="discovery_tick_failed", error_type=type(exc).__name__, error=str(exc)
        )
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])
        return []
```

Update `run_forever` to accept and forward the same two new parameters:

```python
def run_forever(
    connector: LiveMarketDataSource,
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    news_connector: object | None = None,
    external_data_connector: object | None = None,
    screener_runner: AgentRunner | None = None,
    live_connector: object | None = None,
    live_market_data_connector: object | None = None,
) -> None:
    while True:
        run_discovery_tick(
            connector,
            repo,
            runner,
            settings,
            news_connector=news_connector,
            external_data_connector=external_data_connector,
            screener_runner=screener_runner,
            live_connector=live_connector,
            live_market_data_connector=live_market_data_connector,
        )
        time.sleep(settings.pipeline.discovery_interval_minutes * 60)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/test_discovery_loop.py -v`
Expected: PASS (all tests, including every pre-existing test in that file — confirms `live_connector=None` truly changes nothing)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/discovery_loop.py tests/crypto_trading/test_discovery_loop.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): gate discovery on reconciled live capacity/margin

Layer 1 of the two-layer capacity gate (spec §7): when a live_connector
is passed and live capacity/margin is exhausted, the entire discovery
tick is skipped before any snapshot fetch or AI call - saves AI budget
for candidates that would just be declined by live's own authoritative
gate anyway. live_connector=None (unset in this whole plan) leaves
today's behavior byte-for-byte unchanged, proven by the full existing
test_discovery_loop.py suite still passing.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HaqP9r9eK2JEkBVwPyRym8
EOF
)"
```

---

## Task 9: Wire into `run.py` behind the arm flag

**Files:**
- Modify: `crypto_trading/run.py`

**Interfaces:**
- Consumes: `is_live_execution_enabled()` (Task 2), `BingXLiveTradingConnector` (Task 5), `live_execution_loop.run_forever` (Task 7), `BingXMarketDataConnector.get_contracts()` (existing).
- Produces: an optional 9th daemon thread; discovery's thread also gains the live connector so Layer 1 (Task 8) is active whenever live is armed.

- [ ] **Step 1: Add the credentials builder**

In `crypto_trading/run.py`, add near `build_demo_trading_connector_from_env()`:

```python
def build_live_trading_connector_from_env() -> BingXLiveTradingConnector | None:
    """Opt-in, same pattern as build_demo_trading_connector_from_env(): if
    the dedicated LIVE credentials aren't set, the thread simply doesn't
    start. Deliberately reads ONLY BINGX_API_KEY/BINGX_API_SECRET (confirmed
    by the user to be the real, dedicated LIVE-account keys, sitting unused
    in .env specifically reserved for this - never
    CRYPTO_TRADING_BINGX_DEMO_API_KEY/_SECRET, which is Demo's own,
    separate credential pair)."""
    api_key = os.environ.get("BINGX_API_KEY")
    api_secret = os.environ.get("BINGX_API_SECRET")
    if not api_key or not api_secret:
        return None
    return BingXLiveTradingConnector(api_key=api_key, api_secret=api_secret)
```

- [ ] **Step 2: Add the thread-runner function and extend discovery's**

```python
def _run_live_execution_forever(
    market_data_connector: BingXMarketDataConnector,
    live_connector: BingXLiveTradingConnector,
    settings: Settings,
) -> None:
    """Same thread-bound-connection fix as the other _run_*_forever()
    functions. min_notional_by_symbol is built ONCE here from the existing,
    read-only get_contracts() - exact field name confirmed against the real
    account in Task 11 (read-only), defaults to '0' (no floor) if absent so
    this never crashes on an unexpected contract shape."""
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    contracts = market_data_connector.get_contracts()
    quantity_precision_by_symbol = {
        c["symbol"]: int(c.get("quantityPrecision", 0)) for c in contracts
    }
    min_notional_by_symbol = {
        c["symbol"]: Decimal(str(c.get("tradeMinUSDT", "0"))) for c in contracts
    }
    live_execution_loop.run_forever(
        repo, live_connector, market_data_connector, quantity_precision_by_symbol,
        min_notional_by_symbol, settings,
    )
```

Modify `_run_discovery_forever` to accept and forward the live connector:

```python
def _run_discovery_forever(
    connector: BingXMarketDataConnector,
    runner: AgentRunner,
    settings: Settings,
    news_connector: NewsRSSConnector | None,
    external_data_connector: ExternalDataConnector | None,
    screener_runner: AgentRunner | None = None,
    live_connector: BingXLiveTradingConnector | None = None,
) -> None:
    """Konstruerar sin egen Repository (och därmed sqlite3-anslutning) HÄR,
    inne i den tråd som faktiskt kör discovery-loopen. En sqlite3-anslutning
    är trådbunden (check_same_thread=True som default i storage/db.py) -
    AC3-live-körningen 2026-08-28 kraschade omedelbart med
    sqlite3.ProgrammingError eftersom Repository tidigare konstruerades i
    huvudtråden (main()) och sedan skickades in i denna threading.Thread.
    Samma mönster som redan används i
    tests/crypto_trading/storage/test_repository_concurrency.py."""
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    discovery_loop.run_forever(
        connector,
        repo,
        runner,
        settings,
        news_connector=news_connector,
        external_data_connector=external_data_connector,
        screener_runner=screener_runner,
        live_connector=live_connector,
        live_market_data_connector=connector,
    )
```

- [ ] **Step 3: Add imports and wire into `main()`**

Add to the imports at the top:
```python
from decimal import Decimal

from crypto_trading import live_execution_loop
from crypto_trading.connectors.bingx_live_trading import BingXLiveTradingConnector
from crypto_trading.config.loader import is_live_execution_enabled
```

(Add these alongside the existing `demo_execution_loop`/`BingXDemoTradingConnector`/`is_demo_execution_enabled` imports — combine into the same `from crypto_trading import (...)` and `from crypto_trading.config.loader import (...)` blocks rather than duplicating them.)

In `main()`, build the live connector **before** constructing `discovery_thread` (so it can be passed to both threads), and pass it into `discovery_thread`'s `args`:

```python
    live_connector = build_live_trading_connector_from_env() if is_live_execution_enabled() else None

    discovery_thread = threading.Thread(
        target=_run_discovery_forever,
        args=(
            connector, runner, settings, news_connector, external_data_connector,
            screener_runner, live_connector,
        ),
        daemon=True,
    )
```

After the existing `if is_guardian_enabled(): ... else: ...` block, add:

```python
    if is_live_execution_enabled():
        if live_connector is not None:
            threads.append(
                threading.Thread(
                    target=_run_live_execution_forever,
                    args=(connector, live_connector, settings),
                    daemon=True,
                )
            )
        else:
            log_event(
                "startup", event="live_execution_disabled",
                reason="BINGX_API_KEY/BINGX_API_SECRET missing",
            )
    else:
        log_event(
            "startup", event="live_execution_disabled",
            reason="CRYPTO_TRADING_LIVE_EXECUTION_ENABLED not set",
        )
```

- [ ] **Step 4: Manual smoke check (no automated test — this only wires existing, already-tested pieces together)**

Run: `uv run python -c "import crypto_trading.run"` — confirms the module still imports cleanly.
Expected: no output, exit code 0.

Run: `uv run pytest tests/ -v -k "discovery_loop or demo_execution or guardian"` — confirms `_run_discovery_forever`'s new optional parameter didn't break any existing caller (there are none in the test suite that call `_run_discovery_forever` directly — it's exercised only via `main()`, which nothing in the suite invokes — so this step is really just re-confirming `discovery_loop.py`'s own suite from Task 8, which already covers `live_connector=None` byte-for-byte parity).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/run.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): wire live execution as a ninth, opt-in daemon thread

Default off (CRYPTO_TRADING_LIVE_EXECUTION_ENABLED unset - stays unset
through this entire plan). Reads only BINGX_API_KEY/BINGX_API_SECRET,
never the Demo-dedicated CRYPTO_TRADING_BINGX_DEMO_API_KEY/_SECRET.
Discovery's own thread now receives the same live connector so Layer
1's capacity/margin gate (Task 8) is active whenever live is armed.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HaqP9r9eK2JEkBVwPyRym8
EOF
)"
```

---

## Task 10: Live position report

**Files:**
- Create: `crypto_trading/performance/live_track_report.py`
- Test: `tests/crypto_trading/performance/test_live_track_report.py`

**Interfaces:**
- Consumes: `repo.find_all_positions(limit: int, offset: int = 0) -> list[Position]` (existing, same `limit=10_000` convention `performance/paper_track_report.py` already uses), `repo.get_live_execution` (Task 4), `repo.find_latest_guardian_observation` (existing).
- Produces: `build_live_report(repo) -> dict` with key `"live_positions"`: a list of `{position_id, instrument, direction, margin_usdt, notional_usdt, leverage, entry, stop_loss, target, guardian_state, exit_reason, exchange_fill_exit, realized_fees_usdt, realized_funding_usdt, phase}` rows for every position that has a `live_executions` row, plus a top-level `"total_live_pnl_usdt"` summing realized PnL across all `CLOSED` rows (fill-price delta × entry_quantity, minus fees, minus/plus funding).

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/performance/test_live_track_report.py
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.performance.live_track_report import build_live_report
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def test_build_live_report_includes_every_live_position(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = Position(
        position_id="pos-1", candidate_id="pos-1", instrument="BTC-USDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50000"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1", opened_at=_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(event_id="POSITION_OPENED:pos-1", event_type="POSITION_OPENED",
              aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
              run_id="seed", schema_version=1, payload={}),
    )
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50010", None, None, _NOW
    )
    repo.close_live_execution(
        "pos-1", "target", "52000", _NOW, realized_fees_usdt="0.08", realized_funding_usdt="-0.01"
    )

    report = build_live_report(repo)

    assert len(report["live_positions"]) == 1
    row = report["live_positions"][0]
    assert row["position_id"] == "pos-1"
    assert row["margin_usdt"] == "10"
    assert row["notional_usdt"] == "100"
    assert row["leverage"] == "10"
    assert row["exit_reason"] == "target"
    assert row["realized_fees_usdt"] == "0.08"
    assert row["realized_funding_usdt"] == "-0.01"
    # (52000 - 50010) * 0.002 - 0.08 + (-0.01) = 3.98 - 0.08 - 0.01 = 3.89
    assert report["total_live_pnl_usdt"] == "3.89"


def test_build_live_report_empty_when_no_live_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    report = build_live_report(repo)

    assert report["live_positions"] == []
    assert report["total_live_pnl_usdt"] == "0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/crypto_trading/performance/test_live_track_report.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `live_track_report.py`**

```python
# crypto_trading/performance/live_track_report.py
from __future__ import annotations

from decimal import Decimal

from crypto_trading.storage.repository import Repository


def build_live_report(repo: Repository) -> dict:
    """Read-only, pure reporting - same discipline as
    performance/paper_track_report.py. Never writes anything. Joins
    positions with live_executions (Task 4) and the latest Guardian
    observation (existing find_latest_guardian_observation) purely for
    display; this module has no opinion on strategy/risk and makes no
    trading decision."""
    live_positions = []
    total_pnl = Decimal("0")
    for position in repo.find_all_positions(limit=10_000):
        live_row = repo.get_live_execution(position.position_id)
        if live_row is None:
            continue
        guardian = repo.find_latest_guardian_observation(position.position_id)
        row = {
            "position_id": position.position_id,
            "instrument": position.instrument,
            "direction": position.direction,
            "margin_usdt": live_row["margin_usdt"],
            "notional_usdt": live_row["notional_usdt"],
            "leverage": live_row["leverage"],
            "entry": live_row["exchange_fill_entry"],
            "stop_loss": str(position.stop_loss),
            "target": str(position.target),
            "guardian_state": guardian["state"] if guardian else None,
            "exit_reason": live_row["exit_reason"],
            "exchange_fill_exit": live_row["exchange_fill_exit"],
            "realized_fees_usdt": live_row["realized_fees_usdt"],
            "realized_funding_usdt": live_row["realized_funding_usdt"],
            "phase": live_row["phase"],
        }
        live_positions.append(row)
        if live_row["phase"] == "CLOSED" and live_row["exchange_fill_entry"] and live_row["exchange_fill_exit"]:
            entry_qty = Decimal(str(live_row["entry_quantity"] or "0"))
            gross = (
                Decimal(str(live_row["exchange_fill_exit"]))
                - Decimal(str(live_row["exchange_fill_entry"]))
            ) * entry_qty
            fees = Decimal(str(live_row["realized_fees_usdt"] or "0"))
            funding = Decimal(str(live_row["realized_funding_usdt"] or "0"))
            total_pnl += gross - fees + funding
    return {"live_positions": live_positions, "total_live_pnl_usdt": str(total_pnl)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/crypto_trading/performance/test_live_track_report.py -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/performance/live_track_report.py tests/crypto_trading/performance/test_live_track_report.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add read-only live position report

Reports every live_executions row alongside its PAPER position's
SL/target and latest Guardian state - instrument, side, margin,
notional, leverage, entry, SL, target, guardian state, exit, realized
fees/funding, and total realized live PnL. Purely read-only, no
trading decision.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01HaqP9r9eK2JEkBVwPyRym8
EOF
)"
```

---

## Task 11: Full suite regression + git diff review + FINAL GATE (manual, not auto-executed)

**Files:** none new — this task is verification-only.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: all tests pass, including every test added in Tasks 1-10, and every pre-existing PAPER/Demo/Guardian test unchanged and still green (proves LIVE's addition changed nothing about them). Zero real network calls.

- [ ] **Step 2: Review the full diff for unrelated changes**

Run: `git log --oneline -11` — confirm exactly the 10 feature commits from Tasks 1-10 (plus this task's own commit once Step 3 runs) are present, nothing else.
Run: `git diff main --stat` (or against whatever base branch this work started from) — confirm the changed-file list is exactly:
```
SPEC_CRYPTO.md
crypto_trading/config/live_execution.yaml
crypto_trading/config/loader.py
crypto_trading/storage/db.py
crypto_trading/storage/repository.py
crypto_trading/connectors/bingx_live_trading.py
crypto_trading/paper_trading/live_execution.py
crypto_trading/live_execution_loop.py
crypto_trading/discovery_loop.py
crypto_trading/run.py
crypto_trading/performance/live_track_report.py
tests/crypto_trading/config/test_live_execution_config.py
tests/crypto_trading/storage/test_db.py
tests/crypto_trading/storage/test_repository_live_execution.py
tests/crypto_trading/connectors/test_bingx_live_trading.py
tests/crypto_trading/paper_trading/test_live_execution.py
tests/crypto_trading/test_live_execution_loop.py
tests/crypto_trading/test_discovery_loop.py
tests/crypto_trading/performance/test_live_track_report.py
```
If anything else appears (e.g. an accidentally-staged `.env`, a stray `__pycache__`, an unrelated file touched by a different in-progress task), stop and investigate before proceeding — never silently include it.

- [ ] **Step 3: Report the diff and test results to the user, then stop**

Summarize (in the conversation, not as a new file): total tests added/passing, the exact file list from Step 2, and confirmation that `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED` is not set anywhere in this diff. Wait for the user's explicit instruction before doing anything else with this work (further commits beyond Tasks 1-10's own are not this task's job — those already happened per-task; this step is a review checkpoint, not an additional commit).

- [ ] **Step 4: STOP — do not proceed past this point automatically**

Everything above this line may be executed autonomously. **Step 5 requires the user's explicit, separate go-ahead in a live conversation before running** — this is the first point in the whole plan where a call to the real, authenticated `open-api.bingx.com` endpoint happens, using the user's actual `BINGX_API_KEY`/`BINGX_API_SECRET`. No prior task calls this endpoint for real; everything up to here is respx-mocked. **No order is ever placed by this task or any task in this plan.**

Before asking the user to proceed, re-verify by inspection (not by running anything):
- `BingXLiveTradingConnector._base_url` is exactly `"https://open-api.bingx.com"` — read the file, confirm the literal.
- `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED` is unset in the current shell/`.env` (so the automated `run.py` thread does not start on its own) — confirm with `env | grep CRYPTO_TRADING_LIVE_EXECUTION_ENABLED` (bash), expect no output.
- The `BINGX_API_KEY`/`BINGX_API_SECRET` values in `.env` are in fact the user's real BingX account keys (already confirmed by the user during design — re-state this assumption explicitly before the live call).

- [ ] **Step 5: Manual, user-approved READ-ONLY verification (only after explicit go-ahead)**

A minimal standalone script (not part of `run.py`, not run automatically, and **never places an order**):

```python
# scratch verification script - run manually, once, with the user watching.
# READ-ONLY. Does not place, cancel, or modify any order or position.
import os

from dotenv import load_dotenv

from crypto_trading.connectors.bingx_live_trading import BingXLiveTradingConnector

load_dotenv()
connector = BingXLiveTradingConnector(
    api_key=os.environ["BINGX_API_KEY"],
    api_secret=os.environ["BINGX_API_SECRET"],
)
assert connector._base_url == "https://open-api.bingx.com"

# Confirms the real response shapes this plan's code assumes defensively:
print("balance:", connector.get_balance())
print("open positions:", connector.get_all_positions())
```

Also, separately (existing, already-live-used `BingXMarketDataConnector`, no new credential/host involved): run `connector.get_contracts()` and inspect a couple of real entries to confirm the actual field names for minimum quantity/notional (this plan assumed `tradeMinUSDT`, `quantityPrecision` — adjust `_run_live_execution_forever()`'s `min_notional_by_symbol`/`quantity_precision_by_symbol` construction in `run.py` if the real names differ).

After this, update `crypto_trading/connectors/bingx_live_trading.py`'s `get_balance()` docstring and `run.py`'s minimum-notional field name (if it needed adjusting) to remove the "not yet live-verified" caveat, and commit that adjustment alone — still with `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED` unset. **Activating the thread (setting that env var) is a separate, later, explicit decision, not part of this plan.**
