# BingX Demo Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mirror every Gate-approved PAPER trade as a real order on the user's BingX Demo (VST) account, so exchange-side stop-loss/target execution stays accurate even when the bot process isn't running continuously — without ever touching PAPER's own simulation or being able to reach a live BingX account.

**Architecture:** A strictly additive layer. `paper_trading/position_opening.py`/`position_closing.py` and the `positions` table are never modified. A new `demo_executions` table + `paper_trading/demo_execution.py` + a new `demo_execution_loop` thread read the same already-open `positions` rows and mirror them as BingX Demo orders, writing exclusively to `demo_executions`. A hardcoded exact-host guard (`open-api-vst.bingx.com` only) makes it structurally impossible for this code to reach the live BingX host.

**Tech Stack:** Python, httpx + tenacity (existing pattern from `connectors/base.py`), pydantic (config), SQLite (existing `storage/db.py`/`repository.py`), pytest + respx (existing test conventions).

**Spec:** `docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md`

## Global Constraints

- `open-api-vst.bingx.com` is the **only** host this code may ever contact for order placement/cancel/query — hardcoded, never a constructor/env/settings value, checked by exact hostname match (not substring) immediately before every mutating HTTP call.
- Credentials read **only** from `CRYPTO_TRADING_BINGX_DEMO_API_KEY`/`CRYPTO_TRADING_BINGX_DEMO_API_SECRET` — never the generic `BINGX_API_KEY`/`BINGX_API_SECRET` already sitting unused in `.env`, never printed or logged.
- Demo execution code may never create, modify, or close a row in the `positions` table. It only reads `positions` and writes `demo_executions`.
- New thread is gated by `CRYPTO_TRADING_DEMO_EXECUTION_ENABLED` env var, **default off** (same opt-in pattern as `CRYPTO_TRADING_DASHBOARD_ENABLED`/Telegram in `run.py`).
- Leverage fixed at 1x; SL/TP legs `reduceOnly=true`; quantity derived from PAPER's existing notional `size`, never a new sizing formula.
- Zero real network calls in the automated test suite — everything hits `respx`-mocked `https://open-api-vst.bingx.com`.
- **Task 11 (the first real order) is never auto-executed** — it requires the user's explicit, separate go-ahead in a live session.

---

## Task 1: SPEC_CRYPTO.md amendment

**Files:**
- Modify: `SPEC_CRYPTO.md` (§1 "Det här är INTE" list, §19, §20 self-review table)

**Interfaces:** None (documentation only).

- [ ] **Step 1: Amend §1**

In the "Det här är INTE" bullet that currently reads (approximately):

```
- **Ett tradingsystem i verklig mening.** Ingen kod i `crypto_trading/` får:
  - ansluta till BingX (eller annat) mäklarkonto,
  - läsa kontosaldo,
  - hantera broker-credentials eller API-nycklar för orderläggning,
  - placera en riktig order,
  - flytta pengar.

  Detta är en **hård gräns, inte en konfigurationsflagga** — identisk princip som `intelligence/`s SPEC §1/§14, gäller i alla faser. "Paper trading" betyder uteslutande lokal, simulerad bokföring av hypotetiska positioner mot riktig marknadsdata — aldrig en verklig order.
```

replace with:

```
- **Ett tradingsystem i verklig mening mot ett riktigt (live) konto.** Ingen
  kod i `crypto_trading/` får:
  - ansluta till ett riktigt BingX-konto (eller annat riktigt mäklarkonto),
  - läsa ett riktigt kontosaldo,
  - hantera broker-credentials/API-nycklar som kan nå ett riktigt konto,
  - placera en riktig order,
  - flytta riktiga pengar.

  Detta är en **hård gräns, inte en konfigurationsflagga** — identisk princip
  som `intelligence/`s SPEC §1/§14, gäller i alla faser.

  **Explicit, avsiktligt undantag (2026-09-04, se
  `docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md`):**
  `crypto_trading/connectors/bingx_demo_trading.py` får placera/avbryta
  ordrar **uteslutande** mot BingX **Demo (VST)**-kontot, aldrig det riktiga
  kontot. Detta är säkrat på kodnivå, inte bara via konfiguration:
  - `_base_url` är en hårdkodad modulkonstant (`open-api-vst.bingx.com`),
    aldrig en constructor-/env-/settings-parameter.
  - Ett exakt host-guard körs omedelbart före varje order-läggande/ändrande/
    avbrytande anrop och vägrar allt annat än exakt detta värde.
  - Credentials läses uteslutande från `CRYPTO_TRADING_BINGX_DEMO_API_KEY`/
    `_SECRET`, aldrig en generisk `BINGX_API_KEY`-variabel.
  - Tråden som kör detta är avstängd som standard
    (`CRYPTO_TRADING_DEMO_EXECUTION_ENABLED`, opt-in).
  - Denna kod får **aldrig** skapa/ändra/stänga en rad i `positions`-tabellen
    — PAPER och BingX Demo är oberoende, parallella observatörer av samma
    redan Gate-godkända trade (se §8.6/§8.7 nedan, oförändrade för
    `positions`).

  "Paper trading" (den ursprungliga, oförändrade `positions`-tabellen)
  förblir uteslutande lokal, simulerad bokföring — det nya BingX Demo-lagret
  är ett separat, additivt observationslager, inte en ersättning.
```

- [ ] **Step 2: Amend §19**

Find the bullet:
```
- Ingen kod i `crypto_trading/` ansluter till ett mäklarkonto, hanterar broker-credentials, placerar en riktig order, eller flyttar pengar — i någon fas. Hård gräns, inte konfigurationsflagga (§1).
- **Paper trading är 100 % lokal simulering.** Ingen del av `paper_trading/` gör ett nätverksanrop mot ett BingX-konto eller någon order-/account-endpoint — all "execution" är en beräkning mot redan hämtad publik marknadsdata, skriven till den lokala databasen (§16). Verifieras explicit i Phase 1 acceptance criterion 3 (PLAN_CRYPTO.md): ingen kod-sökväg i hela `crypto_trading/` refererar ett BingX-konto, orderendpoint eller broker-credential.
```

replace with:

```
- Ingen kod i `crypto_trading/` ansluter till ett RIKTIGT mäklarkonto, hanterar broker-credentials som kan nå ett riktigt konto, placerar en riktig order, eller flyttar riktiga pengar — i någon fas. Hård gräns, inte konfigurationsflagga (§1). Explicit, avsiktligt undantag: `connectors/bingx_demo_trading.py` mot BingX Demo (VST) uteslutande, se §1 och `docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md`.
- **Paper trading (`positions`-tabellen) är fortsatt 100 % lokal simulering, oförändrad.** `paper_trading/position_opening.py`/`position_closing.py` gör inget nätverksanrop mot ett BingX-konto. Ett separat, additivt lager (`paper_trading/demo_execution.py`, tabellen `demo_executions`) mirror:ar Gate-godkända trades som riktiga ordrar mot BingX Demo (VST) — det lagret rör aldrig `positions`.
```

- [ ] **Step 3: Amend §20 self-review table**

Find the row:
```
| Kan riktig handel ske av misstag? | Nej — inga account/order-endpoints existerar i kodbasen, ingen broker-anslutning, ingen kod för det (§1, §19). |
```

replace with:

```
| Kan riktig (LIVE-konto) handel ske av misstag? | Nej — `connectors/bingx_demo_trading.py` har en hårdkodad `_base_url`-konstant (aldrig en parameter), ett exakt host-guard som körs före varje mutating anrop och vägrar allt utom `open-api-vst.bingx.com`, dedikerade `CRYPTO_TRADING_BINGX_DEMO_API_KEY/_SECRET`-variabler (aldrig en generisk nyckel), och tråden är avstängd som standard (§1, §19, `docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md`). |
| Kan BingX Demo-exekveringen ändra en PAPER-position? | Nej — den skriver uteslutande till `demo_executions`, aldrig till `positions`; `position_opening.py`/`position_closing.py` är oförändrade och opåverkade. |
```

- [ ] **Step 4: Commit**

```bash
git add SPEC_CRYPTO.md
git commit -m "$(cat <<'EOF'
docs(crypto-trading): amend SPEC for BingX Demo execution exception

Narrows the previous absolute no-broker-connection boundary to
explicitly permit BingX Demo (VST) order placement through the new
connectors/bingx_demo_trading.py, gated by a hardcoded exact-host
guard, dedicated credentials, a default-off arm flag, and strict
isolation from the PAPER positions table. See
docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 2: Demo execution config

**Files:**
- Create: `crypto_trading/config/demo_execution.yaml`
- Modify: `crypto_trading/config/loader.py`
- Test: `tests/crypto_trading/config/test_demo_execution_config.py`

**Interfaces:**
- Produces: `DemoExecutionConfig` (pydantic model: `check_interval_seconds: int`, `claim_stale_after_seconds: int`, `max_retries: int`), `Settings.demo_execution: DemoExecutionConfig`, and a module-level function `crypto_trading.config.loader.is_demo_execution_enabled() -> bool` reading `CRYPTO_TRADING_DEMO_EXECUTION_ENABLED` (mirrors `run.py`'s existing `os.environ.get("CRYPTO_TRADING_DASHBOARD_ENABLED")` pattern, kept in `loader.py` since it's config-adjacent and needs `load_dotenv` already called by `get_settings()`).

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/config/test_demo_execution_config.py
import os

from crypto_trading.config.loader import get_settings, is_demo_execution_enabled


def test_settings_load_demo_execution_defaults():
    settings = get_settings()
    assert settings.demo_execution.check_interval_seconds > 0
    assert settings.demo_execution.claim_stale_after_seconds > 0
    assert settings.demo_execution.max_retries > 0


def test_is_demo_execution_enabled_reads_env_flag(monkeypatch):
    monkeypatch.delenv("CRYPTO_TRADING_DEMO_EXECUTION_ENABLED", raising=False)
    assert is_demo_execution_enabled() is False
    monkeypatch.setenv("CRYPTO_TRADING_DEMO_EXECUTION_ENABLED", "1")
    assert is_demo_execution_enabled() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/crypto_trading/config/test_demo_execution_config.py -v`
Expected: FAIL — `demo_execution.yaml` missing / `is_demo_execution_enabled` not defined.

- [ ] **Step 3: Create the YAML file**

```yaml
# crypto_trading/config/demo_execution.yaml
# BingX Demo (VST) execution (2026-09-04) - parallel observer of PAPER, see
# docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md. Whether
# the thread runs at all is an env-var arm flag
# (CRYPTO_TRADING_DEMO_EXECUTION_ENABLED), not this file - these are only
# tunables for when it does run.
check_interval_seconds: 30
claim_stale_after_seconds: 30
max_retries: 3
```

- [ ] **Step 4: Add `DemoExecutionConfig` and wire it into `Settings`**

In `crypto_trading/config/loader.py`, add after `class DetectiveConfig(BaseModel): ...`:

```python
class DemoExecutionConfig(BaseModel):
    # BingX Demo (VST) execution tunables only - whether the thread runs at
    # all is the CRYPTO_TRADING_DEMO_EXECUTION_ENABLED env-var arm flag
    # (is_demo_execution_enabled() below), same opt-in pattern already used
    # for the dashboard/Telegram threads in run.py.
    check_interval_seconds: int = Field(gt=0, default=30)
    claim_stale_after_seconds: int = Field(gt=0, default=30)
    max_retries: int = Field(gt=0, default=3)
```

In `class Settings(BaseModel):`, add:
```python
    demo_execution: DemoExecutionConfig = Field(default_factory=DemoExecutionConfig)
```

In `get_settings()`, add to the `Settings(...)` call:
```python
        demo_execution=_load_yaml_model(_CONFIG_DIR / "demo_execution.yaml", DemoExecutionConfig),
```

At the bottom of the file, add:
```python
def is_demo_execution_enabled() -> bool:
    """Opt-in arm flag for the BingX Demo execution thread - same pattern as
    run.py's existing CRYPTO_TRADING_DASHBOARD_ENABLED check. Deliberately
    an env var, not a YAML setting: matches how the other optional threads
    (dashboard, Telegram) are gated in this codebase, and keeps "should this
    thread run at all" a deploy-time decision, not a checked-in default."""
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    return bool(os.environ.get("CRYPTO_TRADING_DEMO_EXECUTION_ENABLED"))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/crypto_trading/config/test_demo_execution_config.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add crypto_trading/config/demo_execution.yaml crypto_trading/config/loader.py tests/crypto_trading/config/test_demo_execution_config.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add demo execution config and opt-in arm flag

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 3: `demo_executions` table

**Files:**
- Modify: `crypto_trading/storage/db.py`
- Test: `tests/crypto_trading/storage/test_db.py`

**Interfaces:**
- Produces: table `demo_executions` with columns `position_id TEXT PRIMARY KEY, phase TEXT NOT NULL, entry_client_order_id TEXT, entry_exchange_order_id TEXT, entry_quantity TEXT, sl_exchange_order_id TEXT, tp_exchange_order_id TEXT, exit_reason TEXT, exchange_fill_entry TEXT, exchange_fill_exit TEXT, last_error TEXT, claimed_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/crypto_trading/storage/test_db.py
def test_demo_executions_table_exists(tmp_path):
    from crypto_trading.storage.db import get_connection

    conn = get_connection(tmp_path / "t.db")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(demo_executions)").fetchall()}
    assert columns == {
        "position_id", "phase", "entry_client_order_id", "entry_exchange_order_id",
        "entry_quantity", "sl_exchange_order_id", "tp_exchange_order_id", "exit_reason",
        "exchange_fill_entry", "exchange_fill_exit", "last_error", "claimed_at",
        "updated_at", "closed_at",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/crypto_trading/storage/test_db.py::test_demo_executions_table_exists -v`
Expected: FAIL — table does not exist.

- [ ] **Step 3: Add the table to `_SCHEMA`**

In `crypto_trading/storage/db.py`, append inside the `_SCHEMA` string, after the `detective_analyzed_positions` table definition:

```sql
-- BingX Demo (VST) execution (2026-09-04): strictly additive parallel
-- observer of an already-Gate-approved PAPER position, never the other way
-- around - this table is NEVER joined-into or written-from
-- position_opening.py/position_closing.py, see
-- docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md.
-- phase: CLAIMED -> ACTIVE -> CLOSED / FAILED. Claim-before-place
-- idempotency: position_id is the PK, so a duplicate POSITION_OPENED
-- observation or a restart can never produce two demo orders for the same
-- position (INSERT OR IGNORE in repository.py::claim_demo_execution()).
CREATE TABLE IF NOT EXISTS demo_executions (
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
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/crypto_trading/storage/test_db.py::test_demo_executions_table_exists -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/storage/db.py tests/crypto_trading/storage/test_db.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add demo_executions table

Additive-only table for mirroring PAPER positions as BingX Demo
orders; never written from position_opening.py/position_closing.py.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 4: Repository methods for demo execution

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Test: `tests/crypto_trading/storage/test_repository_demo_execution.py`

**Interfaces:**
- Consumes: `Position` (from Task 3's table + existing `positions` table), `SQLiteRepository._row_to_position` (existing).
- Produces (added to both the `Repository` Protocol and `SQLiteRepository`):
  - `claim_demo_execution(position_id: str, claimed_at: datetime) -> bool`
  - `get_demo_execution(position_id: str) -> dict | None`
  - `find_positions_pending_demo_execution(limit: int) -> list[Position]`
  - `find_active_demo_executions() -> list[dict]`
  - `find_stale_claimed_demo_executions(older_than: datetime) -> list[dict]`
  - `update_demo_execution_submitted(position_id: str, entry_client_order_id: str, entry_exchange_order_id: str, entry_quantity: str, exchange_fill_entry: str, sl_exchange_order_id: str | None, tp_exchange_order_id: str | None, updated_at: datetime) -> None`
  - `close_demo_execution(position_id: str, exit_reason: str, exchange_fill_exit: str, closed_at: datetime) -> None`
  - `mark_demo_execution_failed(position_id: str, last_error: str, updated_at: datetime) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto_trading/storage/test_repository_demo_execution.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _open_position(repo: SQLiteRepository, position_id: str = "pos-1") -> Position:
    position = Position(
        position_id=position_id,
        candidate_id=position_id,
        instrument="BTCUSDT",
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


def test_claim_demo_execution_is_idempotent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)

    first = repo.claim_demo_execution("pos-1", _NOW)
    second = repo.claim_demo_execution("pos-1", _NOW)

    assert first is True
    assert second is False
    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "CLAIMED"


def test_find_positions_pending_demo_execution_excludes_claimed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-1")
    _open_position(repo, "pos-2")
    repo.claim_demo_execution("pos-1", _NOW)

    pending = repo.find_positions_pending_demo_execution(limit=10)

    assert [p.position_id for p in pending] == ["pos-2"]


def test_update_demo_execution_submitted_then_close(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_demo_execution("pos-1", _NOW)

    repo.update_demo_execution_submitted(
        "pos-1",
        entry_client_order_id="cid-1",
        entry_exchange_order_id="ex-1",
        entry_quantity="0.02",
        exchange_fill_entry="50030",
        sl_exchange_order_id="sl-1",
        tp_exchange_order_id="tp-1",
        updated_at=_NOW,
    )
    active = repo.find_active_demo_executions()
    assert len(active) == 1
    assert active[0]["phase"] == "ACTIVE"
    assert active[0]["sl_exchange_order_id"] == "sl-1"

    repo.close_demo_execution("pos-1", "target", "52100", _NOW + timedelta(hours=1))
    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "target"
    assert repo.find_active_demo_executions() == []


def test_mark_demo_execution_failed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_demo_execution("pos-1", _NOW)

    repo.mark_demo_execution_failed("pos-1", "ConnectorUnavailableError: boom", _NOW)

    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "boom" in row["last_error"]


def test_find_stale_claimed_demo_executions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_demo_execution("pos-1", _NOW)

    not_yet_stale = repo.find_stale_claimed_demo_executions(_NOW - timedelta(seconds=1))
    stale = repo.find_stale_claimed_demo_executions(_NOW + timedelta(seconds=31))

    assert not_yet_stale == []
    assert len(stale) == 1
    assert stale[0]["position_id"] == "pos-1"


def test_demo_execution_never_writes_to_positions_table(tmp_path):
    """Isolation guarantee (spec §3): every repository method touching
    demo_executions must leave the positions row exactly as it was."""
    repo = SQLiteRepository(tmp_path / "t.db")
    before = _open_position(repo)

    repo.claim_demo_execution("pos-1", _NOW)
    repo.update_demo_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.02", "50030", "sl-1", "tp-1", _NOW
    )
    repo.close_demo_execution("pos-1", "target", "52100", _NOW)

    after = repo.get_position("pos-1")
    assert after == before
    assert after.status == "OPEN_POSITION"  # untouched by demo_execution close
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/storage/test_repository_demo_execution.py -v`
Expected: FAIL — methods don't exist yet.

- [ ] **Step 3: Add methods to the `Repository` Protocol**

In `crypto_trading/storage/repository.py`, inside `class Repository(Protocol):`, add:

```python
    def claim_demo_execution(self, position_id: str, claimed_at: datetime) -> bool: ...
    def get_demo_execution(self, position_id: str) -> dict | None: ...
    def find_positions_pending_demo_execution(self, limit: int) -> list[Position]: ...
    def find_active_demo_executions(self) -> list[dict]: ...
    def find_stale_claimed_demo_executions(self, older_than: datetime) -> list[dict]: ...
    def update_demo_execution_submitted(
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
    def close_demo_execution(
        self, position_id: str, exit_reason: str, exchange_fill_exit: str, closed_at: datetime
    ) -> None: ...
    def mark_demo_execution_failed(
        self, position_id: str, last_error: str, updated_at: datetime
    ) -> None: ...
```

- [ ] **Step 4: Implement on `SQLiteRepository`**

Add to `class SQLiteRepository:` (anywhere after `find_open_positions`):

```python
    def claim_demo_execution(self, position_id: str, claimed_at: datetime) -> bool:
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO demo_executions "
                "(position_id, phase, claimed_at, updated_at) VALUES (?, 'CLAIMED', ?, ?)",
                (position_id, claimed_at.isoformat(), claimed_at.isoformat()),
            )
            claimed = cur.rowcount > 0
            self._conn.commit()
            return claimed
        except Exception:
            self._conn.rollback()
            raise

    def get_demo_execution(self, position_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM demo_executions WHERE position_id = ?", (position_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def find_positions_pending_demo_execution(self, limit: int) -> list[Position]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN_POSITION' "
            "AND position_id NOT IN (SELECT position_id FROM demo_executions) "
            "ORDER BY opened_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def find_active_demo_executions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM demo_executions WHERE phase = 'ACTIVE'"
        ).fetchall()
        return [dict(row) for row in rows]

    def find_stale_claimed_demo_executions(self, older_than: datetime) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM demo_executions WHERE phase = 'CLAIMED' AND claimed_at < ?",
            (older_than.isoformat(),),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_demo_execution_submitted(
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
            "UPDATE demo_executions SET phase = 'ACTIVE', entry_client_order_id = ?, "
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

    def close_demo_execution(
        self, position_id: str, exit_reason: str, exchange_fill_exit: str, closed_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE demo_executions SET phase = 'CLOSED', exit_reason = ?, "
            "exchange_fill_exit = ?, closed_at = ?, updated_at = ? WHERE position_id = ?",
            (exit_reason, exchange_fill_exit, closed_at.isoformat(), closed_at.isoformat(), position_id),
        )
        self._conn.commit()

    def mark_demo_execution_failed(
        self, position_id: str, last_error: str, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE demo_executions SET phase = 'FAILED', last_error = ?, updated_at = ? "
            "WHERE position_id = ?",
            (last_error, updated_at.isoformat(), position_id),
        )
        self._conn.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/storage/test_repository_demo_execution.py -v`
Expected: PASS (all 6 tests)

- [ ] **Step 6: Commit**

```bash
git add crypto_trading/storage/repository.py tests/crypto_trading/storage/test_repository_demo_execution.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add repository methods for demo execution

Claim-before-place idempotency (INSERT OR IGNORE keyed on position_id,
same pattern as create_position_with_event) plus an explicit
isolation test proving these methods never mutate the positions
table.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 5: Extract `compute_hold_hours` for reuse

**Files:**
- Modify: `crypto_trading/paper_trading/monitoring.py`
- Test: `tests/crypto_trading/paper_trading/test_monitoring.py`

**Interfaces:**
- Produces: `compute_hold_hours(position: Position, now: datetime) -> Decimal`, used by both the existing `check_exit_trigger` and the new Task 7 time-limit parity logic — one shared calculation, no drift between PAPER and Demo.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/crypto_trading/paper_trading/test_monitoring.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.paper_trading.monitoring import compute_hold_hours
from crypto_trading.schemas.trade import Position


def _position(opened_at) -> Position:
    return Position(
        position_id="p1", candidate_id="p1", instrument="BTCUSDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("100"),
        simulated_fill_entry=Decimal("100"), stop_loss=Decimal("90"), target=Decimal("110"),
        size=Decimal("1000"), fill_model_version="v1", opened_at=opened_at,
    )


def test_compute_hold_hours_matches_elapsed_time():
    opened_at = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    now = opened_at + timedelta(hours=2, minutes=30)

    hours = compute_hold_hours(_position(opened_at), now)

    assert hours == Decimal("2.5")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/crypto_trading/paper_trading/test_monitoring.py::test_compute_hold_hours_matches_elapsed_time -v`
Expected: FAIL — `compute_hold_hours` not defined.

- [ ] **Step 3: Extract the function**

In `crypto_trading/paper_trading/monitoring.py`, replace:

```python
    hold_seconds = Decimal(str((now - position.opened_at).total_seconds()))
    hold_hours = hold_seconds / _SECONDS_PER_HOUR
    if hold_hours >= max_position_hold_hours:
        return "time_limit", current_price

    return None
```

with:

```python
    if compute_hold_hours(position, now) >= max_position_hold_hours:
        return "time_limit", current_price

    return None


def compute_hold_hours(position: Position, now: datetime) -> Decimal:
    """Shared with paper_trading/demo_execution.py's time-limit parity logic
    (2026-09-04 design) so PAPER and BingX Demo never drift on what counts
    as 'reached max_position_hold_hours' for the same position."""
    hold_seconds = Decimal(str((now - position.opened_at).total_seconds()))
    return hold_seconds / _SECONDS_PER_HOUR
```

- [ ] **Step 4: Run full monitoring test file to verify nothing regressed**

Run: `uv run pytest tests/crypto_trading/paper_trading/test_monitoring.py -v`
Expected: PASS (all tests, including the pre-existing `check_exit_trigger` ones and the new one)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/paper_trading/monitoring.py tests/crypto_trading/paper_trading/test_monitoring.py
git commit -m "$(cat <<'EOF'
refactor(crypto-trading): extract compute_hold_hours from check_exit_trigger

Enables demo_execution.py's time-limit parity logic to reuse the
exact same hold-time calculation PAPER already uses, instead of a
second, potentially drifting implementation.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 6: `BingXDemoTradingConnector`

**Files:**
- Create: `crypto_trading/connectors/bingx_demo_trading.py`
- Test: `tests/crypto_trading/connectors/test_bingx_demo_trading.py`

**Interfaces:**
- Consumes: `crypto_trading.connectors.exceptions.ConnectorUnavailableError` (existing).
- Produces: `DemoExecutionGuardError` (new exception), `BingXDemoTradingConnector(api_key: str, api_secret: str, timeout_seconds: float = 10.0, max_retries: int = 3)` with methods `set_leverage(symbol, leverage=1, side="LONG") -> dict`, `place_entry_order_with_sl_tp(symbol, quantity, client_order_id, stop_loss_price, target_price) -> dict`, `get_order_by_client_order_id(symbol, client_order_id) -> dict | None`, `get_order_status(symbol, order_id) -> dict | None`, `cancel_all_open_orders(symbol) -> dict`, `close_position_market(symbol, quantity, client_order_id) -> dict`.

**Verify live before Task 11:** exact response field names (`orderId`, `avgPrice`, sub-order ids returned for attached `stopLoss`/`takeProfit`) and the exact cancel/query endpoint paths must be confirmed against the real, authenticated BingX Demo endpoint — BingX's docs site is JS-rendered and this plan's request/response shapes are the best-available written record, not live-verified. This connector's code defensively uses `.get(...)` with fallbacks everywhere a response field is read, so an unexpected/missing field degrades to an empty string rather than crashing.

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto_trading/connectors/test_bingx_demo_trading.py
import hashlib
import hmac
from urllib.parse import parse_qs, urlparse

import pytest
import respx
from httpx import Response

from crypto_trading.connectors.bingx_demo_trading import (
    BingXDemoTradingConnector,
    DemoExecutionGuardError,
)
from crypto_trading.connectors.exceptions import ConnectorUnavailableError

_VST_BASE = "https://open-api-vst.bingx.com"


def _connector(**overrides) -> BingXDemoTradingConnector:
    defaults = dict(api_key="k", api_secret="s", timeout_seconds=5, max_retries=2)
    defaults.update(overrides)
    return BingXDemoTradingConnector(**defaults)


@respx.mock
def test_place_entry_order_with_sl_tp_hits_vst_host_with_signed_request():
    route = respx.post(f"{_VST_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {"orderId": "ex-1", "avgPrice": "50030"},
            },
        )
    )

    result = _connector().place_entry_order_with_sl_tp(
        symbol="BTC-USDT",
        quantity="0.02",
        client_order_id="cid-1",
        stop_loss_price="49000",
        target_price="52000",
    )

    assert result == {"orderId": "ex-1", "avgPrice": "50030"}
    request = route.calls[0].request
    assert request.headers["X-BX-APIKEY"] == "k"
    params = parse_qs(urlparse(str(request.url)).query)
    assert params["symbol"] == ["BTC-USDT"]
    assert params["clientOrderID"] == ["cid-1"]
    assert "signature" in params


@respx.mock
def test_place_entry_order_raises_on_api_error_code():
    respx.post(f"{_VST_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 80001, "msg": "insufficient balance", "data": {}})
    )

    with pytest.raises(ConnectorUnavailableError, match="insufficient balance"):
        _connector().place_entry_order_with_sl_tp(
            symbol="BTC-USDT", quantity="0.02", client_order_id="cid-1",
            stop_loss_price="49000", target_price="52000",
        )


def test_refuses_to_place_order_against_a_non_vst_host():
    connector = _connector()
    connector._base_url = "https://open-api.bingx.com"  # simulate a mutated instance

    with pytest.raises(DemoExecutionGuardError):
        connector.place_entry_order_with_sl_tp(
            symbol="BTC-USDT", quantity="0.02", client_order_id="cid-1",
            stop_loss_price="49000", target_price="52000",
        )


def test_refuses_a_lookalike_host():
    """A subdomain/near-miss host must never pass the guard (exact match
    only, no substring check - required per user feedback on the design)."""
    connector = _connector()
    connector._base_url = "https://open-api-vst.bingx.com.evil.example"

    with pytest.raises(DemoExecutionGuardError):
        connector.cancel_all_open_orders("BTC-USDT")


@respx.mock
def test_get_order_by_client_order_id_returns_none_when_not_found():
    respx.get(f"{_VST_BASE}/openApi/swap/v2/trade/order").mock(
        return_value=Response(200, json={"code": 80016, "msg": "order not found", "data": {}})
    )

    result = _connector().get_order_by_client_order_id("BTC-USDT", "cid-missing")

    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/connectors/test_bingx_demo_trading.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement the connector**

```python
# crypto_trading/connectors/bingx_demo_trading.py
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode, urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from crypto_trading.connectors.exceptions import ConnectorUnavailableError

_VST_HOST = "open-api-vst.bingx.com"
_ORDER_PATH = "/openApi/swap/v2/trade/order"
_ALL_OPEN_ORDERS_PATH = "/openApi/swap/v2/trade/allOpenOrders"
_LEVERAGE_PATH = "/openApi/swap/v2/trade/leverage"


class DemoExecutionGuardError(Exception):
    """Raised whenever this connector would otherwise send a mutating
    request to anything other than the exact BingX Demo (VST) host. Refuses
    to proceed rather than risk reaching the user's real BingX account -
    see docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md."""


class BingXDemoTradingConnector:
    """Order placement/cancel/query against the user's BingX Demo (VST)
    account ONLY. `_base_url` is a hardcoded class constant, never a
    constructor parameter or settings/env value - there is no code path
    that can point this connector at the live open-api.bingx.com host
    (SPEC_CRYPTO.md §1/§19 amendment, 2026-09-04)."""

    _base_url = f"https://{_VST_HOST}"

    def __init__(
        self, api_key: str, api_secret: str, timeout_seconds: float = 10.0, max_retries: int = 3
    ):
        self._api_key = api_key
        self._api_secret = api_secret
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    def _guard_host(self) -> None:
        parsed = urlparse(self._base_url)
        if parsed.scheme != "https" or parsed.hostname != _VST_HOST:
            raise DemoExecutionGuardError(
                f"refuses to trade against host={parsed.hostname!r}, "
                f"only {_VST_HOST!r} is permitted"
            )

    def _sign(self, params: dict) -> dict:
        query = urlencode(sorted(params.items()))
        signature = hmac.new(
            self._api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return {**params, "signature": signature}

    def _request(self, method: str, path: str, params: dict) -> dict | None:
        self._guard_host()
        full_params = {**params, "timestamp": int(time.time() * 1000)}
        signed = self._sign(full_params)

        @retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=0.5, max=5),
            retry=retry_if_exception_type(httpx.TransportError),
            reraise=True,
        )
        def _do() -> dict | None:
            self._guard_host()  # re-checked immediately before the network call itself
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.request(
                    method,
                    f"{self._base_url}{path}",
                    params=signed,
                    headers={"X-BX-APIKEY": self._api_key},
                )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ConnectorUnavailableError(f"BingX Demo HTTP-fel: {path} ({exc})") from exc
            body = response.json()
            if body.get("code") != 0:
                raise ConnectorUnavailableError(
                    f"BingX Demo API-fel {body.get('code')}: {body.get('msg')} ({path})"
                )
            return body.get("data")

        return _do()

    def set_leverage(self, symbol: str, leverage: int = 1, side: str = "LONG") -> dict:
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
                {"type": "STOP_MARKET", "stopPrice": stop_loss_price, "workingType": "MARK_PRICE"}
            ),
            "takeProfit": json.dumps(
                {"type": "TAKE_PROFIT_MARKET", "stopPrice": target_price, "workingType": "MARK_PRICE"}
            ),
        }
        return self._request("POST", _ORDER_PATH, params) or {}

    def get_order_by_client_order_id(self, symbol: str, client_order_id: str) -> dict | None:
        try:
            return self._request(
                "GET", _ORDER_PATH, {"symbol": symbol, "clientOrderID": client_order_id}
            )
        except ConnectorUnavailableError:
            return None

    def get_order_status(self, symbol: str, order_id: str) -> dict | None:
        try:
            return self._request("GET", _ORDER_PATH, {"symbol": symbol, "orderId": order_id})
        except ConnectorUnavailableError:
            return None

    def cancel_all_open_orders(self, symbol: str) -> dict:
        return self._request("DELETE", _ALL_OPEN_ORDERS_PATH, {"symbol": symbol}) or {}

    def close_position_market(self, symbol: str, quantity: str, client_order_id: str) -> dict:
        return (
            self._request(
                "POST",
                _ORDER_PATH,
                {
                    "symbol": symbol,
                    "side": "SELL",
                    "positionSide": "LONG",
                    "type": "MARKET",
                    "quantity": quantity,
                    "reduceOnly": "true",
                    "clientOrderID": client_order_id,
                },
            )
            or {}
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/connectors/test_bingx_demo_trading.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/connectors/bingx_demo_trading.py tests/crypto_trading/connectors/test_bingx_demo_trading.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add BingXDemoTradingConnector with exact-host guard

Hardcoded open-api-vst.bingx.com base URL (never a constructor/env
parameter), an exact-hostname guard re-checked immediately before
every mutating call (rejects substring/lookalike hosts too), and
HMAC-SHA256 request signing. Zero real network calls in tests
(respx-mocked).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 7: `paper_trading/demo_execution.py` orchestration

**Files:**
- Create: `crypto_trading/paper_trading/demo_execution.py`
- Test: `tests/crypto_trading/paper_trading/test_demo_execution.py`

**Interfaces:**
- Consumes: `Repository` methods from Task 4, `BingXDemoTradingConnector` from Task 6, `compute_hold_hours` from Task 5, `log_event`/`new_run_id` from `crypto_trading.logging` (existing).
- Produces: `process_pending_positions(repo, connector, quantity_precision_by_symbol, run_id, now, limit=10) -> None`, `recover_stale_claims(repo, connector, quantity_precision_by_symbol, run_id, now, stale_after_seconds) -> None`, `reconcile_active_executions(repo, connector, run_id, now) -> None`, `close_time_limit_positions(repo, connector, max_position_hold_hours, run_id, now) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto_trading/paper_trading/test_demo_execution.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.paper_trading.demo_execution import (
    close_time_limit_positions,
    process_pending_positions,
    reconcile_active_executions,
    recover_stale_claims,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _open_position(repo, position_id="pos-1", opened_at=_NOW) -> Position:
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTC-USDT",
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50000"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1",
        opened_at=opened_at,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
        aggregate_type="position", aggregate_id=position_id, occurred_at=opened_at,
        run_id="seed", schema_version=1, payload={},
    )
    repo.create_position_with_event(position, event)
    return position


class _SpyConnector:
    def __init__(self, order_result=None, raise_on_place=None):
        self.calls = []
        self._order_result = order_result or {
            "orderId": "ex-1", "avgPrice": "50010",
            "stopLoss": {"orderId": "sl-1"}, "takeProfit": {"orderId": "tp-1"},
        }
        self._raise_on_place = raise_on_place
        self.order_lookup_result = None
        self.order_status_by_id = {}

    def set_leverage(self, symbol, leverage=1, side="LONG"):
        self.calls.append(("set_leverage", symbol, leverage))
        return {}

    def place_entry_order_with_sl_tp(self, **kwargs):
        self.calls.append(("place_entry_order_with_sl_tp", kwargs))
        if self._raise_on_place is not None:
            raise self._raise_on_place
        return self._order_result

    def get_order_by_client_order_id(self, symbol, client_order_id):
        self.calls.append(("get_order_by_client_order_id", symbol, client_order_id))
        return self.order_lookup_result

    def get_order_status(self, symbol, order_id):
        self.calls.append(("get_order_status", symbol, order_id))
        return self.order_status_by_id.get(order_id)

    def cancel_all_open_orders(self, symbol):
        self.calls.append(("cancel_all_open_orders", symbol))
        return {}

    def close_position_market(self, symbol, quantity, client_order_id):
        self.calls.append(("close_position_market", symbol, quantity, client_order_id))
        return {"avgPrice": "49500"}


def test_process_pending_positions_places_one_order_and_marks_active(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector()

    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW)

    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "ACTIVE"
    assert row["entry_exchange_order_id"] == "ex-1"
    assert row["sl_exchange_order_id"] == "sl-1"
    assert row["tp_exchange_order_id"] == "tp-1"
    place_calls = [c for c in connector.calls if c[0] == "place_entry_order_with_sl_tp"]
    assert len(place_calls) == 1


def test_process_pending_positions_never_places_twice_for_same_position(tmp_path):
    """Idempotency: simulates a duplicate call (e.g. two ticks racing, or a
    restart re-observing the same still-pending position)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector()

    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW)
    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW)

    place_calls = [c for c in connector.calls if c[0] == "place_entry_order_with_sl_tp"]
    assert len(place_calls) == 1


def test_process_pending_positions_marks_failed_on_connector_error(tmp_path):
    from crypto_trading.connectors.exceptions import ConnectorUnavailableError

    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector(raise_on_place=ConnectorUnavailableError("insufficient balance"))

    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW)

    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "insufficient balance" in row["last_error"]
    # never touches the PAPER position itself
    position = repo.get_position("pos-1")
    assert position.status == "OPEN_POSITION"


def test_recover_stale_claims_resubmits_when_no_order_found(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_demo_execution("pos-1", _NOW - timedelta(seconds=60))
    connector = _SpyConnector()
    connector.order_lookup_result = None  # nothing found on the exchange

    recover_stale_claims(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW, stale_after_seconds=30)

    assert repo.get_demo_execution("pos-1")["phase"] == "ACTIVE"
    place_calls = [c for c in connector.calls if c[0] == "place_entry_order_with_sl_tp"]
    assert len(place_calls) == 1


def test_recover_stale_claims_adopts_existing_order_without_resubmitting(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_demo_execution("pos-1", _NOW - timedelta(seconds=60))
    connector = _SpyConnector()
    connector.order_lookup_result = {"orderId": "ex-1", "avgPrice": "50010"}

    recover_stale_claims(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW, stale_after_seconds=30)

    assert repo.get_demo_execution("pos-1")["phase"] == "ACTIVE"
    place_calls = [c for c in connector.calls if c[0] == "place_entry_order_with_sl_tp"]
    assert len(place_calls) == 0  # adopted the existing order, never resubmitted


def test_reconcile_active_executions_detects_stop_loss_fill(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    connector = _SpyConnector()
    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", _NOW)
    connector.order_status_by_id["sl-1"] = {"status": "FILLED", "avgPrice": "49000"}

    reconcile_active_executions(repo, connector, "run-1", _NOW + timedelta(minutes=5))

    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "stop_loss"
    assert row["exchange_fill_exit"] == "49000"


def test_close_time_limit_positions_closes_and_never_touches_positions_table(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    opened_at = _NOW - timedelta(hours=25)
    _open_position(repo, opened_at=opened_at)
    connector = _SpyConnector()
    process_pending_positions(repo, connector, {"BTC-USDT": 3}, "run-1", opened_at)

    close_time_limit_positions(repo, connector, max_position_hold_hours=24, run_id="run-1", now=_NOW)

    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "TIME_LIMIT"
    assert ("cancel_all_open_orders", "BTC-USDT") in connector.calls
    position = repo.get_position("pos-1")
    assert position.status == "OPEN_POSITION"  # PAPER untouched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/paper_trading/test_demo_execution.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement the module**

```python
# crypto_trading/paper_trading/demo_execution.py
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from crypto_trading.connectors.bingx_demo_trading import (
    BingXDemoTradingConnector,
    DemoExecutionGuardError,
)
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.monitoring import compute_hold_hours
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_GUARDED_ERRORS = (ConnectorUnavailableError, DemoExecutionGuardError)


def _client_order_id(position_id: str, suffix: str) -> str:
    return f"pt{position_id[:24]}{suffix}"[:32]


def _quantity_for(position: Position, quantity_precision: int) -> str:
    raw_quantity = position.size / position.simulated_fill_entry
    quantum = Decimal(1).scaleb(-quantity_precision)
    return str(raw_quantity.quantize(quantum, rounding=ROUND_DOWN))


def _submit_entry_order(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    position: Position,
    quantity_precision_by_symbol: dict[str, int],
    run_id: str,
    now: datetime,
) -> None:
    client_order_id = _client_order_id(position.position_id, "e")
    try:
        precision = quantity_precision_by_symbol.get(position.instrument, 0)
        quantity = _quantity_for(position, precision)
        connector.set_leverage(position.instrument, leverage=1)
        result = connector.place_entry_order_with_sl_tp(
            symbol=position.instrument,
            quantity=quantity,
            client_order_id=client_order_id,
            stop_loss_price=str(position.stop_loss),
            target_price=str(position.target),
        )
        repo.update_demo_execution_submitted(
            position.position_id,
            entry_client_order_id=client_order_id,
            entry_exchange_order_id=str(result.get("orderId", "")),
            entry_quantity=quantity,
            exchange_fill_entry=str(result.get("avgPrice", "")),
            sl_exchange_order_id=str(result.get("stopLoss", {}).get("orderId", "")) or None,
            tp_exchange_order_id=str(result.get("takeProfit", {}).get("orderId", "")) or None,
            updated_at=now,
        )
        log_event(
            run_id, event="demo_order_submitted", position_id=position.position_id,
            instrument=position.instrument, exchange_order_id=str(result.get("orderId", "")),
        )
    except _GUARDED_ERRORS as exc:
        repo.mark_demo_execution_failed(position.position_id, f"{type(exc).__name__}: {exc}", now)
        log_event(
            run_id, event="demo_order_failed", position_id=position.position_id,
            error_type=type(exc).__name__, error=str(exc),
        )


def process_pending_positions(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    quantity_precision_by_symbol: dict[str, int],
    run_id: str,
    now: datetime,
    limit: int = 10,
) -> None:
    """Claim-before-place: repo.claim_demo_execution() is an atomic INSERT
    OR IGNORE keyed on position_id. A False return means another
    run/duplicate observation already claimed this position - skip it,
    never place a second order (SPEC amendment / design doc §8)."""
    for position in repo.find_positions_pending_demo_execution(limit):
        if not repo.claim_demo_execution(position.position_id, now):
            continue
        _submit_entry_order(repo, connector, position, quantity_precision_by_symbol, run_id, now)


def recover_stale_claims(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    quantity_precision_by_symbol: dict[str, int],
    run_id: str,
    now: datetime,
    stale_after_seconds: int,
) -> None:
    """Crash recovery: a row stuck in CLAIMED past the grace window means
    the process died between claiming and confirming submission. Look the
    order up by its deterministic clientOrderID BEFORE ever resubmitting -
    never a blind retry (design doc §8)."""
    stale_before = now - timedelta(seconds=stale_after_seconds)
    for row in repo.find_stale_claimed_demo_executions(stale_before):
        position = repo.get_position(row["position_id"])
        if position is None:
            continue
        client_order_id = _client_order_id(position.position_id, "e")
        existing = connector.get_order_by_client_order_id(position.instrument, client_order_id)
        if existing is not None:
            repo.update_demo_execution_submitted(
                position.position_id,
                entry_client_order_id=client_order_id,
                entry_exchange_order_id=str(existing.get("orderId", "")),
                entry_quantity=str(existing.get("origQty", "")),
                exchange_fill_entry=str(existing.get("avgPrice", "")),
                sl_exchange_order_id=None,
                tp_exchange_order_id=None,
                updated_at=now,
            )
            continue
        _submit_entry_order(repo, connector, position, quantity_precision_by_symbol, run_id, now)


def reconcile_active_executions(
    repo: Repository, connector: BingXDemoTradingConnector, run_id: str, now: datetime
) -> None:
    """Polls each ACTIVE demo_executions row's attached SL/TP order status.
    BingX's own matching engine triggers these independent of whether this
    process is running - this loop only needs to notice and record it
    afterwards, never to cause the close itself."""
    for row in repo.find_active_demo_executions():
        position = repo.get_position(row["position_id"])
        if position is None:
            continue
        sl_id, tp_id = row.get("sl_exchange_order_id"), row.get("tp_exchange_order_id")
        sl_status = connector.get_order_status(position.instrument, sl_id) if sl_id else None
        if sl_status and sl_status.get("status") == "FILLED":
            repo.close_demo_execution(
                position.position_id, "stop_loss", str(sl_status.get("avgPrice", "")), now
            )
            log_event(run_id, event="demo_position_closed", position_id=position.position_id,
                       exit_reason="stop_loss")
            continue
        tp_status = connector.get_order_status(position.instrument, tp_id) if tp_id else None
        if tp_status and tp_status.get("status") == "FILLED":
            repo.close_demo_execution(
                position.position_id, "target", str(tp_status.get("avgPrice", "")), now
            )
            log_event(run_id, event="demo_position_closed", position_id=position.position_id,
                       exit_reason="target")


def close_time_limit_positions(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    max_position_hold_hours: int,
    run_id: str,
    now: datetime,
) -> None:
    """BingX has no server-side time-based close; PAPER does. Reuses the
    exact same compute_hold_hours() PAPER's check_exit_trigger() uses, so
    the two systems never disagree on when the limit is reached (design doc
    §10). Only ever writes to demo_executions - never touches `positions`."""
    for row in repo.find_active_demo_executions():
        position = repo.get_position(row["position_id"])
        if position is None or position.status != "OPEN_POSITION":
            continue
        if compute_hold_hours(position, now) < max_position_hold_hours:
            continue
        try:
            connector.cancel_all_open_orders(position.instrument)
            client_order_id = _client_order_id(position.position_id, "x")
            result = connector.close_position_market(
                position.instrument,
                quantity=row.get("entry_quantity") or "0",
                client_order_id=client_order_id,
            )
            repo.close_demo_execution(
                position.position_id, "TIME_LIMIT", str(result.get("avgPrice", "")), now
            )
            log_event(run_id, event="demo_time_limit_closed", position_id=position.position_id)
        except _GUARDED_ERRORS as exc:
            log_event(
                run_id, event="demo_time_limit_close_failed", position_id=position.position_id,
                error_type=type(exc).__name__, error=str(exc),
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/paper_trading/test_demo_execution.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/paper_trading/demo_execution.py tests/crypto_trading/paper_trading/test_demo_execution.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add demo_execution orchestration module

process_pending_positions() (claim-before-place), recover_stale_claims()
(crash recovery via lookup-before-retry), reconcile_active_executions()
(polls exchange-reported SL/TP fills), and close_time_limit_positions()
(active TIME_LIMIT parity with PAPER, reusing compute_hold_hours()).
Every path is proven, by test, to never write to the positions table.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 8: `demo_execution_loop.py`

**Files:**
- Create: `crypto_trading/demo_execution_loop.py`
- Test: `tests/crypto_trading/test_demo_execution_loop.py`

**Interfaces:**
- Consumes: everything from Task 7, `Settings` (Task 2), `Repository`, `BingXDemoTradingConnector.get_contracts`-style precision lookup — actually reuses `BingXMarketDataConnector.get_contracts()` (existing, public, read-only) to build `quantity_precision_by_symbol`, passed in from `run.py` (Task 9) rather than fetched inside the loop every tick.
- Produces: `run_demo_execution_tick(repo, connector, quantity_precision_by_symbol, settings, now) -> None`, `run_forever(repo, connector, quantity_precision_by_symbol, settings) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/test_demo_execution_loop.py
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.config.loader import get_settings
from crypto_trading.demo_execution_loop import run_demo_execution_tick
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _SpyConnector:
    def __init__(self):
        self.calls = []

    def set_leverage(self, symbol, leverage=1, side="LONG"):
        return {}

    def place_entry_order_with_sl_tp(self, **kwargs):
        self.calls.append(kwargs)
        return {"orderId": "ex-1", "avgPrice": "50010"}

    def get_order_by_client_order_id(self, symbol, client_order_id):
        return None

    def get_order_status(self, symbol, order_id):
        return None

    def cancel_all_open_orders(self, symbol):
        return {}

    def close_position_market(self, symbol, quantity, client_order_id):
        return {"avgPrice": "0"}


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


def test_run_demo_execution_tick_processes_pending_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo)
    connector = _SpyConnector()

    run_demo_execution_tick(repo, connector, {"BTC-USDT": 3}, get_settings(), _NOW)

    row = repo.get_demo_execution("pos-1")
    assert row["phase"] == "ACTIVE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/crypto_trading/test_demo_execution_loop.py -v`
Expected: FAIL — `crypto_trading.demo_execution_loop` module doesn't exist yet.

- [ ] **Step 3: Implement the loop**

```python
# crypto_trading/demo_execution_loop.py
from __future__ import annotations

import time
from datetime import UTC, datetime

from crypto_trading.config.loader import Settings
from crypto_trading.connectors.bingx_demo_trading import BingXDemoTradingConnector
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.paper_trading.demo_execution import (
    close_time_limit_positions,
    process_pending_positions,
    reconcile_active_executions,
    recover_stale_claims,
)
from crypto_trading.storage.repository import Repository


def run_demo_execution_tick(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    quantity_precision_by_symbol: dict[str, int],
    settings: Settings,
    now: datetime,
) -> None:
    """One demo-execution tick. Same outer fail-safe principle as
    discovery_loop.run_discovery_tick()/monitoring_loop.run_monitoring_tick():
    an unexpected exception never crashes run_forever()."""
    run_id = new_run_id()
    repo.start_run(run_id, "demo_execution", now)
    try:
        process_pending_positions(repo, connector, quantity_precision_by_symbol, run_id, now)
        recover_stale_claims(
            repo, connector, quantity_precision_by_symbol, run_id, now,
            stale_after_seconds=settings.demo_execution.claim_stale_after_seconds,
        )
        reconcile_active_executions(repo, connector, run_id, now)
        close_time_limit_positions(
            repo, connector, settings.risk_limits.max_position_hold_hours, run_id, now
        )
        repo.complete_run(run_id, datetime.now(UTC), "ok", [])
    except Exception as exc:
        log_event(
            run_id, event="demo_execution_tick_failed",
            error_type=type(exc).__name__, error=str(exc),
        )
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])


def run_forever(
    repo: Repository,
    connector: BingXDemoTradingConnector,
    quantity_precision_by_symbol: dict[str, int],
    settings: Settings,
) -> None:
    while True:
        run_demo_execution_tick(
            repo, connector, quantity_precision_by_symbol, settings, datetime.now(UTC)
        )
        time.sleep(settings.demo_execution.check_interval_seconds)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/crypto_trading/test_demo_execution_loop.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/demo_execution_loop.py tests/crypto_trading/test_demo_execution_loop.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add demo_execution_loop tick/run_forever

Same fail-safe-never-crashes-run_forever shape as discovery_loop and
monitoring_loop.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 9: Wire into `run.py` behind the arm flag

**Files:**
- Modify: `crypto_trading/run.py`

**Interfaces:**
- Consumes: `is_demo_execution_enabled()` (Task 2), `BingXDemoTradingConnector` (Task 6), `demo_execution_loop.run_forever` (Task 8), `BingXMarketDataConnector.get_contracts()` (existing, for `quantity_precision_by_symbol`).
- Produces: an optional 6th daemon thread, same shape as the existing dashboard/Telegram opt-in threads.

- [ ] **Step 1: Add the builder function**

In `crypto_trading/run.py`, add near the other `build_*_from_env()` functions:

```python
def build_demo_trading_connector_from_env() -> BingXDemoTradingConnector | None:
    """Opt-in, same pattern as build_notifier_from_env(): if the dedicated
    demo credentials aren't set, the thread simply doesn't start - never a
    fail-fast requirement like ANTHROPIC_API_KEY, since PAPER trading works
    completely without it. Deliberately reads ONLY the dedicated
    CRYPTO_TRADING_BINGX_DEMO_API_KEY/_SECRET names, never the generic
    BINGX_API_KEY/BINGX_API_SECRET also present in .env (2026-09-04 design
    decision - a generic name risks accidental reuse by a future live-
    account integration)."""
    api_key = os.environ.get("CRYPTO_TRADING_BINGX_DEMO_API_KEY")
    api_secret = os.environ.get("CRYPTO_TRADING_BINGX_DEMO_API_SECRET")
    if not api_key or not api_secret:
        return None
    return BingXDemoTradingConnector(api_key=api_key, api_secret=api_secret)
```

- [ ] **Step 2: Add the thread-runner function**

```python
def _run_demo_execution_forever(
    market_data_connector: BingXMarketDataConnector,
    demo_connector: BingXDemoTradingConnector,
    settings: Settings,
) -> None:
    """Same thread-bound-connection fix as the other _run_*_forever()
    functions above. quantity_precision_by_symbol is built ONCE here from
    the existing, read-only get_contracts() - not re-fetched every tick."""
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    contracts = market_data_connector.get_contracts()
    quantity_precision_by_symbol = {
        c["symbol"]: int(c.get("quantityPrecision", 0)) for c in contracts
    }
    demo_execution_loop.run_forever(repo, demo_connector, quantity_precision_by_symbol, settings)
```

- [ ] **Step 3: Add the import and wire the thread into `main()`**

Add to the imports at the top:
```python
from crypto_trading import demo_execution_loop
from crypto_trading.connectors.bingx_demo_trading import BingXDemoTradingConnector
from crypto_trading.config.loader import is_demo_execution_enabled
```

In `main()`, after the dashboard-thread `if`/`else` block, add:

```python
    if is_demo_execution_enabled():
        demo_connector = build_demo_trading_connector_from_env()
        if demo_connector is not None:
            threads.append(
                threading.Thread(
                    target=_run_demo_execution_forever,
                    args=(connector, demo_connector, settings),
                    daemon=True,
                )
            )
        else:
            log_event(
                "startup", event="demo_execution_disabled",
                reason="CRYPTO_TRADING_BINGX_DEMO_API_KEY/_SECRET missing",
            )
    else:
        log_event(
            "startup", event="demo_execution_disabled",
            reason="CRYPTO_TRADING_DEMO_EXECUTION_ENABLED not set",
        )
```

- [ ] **Step 4: Manual smoke check (no automated test — this only wires existing, already-tested pieces together)**

Run: `uv run python -c "import crypto_trading.run"` — confirms the module still imports cleanly with no syntax/import errors.
Expected: no output, exit code 0.

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/run.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): wire demo execution as a sixth, opt-in daemon thread

Default off (CRYPTO_TRADING_DEMO_EXECUTION_ENABLED unset). Reads only
the dedicated CRYPTO_TRADING_BINGX_DEMO_API_KEY/_SECRET env vars,
never the generic BINGX_API_KEY/BINGX_API_SECRET.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 10: Comparison report extension

**Files:**
- Modify: `crypto_trading/performance/paper_track_report.py`
- Test: `tests/crypto_trading/performance/test_paper_track_report.py` (extend existing, or create if none — check first)

**Interfaces:**
- Consumes: `repo.find_all_positions`, `repo.get_demo_execution` (Task 4).
- Produces: a new `"demo_comparison"` key in `build_report()`'s returned dict — a list of `{position_id, instrument, paper_exit_reason, demo_exit_reason, paper_fill_entry, demo_fill_entry, paper_fill_exit, demo_fill_exit, entry_divergence_usdt, exit_divergence_usdt}` rows for every closed PAPER position that also has a `demo_executions` row.

- [ ] **Step 1: Check for an existing test file**

Run: `find tests/crypto_trading/performance -iname "*paper_track_report*"`

If none exists, create `tests/crypto_trading/performance/test_paper_track_report.py`; if one exists, add the test below to it.

- [ ] **Step 2: Write the failing test**

```python
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.performance.paper_track_report import build_report
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _NoopConnector:
    def get_ticker(self, symbol):
        return {"lastPrice": "0"}


def test_build_report_includes_demo_comparison_for_matched_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = Position(
        position_id="pos-1", candidate_id="pos-1", instrument="BTC-USDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1", opened_at=_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(event_id="POSITION_OPENED:pos-1", event_type="POSITION_OPENED",
              aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
              run_id="seed", schema_version=1, payload={}),
    )
    repo.close_position_with_event(
        position_id="pos-1", theoretical_exit=Decimal("52000"),
        simulated_fill_exit=Decimal("51975"), exit_reason="target",
        fees=Decimal("0.4"), funding=Decimal("0"), closed_at=_NOW,
        event=Event(event_id="POSITION_CLOSED:pos-1", event_type="POSITION_CLOSED",
                    aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
                    run_id="seed", schema_version=1, payload={}),
    )
    repo.claim_demo_execution("pos-1", _NOW)
    repo.update_demo_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.02", "50040", "sl-1", "tp-1", _NOW
    )
    repo.close_demo_execution("pos-1", "target", "51980", _NOW)

    report = build_report(repo, _NoopConnector(), log_glob="nonexistent-*.log")

    comparison = report["demo_comparison"]
    assert len(comparison) == 1
    row = comparison[0]
    assert row["position_id"] == "pos-1"
    assert row["paper_exit_reason"] == "target"
    assert row["demo_exit_reason"] == "target"
    assert row["paper_fill_exit"] == "51975"
    assert row["demo_fill_exit"] == "51980"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/crypto_trading/performance/test_paper_track_report.py::test_build_report_includes_demo_comparison_for_matched_positions -v`
Expected: FAIL — `"demo_comparison"` key missing.

- [ ] **Step 4: Add the comparison section**

In `crypto_trading/performance/paper_track_report.py`, inside `build_report()`, after the `closed_position_rows` block and before the `return {...}` statement, add:

```python
    demo_comparison_rows = []
    for p in all_positions:
        demo_row = repo.get_demo_execution(p.position_id)
        if demo_row is None or demo_row["phase"] != "CLOSED":
            continue
        entry_divergence = (
            Decimal(demo_row["exchange_fill_entry"]) - p.simulated_fill_entry
            if demo_row["exchange_fill_entry"]
            else None
        )
        exit_divergence = (
            Decimal(demo_row["exchange_fill_exit"]) - (p.simulated_fill_exit or Decimal("0"))
            if demo_row["exchange_fill_exit"] and p.simulated_fill_exit is not None
            else None
        )
        demo_comparison_rows.append(
            {
                "position_id": p.position_id,
                "instrument": p.instrument,
                "paper_exit_reason": p.exit_reason,
                "demo_exit_reason": demo_row["exit_reason"],
                "paper_fill_entry": str(p.simulated_fill_entry),
                "demo_fill_entry": demo_row["exchange_fill_entry"],
                "paper_fill_exit": str(p.simulated_fill_exit) if p.simulated_fill_exit else None,
                "demo_fill_exit": demo_row["exchange_fill_exit"],
                "entry_divergence_usdt": str(entry_divergence) if entry_divergence is not None else None,
                "exit_divergence_usdt": str(exit_divergence) if exit_divergence is not None else None,
            }
        )
```

Then add `"demo_comparison": demo_comparison_rows,` as a new key in the function's final `return {...}` dict.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/crypto_trading/performance/test_paper_track_report.py -v`
Expected: PASS (including any pre-existing tests in that file)

- [ ] **Step 6: Commit**

```bash
git add crypto_trading/performance/paper_track_report.py tests/crypto_trading/performance/test_paper_track_report.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add PAPER vs BingX Demo comparison to track report

Purely additive, read-only join on position_id - no change to any
existing report field or write path.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 11: Full suite regression + FINAL GATE (manual, not auto-executed)

**Files:** none new — this task is verification-only.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -v`
Expected: all tests pass, including every test added in Tasks 1-10. Zero real network calls (no `live`-marked test runs by default per `pyproject.toml`'s existing marker config).

- [ ] **Step 2: STOP — do not proceed past this point automatically**

Everything above this line may be executed autonomously. **This step and Step 3 require the user's explicit, separate go-ahead in a live conversation before running** — this is the first point in the whole plan where a call to the real, authenticated `open-api-vst.bingx.com` endpoint (using the user's actual `CRYPTO_TRADING_BINGX_DEMO_API_KEY/_SECRET`) happens. No prior task calls this endpoint for real; everything up to here is respx-mocked.

Before asking the user to proceed, re-verify by inspection (not by running anything):
- `BingXDemoTradingConnector._base_url` is exactly `"https://open-api-vst.bingx.com"` — read the file, confirm the literal.
- `CRYPTO_TRADING_DEMO_EXECUTION_ENABLED` is unset in the current shell/`.env` (so the automated `run.py` thread does not start on its own the next time the bot runs) — confirm with `env | grep CRYPTO_TRADING_DEMO_EXECUTION_ENABLED` (bash) and expect no output.
- The dedicated `CRYPTO_TRADING_BINGX_DEMO_API_KEY/_SECRET` values in `.env` are in fact the user's BingX **Demo** account keys (already confirmed by the user during design, but re-state this assumption explicitly to the user before the live call).

- [ ] **Step 3: Manual, user-approved live verification (only after explicit go-ahead)**

A minimal standalone script (not part of `run.py`, not run automatically):

```python
# scratch verification script - run manually, once, with the user watching
import os
from datetime import UTC, datetime

from dotenv import load_dotenv

from crypto_trading.connectors.bingx_demo_trading import BingXDemoTradingConnector

load_dotenv()
connector = BingXDemoTradingConnector(
    api_key=os.environ["CRYPTO_TRADING_BINGX_DEMO_API_KEY"],
    api_secret=os.environ["CRYPTO_TRADING_BINGX_DEMO_API_SECRET"],
)
assert connector._base_url == "https://open-api-vst.bingx.com"

# Confirm precise BingX response field names before trusting Task 7's
# .get("orderId")/.get("avgPrice")/.get("stopLoss", {}).get("orderId")
# assumptions - adjust demo_execution.py if the real shape differs.
result = connector.place_entry_order_with_sl_tp(
    symbol="BTC-USDT",
    quantity="0.001",
    client_order_id=f"verify{int(datetime.now(UTC).timestamp())}",
    stop_loss_price="1000",   # deliberately far away - this is a verification
    target_price="1000000",  # order, not a real trade decision
)
print(result)
```

Verify manually in the BingX web UI (Demo mode) that: (a) a position actually opened on the Demo/VST account, (b) the attached SL/TP orders are visible, (c) closing it manually in the UI is reflected correctly if `reconcile_active_executions` is pointed at it afterwards. Only after this manual proof should `CRYPTO_TRADING_DEMO_EXECUTION_ENABLED=1` be set to let the automated thread run continuously.

**Do not commit the verification script** — it's a one-time, human-supervised check, not part of the codebase.
