# Profit Protection Experiment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an isolated, PAPER-only, forward-only shadow experiment that
tests whether moving a position's stop-loss to break-even at +1.0%/+1.5%
unrealized profit improves outcomes vs. the current ("baseline") exit
logic — without ever touching LIVE, Demo, Guardian, Gate, Risk Agent, AI
roles, sizing, screener, discovery, or baseline exit behavior.

**Architecture:** A new module (`profit_protection_experiment.py`) runs a
tick-based state machine, hooked into the existing `monitoring_loop.py`
tick strictly *after* the real close logic runs, wrapped in its own
`try/except` so it can never affect or crash real position closing. It
reads already-open `Position` rows and the same per-tick candle data
`run_monitoring_tick` already fetches, and writes only to its own new
SQLite table. A separate, read-only report script computes all
comparison metrics.

**Tech Stack:** Python 3.11+, SQLite (existing `storage/db.py`/
`repository.py`), pydantic (config), pytest (existing test suite).

**Spec:** `docs/superpowers/specs/2026-09-11-profit-protection-experiment-design.md`
(commit `5f3b7a9`, approved). Read that spec's §1–§10 before starting —
this plan implements it exactly, with three corrections spelled out
below (the spec itself is not edited).

## Corrections to the approved spec (decided during planning, not asked back to the user — see conversation for full rationale)

- **C1 (resolves spec review finding M1):** No `profit_protection_activation`
  table. The one-time activation watermark is a `schema_meta` row (key
  `'profit_protection_activated_at'`), exactly mirroring the existing
  `recovery_sweep_activated_at` pattern in `storage/repository.py`
  (`get_recovery_sweep_activated_at`/`set_recovery_sweep_activated_at_if_missing`).
- **C2 (resolves M2):** Every experiment tick runs, in strict order: (1)
  seed any missing shadow rows for positions in this tick's
  `open_positions` whose instrument has a `price_lookup` entry this tick,
  (2) advance every currently-`OPEN` shadow row (including any just
  seeded in step 1) against this tick's `price_lookup`, (3) backfill the
  baseline outcome for any position `close_triggered_positions` closed
  this same tick. This guarantees a shadow seeded on the same tick its
  underlying position's first (and possibly only) candle arrives is
  never left stranded un-advanced.
- **C3 (resolves M3):** New required file `config/profit_protection_experiment.yaml`
  and a new line in `config/loader.py::get_settings()` — both are
  explicit deliverables in Task 1 below.
- **Repository method shapes:** the spec's §4.3 method list is
  illustrative; this plan uses fully explicit, typed methods (no
  `**kwargs`) for every write, matching every other method already in
  `storage/repository.py` (`claim_demo_execution`, `close_position_with_event`,
  etc. — none of them take a generic kwargs dict).
- **PnL-parity data sourcing:** the shadow table does not store
  `size`/`simulated_fill_entry`/live `funding_rate` (per spec — these
  belong to the real position or the live tick, not duplicated). At
  shadow-close time, `size`/`simulated_fill_entry` are read via a single
  `repo.get_position(position_id)` call (read-only, always available from
  the moment the real position opens — these two fields never change
  before close) and `funding_rate` is the same tick's own
  `price_lookup[instrument][3]`, exactly like baseline's own
  `close_triggered_positions` already sources it.

## Global Constraints

- **LIVE, Demo, Guardian, Gate, Risk Agent, AI roles, sizing, leverage, AI
  budget, Discovery/capacity logic, and baseline exit logic
  (`paper_trading/position_closing.py`, `paper_trading/monitoring.py`) are
  never modified.** (Spec G1–G4.)
- **No historical position before the activation watermark is ever
  seeded.** `position.opened_at >= activated_at`, strictly. (Spec G6.)
- **No look-ahead.** Every tick only ever uses that tick's own
  `price_lookup` entry. Same-candle stop-vs-threshold ambiguity always
  resolves as "the stop fired first" (checked against the *not-yet-updated*
  SL). (Spec G7/G8.)
- **+1.0% and +1.5% are a frozen module constant, never a config field.**
  (Spec G9.)
- **The 0.3% "approx breakeven" report bucket is reporting-only** — it
  never feeds back into any simulated price, exit, or dollar figure.
  (Spec G9 addendum, §7.2.)
- **Existing PAPER positions are never retroactively changed.** (Spec G5.)
- Every new SQL write goes through the `Repository` Protocol + a typed
  `SQLiteRepository` method — no ad-hoc SQL anywhere else.
- Every new file/edit is additive; the only edit to a currently-shipping
  production file is the one described in Task 10.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `crypto_trading/config/loader.py` | Modify | Add `ProfitProtectionExperimentConfig`, wire into `Settings`/`get_settings()` |
| `crypto_trading/config/profit_protection_experiment.yaml` | Create | The only tunable: `enabled: false` |
| `crypto_trading/storage/db.py` | Modify | Add `profit_protection_shadow_positions` table + 2 indexes to `_SCHEMA` |
| `crypto_trading/storage/repository.py` | Modify | Add Protocol methods + `SQLiteRepository` implementations (watermark, seed, read, tick-update, close, backfill) |
| `crypto_trading/paper_trading/profit_protection_experiment.py` | Create | Frozen thresholds constant, Guardian-staleness duplicate, seeding, state machine (`advance_shadow`), closing/PnL parity, orchestration (`run_profit_protection_experiment_tick`) |
| `crypto_trading/monitoring_loop.py` | Modify | One hook: capture `open_positions` once, call the orchestration function after `close_triggered_positions`, wrapped in its own `try/except` |
| `crypto_trading/performance/profit_protection_report.py` | Create | Read-only report: sample sizes, classification, improved/worsened, conversion ratio, chronological split |
| `tests/crypto_trading/config/test_profit_protection_experiment_config.py` | Create | Config wiring tests (mirrors `test_guardian_config.py`) |
| `tests/crypto_trading/storage/test_repository_profit_protection.py` | Create | New repository method tests (mirrors `test_repository_recovery_sweep.py`) |
| `tests/crypto_trading/paper_trading/test_profit_protection_experiment.py` | Create | State machine + orchestration tests |
| `tests/crypto_trading/test_monitoring_loop.py` | Modify | G10 isolation test |
| `tests/crypto_trading/performance/test_profit_protection_report.py` | Create | Report tests |

---

## Task 1: Config wiring

**Files:**
- Create: `crypto_trading/config/profit_protection_experiment.yaml`
- Modify: `crypto_trading/config/loader.py`
- Test: `tests/crypto_trading/config/test_profit_protection_experiment_config.py` (new file — this
  codebase splits config tests per feature, e.g. `test_guardian_config.py`,
  `test_demo_execution_config.py`, never a shared `test_loader.py` for
  feature-specific config; mirror that convention, do not add to
  `test_loader.py`)

**Interfaces:**
- Produces: `ProfitProtectionExperimentConfig(enabled: bool)`, `Settings.profit_protection_experiment`

- [ ] **Step 1: Write the failing test**

Create `tests/crypto_trading/config/test_profit_protection_experiment_config.py`
(mirroring `test_guardian_config.py`'s exact shape — a bare `get_settings()`
call against the real checked-in `config/*.yaml` files, no synthetic
`Settings()` construction):

```python
from crypto_trading.config.loader import ProfitProtectionExperimentConfig, get_settings


def test_profit_protection_experiment_config_has_only_an_enabled_field():
    config = ProfitProtectionExperimentConfig()
    assert config.enabled is False
    assert set(ProfitProtectionExperimentConfig.model_fields.keys()) == {"enabled"}


def test_settings_load_profit_protection_experiment_defaults():
    settings = get_settings()
    assert settings.profit_protection_experiment.enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/config/test_profit_protection_experiment_config.py -k profit_protection -v`
Expected: FAIL with `ImportError`/`AttributeError` (class doesn't exist yet).

- [ ] **Step 3: Create the YAML file**

`crypto_trading/config/profit_protection_experiment.yaml`:

```yaml
# Profit Protection PAPER shadow experiment (2026-09-11) - see
# docs/superpowers/specs/2026-09-11-profit-protection-experiment-design.md.
# The only tunable is whether the experiment runs at all, exactly like
# guardian.yaml's assisted_exit_enabled - no separate thread, no env-var
# arm flag (this hooks into the already-running monitoring tick). The two
# tested thresholds (+1.0%/+1.5%) are NOT configurable here - they are a
# frozen constant, FROZEN_THRESHOLDS_PCT, in
# paper_trading/profit_protection_experiment.py (spec G9).
enabled: false
```

- [ ] **Step 4: Add the config class and wire it into Settings**

In `crypto_trading/config/loader.py`, add after `LiveExecutionConfig`:

```python
class ProfitProtectionExperimentConfig(BaseModel):
    # Profit Protection PAPER shadow experiment (2026-09-11) - see
    # docs/superpowers/specs/2026-09-11-profit-protection-experiment-design.md.
    # `enabled` is the ONLY field: turns the experiment's seed/tick/backfill
    # logic on or off inside monitoring_loop.py's existing tick, same shape
    # as GuardianConfig.assisted_exit_enabled - no separate thread. The two
    # tested thresholds are NOT here (spec G9): they are the frozen
    # FROZEN_THRESHOLDS_PCT constant in
    # paper_trading/profit_protection_experiment.py, never config-driven.
    enabled: bool = False
```

In the `Settings` class, add:

```python
    profit_protection_experiment: ProfitProtectionExperimentConfig = Field(
        default_factory=ProfitProtectionExperimentConfig
    )
```

In `get_settings()`, add to the `Settings(...)` call:

```python
        profit_protection_experiment=_load_yaml_model(
            _CONFIG_DIR / "profit_protection_experiment.yaml", ProfitProtectionExperimentConfig
        ),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/crypto_trading/config/test_profit_protection_experiment_config.py -k profit_protection -v`
Expected: PASS

- [ ] **Step 6: Run the full config test directory to confirm no regression**

Run: `pytest tests/crypto_trading/config/ -v`
Expected: all PASS (every existing config test file, e.g. `test_guardian_config.py`, still green)

- [ ] **Step 7: Commit**

```bash
git add crypto_trading/config/loader.py crypto_trading/config/profit_protection_experiment.yaml tests/crypto_trading/config/test_profit_protection_experiment_config.py
git commit -m "feat(crypto-trading): add Profit Protection experiment config (opt-in, no thresholds)"
```

---

## Task 2: Storage schema + activation watermark

**Files:**
- Modify: `crypto_trading/storage/db.py`
- Modify: `crypto_trading/storage/repository.py`
- Test: `tests/crypto_trading/storage/test_repository_profit_protection.py` (new file — this
  codebase splits repository tests per feature, e.g.
  `test_repository_recovery_sweep.py`, `test_repository_guardian.py`;
  mirror that convention with one new file covering every Profit
  Protection repository method added across Tasks 2-4)

**Interfaces:**
- Produces: table `profit_protection_shadow_positions`; `Repository.get_profit_protection_activated_at() -> datetime | None`; `Repository.set_profit_protection_activated_at_if_missing(activated_at: datetime) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/crypto_trading/storage/test_repository_profit_protection.py`:

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def test_profit_protection_activation_watermark_is_none_before_first_activation(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert repo.get_profit_protection_activated_at() is None


def test_profit_protection_activation_watermark_set_once_wins(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    first = datetime(2026, 9, 11, 10, 0, tzinfo=UTC)
    second = datetime(2026, 9, 11, 11, 0, tzinfo=UTC)

    first_call = repo.set_profit_protection_activated_at_if_missing(first)
    second_call = repo.set_profit_protection_activated_at_if_missing(second)

    assert first_call is True
    assert second_call is False
    assert repo.get_profit_protection_activated_at() == first


def test_profit_protection_shadow_positions_table_exists(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    columns = {
        row["name"]
        for row in repo._conn.execute(
            "PRAGMA table_info(profit_protection_shadow_positions)"
        ).fetchall()
    }
    assert columns == {
        "shadow_id", "position_id", "instrument", "threshold_pct", "entry_price",
        "original_stop_loss", "target", "threshold_price", "opened_at", "status",
        "threshold_reached", "threshold_reached_at", "breakeven_stop_loss", "mfe", "mae",
        "exit_reason", "theoretical_exit", "simulated_fill_exit", "fees", "funding",
        "closed_at", "shadow_realized_pnl", "hypothetical_baseline_exit_reason",
        "hypothetical_baseline_pnl", "pnl_difference", "created_at", "updated_at",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/storage/test_repository_profit_protection.py -v`
Expected: FAIL (`AttributeError`/`sqlite3.OperationalError: no such table`)

- [ ] **Step 3: Add the table to the schema**

In `crypto_trading/storage/db.py`, add inside the `_SCHEMA` string, right after
the `guardian_observations` block (before the closing `"""`):

```sql

-- Profit Protection PAPER shadow experiment (2026-09-11) - strictly
-- additive shadow simulation of an already-open PAPER position, see
-- docs/superpowers/specs/2026-09-11-profit-protection-experiment-design.md.
-- NEVER joined-into or written-from position_opening.py/position_closing.py,
-- never read by Gate/Risk/Guardian/LIVE. One row per (position_id,
-- threshold_pct) - at most 2 rows per real position (FROZEN_THRESHOLDS_PCT
-- always has exactly two values). The one-time activation watermark for
-- this feature lives in schema_meta (key 'profit_protection_activated_at'),
-- same pattern as recovery_sweep_activated_at - deliberately no separate
-- table for that.
CREATE TABLE IF NOT EXISTS profit_protection_shadow_positions (
    shadow_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    threshold_pct TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    original_stop_loss TEXT NOT NULL,
    target TEXT NOT NULL,
    threshold_price TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    status TEXT NOT NULL,
    threshold_reached INTEGER NOT NULL DEFAULT 0,
    threshold_reached_at TEXT,
    breakeven_stop_loss TEXT,
    mfe TEXT NOT NULL DEFAULT '0',
    mae TEXT NOT NULL DEFAULT '0',
    exit_reason TEXT,
    theoretical_exit TEXT,
    simulated_fill_exit TEXT,
    fees TEXT,
    funding TEXT,
    closed_at TEXT,
    shadow_realized_pnl TEXT,
    hypothetical_baseline_exit_reason TEXT,
    hypothetical_baseline_pnl TEXT,
    pnl_difference TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pp_shadow_position
    ON profit_protection_shadow_positions(position_id);
CREATE INDEX IF NOT EXISTS idx_pp_shadow_status
    ON profit_protection_shadow_positions(status);
```

- [ ] **Step 4: Add the watermark methods to the Repository Protocol**

In `crypto_trading/storage/repository.py`, add to the `Repository(Protocol)` class
(near `get_recovery_sweep_activated_at`/`set_recovery_sweep_activated_at_if_missing`
if those are declared there, otherwise anywhere in the Protocol body):

```python
    def get_profit_protection_activated_at(self) -> datetime | None: ...
    def set_profit_protection_activated_at_if_missing(self, activated_at: datetime) -> bool: ...
```

- [ ] **Step 5: Implement the watermark methods on SQLiteRepository**

Add near `get_recovery_sweep_activated_at`/`set_recovery_sweep_activated_at_if_missing`:

```python
    def get_profit_protection_activated_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'profit_protection_activated_at'"
        ).fetchone()
        return datetime.fromisoformat(row["value"]) if row is not None else None

    def set_profit_protection_activated_at_if_missing(self, activated_at: datetime) -> bool:
        """One-time activation watermark (spec G6, plan correction C1) -
        same INSERT OR IGNORE first-writer-wins idempotency as
        set_recovery_sweep_activated_at_if_missing(). Whichever timestamp is
        set FIRST is authoritative forever: any position opened before it
        is permanently excluded from the experiment."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta (key, value) VALUES "
            "('profit_protection_activated_at', ?)",
            (activated_at.isoformat(),),
        )
        self._conn.commit()
        return cur.rowcount > 0
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/storage/test_repository_profit_protection.py -k profit_protection -v`
Expected: PASS

- [ ] **Step 7: Run the full storage test suite**

Run: `pytest tests/crypto_trading/storage/ -v`
Expected: all PASS (zero regressions in existing storage tests)

- [ ] **Step 8: Commit**

```bash
git add crypto_trading/storage/db.py crypto_trading/storage/repository.py tests/crypto_trading/storage/test_repository_profit_protection.py
git commit -m "feat(crypto-trading): add profit_protection_shadow_positions table and activation watermark"
```

---

## Task 3: Repository — seed and read methods

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Test: `tests/crypto_trading/storage/test_repository_profit_protection.py`

**Interfaces:**
- Consumes: table from Task 2
- Produces: `Repository.seed_profit_protection_shadow(...) -> bool`, `Repository.get_profit_protection_shadow(shadow_id: str) -> dict | None`, `Repository.find_open_profit_protection_shadows() -> list[dict]`, `Repository.find_all_profit_protection_shadows() -> list[dict]`

- [ ] **Step 1: Write the failing tests**

```python
def _seed_shadow_kwargs(**overrides) -> dict:
    defaults = dict(
        shadow_id="pos-1:0.010", position_id="pos-1", instrument="BTCUSDT",
        threshold_pct="0.010", entry_price=Decimal("50000"),
        original_stop_loss=Decimal("49000"), target=Decimal("52000"),
        threshold_price=Decimal("50500"), opened_at=_NOW, created_at=_NOW,
    )
    defaults.update(overrides)
    return defaults


def test_seed_profit_protection_shadow_creates_a_row_with_open_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    created = repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    assert created is True
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "OPEN"
    assert row["threshold_reached"] == 0
    assert row["entry_price"] == "50000"


def test_seed_profit_protection_shadow_is_idempotent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    first = repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    second = repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    assert first is True
    assert second is False


def test_find_open_profit_protection_shadows_excludes_closed_rows(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs(shadow_id="a", position_id="a"))
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs(shadow_id="b", position_id="b"))
    repo.close_profit_protection_shadow(
        shadow_id="a", exit_reason="target", theoretical_exit=Decimal("52000"),
        simulated_fill_exit=Decimal("51974"), fees=Decimal("2"), funding=Decimal("0"),
        closed_at=_NOW, shadow_realized_pnl=Decimal("100"), updated_at=_NOW,
    )
    open_rows = repo.find_open_profit_protection_shadows()
    assert [r["shadow_id"] for r in open_rows] == ["b"]


def test_find_all_profit_protection_shadows_returns_open_and_closed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    assert len(repo.find_all_profit_protection_shadows()) == 1
```

(`_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)` module-level constant;
add if the file doesn't already have one under that name — otherwise reuse
the existing one.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/storage/test_repository_profit_protection.py -k "seed_profit_protection or find_open_profit_protection or find_all_profit_protection" -v`
Expected: FAIL (`AttributeError`, methods don't exist)

- [ ] **Step 3: Add Protocol declarations**

```python
    def seed_profit_protection_shadow(
        self,
        shadow_id: str,
        position_id: str,
        instrument: str,
        threshold_pct: str,
        entry_price: Decimal,
        original_stop_loss: Decimal,
        target: Decimal,
        threshold_price: Decimal,
        opened_at: datetime,
        created_at: datetime,
    ) -> bool: ...
    def get_profit_protection_shadow(self, shadow_id: str) -> dict | None: ...
    def find_open_profit_protection_shadows(self) -> list[dict]: ...
    def find_all_profit_protection_shadows(self) -> list[dict]: ...
```

- [ ] **Step 4: Implement on SQLiteRepository**

```python
    def seed_profit_protection_shadow(
        self,
        shadow_id: str,
        position_id: str,
        instrument: str,
        threshold_pct: str,
        entry_price: Decimal,
        original_stop_loss: Decimal,
        target: Decimal,
        threshold_price: Decimal,
        opened_at: datetime,
        created_at: datetime,
    ) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO profit_protection_shadow_positions "
            "(shadow_id, position_id, instrument, threshold_pct, entry_price, "
            "original_stop_loss, target, threshold_price, opened_at, status, "
            "threshold_reached, mfe, mae, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 0, '0', '0', ?, ?)",
            (
                shadow_id, position_id, instrument, threshold_pct, str(entry_price),
                str(original_stop_loss), str(target), str(threshold_price),
                opened_at.isoformat(), created_at.isoformat(), created_at.isoformat(),
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_profit_protection_shadow(self, shadow_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM profit_protection_shadow_positions WHERE shadow_id = ?",
            (shadow_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def find_open_profit_protection_shadows(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM profit_protection_shadow_positions WHERE status = 'OPEN'"
        ).fetchall()
        return [dict(row) for row in rows]

    def find_all_profit_protection_shadows(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM profit_protection_shadow_positions"
        ).fetchall()
        return [dict(row) for row in rows]
```

(`close_profit_protection_shadow`, used by Step 1's third test, is
implemented in Task 4 — write Task 4's Step 4 implementation before running
this task's tests if working strictly task-by-task in one sitting; otherwise
that one test will fail until Task 4 lands. If executing tasks in strict
isolation via subagent-driven-development, move that one test to Task 4's
test list instead.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/storage/test_repository_profit_protection.py -k "seed_profit_protection or find_open_profit_protection or find_all_profit_protection" -v`
Expected: PASS (except the closed-row test, which depends on Task 4 — see note above)

- [ ] **Step 6: Commit**

```bash
git add crypto_trading/storage/repository.py tests/crypto_trading/storage/test_repository_profit_protection.py
git commit -m "feat(crypto-trading): add profit protection shadow seed/read repository methods"
```

---

## Task 4: Repository — tick-update, close, backfill

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Test: `tests/crypto_trading/storage/test_repository_profit_protection.py`

**Interfaces:**
- Consumes: Task 3's `seed_profit_protection_shadow`/`get_profit_protection_shadow`
- Produces: `Repository.record_profit_protection_tick(...)`, `Repository.activate_profit_protection_breakeven(...)`, `Repository.close_profit_protection_shadow(...)`, `Repository.backfill_profit_protection_baseline_outcome(...)`

- [ ] **Step 1: Write the failing tests**

```python
def test_record_profit_protection_tick_updates_mfe_and_mae(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    repo.record_profit_protection_tick("pos-1:0.010", Decimal("600"), Decimal("-100"), _NOW)
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["mfe"] == "600"
    assert row["mae"] == "-100"


def test_activate_profit_protection_breakeven_sets_fields_once(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    repo.activate_profit_protection_breakeven("pos-1:0.010", Decimal("50000"), _NOW, _NOW)
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["threshold_reached"] == 1
    assert row["breakeven_stop_loss"] == "50000"


def test_activate_profit_protection_breakeven_is_a_no_op_once_already_active(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    repo.activate_profit_protection_breakeven("pos-1:0.010", Decimal("50000"), _NOW, _NOW)
    later = _NOW + timedelta(minutes=5)
    repo.activate_profit_protection_breakeven("pos-1:0.010", Decimal("99999"), later, later)
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["breakeven_stop_loss"] == "50000"  # never overwritten


def test_close_profit_protection_shadow_sets_terminal_fields(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    repo.close_profit_protection_shadow(
        shadow_id="pos-1:0.010", exit_reason="stop_loss",
        theoretical_exit=Decimal("49000"), simulated_fill_exit=Decimal("48975.5"),
        fees=Decimal("2"), funding=Decimal("0"), closed_at=_NOW,
        shadow_realized_pnl=Decimal("-125"), updated_at=_NOW,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "stop_loss"
    assert row["shadow_realized_pnl"] == "-125"
    assert row["pnl_difference"] is None  # baseline not yet known


def test_close_profit_protection_shadow_computes_pnl_difference_if_baseline_already_known(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    repo.backfill_profit_protection_baseline_outcome("pos-1", "stop_loss", Decimal("-500"), _NOW)
    repo.close_profit_protection_shadow(
        shadow_id="pos-1:0.010", exit_reason="stop_loss",
        theoretical_exit=Decimal("49000"), simulated_fill_exit=Decimal("48975.5"),
        fees=Decimal("2"), funding=Decimal("0"), closed_at=_NOW,
        shadow_realized_pnl=Decimal("-125"), updated_at=_NOW,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["pnl_difference"] == "375"  # -125 - (-500)


def test_backfill_profit_protection_baseline_outcome_updates_all_thresholds_for_a_position(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs(shadow_id="pos-1:0.010", threshold_pct="0.010"))
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs(shadow_id="pos-1:0.015", threshold_pct="0.015"))
    repo.backfill_profit_protection_baseline_outcome("pos-1", "target", Decimal("1000"), _NOW)
    for shadow_id in ("pos-1:0.010", "pos-1:0.015"):
        row = repo.get_profit_protection_shadow(shadow_id)
        assert row["hypothetical_baseline_exit_reason"] == "target"
        assert row["hypothetical_baseline_pnl"] == "1000"


def test_backfill_computes_pnl_difference_if_shadow_already_closed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.seed_profit_protection_shadow(**_seed_shadow_kwargs())
    repo.close_profit_protection_shadow(
        shadow_id="pos-1:0.010", exit_reason="stop_loss",
        theoretical_exit=Decimal("49000"), simulated_fill_exit=Decimal("48975.5"),
        fees=Decimal("2"), funding=Decimal("0"), closed_at=_NOW,
        shadow_realized_pnl=Decimal("-125"), updated_at=_NOW,
    )
    repo.backfill_profit_protection_baseline_outcome("pos-1", "stop_loss", Decimal("-500"), _NOW)
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["pnl_difference"] == "375"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/storage/test_repository_profit_protection.py -k "record_profit_protection_tick or activate_profit_protection_breakeven or close_profit_protection_shadow or backfill_profit_protection" -v`
Expected: FAIL

- [ ] **Step 3: Add Protocol declarations**

```python
    def record_profit_protection_tick(
        self, shadow_id: str, mfe: Decimal, mae: Decimal, updated_at: datetime
    ) -> None: ...
    def activate_profit_protection_breakeven(
        self,
        shadow_id: str,
        breakeven_stop_loss: Decimal,
        threshold_reached_at: datetime,
        updated_at: datetime,
    ) -> None: ...
    def close_profit_protection_shadow(
        self,
        shadow_id: str,
        exit_reason: str,
        theoretical_exit: Decimal,
        simulated_fill_exit: Decimal,
        fees: Decimal,
        funding: Decimal,
        closed_at: datetime,
        shadow_realized_pnl: Decimal,
        updated_at: datetime,
    ) -> None: ...
    def backfill_profit_protection_baseline_outcome(
        self, position_id: str, exit_reason: str, baseline_pnl: Decimal, updated_at: datetime
    ) -> None: ...
```

- [ ] **Step 4: Implement on SQLiteRepository**

```python
    def record_profit_protection_tick(
        self, shadow_id: str, mfe: Decimal, mae: Decimal, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE profit_protection_shadow_positions SET mfe = ?, mae = ?, updated_at = ? "
            "WHERE shadow_id = ? AND status = 'OPEN'",
            (str(mfe), str(mae), updated_at.isoformat(), shadow_id),
        )
        self._conn.commit()

    def activate_profit_protection_breakeven(
        self,
        shadow_id: str,
        breakeven_stop_loss: Decimal,
        threshold_reached_at: datetime,
        updated_at: datetime,
    ) -> None:
        # WHERE threshold_reached = 0 makes this a one-time transition -
        # a later, accidental second call (e.g. a retried tick) can never
        # move an already-active breakeven level, spec G7/G8's "next tick
        # only" guarantee holds even under retries.
        self._conn.execute(
            "UPDATE profit_protection_shadow_positions SET threshold_reached = 1, "
            "threshold_reached_at = ?, breakeven_stop_loss = ?, updated_at = ? "
            "WHERE shadow_id = ? AND status = 'OPEN' AND threshold_reached = 0",
            (
                threshold_reached_at.isoformat(), str(breakeven_stop_loss),
                updated_at.isoformat(), shadow_id,
            ),
        )
        self._conn.commit()

    def close_profit_protection_shadow(
        self,
        shadow_id: str,
        exit_reason: str,
        theoretical_exit: Decimal,
        simulated_fill_exit: Decimal,
        fees: Decimal,
        funding: Decimal,
        closed_at: datetime,
        shadow_realized_pnl: Decimal,
        updated_at: datetime,
    ) -> None:
        row = self._conn.execute(
            "SELECT hypothetical_baseline_pnl FROM profit_protection_shadow_positions "
            "WHERE shadow_id = ?",
            (shadow_id,),
        ).fetchone()
        pnl_difference = None
        if row is not None and row["hypothetical_baseline_pnl"] is not None:
            pnl_difference = shadow_realized_pnl - Decimal(row["hypothetical_baseline_pnl"])
        self._conn.execute(
            "UPDATE profit_protection_shadow_positions SET status = 'CLOSED', "
            "exit_reason = ?, theoretical_exit = ?, simulated_fill_exit = ?, fees = ?, "
            "funding = ?, closed_at = ?, shadow_realized_pnl = ?, pnl_difference = ?, "
            "updated_at = ? WHERE shadow_id = ? AND status = 'OPEN'",
            (
                exit_reason, str(theoretical_exit), str(simulated_fill_exit), str(fees),
                str(funding), closed_at.isoformat(), str(shadow_realized_pnl),
                str(pnl_difference) if pnl_difference is not None else None,
                updated_at.isoformat(), shadow_id,
            ),
        )
        self._conn.commit()

    def backfill_profit_protection_baseline_outcome(
        self, position_id: str, exit_reason: str, baseline_pnl: Decimal, updated_at: datetime
    ) -> None:
        """Called once per real position close (spec §5.5) - updates every
        threshold's shadow row for that position_id. Whichever of
        (shadow close, this backfill) happens second is what fills
        pnl_difference (spec §4.3) - this method fills it here if the
        shadow already closed; close_profit_protection_shadow() fills it
        there if this backfill already ran first."""
        rows = self._conn.execute(
            "SELECT shadow_id, status, shadow_realized_pnl "
            "FROM profit_protection_shadow_positions WHERE position_id = ?",
            (position_id,),
        ).fetchall()
        for row in rows:
            pnl_difference = None
            if row["status"] == "CLOSED" and row["shadow_realized_pnl"] is not None:
                pnl_difference = Decimal(row["shadow_realized_pnl"]) - baseline_pnl
            self._conn.execute(
                "UPDATE profit_protection_shadow_positions SET "
                "hypothetical_baseline_exit_reason = ?, hypothetical_baseline_pnl = ?, "
                "pnl_difference = COALESCE(?, pnl_difference), updated_at = ? "
                "WHERE shadow_id = ?",
                (
                    exit_reason, str(baseline_pnl),
                    str(pnl_difference) if pnl_difference is not None else None,
                    updated_at.isoformat(), row["shadow_id"],
                ),
            )
        self._conn.commit()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/storage/test_repository_profit_protection.py -k "record_profit_protection_tick or activate_profit_protection_breakeven or close_profit_protection_shadow or backfill_profit_protection or seed_profit_protection or find_open_profit_protection or find_all_profit_protection" -v`
Expected: all PASS (including Task 3's previously-blocked closed-row test)

- [ ] **Step 6: Run the full storage test suite**

Run: `pytest tests/crypto_trading/storage/ -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add crypto_trading/storage/repository.py tests/crypto_trading/storage/test_repository_profit_protection.py
git commit -m "feat(crypto-trading): add profit protection shadow tick/close/backfill repository methods"
```

---

## Task 5: State machine module — constants + Guardian-staleness duplicate

**Files:**
- Create: `crypto_trading/paper_trading/profit_protection_experiment.py`
- Test: `tests/crypto_trading/paper_trading/test_profit_protection_experiment.py`

**Interfaces:**
- Consumes: `crypto_trading.config.loader.GuardianConfig`, `Repository.find_latest_guardian_observation`
- Produces: `FROZEN_THRESHOLDS_PCT: tuple[Decimal, ...]`, `_guardian_state_for(repo, position_id, now, guardian_config) -> str | None`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig
from crypto_trading.paper_trading.profit_protection_experiment import (
    FROZEN_THRESHOLDS_PCT,
    _guardian_state_for,
)
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def test_frozen_thresholds_are_exactly_one_and_one_half_percent():
    assert FROZEN_THRESHOLDS_PCT == (Decimal("0.010"), Decimal("0.015"))


def test_guardian_state_lookup_returns_none_when_assisted_exit_disabled(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    config = GuardianConfig(assisted_exit_enabled=False)
    assert _guardian_state_for(repo, "pos-1", _NOW, config) is None


def test_guardian_state_lookup_returns_none_with_no_observation(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    config = GuardianConfig(assisted_exit_enabled=True, check_interval_seconds=60)
    assert _guardian_state_for(repo, "pos-1", _NOW, config) is None


def test_guardian_state_lookup_returns_state_when_fresh(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    config = GuardianConfig(assisted_exit_enabled=True, check_interval_seconds=60)
    repo.save_guardian_observation(
        GuardianObservation(
            observation_id="obs-1", position_id="pos-1", observed_at=_NOW - timedelta(seconds=30),
            state="EXIT", decay_score=Decimal("0.9"), progress_ratio=Decimal("0"),
            unrealized_pnl=Decimal("0"), factors={}, run_id="run-1",
        )
    )
    assert _guardian_state_for(repo, "pos-1", _NOW, config) == "EXIT"


def test_guardian_state_lookup_returns_none_when_stale(tmp_path):
    """Same 2x check_interval_seconds staleness limit as
    position_closing.py::close_triggered_positions - proven identical by
    this and the previous test using the exact same boundary math."""
    repo = SQLiteRepository(tmp_path / "t.db")
    config = GuardianConfig(assisted_exit_enabled=True, check_interval_seconds=60)
    repo.save_guardian_observation(
        GuardianObservation(
            observation_id="obs-1", position_id="pos-1",
            observed_at=_NOW - timedelta(seconds=121),  # > 2 * 60s
            state="EXIT", decay_score=Decimal("0.9"), progress_ratio=Decimal("0"),
            unrealized_pnl=Decimal("0"), factors={}, run_id="run-1",
        )
    )
    assert _guardian_state_for(repo, "pos-1", _NOW, config) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/paper_trading/test_profit_protection_experiment.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Create the module**

`crypto_trading/paper_trading/profit_protection_experiment.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig, RiskLimitsConfig, Settings
from crypto_trading.paper_trading.execution import (
    FILL_MODEL_VERSION,
    compute_fees,
    compute_fill_price,
    compute_funding,
    compute_pnl,
)
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

# Pre-registered hypotheses under test (2026-09-11, spec G9). Frozen for
# the duration of this experiment - not a tuning parameter, not read from
# config/YAML/env. Changing this set is a source-code change requiring the
# same review as any other logic change, never a deploy-time toggle.
FROZEN_THRESHOLDS_PCT: tuple[Decimal, ...] = (Decimal("0.010"), Decimal("0.015"))

_DIRECTION = "LONG"


def _guardian_state_for(
    repo: Repository, position_id: str, now: datetime, guardian_config: GuardianConfig
) -> str | None:
    """Read-only DUPLICATE of position_closing.py::close_triggered_positions's
    own staleness-guarded Guardian read (spec G4) - deliberately duplicated,
    not shared, so position_closing.py (baseline exit logic) stays
    completely untouched. See
    test_guardian_state_lookup_matches_close_triggered_positions_exactly in
    Task 9 for the parity proof against the real function."""
    if not guardian_config.assisted_exit_enabled:
        return None
    latest_observation = repo.find_latest_guardian_observation(position_id)
    if latest_observation is None:
        return None
    observed_at = datetime.fromisoformat(latest_observation["observed_at"])
    staleness_limit = timedelta(seconds=2 * guardian_config.check_interval_seconds)
    if now - observed_at <= staleness_limit:
        return latest_observation["state"]
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/paper_trading/test_profit_protection_experiment.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/paper_trading/profit_protection_experiment.py tests/crypto_trading/paper_trading/test_profit_protection_experiment.py
git commit -m "feat(crypto-trading): add frozen threshold constant and Guardian-staleness duplicate for PP experiment"
```

---

## Task 6: Seeding

**Files:**
- Modify: `crypto_trading/paper_trading/profit_protection_experiment.py`
- Test: `tests/crypto_trading/paper_trading/test_profit_protection_experiment.py`

**Interfaces:**
- Consumes: `Repository.seed_profit_protection_shadow`, `Repository.get_profit_protection_shadow`, `FROZEN_THRESHOLDS_PCT`
- Produces: `seed_shadows_for_position(repo, position: Position, activated_at: datetime, now: datetime) -> None`

- [ ] **Step 1: Write the failing tests**

```python
from crypto_trading.paper_trading.profit_protection_experiment import seed_shadows_for_position
from crypto_trading.schemas.trade import Position


def _position(position_id="pos-1", opened_at=_NOW, instrument="BTCUSDT") -> Position:
    return Position(
        position_id=position_id, candidate_id=position_id, instrument=instrument,
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("5000"), fill_model_version="v1",
        opened_at=opened_at,
    )


def test_seed_shadows_for_position_creates_one_row_per_frozen_threshold(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    seed_shadows_for_position(repo, _position(), activated_at=_NOW, now=_NOW)
    shadows = repo.find_all_profit_protection_shadows()
    assert {s["threshold_pct"] for s in shadows} == {"0.010", "0.015"}
    assert all(s["position_id"] == "pos-1" for s in shadows)


def test_seed_shadows_for_position_computes_correct_threshold_price(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    seed_shadows_for_position(repo, _position(), activated_at=_NOW, now=_NOW)
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert Decimal(row["threshold_price"]) == Decimal("50500")  # 50000 * 1.010


def test_seed_shadows_for_position_never_seeds_before_activation_watermark(tmp_path):
    """Spec G6 / plan correction C1 - a position opened strictly before the
    watermark is permanently excluded."""
    repo = SQLiteRepository(tmp_path / "t.db")
    activated_at = _NOW
    early_position = _position(opened_at=_NOW - timedelta(seconds=1))
    seed_shadows_for_position(repo, early_position, activated_at=activated_at, now=_NOW)
    assert repo.find_all_profit_protection_shadows() == []


def test_seed_shadows_for_position_is_idempotent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    seed_shadows_for_position(repo, _position(), activated_at=_NOW, now=_NOW)
    seed_shadows_for_position(repo, _position(), activated_at=_NOW, now=_NOW)
    assert len(repo.find_all_profit_protection_shadows()) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/paper_trading/test_profit_protection_experiment.py -k seed_shadows -v`
Expected: FAIL

- [ ] **Step 3: Implement**

Add to `profit_protection_experiment.py`:

```python
def _shadow_id(position_id: str, threshold_pct: Decimal) -> str:
    return f"{position_id}:{threshold_pct}"


def seed_shadows_for_position(
    repo: Repository, position: Position, activated_at: datetime, now: datetime
) -> None:
    """Spec §5.1, G6: only ever seeds a position opened at-or-after the
    activation watermark. Idempotent per (position_id, threshold) via
    Repository.seed_profit_protection_shadow's own INSERT OR IGNORE."""
    if position.opened_at < activated_at:
        return
    for threshold_pct in FROZEN_THRESHOLDS_PCT:
        shadow_id = _shadow_id(position.position_id, threshold_pct)
        threshold_price = position.theoretical_entry * (1 + threshold_pct)
        repo.seed_profit_protection_shadow(
            shadow_id=shadow_id,
            position_id=position.position_id,
            instrument=position.instrument,
            threshold_pct=str(threshold_pct),
            entry_price=position.theoretical_entry,
            original_stop_loss=position.stop_loss,
            target=position.target,
            threshold_price=threshold_price,
            opened_at=position.opened_at,
            created_at=now,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/paper_trading/test_profit_protection_experiment.py -k seed_shadows -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/paper_trading/profit_protection_experiment.py tests/crypto_trading/paper_trading/test_profit_protection_experiment.py
git commit -m "feat(crypto-trading): add PP experiment shadow seeding, gated by activation watermark"
```

---

## Task 7: State machine — advance_shadow (conservative ordering)

**Files:**
- Modify: `crypto_trading/paper_trading/profit_protection_experiment.py`
- Test: `tests/crypto_trading/paper_trading/test_profit_protection_experiment.py`

**Interfaces:**
- Consumes: `Repository.record_profit_protection_tick`, `Repository.activate_profit_protection_breakeven`, Task 8's `_close_shadow` (write Task 8 first if executing strictly in order, or stub it here and replace in Task 8)
- Produces: `advance_shadow(shadow: dict, candle_low: Decimal, candle_high: Decimal, current_price: Decimal, funding_rate: Decimal, now: datetime, max_position_hold_hours: int, guardian_state: str | None, guardian_assisted_exit_enabled: bool, risk_limits: RiskLimitsConfig, repo: Repository) -> None`

**Note:** this task's tests need `_close_shadow` (Task 8). Implement Task 8
immediately after Task 7's Step 3 and before running Task 7's tests, OR do
Tasks 7 and 8 as one combined session — they are listed separately only
because they test different concerns (ordering vs. PnL parity).

- [ ] **Step 1: Write the failing tests**

```python
def _shadow_row(repo, **overrides) -> dict:
    defaults = dict(
        shadow_id="pos-1:0.010", position_id="pos-1", instrument="BTCUSDT",
        threshold_pct="0.010", entry_price=Decimal("50000"),
        original_stop_loss=Decimal("49000"), target=Decimal("52000"),
        threshold_price=Decimal("50500"), opened_at=_NOW, created_at=_NOW,
    )
    defaults.update(overrides)
    repo.seed_profit_protection_shadow(**defaults)
    return repo.get_profit_protection_shadow(defaults["shadow_id"])


def _risk_limits(**overrides) -> RiskLimitsConfig:
    defaults = dict(
        starting_capital_usdt=Decimal("10000"), risk_per_trade_pct=Decimal("0.01"),
        max_concurrent_positions=5, max_total_exposure_pct=Decimal("1.0"),
        max_position_notional_usdt=Decimal("1000000"), spread_pct=Decimal("0.0005"),
        slippage_pct=Decimal("0.0005"), fee_pct=Decimal("0.0004"), max_position_hold_hours=24,
    )
    defaults.update(overrides)
    return RiskLimitsConfig(**defaults)


def _seed_real_position(repo, **overrides) -> None:
    from crypto_trading.schemas.event import Event
    position = _position(**overrides)
    repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_OPENED:{position.position_id}", event_type="POSITION_OPENED",
            aggregate_type="position", aggregate_id=position.position_id,
            occurred_at=position.opened_at, run_id="seed", schema_version=1, payload={},
        ),
    )


def test_advance_shadow_does_not_move_sl_when_threshold_not_reached(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("49500"), candle_high=Decimal("50100"),
        current_price=Decimal("50000"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "OPEN"
    assert row["threshold_reached"] == 0
    assert row["breakeven_stop_loss"] is None


def test_advance_shadow_activates_breakeven_starting_next_tick_only(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    # Tick 1: threshold touched (candle_high 50600 >= 50500), no stop/target hit
    advance_shadow(
        shadow, candle_low=Decimal("50100"), candle_high=Decimal("50600"),
        current_price=Decimal("50400"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    after_tick_1 = repo.get_profit_protection_shadow("pos-1:0.010")
    assert after_tick_1["threshold_reached"] == 1
    assert after_tick_1["breakeven_stop_loss"] == "50000"
    assert after_tick_1["status"] == "OPEN"  # never closed same tick it activated

    # Tick 2: price drops to exactly breakeven - now the active SL, closes here
    tick_2_time = _NOW + timedelta(minutes=1)
    advance_shadow(
        after_tick_1, candle_low=Decimal("49900"), candle_high=Decimal("50200"),
        current_price=Decimal("50000"), funding_rate=Decimal("0"), now=tick_2_time,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    after_tick_2 = repo.get_profit_protection_shadow("pos-1:0.010")
    assert after_tick_2["status"] == "CLOSED"
    assert after_tick_2["exit_reason"] == "stop_loss"


def test_advance_shadow_same_candle_threshold_and_stop_resolves_stop_first(tmp_path):
    """Spec G8: a candle whose low <= original SL (49000) and whose high
    also >= threshold (50500) must be resolved as the stop firing first -
    threshold is never considered reached on a candle that also breached
    the pre-existing SL."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("48900"), candle_high=Decimal("50600"),
        current_price=Decimal("49500"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "stop_loss"
    assert row["threshold_reached"] == 0  # never got the chance - stop fired first


def test_advance_shadow_time_limit_fires_when_hold_hours_exceeded(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    later = _NOW + timedelta(hours=25)
    advance_shadow(
        shadow, candle_low=Decimal("49500"), candle_high=Decimal("50100"),
        current_price=Decimal("49800"), funding_rate=Decimal("0"), now=later,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "time_limit"
    assert row["theoretical_exit"] == "49800"  # current_price, not candle_high/low


def test_advance_shadow_guardian_exit_fires_only_when_enabled_and_state_is_exit(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("49500"), candle_high=Decimal("50100"),
        current_price=Decimal("49900"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state="EXIT",
        guardian_assisted_exit_enabled=True, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "CLOSED"
    assert row["exit_reason"] == "guardian_exit"


def test_advance_shadow_guardian_exit_state_never_closes_when_disabled(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("49500"), candle_high=Decimal("50100"),
        current_price=Decimal("49900"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state="EXIT",
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["status"] == "OPEN"


def test_advance_shadow_updates_mfe_and_mae_before_any_exit_check(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo)
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("49700"), candle_high=Decimal("50300"),
        current_price=Decimal("50000"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["mfe"] == "300"   # 50300 - 50000
    assert row["mae"] == "-300"  # 49700 - 50000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/paper_trading/test_profit_protection_experiment.py -k advance_shadow -v`
Expected: FAIL

- [ ] **Step 3: Implement `advance_shadow` (and its `_close_shadow` dependency from Task 8)**

Add to `profit_protection_experiment.py` (this includes Task 8's
`_close_shadow` inline since Task 7's tests exercise the full close path —
see Task 8 for `_close_shadow`'s own dedicated PnL-parity tests):

```python
def advance_shadow(
    shadow: dict,
    candle_low: Decimal,
    candle_high: Decimal,
    current_price: Decimal,
    funding_rate: Decimal,
    now: datetime,
    max_position_hold_hours: int,
    guardian_state: str | None,
    guardian_assisted_exit_enabled: bool,
    risk_limits: RiskLimitsConfig,
    repo: Repository,
) -> None:
    """One shadow row, one tick (spec §5.2). Conservative ordering (G7/G8):
    stop -> target -> time_limit -> guardian_exit, always checked against
    the ACTIVE sl as of the START of this tick - a new threshold-touch is
    only ever detected AFTER all four checks, and only takes effect
    starting the tick after this one (activate_profit_protection_breakeven
    is a separate call the NEXT tick will see via `shadow["breakeven_stop_loss"]`
    being non-None, never within this same call)."""
    shadow_id = shadow["shadow_id"]
    entry_price = Decimal(shadow["entry_price"])
    original_stop_loss = Decimal(shadow["original_stop_loss"])
    target = Decimal(shadow["target"])
    threshold_price = Decimal(shadow["threshold_price"])
    breakeven_stop_loss = (
        Decimal(shadow["breakeven_stop_loss"])
        if shadow["breakeven_stop_loss"] is not None
        else None
    )
    active_sl = breakeven_stop_loss if breakeven_stop_loss is not None else original_stop_loss

    mfe = max(Decimal(shadow["mfe"]), candle_high - entry_price)
    mae = min(Decimal(shadow["mae"]), candle_low - entry_price)
    repo.record_profit_protection_tick(shadow_id, mfe, mae, now)

    opened_at = datetime.fromisoformat(shadow["opened_at"])
    hold_hours = Decimal(str((now - opened_at).total_seconds())) / Decimal("3600")

    exit_reason: str | None = None
    theoretical_exit: Decimal | None = None
    if candle_low <= active_sl:
        exit_reason, theoretical_exit = "stop_loss", min(candle_low, active_sl)
    elif candle_high >= target:
        exit_reason, theoretical_exit = "target", min(candle_high, target)
    elif hold_hours >= max_position_hold_hours:
        exit_reason, theoretical_exit = "time_limit", current_price
    elif guardian_assisted_exit_enabled and guardian_state == "EXIT":
        exit_reason, theoretical_exit = "guardian_exit", current_price

    if exit_reason is not None:
        _close_shadow(repo, shadow, exit_reason, theoretical_exit, funding_rate, risk_limits, now)
        return

    if not shadow["threshold_reached"] and candle_high >= threshold_price:
        repo.activate_profit_protection_breakeven(shadow_id, entry_price, now, now)


def _close_shadow(
    repo: Repository,
    shadow: dict,
    exit_reason: str,
    theoretical_exit: Decimal,
    funding_rate: Decimal,
    risk_limits: RiskLimitsConfig,
    closed_at: datetime,
) -> None:
    """Spec §5.4: size/simulated_fill_entry are read from the real
    position (plan correction, PnL-parity data sourcing) - they never
    change before the real position closes, so this read is always safe
    whether or not the real position has closed yet. Never writes to
    `positions` - only reads via repo.get_position() and writes via
    repo.close_profit_protection_shadow()."""
    real_position = repo.get_position(shadow["position_id"])
    simulated_fill_exit = compute_fill_price(
        theoretical_exit, _DIRECTION, risk_limits.spread_pct, risk_limits.slippage_pct, "exit"
    )
    fees = compute_fees(real_position.size, risk_limits.fee_pct)
    opened_at = datetime.fromisoformat(shadow["opened_at"])
    hold_hours = Decimal(str((closed_at - opened_at).total_seconds())) / Decimal("3600")
    funding = compute_funding(real_position.size, funding_rate, hold_hours)

    ephemeral = Position(
        position_id=shadow["shadow_id"],
        candidate_id="profit_protection_experiment",
        instrument=shadow["instrument"],
        direction=_DIRECTION,
        status="CLOSED",
        theoretical_entry=Decimal(shadow["entry_price"]),
        simulated_fill_entry=real_position.simulated_fill_entry,
        stop_loss=Decimal(shadow["original_stop_loss"]),
        target=Decimal(shadow["target"]),
        size=real_position.size,
        fill_model_version=FILL_MODEL_VERSION,
        opened_at=opened_at,
        theoretical_exit=theoretical_exit,
        simulated_fill_exit=simulated_fill_exit,
        exit_reason=exit_reason,
        fees=fees,
        funding=funding,
        closed_at=closed_at,
    )
    shadow_realized_pnl = compute_pnl(ephemeral)
    repo.close_profit_protection_shadow(
        shadow_id=shadow["shadow_id"],
        exit_reason=exit_reason,
        theoretical_exit=theoretical_exit,
        simulated_fill_exit=simulated_fill_exit,
        fees=fees,
        funding=funding,
        closed_at=closed_at,
        shadow_realized_pnl=shadow_realized_pnl,
        updated_at=closed_at,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/paper_trading/test_profit_protection_experiment.py -k advance_shadow -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/paper_trading/profit_protection_experiment.py tests/crypto_trading/paper_trading/test_profit_protection_experiment.py
git commit -m "feat(crypto-trading): add PP experiment advance_shadow state machine with conservative ordering"
```

---

## Task 8: PnL parity with `compute_pnl()`

**Files:**
- Modify: `tests/crypto_trading/paper_trading/test_profit_protection_experiment.py`

**Interfaces:**
- Consumes: `_close_shadow` (already implemented in Task 7)

- [ ] **Step 1: Write tests proving bit-for-bit PnL parity**

```python
from crypto_trading.paper_trading.execution import compute_fees, compute_fill_price, compute_pnl


def test_shadow_realized_pnl_matches_compute_pnl_formula_exactly(tmp_path):
    """Proves the ephemeral Position built in _close_shadow produces the
    exact same number compute_pnl() would produce for an equivalent real
    position - the single-source-of-truth guarantee from spec §5.4."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo, position_id="pos-1")
    shadow = _shadow_row(repo)
    risk_limits = _risk_limits()

    advance_shadow(
        shadow, candle_low=Decimal("52100"), candle_high=Decimal("52100"),
        current_price=Decimal("52050"), funding_rate=Decimal("0.0001"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=risk_limits, repo=repo,
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["exit_reason"] == "target"

    # Recompute independently, using the real position's own known fields
    # plus the same formulas, and assert equality with what got stored.
    real_position = repo.get_position("pos-1")
    expected_theoretical_exit = min(Decimal("52100"), Decimal("52000"))  # target=52000
    expected_fill_exit = compute_fill_price(
        expected_theoretical_exit, "LONG", risk_limits.spread_pct, risk_limits.slippage_pct, "exit"
    )
    expected_fees = compute_fees(real_position.size, risk_limits.fee_pct)
    assert Decimal(row["simulated_fill_exit"]) == expected_fill_exit
    assert Decimal(row["fees"]) == expected_fees

    from crypto_trading.schemas.trade import Position as _P
    expected_ephemeral = _P(
        position_id="x", candidate_id="x", instrument="BTCUSDT", direction="LONG",
        status="CLOSED", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=real_position.simulated_fill_entry,
        stop_loss=Decimal("49000"), target=Decimal("52000"), size=real_position.size,
        fill_model_version="v1", opened_at=_NOW,
        theoretical_exit=expected_theoretical_exit, simulated_fill_exit=expected_fill_exit,
        exit_reason="target", fees=expected_fees,
        funding=Decimal(row["funding"]), closed_at=_NOW,
    )
    assert Decimal(row["shadow_realized_pnl"]) == compute_pnl(expected_ephemeral)


def test_shadow_close_never_writes_to_positions_table(tmp_path):
    """G1: closing a shadow must never mutate the real position row."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo, position_id="pos-1")
    before = repo.get_position("pos-1")
    shadow = _shadow_row(repo)
    advance_shadow(
        shadow, candle_low=Decimal("48900"), candle_high=Decimal("49000"),
        current_price=Decimal("48950"), funding_rate=Decimal("0"), now=_NOW,
        max_position_hold_hours=24, guardian_state=None,
        guardian_assisted_exit_enabled=False, risk_limits=_risk_limits(), repo=repo,
    )
    after = repo.get_position("pos-1")
    assert before == after
    assert after.status == "OPEN_POSITION"  # still open - only the shadow closed
```

- [ ] **Step 2: Run tests to verify they fail or pass**

Run: `pytest tests/crypto_trading/paper_trading/test_profit_protection_experiment.py -k "pnl_matches or never_writes_to_positions" -v`
Expected: since `_close_shadow` already exists from Task 7, these should
PASS immediately if Task 7 was implemented correctly — this task is a
verification/parity-proof task, not new production code. If any assertion
fails, fix `_close_shadow` (Task 7) until it does, then re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/crypto_trading/paper_trading/test_profit_protection_experiment.py
git commit -m "test(crypto-trading): prove PP experiment PnL parity with compute_pnl() and positions-table isolation"
```

---

## Task 9: Orchestration — `run_profit_protection_experiment_tick`

**Files:**
- Modify: `crypto_trading/paper_trading/profit_protection_experiment.py`
- Test: `tests/crypto_trading/paper_trading/test_profit_protection_experiment.py`

**Interfaces:**
- Consumes: `seed_shadows_for_position`, `advance_shadow`, `_guardian_state_for`, `Repository.set_profit_protection_activated_at_if_missing`, `Repository.get_profit_protection_activated_at`, `Repository.find_open_profit_protection_shadows`, `Repository.backfill_profit_protection_baseline_outcome`
- Produces: `run_profit_protection_experiment_tick(repo: Repository, open_positions: list[Position], closed_positions: list[Position], price_lookup: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]], now: datetime, settings: Settings, run_id: str) -> None`

- [ ] **Step 1: Write the failing tests**

```python
from crypto_trading.config.loader import ProfitProtectionExperimentConfig
from crypto_trading.paper_trading.profit_protection_experiment import (
    run_profit_protection_experiment_tick,
)
from tests.crypto_trading.test_market_snapshot import _settings as _market_settings


def _settings_with_pp(enabled: bool, guardian_assisted: bool = False) -> Settings:
    # _market_settings(top_n=1) is the exact same full-Settings builder
    # tests/crypto_trading/test_monitoring_loop.py already uses (imported
    # there the same way, from test_market_snapshot.py) - avoids
    # constructing a second, divergent fake Settings for this same purpose.
    settings = _market_settings(top_n=1)
    settings.profit_protection_experiment = ProfitProtectionExperimentConfig(enabled=enabled)
    settings.guardian.assisted_exit_enabled = guardian_assisted
    return settings


def test_tick_does_nothing_when_experiment_disabled(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo, position_id="pos-1")
    position = repo.get_position("pos-1")
    price_lookup = {"BTCUSDT": (Decimal("49500"), Decimal("50100"), Decimal("49900"), Decimal("0"))}
    run_profit_protection_experiment_tick(
        repo, [position], [], price_lookup, _NOW, _settings_with_pp(enabled=False), "run-1"
    )
    assert repo.find_all_profit_protection_shadows() == []
    assert repo.get_profit_protection_activated_at() is None


def test_tick_seeds_and_advances_a_newly_opened_position_in_the_same_tick(tmp_path):
    """Plan correction C2 - a position that opens and immediately stops
    out on the very first tick the experiment observes it must still be
    seeded AND closed within that same tick call, never left stranded."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo, position_id="pos-1")
    position = repo.get_position("pos-1")
    price_lookup = {
        "BTCUSDT": (Decimal("48900"), Decimal("49000"), Decimal("48950"), Decimal("0"))
    }
    run_profit_protection_experiment_tick(
        repo, [position], [], price_lookup, _NOW, _settings_with_pp(enabled=True), "run-1"
    )
    shadows = repo.find_all_profit_protection_shadows()
    assert len(shadows) == 2
    assert all(s["status"] == "CLOSED" for s in shadows)
    assert all(s["exit_reason"] == "stop_loss" for s in shadows)


def test_tick_defers_seeding_when_instrument_missing_from_price_lookup(tmp_path):
    """Plan correction C2 - mirrors close_triggered_positions's own
    'instrument not in price_lookup -> skip' behavior; never seeds on
    absent data."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo, position_id="pos-1")
    position = repo.get_position("pos-1")
    run_profit_protection_experiment_tick(
        repo, [position], [], {}, _NOW, _settings_with_pp(enabled=True), "run-1"
    )
    assert repo.find_all_profit_protection_shadows() == []


def test_tick_backfills_baseline_outcome_for_positions_closed_this_tick(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_real_position(repo, position_id="pos-1")
    position = repo.get_position("pos-1")
    settings = _settings_with_pp(enabled=True)
    price_lookup = {"BTCUSDT": (Decimal("49700"), Decimal("50100"), Decimal("50000"), Decimal("0"))}
    run_profit_protection_experiment_tick(
        repo, [position], [], price_lookup, _NOW, settings, "run-1"
    )
    # Simulate the real position closing on a later tick (as
    # close_triggered_positions would report it):
    closed_position = position.model_copy(update={
        "status": "CLOSED", "exit_reason": "stop_loss",
        "theoretical_exit": Decimal("49000"), "simulated_fill_exit": Decimal("48975.5"),
        "fees": Decimal("2"), "funding": Decimal("0"), "closed_at": _NOW,
    })
    later = _NOW + timedelta(minutes=1)
    run_profit_protection_experiment_tick(
        repo, [], [closed_position], {}, later, settings, "run-2"
    )
    row = repo.get_profit_protection_shadow("pos-1:0.010")
    assert row["hypothetical_baseline_exit_reason"] == "stop_loss"
    assert row["hypothetical_baseline_pnl"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/paper_trading/test_profit_protection_experiment.py -k tick_ -v`
Expected: FAIL (`ImportError`)

- [ ] **Step 3: Implement**

Add to `profit_protection_experiment.py`:

```python
from crypto_trading.paper_trading.execution import compute_pnl as _compute_pnl_for_backfill
```

(Already imported as `compute_pnl` above — no new import needed; this line
is illustrative only, do not duplicate the import.)

```python
def run_profit_protection_experiment_tick(
    repo: Repository,
    open_positions: list[Position],
    closed_positions: list[Position],
    price_lookup: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]],
    now: datetime,
    settings: Settings,
    run_id: str,
) -> None:
    """Spec §3.2/§5, plan correction C2: seed -> advance -> backfill, in
    that strict order, every tick. Called from monitoring_loop.py AFTER
    close_triggered_positions, wrapped in the caller's own try/except -
    this function itself never needs to guard against crashing the caller,
    only against writing anything outside profit_protection_shadow_positions."""
    if not settings.profit_protection_experiment.enabled:
        return

    repo.set_profit_protection_activated_at_if_missing(now)
    activated_at = repo.get_profit_protection_activated_at()

    # 1) Seed - only for positions whose instrument's candle is actually
    # available this tick (plan correction C2: never seed on absent data,
    # mirrors close_triggered_positions's own price_lookup-presence skip).
    for position in open_positions:
        if position.instrument not in price_lookup:
            continue
        seed_shadows_for_position(repo, position, activated_at, now)

    # 2) Advance every currently-open shadow (includes any just seeded
    # above, since find_open_profit_protection_shadows() re-queries after
    # the seed loop's commits) against this same tick's price_lookup.
    guardian_assisted_exit_enabled = settings.guardian.assisted_exit_enabled
    for shadow in repo.find_open_profit_protection_shadows():
        if shadow["instrument"] not in price_lookup:
            continue
        candle_low, candle_high, current_price, funding_rate = price_lookup[shadow["instrument"]]
        guardian_state = (
            _guardian_state_for(repo, shadow["position_id"], now, settings.guardian)
            if guardian_assisted_exit_enabled
            else None
        )
        advance_shadow(
            shadow, candle_low, candle_high, current_price, funding_rate, now,
            settings.risk_limits.max_position_hold_hours, guardian_state,
            guardian_assisted_exit_enabled, settings.risk_limits, repo,
        )

    # 3) Backfill baseline outcome for whatever close_triggered_positions
    # closed this same tick (spec §5.5) - read-only against `positions`.
    for position in closed_positions:
        if position.exit_reason is None or position.fees is None or position.funding is None:
            continue  # defensive - close_triggered_positions always sets these
        baseline_pnl = compute_pnl(position)
        repo.backfill_profit_protection_baseline_outcome(
            position.position_id, position.exit_reason, baseline_pnl, now
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/paper_trading/test_profit_protection_experiment.py -k tick_ -v`
Expected: PASS

- [ ] **Step 5: Run the full test file**

Run: `pytest tests/crypto_trading/paper_trading/test_profit_protection_experiment.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add crypto_trading/paper_trading/profit_protection_experiment.py tests/crypto_trading/paper_trading/test_profit_protection_experiment.py
git commit -m "feat(crypto-trading): add PP experiment tick orchestration (seed/advance/backfill)"
```

---

## Task 10: `monitoring_loop.py` integration + G10 isolation test

**Files:**
- Modify: `crypto_trading/monitoring_loop.py`
- Test: `tests/crypto_trading/test_monitoring_loop.py`

**Interfaces:**
- Consumes: `run_profit_protection_experiment_tick` (Task 9)

This is **the only edit to a currently-shipping production file** in this
plan. Everything else is new, additive files.

- [ ] **Step 1: Write the failing isolation test (G10)**

Add to `tests/crypto_trading/test_monitoring_loop.py`:

```python
def test_a_crash_in_the_profit_protection_experiment_never_affects_real_position_closing(
    tmp_path, monkeypatch
):
    """Spec G10 (explicit user requirement #8): forces the experiment tick
    to raise and proves (a) the real stop_loss close still happens and is
    still returned, (b) no exception propagates out of run_monitoring_tick,
    (c) the failure is logged."""
    import crypto_trading.monitoring_loop as monitoring_loop_module

    def _raiser(*args, **kwargs):
        raise RuntimeError("boom - simulated PP experiment failure")

    monkeypatch.setattr(
        monitoring_loop_module, "run_profit_protection_experiment_tick", _raiser
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    closed = run_monitoring_tick(connector, repo, _settings())  # must never raise

    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"
    assert closed[0].status == "CLOSED"
    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'monitoring'").fetchone()
    assert row["status"] == "ok"  # the OUTER try/except never even saw the failure
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/crypto_trading/test_monitoring_loop.py -k profit_protection_experiment_never_affects -v`
Expected: FAIL (`run_profit_protection_experiment_tick` doesn't exist in
`monitoring_loop` module yet, so `monkeypatch.setattr` raises `AttributeError`)

- [ ] **Step 3: Make the one production edit**

In `crypto_trading/monitoring_loop.py`:

Add to the imports:

```python
from crypto_trading.paper_trading.profit_protection_experiment import (
    run_profit_protection_experiment_tick,
)
```

Change this block inside `run_monitoring_tick`:

```python
        for position in repo.find_open_positions():
            symbol = position.instrument
```

to:

```python
        open_positions = list(repo.find_open_positions())
        for position in open_positions:
            symbol = position.instrument
```

And change this block:

```python
        closed = close_triggered_positions(
            repo, price_lookup, now, settings.risk_limits, run_id, guardian_config=settings.guardian
        )
        repo.complete_run(
            run_id, datetime.now(UTC), "ok" if not errors else "partial_error", errors
        )
        return closed
```

to:

```python
        closed = close_triggered_positions(
            repo, price_lookup, now, settings.risk_limits, run_id, guardian_config=settings.guardian
        )
        try:
            run_profit_protection_experiment_tick(
                repo, open_positions, closed, price_lookup, now, settings, run_id
            )
        except Exception as exc:
            log_event(
                run_id, event="profit_protection_experiment_tick_failed",
                error_type=type(exc).__name__, error=str(exc),
            )
        repo.complete_run(
            run_id, datetime.now(UTC), "ok" if not errors else "partial_error", errors
        )
        return closed
```

No other line of `monitoring_loop.py` changes.

- [ ] **Step 4: Run the new test to verify it passes**

Run: `pytest tests/crypto_trading/test_monitoring_loop.py -k profit_protection_experiment_never_affects -v`
Expected: PASS

- [ ] **Step 5: Run the full monitoring_loop test file**

Run: `pytest tests/crypto_trading/test_monitoring_loop.py -v`
Expected: all PASS (zero regressions — every pre-existing test in this file
must still pass unchanged)

- [ ] **Step 6: Run the full paper_trading + storage + config suites again**

Run: `pytest tests/crypto_trading/paper_trading/ tests/crypto_trading/storage/ tests/crypto_trading/config/ -v`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add crypto_trading/monitoring_loop.py tests/crypto_trading/test_monitoring_loop.py
git commit -m "feat(crypto-trading): hook PP experiment into monitoring tick, isolated by its own try/except (G10)"
```

---

## Task 11: Report generator

**Files:**
- Create: `crypto_trading/performance/profit_protection_report.py`
- Test: `tests/crypto_trading/performance/test_profit_protection_report.py`

**Interfaces:**
- Consumes: `Repository.find_all_profit_protection_shadows`, `Repository.get_position`, `FROZEN_THRESHOLDS_PCT`, `performance/metrics.py::compute_win_rate/compute_expectancy/compute_profit_factor`

- [ ] **Step 1: Write the failing tests**

```python
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.performance.profit_protection_report import (
    _BREAKEVEN_BAND_PCT,
    _classify_reach,
    _conversion_ratio,
    _sample_sizes,
    build_report,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _row(**overrides) -> dict:
    defaults = dict(
        shadow_id="pos-1:0.010", position_id="pos-1", instrument="BTCUSDT",
        threshold_pct="0.010", status="CLOSED", threshold_reached=1,
        entry_price="50000", mfe="1000", mae="-200",
        shadow_realized_pnl="300", hypothetical_baseline_exit_reason="target",
        hypothetical_baseline_pnl="1000", exit_reason="stop_loss",
    )
    defaults.update(overrides)
    return defaults


def test_classify_reach_never_reached(tmp_path):
    row = _row(threshold_reached=0, hypothetical_baseline_pnl=None,
               hypothetical_baseline_exit_reason=None)
    assert _classify_reach(row, position_size=Decimal("5000")) == "never_reached_threshold"


def test_classify_reach_loss(tmp_path):
    row = _row(hypothetical_baseline_pnl="-500")
    assert _classify_reach(row, position_size=Decimal("5000")) == "reached_threshold_baseline_loss"


def test_classify_reach_approx_breakeven(tmp_path):
    row = _row(hypothetical_baseline_pnl="10")  # 10/5000 = 0.002 <= 0.003 band
    assert (
        _classify_reach(row, position_size=Decimal("5000"))
        == "reached_threshold_baseline_approx_breakeven"
    )


def test_classify_reach_big_winner(tmp_path):
    row = _row(hypothetical_baseline_pnl="1000", hypothetical_baseline_exit_reason="target")
    assert (
        _classify_reach(row, position_size=Decimal("5000"))
        == "reached_threshold_baseline_big_winner"
    )


def test_classify_reach_moderate_gain(tmp_path):
    row = _row(hypothetical_baseline_pnl="200", hypothetical_baseline_exit_reason="time_limit")
    assert (
        _classify_reach(row, position_size=Decimal("5000"))
        == "reached_threshold_baseline_moderate_gain"
    )


def test_classify_reach_pending_when_baseline_not_yet_known(tmp_path):
    row = _row(hypothetical_baseline_pnl=None, hypothetical_baseline_exit_reason=None)
    assert (
        _classify_reach(row, position_size=Decimal("5000"))
        == "reached_threshold_baseline_pending"
    )


def test_classification_buckets_are_exhaustive_and_mutually_exclusive():
    known_buckets = {
        "never_reached_threshold", "reached_threshold_baseline_loss",
        "reached_threshold_baseline_approx_breakeven", "reached_threshold_baseline_big_winner",
        "reached_threshold_baseline_moderate_gain", "reached_threshold_baseline_pending",
    }
    scenarios = [
        _row(threshold_reached=0, hypothetical_baseline_pnl=None, hypothetical_baseline_exit_reason=None),
        _row(hypothetical_baseline_pnl="-1"),
        _row(hypothetical_baseline_pnl="0"),
        _row(hypothetical_baseline_pnl="5000", hypothetical_baseline_exit_reason="target"),
        _row(hypothetical_baseline_pnl="200", hypothetical_baseline_exit_reason="time_limit"),
        _row(hypothetical_baseline_pnl=None, hypothetical_baseline_exit_reason=None, threshold_reached=1),
    ]
    labels = [_classify_reach(row, position_size=Decimal("5000")) for row in scenarios]
    assert set(labels) <= known_buckets
    assert len(labels) == len(scenarios)  # one label per row, none skipped/duplicated


def test_conversion_ratio_is_none_when_mfe_not_positive():
    row = _row(mfe="0")
    assert _conversion_ratio(row, position_size=Decimal("5000"), entry_price=Decimal("50000")) is None


def test_conversion_ratio_dimensionless_formula():
    row = _row(mfe="500", shadow_realized_pnl="250")  # mfe_pct=0.01, pnl_pct=0.05
    ratio = _conversion_ratio(row, position_size=Decimal("5000"), entry_price=Decimal("50000"))
    assert ratio == Decimal("5")  # 0.05 / 0.01


def test_sample_sizes_counts_match_definitions():
    rows = [
        _row(shadow_id="a", threshold_reached=0, hypothetical_baseline_pnl=None,
             hypothetical_baseline_exit_reason=None, shadow_realized_pnl="0"),
        _row(shadow_id="b", hypothetical_baseline_pnl="-500", shadow_realized_pnl="0"),
        _row(shadow_id="c", hypothetical_baseline_exit_reason="target", shadow_realized_pnl="300"),
        _row(shadow_id="d", shadow_realized_pnl="-50", hypothetical_baseline_pnl="-50"),
    ]
    sizes = _sample_sizes(rows)
    assert sizes["n_closed"] == 4
    assert sizes["n_reached_threshold"] == 3
    assert sizes["n_not_reached"] == 1
    assert sizes["n_baseline_losses_after_threshold"] == 2  # b and d
    assert sizes["n_baseline_target_winners_after_threshold"] == 1  # c
    assert sizes["n_shadow_winners"] == 1  # c
    assert sizes["n_shadow_losses"] == 1  # d


def test_report_note_is_always_present_regardless_of_data(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    report = build_report(repo)
    assert "Pre-registered hypotheses under test" in report["note"]
    assert "never selects a winner" in report["note"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/performance/test_profit_protection_report.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement the report module**

`crypto_trading/performance/profit_protection_report.py`:

```python
"""Read-only Profit Protection experiment report (2026-09-11). See
docs/superpowers/specs/2026-09-11-profit-protection-experiment-design.md.

Never writes to the DB, never started by run.py - run manually:
`python -m crypto_trading.performance.profit_protection_report`.

Spec G9 / plan correction: +1.0% and +1.5% are frozen, pre-registered
hypotheses. This report never selects a winner or recommends promotion to
production - see the fixed `note` field, always present regardless of
data."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.config.loader import get_settings
from crypto_trading.paper_trading.profit_protection_experiment import FROZEN_THRESHOLDS_PCT
from crypto_trading.performance.metrics import (
    compute_expectancy,
    compute_profit_factor,
    compute_win_rate,
)
from crypto_trading.storage.repository import Repository, SQLiteRepository

_BREAKEVEN_BAND_PCT = Decimal("0.003")

_NOTE = (
    "Pre-registered hypotheses under test: +1.0% and +1.5%. This report "
    "never selects a winner or recommends promotion to production - that "
    "is a separate, later, explicit human decision."
)


def _classify_reach(row: dict, position_size: Decimal) -> str:
    """Reporting-only classification (spec §7.2, G9 addendum) - never
    feeds back into simulation, PnL, or any comparison."""
    if not row["threshold_reached"]:
        return "never_reached_threshold"
    if row["hypothetical_baseline_pnl"] is None:
        return "reached_threshold_baseline_pending"
    baseline_pnl = Decimal(row["hypothetical_baseline_pnl"])
    if baseline_pnl < 0:
        return "reached_threshold_baseline_loss"
    if abs(baseline_pnl / position_size) <= _BREAKEVEN_BAND_PCT:
        return "reached_threshold_baseline_approx_breakeven"
    if row["hypothetical_baseline_exit_reason"] == "target":
        return "reached_threshold_baseline_big_winner"
    return "reached_threshold_baseline_moderate_gain"


def _conversion_ratio(row: dict, position_size: Decimal, entry_price: Decimal) -> Decimal | None:
    """realized_pnl_pct / mfe_pct - dimensionless, comparable across
    instruments (corrected definition, see spec commit 5f3b7a9)."""
    mfe = Decimal(row["mfe"])
    if mfe <= 0:
        return None
    mfe_pct = mfe / entry_price
    realized_pnl_pct = Decimal(row["shadow_realized_pnl"]) / position_size
    return realized_pnl_pct / mfe_pct


def _sample_sizes(rows: list[dict]) -> dict:
    n_closed = len(rows)
    n_reached = sum(1 for r in rows if r["threshold_reached"])
    n_baseline_losses = sum(
        1 for r in rows
        if r["threshold_reached"] and r["hypothetical_baseline_pnl"] is not None
        and Decimal(r["hypothetical_baseline_pnl"]) < 0
    )
    n_baseline_target_winners = sum(
        1 for r in rows
        if r["threshold_reached"] and r["hypothetical_baseline_exit_reason"] == "target"
    )
    n_shadow_winners = sum(
        1 for r in rows
        if r["shadow_realized_pnl"] is not None and Decimal(r["shadow_realized_pnl"]) > 0
    )
    n_shadow_losses = sum(
        1 for r in rows
        if r["shadow_realized_pnl"] is not None and Decimal(r["shadow_realized_pnl"]) < 0
    )
    return {
        "n_closed": n_closed,
        "n_reached_threshold": n_reached,
        "n_not_reached": n_closed - n_reached,
        "n_baseline_losses_after_threshold": n_baseline_losses,
        "n_baseline_target_winners_after_threshold": n_baseline_target_winners,
        "n_shadow_winners": n_shadow_winners,
        "n_shadow_losses": n_shadow_losses,
    }


def _max_drawdown(pnls_in_order: list[Decimal]) -> Decimal | None:
    if not pnls_in_order:
        return None
    running = Decimal("0")
    peak = Decimal("0")
    max_dd = Decimal("0")
    for pnl in pnls_in_order:
        running += pnl
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    return max_dd


def _outcome_label(row: dict) -> str:
    if row["pnl_difference"] is None:
        return "pending"
    diff = Decimal(row["pnl_difference"])
    baseline_pnl = Decimal(row["hypothetical_baseline_pnl"])
    shadow_pnl = Decimal(row["shadow_realized_pnl"])
    if diff == 0:
        return "protection_no_change"
    if baseline_pnl < 0 and shadow_pnl >= 0:
        return "protection_saved_a_loss"
    if row["hypothetical_baseline_exit_reason"] == "target" and diff < 0:
        return "protection_clipped_a_winner"
    return "protection_improved_other" if diff > 0 else "protection_worsened_other"


def _stats_block(rows: list[dict], repo: Repository) -> dict:
    closed = [r for r in rows if r["status"] == "CLOSED"]
    sample_sizes = _sample_sizes(closed)

    shadow_pnls, baseline_pnls, ratios, ratios_excluded = [], [], [], 0
    trades = []
    reach_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}

    for row in closed:
        real_position = repo.get_position(row["position_id"])
        position_size = real_position.size if real_position is not None else Decimal("1")
        entry_price = Decimal(row["entry_price"])

        bucket = _classify_reach(row, position_size)
        reach_counts[bucket] = reach_counts.get(bucket, 0) + 1

        if row["shadow_realized_pnl"] is not None:
            shadow_pnls.append(Decimal(row["shadow_realized_pnl"]))
        if row["hypothetical_baseline_pnl"] is not None:
            baseline_pnls.append(Decimal(row["hypothetical_baseline_pnl"]))

        ratio = _conversion_ratio(row, position_size, entry_price)
        if ratio is None:
            ratios_excluded += 1
        else:
            ratios.append(ratio)

        outcome = _outcome_label(row)
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

        trades.append({
            "position_id": row["position_id"],
            "baseline_actual_exit_reason": row["hypothetical_baseline_exit_reason"],
            "baseline_actual_pnl": row["hypothetical_baseline_pnl"],
            "profit_protection_hypothetical_exit_reason": row["exit_reason"],
            "profit_protection_hypothetical_pnl": row["shadow_realized_pnl"],
            "pnl_difference": row["pnl_difference"],
            "outcome_label": outcome,
            "reach_classification": bucket,
        })

    improved = sum(1 for r in closed if r["pnl_difference"] is not None and Decimal(r["pnl_difference"]) > 0)
    worsened = sum(1 for r in closed if r["pnl_difference"] is not None and Decimal(r["pnl_difference"]) < 0)
    loss_saved = sum(
        1 for r in closed
        if r["hypothetical_baseline_pnl"] is not None and r["shadow_realized_pnl"] is not None
        and Decimal(r["hypothetical_baseline_pnl"]) < 0 and Decimal(r["shadow_realized_pnl"]) >= 0
    )
    winner_clipped = sum(
        1 for r in closed
        if r["hypothetical_baseline_exit_reason"] == "target"
        and r["exit_reason"] != "target"
        and r["pnl_difference"] is not None and Decimal(r["pnl_difference"]) < 0
    )

    return {
        "sample_sizes": sample_sizes,
        "reach_classification_counts": reach_counts,
        "profit_protection_improved_pl": {"count": improved, "total_usdt": str(sum(
            (Decimal(r["pnl_difference"]) for r in closed if r["pnl_difference"] is not None
             and Decimal(r["pnl_difference"]) > 0), Decimal("0")))},
        "profit_protection_worsened_pl": {"count": worsened, "total_usdt": str(sum(
            (Decimal(r["pnl_difference"]) for r in closed if r["pnl_difference"] is not None
             and Decimal(r["pnl_difference"]) < 0), Decimal("0")))},
        "loss_saved_count": loss_saved,
        "large_winner_clipped_count": winner_clipped,
        "outcome_label_counts": outcome_counts,
        "shadow_total_pnl_usdt": str(sum(shadow_pnls, Decimal("0"))),
        "shadow_win_rate": str(compute_win_rate(shadow_pnls)) if compute_win_rate(shadow_pnls) is not None else None,
        "shadow_expectancy_usdt": str(compute_expectancy(shadow_pnls)) if compute_expectancy(shadow_pnls) is not None else None,
        "shadow_profit_factor": str(compute_profit_factor(shadow_pnls)) if compute_profit_factor(shadow_pnls) is not None else None,
        "shadow_max_drawdown_usdt": str(_max_drawdown(shadow_pnls)) if _max_drawdown(shadow_pnls) is not None else None,
        "baseline_total_pnl_usdt": str(sum(baseline_pnls, Decimal("0"))),
        "baseline_win_rate": str(compute_win_rate(baseline_pnls)) if compute_win_rate(baseline_pnls) is not None else None,
        "baseline_expectancy_usdt": str(compute_expectancy(baseline_pnls)) if compute_expectancy(baseline_pnls) is not None else None,
        "conversion_ratio_avg": str(sum(ratios, Decimal("0")) / len(ratios)) if ratios else None,
        "conversion_ratio_excluded_count": ratios_excluded,
        "trades": trades,
    }


def build_report(repo: Repository) -> dict:
    all_rows = repo.find_all_profit_protection_shadows()
    per_threshold = {}
    for threshold_pct in FROZEN_THRESHOLDS_PCT:
        key = str(threshold_pct)
        rows_for_threshold = [r for r in all_rows if r["threshold_pct"] == key]
        rows_for_threshold.sort(key=lambda r: r["opened_at"])
        midpoint = len(rows_for_threshold) // 2
        per_threshold[key] = {
            **_stats_block(rows_for_threshold, repo),
            "chronological_split": {
                "first_half": _stats_block(rows_for_threshold[:midpoint], repo),
                "second_half": _stats_block(rows_for_threshold[midpoint:], repo),
            },
        }

    combined_closed = [r for r in all_rows if r["status"] == "CLOSED"]
    combined = {
        "note_on_combined": (
            "Row-level counts pooled across both thresholds - a single "
            "real position that produced two closed shadow rows (one per "
            "threshold) contributes two rows here, this is not a "
            "deduplicated position count."
        ),
        "sample_sizes": _sample_sizes(combined_closed),
    }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "note": _NOTE,
        "per_threshold": per_threshold,
        "combined": combined,
    }


def main() -> None:
    settings = get_settings()
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    report = build_report(repo)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/performance/test_profit_protection_report.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/performance/profit_protection_report.py tests/crypto_trading/performance/test_profit_protection_report.py
git commit -m "feat(crypto-trading): add read-only Profit Protection experiment report"
```

---

## Task 12: Full-suite run + scoped read-only diff verification

**Files:** none created/modified — verification only.

- [ ] **Step 1: Run the entire test suite**

Run: `pytest -q`
Expected: 0 failures. Record the exact pass count in your final report to
the user.

- [ ] **Step 2: Confirm zero diff in every file the spec and this plan say must
never change**

Run (from the repo root, comparing against the commit immediately before
Task 1's first commit — substitute `<pre-feature-sha>` with that commit
hash):

```bash
git diff <pre-feature-sha> HEAD -- \
  crypto_trading/guardian/ \
  crypto_trading/gate/ \
  crypto_trading/agents/ \
  crypto_trading/screening/ \
  crypto_trading/live_execution_loop.py \
  crypto_trading/paper_trading/live_execution.py \
  crypto_trading/connectors/bingx_live_trading.py \
  crypto_trading/paper_trading/position_opening.py \
  crypto_trading/paper_trading/position_sizing.py \
  crypto_trading/paper_trading/monitoring.py \
  crypto_trading/paper_trading/position_closing.py \
  crypto_trading/paper_trading/execution.py \
  crypto_trading/discovery_loop.py \
  crypto_trading/demo_execution_loop.py \
  crypto_trading/run.py
```

Expected: **empty output** — zero diff hunks in every one of these files.
If anything shows up here, stop and fix it before proceeding; this is the
literal, direct proof of spec G1–G4.

- [ ] **Step 3: Confirm the `monitoring_loop.py` diff is exactly the two
changes described in Task 10, nothing else**

```bash
git diff <pre-feature-sha> HEAD -- crypto_trading/monitoring_loop.py
```

Expected: the diff shows only (a) the new import, (b)
`open_positions = list(repo.find_open_positions())` replacing the direct
`for position in repo.find_open_positions():`, and (c) the new
`try:`/`except Exception:` block calling
`run_profit_protection_experiment_tick`. No other line changed.

- [ ] **Step 4: Confirm the `config/loader.py` diff is purely additive**

```bash
git diff <pre-feature-sha> HEAD -- crypto_trading/config/loader.py
```

Expected: only the new `ProfitProtectionExperimentConfig` class, the new
`Settings.profit_protection_experiment` field, and the new
`_load_yaml_model(...)` line in `get_settings()`. No existing field,
class, or function body changed.

- [ ] **Step 5: Confirm `storage/db.py`/`storage/repository.py` diffs are
purely additive**

```bash
git diff <pre-feature-sha> HEAD -- crypto_trading/storage/db.py crypto_trading/storage/repository.py
```

Expected: only the new `CREATE TABLE`/`CREATE INDEX` statements, the new
`schema_meta`-backed watermark methods, and the new
`profit_protection_shadow_positions`-backed methods. No existing table
definition, migration function, or method body changed.

- [ ] **Step 6: Confirm no LIVE-money or demo-order code path was touched**

```bash
git diff <pre-feature-sha> HEAD --stat
```

Expected: the full list of changed files matches exactly the "File
Structure" table at the top of this plan — no file outside that list
appears.

- [ ] **Step 7: Report to the user**

Prepare a summary containing:
- The final commit hash.
- The exact file list from `git diff <pre-feature-sha> HEAD --stat`.
- The `pytest -q` pass count.
- Confirmation text: "LIVE execution, Demo execution, Guardian, Gate, Risk
  Agent, the AI roles, position sizing, the screener, Discovery, and
  baseline PAPER exit logic (`position_closing.py`, `monitoring.py`) have
  zero diff hunks — verified directly above, not asserted."
- How to enable: set `enabled: true` in
  `crypto_trading/config/profit_protection_experiment.yaml`, restart the
  monitoring process.
- How to read results:
  `python -m crypto_trading.performance.profit_protection_report`.

---

## Self-Review Notes (completed during planning)

- **Spec coverage:** §1 (Task 9's `enabled` gate) · §2/G1-G10 (Tasks 2-10,
  each cross-referenced above) · §3 (Tasks 1-11 file list matches §3.1
  exactly plus the 3 correction files) · §4 (Tasks 2-4) · §5 (Tasks 5-9) ·
  §6 (Task 1) · §7 (Task 11, every named metric present: sample sizes,
  reach classification, improved/worsened, loss-saved/winner-clipped,
  corrected conversion ratio, chronological split, frozen note) · §8
  (every task's own test list, plus Task 10's G10 test) · §9 (Task 12) ·
  §10 (Task 12 Step 7's user-facing summary).
- **Placeholder scan:** none found — every step has runnable code or an
  exact shell command.
- **Type consistency:** `shadow_id`, `advance_shadow`'s full parameter
  list, and every `Repository` method name/signature are identical
  everywhere they are used across Tasks 3-10 (cross-checked during
  writing). `run_profit_protection_experiment_tick`'s signature
  (`repo, open_positions, closed_positions, price_lookup, now, settings,
  run_id`) matches exactly between Task 9's definition and Task 10's call
  site in `monitoring_loop.py`.
