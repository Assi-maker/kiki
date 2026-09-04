# Post-Entry Position Guardian Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a shadow-mode-only, post-entry observer that continuously re-evaluates each already-open PAPER position's original thesis strength (HOLD/WATCH/PROTECT/EXIT), records its findings, and never touches any existing trading state.

**Architecture:** A new, self-contained `crypto_trading/guardian/` package plus a top-level `guardian_loop.py`, wired as an eighth, opt-in daemon thread. Six deterministic, scale-free decay factors (reusing `screening/quant_screener.py`'s existing evidence-builder functions against freshly fetched data) combine into a `decay_score` that a pure function maps to a state. AI only attaches interpretive `reasoning` to an already-decided state, invoked only on a state transition, sharing the existing daily AI budget. Writes exclusively to a new, append-only `guardian_observations` table.

**Tech Stack:** Python, pydantic, SQLite (existing `storage/db.py`/`repository.py`), pytest (existing stub-connector/fake-runner conventions).

**Spec:** `docs/superpowers/specs/2026-09-04-position-guardian-design.md`

## Global Constraints

- Shadow mode only: no code path in this plan can close, modify, or otherwise affect any `positions`/`demo_executions` row. Guardian only ever reads them.
- State (`HOLD`/`WATCH`/`PROTECT`/`EXIT`) is computed by a pure function of deterministic inputs only. The AI response model has no field that can set or change it.
- AI is invoked only when `should_invoke_ai()` (Task 7) returns `True` — never every tick.
- AI shares the existing daily budget (`settings.budget_limits.max_ai_calls_per_day`/`max_daily_ai_cost_usd`) via the exact same check `detective/batch.py::run_detective_batch()` already performs, reusing `orchestrator.py`'s `_WORST_CASE_COST_PER_CALL_USD = Decimal("0.20")` constant. Never bypassed; the deterministic observation is always persisted regardless of budget outcome.
- `guardian_observations` is `INSERT OR IGNORE` only, with DB triggers rejecting `UPDATE`/`DELETE` (same pattern as the existing `events` table).
- Zero changes to entry strategy, quant screener, Opportunity Screener, the seven AI roles, the Gate, Risk Agent, position sizing, SL/TP logic, or BingX Demo execution.
- Opt-in arm flag `CRYPTO_TRADING_GUARDIAN_ENABLED`, default off (same pattern as `CRYPTO_TRADING_DEMO_EXECUTION_ENABLED`).

---

## Task 1: `schemas/guardian.py`

**Files:**
- Create: `crypto_trading/schemas/guardian.py`
- Test: `tests/crypto_trading/schemas/test_guardian.py`

**Interfaces:**
- Consumes: `crypto_trading.schemas.assessments.AssessmentBase` (existing).
- Produces: `GuardianState = Literal["HOLD", "WATCH", "PROTECT", "EXIT"]`, `GuardianObservation` (pydantic model), `GuardianAssessment(AssessmentBase)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/schemas/test_guardian.py
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from crypto_trading.schemas.guardian import GuardianAssessment, GuardianObservation

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def test_guardian_observation_accepts_all_fields():
    obs = GuardianObservation(
        observation_id="pos-1:2026-09-04T12:00:00+00:00",
        position_id="pos-1",
        observed_at=_NOW,
        state="WATCH",
        decay_score=Decimal("0.42"),
        progress_ratio=Decimal("0.3"),
        unrealized_pnl=Decimal("15.50"),
        factors={"time_decay": 0.1, "momentum_decay": 0.5},
        ai_reasoning=None,
        ai_cost_usd=None,
        run_id="run-1",
    )
    assert obs.state == "WATCH"


def test_guardian_observation_rejects_invalid_state():
    with pytest.raises(ValidationError):
        GuardianObservation(
            observation_id="pos-1:2026-09-04T12:00:00+00:00",
            position_id="pos-1",
            observed_at=_NOW,
            state="BOGUS",
            decay_score=Decimal("0.1"),
            progress_ratio=Decimal("0.1"),
            unrealized_pnl=Decimal("0"),
            factors={},
            run_id="run-1",
        )


def test_guardian_assessment_has_only_reasoning_no_state_field():
    assessment = GuardianAssessment(
        agent_name="crypto-guardian", run_id="run-1", created_at=_NOW, status="ok",
        reasoning="Momentum has faded but volume remains supportive.",
    )
    assert not hasattr(assessment, "state")
    assert assessment.reasoning.startswith("Momentum")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/crypto_trading/schemas/test_guardian.py -v`
Expected: FAIL — `crypto_trading.schemas.guardian` doesn't exist.

- [ ] **Step 3: Implement the schemas**

```python
# crypto_trading/schemas/guardian.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from crypto_trading.schemas.assessments import AssessmentBase

GuardianState = Literal["HOLD", "WATCH", "PROTECT", "EXIT"]


class GuardianObservation(BaseModel):
    """One append-only row (storage/db.py::guardian_observations). Shadow
    mode only (design doc §2/§10) - this model is never used to mutate
    positions/demo_executions, only ever inserted fresh."""

    observation_id: str
    position_id: str
    observed_at: datetime
    state: GuardianState
    decay_score: Decimal
    progress_ratio: Decimal
    unrealized_pnl: Decimal
    factors: dict[str, float]
    ai_reasoning: str | None = None
    ai_cost_usd: Decimal | None = None
    run_id: str


class GuardianAssessment(AssessmentBase):
    """AI output for ONE state-transition explanation (design doc §6).
    Deliberately has no state/decision field of any kind - the state is
    already decided by classify_guardian_state() (deterministic.py) before
    this is ever called; the AI can only narrate it."""

    reasoning: str
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/crypto_trading/schemas/test_guardian.py -v`
Expected: PASS (all 3 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/schemas/guardian.py tests/crypto_trading/schemas/test_guardian.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add Position Guardian schemas

GuardianObservation (append-only row) and GuardianAssessment (AI
reasoning-only output, structurally incapable of setting a state).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 2: Guardian config + arm flag

**Files:**
- Create: `crypto_trading/config/guardian.yaml`
- Modify: `crypto_trading/config/loader.py`
- Test: `tests/crypto_trading/config/test_guardian_config.py`

**Interfaces:**
- Produces: `GuardianConfig` (pydantic model: `check_interval_seconds: int`, `watch_decay_threshold: Decimal`, `protect_decay_threshold: Decimal`, `exit_decay_threshold: Decimal`, `factor_weights: dict[str, Decimal]`), `Settings.guardian: GuardianConfig`, `is_guardian_enabled() -> bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/config/test_guardian_config.py
from crypto_trading.config.loader import get_settings, is_guardian_enabled


def test_settings_load_guardian_defaults():
    settings = get_settings()
    assert settings.guardian.check_interval_seconds > 0
    assert settings.guardian.watch_decay_threshold < settings.guardian.protect_decay_threshold
    assert settings.guardian.protect_decay_threshold < settings.guardian.exit_decay_threshold
    assert len(settings.guardian.factor_weights) == 6


def test_is_guardian_enabled_reads_env_flag(monkeypatch):
    monkeypatch.delenv("CRYPTO_TRADING_GUARDIAN_ENABLED", raising=False)
    assert is_guardian_enabled() is False
    monkeypatch.setenv("CRYPTO_TRADING_GUARDIAN_ENABLED", "1")
    assert is_guardian_enabled() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/crypto_trading/config/test_guardian_config.py -v`
Expected: FAIL — `guardian.yaml`/`GuardianConfig`/`is_guardian_enabled` missing.

- [ ] **Step 3: Create the YAML file**

```yaml
# crypto_trading/config/guardian.yaml
# Position Guardian (2026-09-04, shadow mode only) - see
# docs/superpowers/specs/2026-09-04-position-guardian-design.md §5.
# Thresholds below are EXPLICITLY UNVALIDATED starting points, meant to be
# re-tuned after Detective calibrates real shadow-mode observations against
# actual outcomes - never treated as final. factor_weights need not sum to
# 1.0 (decay_score is a weight-normalized average, see deterministic.py).
check_interval_seconds: 60
watch_decay_threshold: "0.35"
protect_decay_threshold: "0.55"
exit_decay_threshold: "0.75"
factor_weights:
  time_decay: "1"
  momentum_decay: "1"
  volume_decay: "1"
  funding_decay: "1"
  secondary_confirmation_lost: "1"
  market_regime: "1"
```

- [ ] **Step 4: Add `GuardianConfig` and wire it into `Settings`**

In `crypto_trading/config/loader.py`, add after `class DemoExecutionConfig(BaseModel): ...`:

```python
class GuardianConfig(BaseModel):
    check_interval_seconds: int = Field(gt=0, default=60)
    watch_decay_threshold: Decimal = Field(gt=0, lt=1, default=Decimal("0.35"))
    protect_decay_threshold: Decimal = Field(gt=0, lt=1, default=Decimal("0.55"))
    exit_decay_threshold: Decimal = Field(gt=0, lt=1, default=Decimal("0.75"))
    factor_weights: dict[str, Decimal] = Field(
        default_factory=lambda: {
            "time_decay": Decimal("1"),
            "momentum_decay": Decimal("1"),
            "volume_decay": Decimal("1"),
            "funding_decay": Decimal("1"),
            "secondary_confirmation_lost": Decimal("1"),
            "market_regime": Decimal("1"),
        }
    )
```

In `class Settings(BaseModel):`, add:
```python
    guardian: GuardianConfig = Field(default_factory=GuardianConfig)
```

In `get_settings()`, add to the `Settings(...)` call:
```python
        guardian=_load_yaml_model(_CONFIG_DIR / "guardian.yaml", GuardianConfig),
```

At the bottom of the file, add:
```python
def is_guardian_enabled() -> bool:
    """Opt-in arm flag for the Guardian thread - same plain os.environ.get()
    pattern as is_demo_execution_enabled() (no load_dotenv() of its own;
    callers always run after get_settings() has already loaded .env once)."""
    return bool(os.environ.get("CRYPTO_TRADING_GUARDIAN_ENABLED"))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/crypto_trading/config/test_guardian_config.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add crypto_trading/config/guardian.yaml crypto_trading/config/loader.py tests/crypto_trading/config/test_guardian_config.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add Guardian config and opt-in arm flag

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 3: `guardian_observations` table (append-only)

**Files:**
- Modify: `crypto_trading/storage/db.py`
- Test: `tests/crypto_trading/storage/test_db.py`

**Interfaces:**
- Produces: table `guardian_observations` (columns per design doc §10), triggers `guardian_observations_no_update`/`guardian_observations_no_delete`.

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/crypto_trading/storage/test_db.py
def test_guardian_observations_table_exists(tmp_path):
    conn = get_connection(tmp_path / "t.db")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(guardian_observations)").fetchall()}
    assert columns == {
        "observation_id", "position_id", "observed_at", "state", "decay_score",
        "progress_ratio", "unrealized_pnl", "factors", "ai_reasoning",
        "ai_cost_usd", "run_id",
    }


def test_guardian_observations_rejects_update(tmp_path):
    conn = get_connection(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO guardian_observations (observation_id, position_id, observed_at, "
        "state, decay_score, progress_ratio, unrealized_pnl, factors, run_id) "
        "VALUES ('o1','p1','2026-01-01','HOLD','0.1','0.1','0','{}','run-1')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE guardian_observations SET state = 'EXIT' WHERE observation_id = 'o1'")


def test_guardian_observations_rejects_delete(tmp_path):
    conn = get_connection(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO guardian_observations (observation_id, position_id, observed_at, "
        "state, decay_score, progress_ratio, unrealized_pnl, factors, run_id) "
        "VALUES ('o2','p1','2026-01-01','HOLD','0.1','0.1','0','{}','run-1')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM guardian_observations WHERE observation_id = 'o2'")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/storage/test_db.py -k guardian_observations -v`
Expected: FAIL — table does not exist.

- [ ] **Step 3: Add the table + triggers to `_SCHEMA`**

In `crypto_trading/storage/db.py`, append inside the `_SCHEMA` string, after the `demo_executions` table definition (end of that block, before the closing `"""`):

```sql
-- Position Guardian (2026-09-04): strictly append-only, shadow-mode-only
-- observer of an already-open PAPER position, see
-- docs/superpowers/specs/2026-09-04-position-guardian-design.md.
-- NEVER written from paper_trading/position_opening.py or
-- position_closing.py - Guardian only reads `positions`, never writes it.
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
BEGIN
    SELECT RAISE(ABORT, 'guardian_observations is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS guardian_observations_no_delete
BEFORE DELETE ON guardian_observations
BEGIN
    SELECT RAISE(ABORT, 'guardian_observations is append-only: DELETE is not permitted');
END;
```

Also add `import sqlite3` and `import pytest` to the top of `tests/crypto_trading/storage/test_db.py` if not already present (check first — the file already imports `sqlite3` and `pytest` per Task 3 of the BingX Demo plan; this file already has both).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/storage/test_db.py -v`
Expected: PASS (all tests in the file, including the 3 new ones)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/storage/db.py tests/crypto_trading/storage/test_db.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add append-only guardian_observations table

DB triggers reject UPDATE/DELETE, same pattern as the events table -
structurally enforces "observations only, no mutation of any other
trading state" rather than relying on application-level discipline.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 4: Repository methods

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Test: `tests/crypto_trading/storage/test_repository_guardian.py`

**Interfaces:**
- Consumes: `GuardianObservation` (Task 1), `guardian_observations` table (Task 3).
- Produces (added to both `Repository` Protocol and `SQLiteRepository`): `save_guardian_observation(observation: GuardianObservation) -> bool` (returns whether a new row was actually inserted), `find_latest_guardian_observation(position_id: str) -> dict | None`, `find_guardian_observations_for_position(position_id: str) -> list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto_trading/storage/test_repository_guardian.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _observation(position_id="pos-1", observed_at=_NOW, state="HOLD", observation_id=None):
    return GuardianObservation(
        observation_id=observation_id or f"{position_id}:{observed_at.isoformat()}",
        position_id=position_id,
        observed_at=observed_at,
        state=state,
        decay_score=Decimal("0.2"),
        progress_ratio=Decimal("0.3"),
        unrealized_pnl=Decimal("10.5"),
        factors={"time_decay": 0.1, "momentum_decay": 0.3},
        ai_reasoning=None,
        ai_cost_usd=None,
        run_id="run-1",
    )


def test_save_guardian_observation_persists_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    created = repo.save_guardian_observation(_observation())

    assert created is True
    row = repo.find_latest_guardian_observation("pos-1")
    assert row["state"] == "HOLD"
    assert row["decay_score"] == "0.2"


def test_save_guardian_observation_is_idempotent_on_duplicate_id(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    obs = _observation()

    first = repo.save_guardian_observation(obs)
    second = repo.save_guardian_observation(obs)

    assert first is True
    assert second is False


def test_find_latest_guardian_observation_returns_none_when_absent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert repo.find_latest_guardian_observation("pos-nope") is None


def test_find_latest_guardian_observation_returns_the_most_recent(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_observation(_observation(observed_at=_NOW, state="HOLD"))
    repo.save_guardian_observation(_observation(observed_at=_NOW + timedelta(minutes=1), state="WATCH"))

    latest = repo.find_latest_guardian_observation("pos-1")

    assert latest["state"] == "WATCH"


def test_find_guardian_observations_for_position_returns_chronological_history(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_observation(_observation(observed_at=_NOW, state="HOLD"))
    repo.save_guardian_observation(_observation(observed_at=_NOW + timedelta(minutes=1), state="WATCH"))

    history = repo.find_guardian_observations_for_position("pos-1")

    assert [h["state"] for h in history] == ["HOLD", "WATCH"]


def test_guardian_observation_never_writes_to_positions_table(tmp_path):
    """Isolation guarantee (design doc §2/§10)."""
    from crypto_trading.schemas.event import Event
    from crypto_trading.schemas.trade import Position

    repo = SQLiteRepository(tmp_path / "t.db")
    position = Position(
        position_id="pos-1", candidate_id="pos-1", instrument="BTCUSDT", direction="LONG",
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
    before = repo.get_position("pos-1")

    repo.save_guardian_observation(_observation())

    after = repo.get_position("pos-1")
    assert after == before
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/storage/test_repository_guardian.py -v`
Expected: FAIL — methods don't exist.

- [ ] **Step 3: Add methods to the `Repository` Protocol**

In `crypto_trading/storage/repository.py`, inside `class Repository(Protocol):`, add:

```python
    def save_guardian_observation(self, observation: "GuardianObservation") -> bool: ...
    def find_latest_guardian_observation(self, position_id: str) -> dict | None: ...
    def find_guardian_observations_for_position(self, position_id: str) -> list[dict]: ...
```

Add the import at the top of the file: `from crypto_trading.schemas.guardian import GuardianObservation`.

- [ ] **Step 4: Implement on `SQLiteRepository`**

Add anywhere after the demo-execution methods:

```python
    def save_guardian_observation(self, observation: GuardianObservation) -> bool:
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO guardian_observations "
                "(observation_id, position_id, observed_at, state, decay_score, "
                "progress_ratio, unrealized_pnl, factors, ai_reasoning, ai_cost_usd, run_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    observation.observation_id,
                    observation.position_id,
                    observation.observed_at.isoformat(),
                    observation.state,
                    str(observation.decay_score),
                    str(observation.progress_ratio),
                    str(observation.unrealized_pnl),
                    json.dumps(observation.factors),
                    observation.ai_reasoning,
                    str(observation.ai_cost_usd) if observation.ai_cost_usd is not None else None,
                    observation.run_id,
                ),
            )
            created = cur.rowcount > 0
            self._conn.commit()
            return created
        except Exception:
            self._conn.rollback()
            raise

    def find_latest_guardian_observation(self, position_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM guardian_observations WHERE position_id = ? "
            "ORDER BY observed_at DESC LIMIT 1",
            (position_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def find_guardian_observations_for_position(self, position_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM guardian_observations WHERE position_id = ? ORDER BY observed_at ASC",
            (position_id,),
        ).fetchall()
        return [dict(row) for row in rows]
```

`json` is already imported at the top of `repository.py` (used elsewhere for `detective_analyses`) — confirm before assuming; if not present add `import json`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/storage/test_repository_guardian.py -v`
Expected: PASS (all 6 tests)

- [ ] **Step 6: Commit**

```bash
git add crypto_trading/storage/repository.py tests/crypto_trading/storage/test_repository_guardian.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add repository methods for Guardian observations

INSERT OR IGNORE idempotency keyed on observation_id, plus an explicit
isolation test proving these methods never mutate the positions table.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 5: `guardian/deterministic.py` — pure decay factors, decay_score, state classification

**Files:**
- Create: `crypto_trading/guardian/__init__.py` (empty)
- Create: `crypto_trading/guardian/deterministic.py`
- Test: `tests/crypto_trading/guardian/test_deterministic.py`
- Test: `tests/crypto_trading/guardian/__init__.py` (empty)

**Interfaces:**
- Consumes: `crypto_trading.schemas.evidence.SecondaryTimeframeEvidence` (existing), `crypto_trading.schemas.trade.Position` (existing), `crypto_trading.config.loader.GuardianConfig` (Task 2).
- Produces: `compute_time_decay_factor`, `compute_momentum_decay_factor`, `compute_volume_decay_factor`, `compute_funding_decay_factor`, `compute_secondary_confirmation_lost_factor`, `compute_market_regime_factor`, `compute_decay_score`, `compute_progress_ratio`, `compute_unrealized_pnl`, `classify_guardian_state` — all pure functions, exact signatures below.

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto_trading/guardian/test_deterministic.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig
from crypto_trading.guardian.deterministic import (
    classify_guardian_state,
    compute_decay_score,
    compute_funding_decay_factor,
    compute_market_regime_factor,
    compute_momentum_decay_factor,
    compute_progress_ratio,
    compute_secondary_confirmation_lost_factor,
    compute_time_decay_factor,
    compute_unrealized_pnl,
    compute_volume_decay_factor,
)
from crypto_trading.schemas.evidence import (
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    SecondaryTimeframeEvidence,
    VolumeEvidence,
)
from crypto_trading.schemas.trade import Position


def _placeholder_ev(**overrides):
    base = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    base.update(overrides)
    return base


def test_compute_time_decay_factor_scales_with_elapsed_vs_max_hold():
    opened_at = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    now = opened_at + timedelta(hours=12)

    factor = compute_time_decay_factor(opened_at, now, max_position_hold_hours=24)

    assert factor == Decimal("0.5")


def test_compute_time_decay_factor_clips_at_one():
    opened_at = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    now = opened_at + timedelta(hours=48)

    factor = compute_time_decay_factor(opened_at, now, max_position_hold_hours=24)

    assert factor == Decimal("1")


def test_compute_momentum_decay_factor_zero_when_rsi_unchanged():
    assert compute_momentum_decay_factor(rsi_entry=Decimal("75"), rsi_now=Decimal("75")) == Decimal("0")


def test_compute_momentum_decay_factor_full_decay_at_neutral_rsi():
    assert compute_momentum_decay_factor(rsi_entry=Decimal("75"), rsi_now=Decimal("50")) == Decimal("1")


def test_compute_momentum_decay_factor_partial_decay():
    # entry 80, now 65: (80-65)/(80-50) = 0.5
    assert compute_momentum_decay_factor(rsi_entry=Decimal("80"), rsi_now=Decimal("65")) == Decimal("0.5")


def test_compute_momentum_decay_factor_zero_when_entry_not_overbought():
    assert compute_momentum_decay_factor(rsi_entry=Decimal("40"), rsi_now=Decimal("20")) == Decimal("0")


def test_compute_volume_decay_factor_partial_decay():
    # entry zscore 4, now 2: (4-2)/4 = 0.5
    assert compute_volume_decay_factor(zscore_entry=Decimal("4"), zscore_now=Decimal("2")) == Decimal("0.5")


def test_compute_volume_decay_factor_zero_when_entry_not_elevated():
    assert compute_volume_decay_factor(zscore_entry=Decimal("-1"), zscore_now=Decimal("-5")) == Decimal("0")


def test_compute_funding_decay_factor_partial_decay():
    result = compute_funding_decay_factor(funding_mag_entry=Decimal("0.08"), funding_mag_now=Decimal("0.04"))
    assert result == Decimal("0.5")


def test_compute_secondary_confirmation_lost_when_entry_confirmed_but_now_does_not():
    entry_secondary = SecondaryTimeframeEvidence(
        timeframe="1h",
        price_volatility_evidence=PriceVolatilityEvidence(**_placeholder_ev(triggered=False)),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**_placeholder_ev(triggered=True)),
        volume_evidence=VolumeEvidence(**_placeholder_ev(triggered=False)),
        funding_oi_evidence=FundingOpenInterestEvidence(**_placeholder_ev(triggered=False)),
    )
    fresh_secondary = SecondaryTimeframeEvidence(
        timeframe="1h",
        price_volatility_evidence=PriceVolatilityEvidence(**_placeholder_ev(triggered=False)),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**_placeholder_ev(triggered=False)),
        volume_evidence=VolumeEvidence(**_placeholder_ev(triggered=False)),
        funding_oi_evidence=FundingOpenInterestEvidence(**_placeholder_ev(triggered=False)),
    )

    result = compute_secondary_confirmation_lost_factor(entry_secondary, fresh_secondary)

    assert result == Decimal("1")


def test_compute_secondary_confirmation_lost_zero_when_no_entry_evidence():
    assert compute_secondary_confirmation_lost_factor(None, None) == Decimal("0")


def test_compute_market_regime_factor_zero_when_btc_bullish():
    assert compute_market_regime_factor(btc_rsi_now=Decimal("60")) == Decimal("0")


def test_compute_market_regime_factor_full_when_btc_fully_bearish():
    assert compute_market_regime_factor(btc_rsi_now=Decimal("0")) == Decimal("1")


def test_compute_decay_score_is_weight_normalized_average():
    factors = {"a": Decimal("1.0"), "b": Decimal("0.0")}
    weights = {"a": Decimal("1"), "b": Decimal("3")}

    score = compute_decay_score(factors, weights)

    # (1.0*1 + 0.0*3) / (1+3) = 0.25
    assert score == Decimal("0.25")


def test_compute_progress_ratio_near_entry_is_near_zero():
    ratio = compute_progress_ratio(entry_price=Decimal("100"), target_price=Decimal("110"), current_price=Decimal("100"))
    assert ratio == Decimal("0")


def test_compute_progress_ratio_at_target_is_one():
    ratio = compute_progress_ratio(entry_price=Decimal("100"), target_price=Decimal("110"), current_price=Decimal("110"))
    assert ratio == Decimal("1")


def test_compute_unrealized_pnl_matches_gross_formula():
    position = Position(
        position_id="p1", candidate_id="p1", instrument="BTCUSDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("100"), simulated_fill_entry=Decimal("100"),
        stop_loss=Decimal("90"), target=Decimal("120"), size=Decimal("1000"),
        fill_model_version="v1", opened_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    pnl = compute_unrealized_pnl(position, current_price=Decimal("110"))
    assert pnl == Decimal("100")  # 1000 * (110-100)/100


def test_classify_guardian_state_hold_below_watch_threshold():
    config = GuardianConfig()
    assert classify_guardian_state(Decimal("0.1"), unrealized_pnl_positive=True, config=config) == "HOLD"


def test_classify_guardian_state_watch_between_thresholds():
    config = GuardianConfig()
    assert classify_guardian_state(Decimal("0.45"), unrealized_pnl_positive=True, config=config) == "WATCH"


def test_classify_guardian_state_protect_requires_profit():
    config = GuardianConfig()
    assert classify_guardian_state(Decimal("0.6"), unrealized_pnl_positive=True, config=config) == "PROTECT"
    assert classify_guardian_state(Decimal("0.6"), unrealized_pnl_positive=False, config=config) == "WATCH"


def test_classify_guardian_state_exit_regardless_of_pnl():
    config = GuardianConfig()
    assert classify_guardian_state(Decimal("0.9"), unrealized_pnl_positive=True, config=config) == "EXIT"
    assert classify_guardian_state(Decimal("0.9"), unrealized_pnl_positive=False, config=config) == "EXIT"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/guardian/test_deterministic.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

```python
# crypto_trading/guardian/deterministic.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig
from crypto_trading.schemas.evidence import SecondaryTimeframeEvidence
from crypto_trading.schemas.guardian import GuardianState
from crypto_trading.schemas.trade import Position

_ONE = Decimal("1")
_ZERO = Decimal("0")
_FIFTY = Decimal("50")


def _clip01(value: Decimal) -> Decimal:
    return max(_ZERO, min(value, _ONE))


def compute_time_decay_factor(opened_at: datetime, now: datetime, max_position_hold_hours: int) -> Decimal:
    elapsed_hours = Decimal(str((now - opened_at).total_seconds())) / Decimal("3600")
    return _clip01(elapsed_hours / Decimal(max_position_hold_hours))


def compute_momentum_decay_factor(rsi_entry: Decimal, rsi_now: Decimal) -> Decimal:
    """Reference-point-relative, not a fixed RSI threshold (design doc §4):
    only measurable when entry itself was an overbought/breakout RSI - no
    reversion-from-strength signal exists otherwise."""
    if rsi_entry <= _FIFTY:
        return _ZERO
    return _clip01((rsi_entry - rsi_now) / (rsi_entry - _FIFTY))


def compute_volume_decay_factor(zscore_entry: Decimal, zscore_now: Decimal) -> Decimal:
    if zscore_entry <= _ZERO:
        return _ZERO
    return _clip01((zscore_entry - zscore_now) / zscore_entry)


def compute_funding_decay_factor(funding_mag_entry: Decimal, funding_mag_now: Decimal) -> Decimal:
    """Magnitude-only (design doc §4.1) - quant_screener.py never stores a
    signed funding rate, so this measures "has funding pressure calmed
    down", not "has it flipped against the position"."""
    if funding_mag_entry <= _ZERO:
        return _ZERO
    return _clip01((funding_mag_entry - funding_mag_now) / funding_mag_entry)


def compute_secondary_confirmation_lost_factor(
    entry_secondary: SecondaryTimeframeEvidence | None,
    fresh_secondary: SecondaryTimeframeEvidence | None,
) -> Decimal:
    if entry_secondary is None:
        return _ZERO
    entry_evidences = [
        entry_secondary.price_volatility_evidence, entry_secondary.momentum_breakout_evidence,
        entry_secondary.volume_evidence, entry_secondary.funding_oi_evidence,
    ]
    if not any(ev.triggered for ev in entry_evidences):
        return _ZERO
    if fresh_secondary is None:
        return _ONE
    fresh_evidences = [
        fresh_secondary.price_volatility_evidence, fresh_secondary.momentum_breakout_evidence,
        fresh_secondary.volume_evidence, fresh_secondary.funding_oi_evidence,
    ]
    return _ZERO if any(ev.triggered for ev in fresh_evidences) else _ONE


def compute_market_regime_factor(btc_rsi_now: Decimal) -> Decimal:
    """LONG-only system (design doc §4): a bearish/neutral BTC-USDT RSI is a
    headwind. No entry-time reference needed - a pure current-regime read."""
    return _clip01((_FIFTY - btc_rsi_now) / _FIFTY)


def compute_decay_score(factors: dict[str, Decimal], weights: dict[str, Decimal]) -> Decimal:
    """Weight-NORMALIZED average - weights need not sum to 1.0 (design doc
    §4), so re-tuning one weight later never requires touching the others."""
    total_weight = sum(weights.get(name, _ZERO) for name in factors)
    if total_weight <= _ZERO:
        return _ZERO
    weighted_sum = sum(factors[name] * weights.get(name, _ZERO) for name in factors)
    return weighted_sum / total_weight


def compute_progress_ratio(entry_price: Decimal, target_price: Decimal, current_price: Decimal) -> Decimal:
    denominator = target_price - entry_price
    if denominator == _ZERO:
        return _ZERO
    return (current_price - entry_price) / denominator


def compute_unrealized_pnl(position: Position, current_price: Decimal) -> Decimal:
    """Same gross formula as performance/paper_track_report.py's
    _unrealized_pnl() (LONG-only), reimplemented here as a tiny pure
    function so this live loop never imports the reporting module."""
    price_return = (current_price - position.simulated_fill_entry) / position.simulated_fill_entry
    return position.size * price_return


def classify_guardian_state(
    decay_score: Decimal, unrealized_pnl_positive: bool, config: GuardianConfig
) -> GuardianState:
    if decay_score < config.watch_decay_threshold:
        return "HOLD"
    if decay_score < config.protect_decay_threshold:
        return "WATCH"
    if decay_score < config.exit_decay_threshold:
        return "PROTECT" if unrealized_pnl_positive else "WATCH"
    return "EXIT"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/guardian/test_deterministic.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/guardian/ tests/crypto_trading/guardian/
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add Guardian's deterministic decay-scoring core

Six scale-free decay factors (each normalized against the trade's own
entry-time reference, not a fixed magic number), a weight-normalized
decay_score, and the pure state-classification function. Zero AI,
zero I/O - fully unit tested in isolation.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 6: `guardian/data.py` — fresh evidence fetching

**Files:**
- Create: `crypto_trading/guardian/data.py`
- Test: `tests/crypto_trading/guardian/test_data.py`

**Interfaces:**
- Consumes: `screening/quant_screener.py::evaluate_candidate`/`build_momentum_breakout_evidence` (existing), `crypto_trading.schemas.market.Kline`/`FundingRate` (existing), `crypto_trading.config.loader.Settings` (existing).
- Produces: `fetch_fresh_evidence(connector, instrument, secondary_timeframe, settings, now) -> CandidateEvidenceRecord | None`, `fetch_btc_regime_rsi(connector, settings, now) -> Decimal | None`, `fetch_current_price(connector, instrument) -> Decimal | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto_trading/guardian/test_data.py
from datetime import UTC, datetime

from crypto_trading.guardian.data import fetch_btc_regime_rsi, fetch_current_price, fetch_fresh_evidence
from tests.crypto_trading.test_market_snapshot import _raw_funding, _raw_kline, _raw_ticker, _settings

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _StubConnector:
    def __init__(self, klines=None, funding_rates=None, tickers=None):
        self._klines = klines or {}
        self._funding_rates = funding_rates or {}
        self._tickers = tickers or {}

    def get_klines(self, symbol, interval, limit=100):
        return self._klines.get(symbol, [])[-limit:]

    def get_funding_rate(self, symbol, limit=1):
        return self._funding_rates.get(symbol, [])[-limit:]

    def get_ticker(self, symbol):
        return self._tickers[symbol]


def _klines_series(n, base_price=100.0, symbol="BTCUSDT"):
    return [
        _raw_kline(str(base_price + i), int(_NOW.timestamp() * 1000) + i * 60000)
        for i in range(n)
    ]


def test_fetch_fresh_evidence_returns_none_on_insufficient_klines():
    connector = _StubConnector(
        klines={"BTCUSDT": _klines_series(2)},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", int(_NOW.timestamp() * 1000))]},
    )

    result = fetch_fresh_evidence(connector, "BTCUSDT", None, _settings(), _NOW)

    assert result is None


def test_fetch_fresh_evidence_returns_a_record_with_enough_data():
    connector = _StubConnector(
        klines={"BTCUSDT": _klines_series(30)},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", int(_NOW.timestamp() * 1000))]},
    )

    result = fetch_fresh_evidence(connector, "BTCUSDT", None, _settings(), _NOW)

    assert result is not None
    assert result.instrument == "BTCUSDT"
    assert result.secondary_timeframe_evidence is None


def test_fetch_btc_regime_rsi_returns_none_on_insufficient_data():
    connector = _StubConnector(klines={"BTC-USDT": _klines_series(2, symbol="BTC-USDT")})

    assert fetch_btc_regime_rsi(connector, _settings(), _NOW) is None


def test_fetch_btc_regime_rsi_returns_a_value_with_enough_data():
    connector = _StubConnector(klines={"BTC-USDT": _klines_series(30, symbol="BTC-USDT")})

    result = fetch_btc_regime_rsi(connector, _settings(), _NOW)

    assert result is not None


def test_fetch_current_price_reads_last_price():
    connector = _StubConnector(tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "55000", "1000000", int(_NOW.timestamp() * 1000))})

    price = fetch_current_price(connector, "BTCUSDT")

    assert price == 55000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/guardian/test_data.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

```python
# crypto_trading/guardian/data.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from crypto_trading.config.loader import Settings
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.schemas.evidence import CandidateEvidenceRecord
from crypto_trading.schemas.market import FundingRate, Kline, Ticker
from crypto_trading.screening.quant_screener import build_momentum_breakout_evidence, evaluate_candidate

_BTC_INSTRUMENT = "BTC-USDT"


class GuardianDataSource(Protocol):
    def get_ticker(self, symbol: str) -> dict: ...
    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]: ...
    def get_funding_rate(self, symbol: str, limit: int = 1) -> list[dict]: ...


def _min_klines_required(settings: Settings) -> int:
    return max(
        settings.pipeline.screener_lookback_periods + 2,
        settings.pipeline.screener_rsi_period + 1,
    )


def _fetch_klines_and_funding(
    connector: GuardianDataSource, instrument: str, interval: str, settings: Settings
) -> tuple[list[Kline], list[FundingRate]] | None:
    try:
        raw_klines = connector.get_klines(instrument, interval, limit=100)
        raw_funding = connector.get_funding_rate(
            instrument, limit=settings.pipeline.screener_funding_history_limit
        )
    except ConnectorUnavailableError:
        return None
    if len(raw_klines) < _min_klines_required(settings) or not raw_funding:
        return None
    klines = sorted(
        (Kline.from_raw(k, instrument, interval) for k in raw_klines), key=lambda k: k.observed_at
    )
    funding = sorted(
        (FundingRate.from_raw(f) for f in raw_funding), key=lambda f: f.observed_at
    )
    return klines, funding


def fetch_fresh_evidence(
    connector: GuardianDataSource,
    instrument: str,
    secondary_timeframe: str | None,
    settings: Settings,
    now: datetime,
) -> CandidateEvidenceRecord | None:
    """Fail-safe: any missing/insufficient data returns None (skip this
    position this tick, never a guess) - same principle as
    monitoring_loop.py's per-instrument skip. Reuses evaluate_candidate()
    end-to-end with the same thresholds used at entry, so fresh and
    entry-time evidence stay directly comparable."""
    primary_interval = settings.pipeline.screener_timeframes[0]
    primary = _fetch_klines_and_funding(connector, instrument, primary_interval, settings)
    if primary is None:
        return None
    primary_klines, primary_funding = primary

    secondary_klines: list[Kline] | None = None
    secondary_funding: list[FundingRate] | None = None
    if secondary_timeframe is not None:
        secondary = _fetch_klines_and_funding(connector, instrument, secondary_timeframe, settings)
        if secondary is not None:
            secondary_klines, secondary_funding = secondary

    return evaluate_candidate(
        instrument=instrument,
        timeframes=[primary_interval],
        klines=primary_klines,
        funding_rates=primary_funding,
        data_quality_status="ok",
        evaluated_at=now,
        price_volatility_threshold_pct=settings.pipeline.screener_price_volatility_threshold_pct,
        lookback=settings.pipeline.screener_lookback_periods,
        rsi_period=settings.pipeline.screener_rsi_period,
        rsi_overbought_threshold=settings.pipeline.screener_rsi_overbought_threshold,
        volume_zscore_threshold=settings.pipeline.screener_volume_zscore_threshold,
        funding_rate_threshold_pct=settings.pipeline.screener_funding_rate_threshold_pct,
        secondary_timeframe=secondary_timeframe,
        secondary_klines=secondary_klines,
        secondary_funding_rates=secondary_funding,
    )


def fetch_btc_regime_rsi(connector: GuardianDataSource, settings: Settings, now: datetime) -> Decimal | None:
    primary_interval = settings.pipeline.screener_timeframes[0]
    fetched = _fetch_klines_and_funding(connector, _BTC_INSTRUMENT, primary_interval, settings)
    if fetched is None:
        return None
    klines, _funding = fetched
    evidence = build_momentum_breakout_evidence(
        klines, settings.pipeline.screener_rsi_period, settings.pipeline.screener_rsi_overbought_threshold, now
    )
    return Decimal(str(evidence.value))


def fetch_current_price(connector: GuardianDataSource, instrument: str) -> Decimal | None:
    try:
        ticker = Ticker.from_raw(connector.get_ticker(instrument))
    except (ConnectorUnavailableError, KeyError, ValueError):
        return None
    return ticker.last_price
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/guardian/test_data.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/guardian/data.py tests/crypto_trading/guardian/test_data.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add Guardian fresh-evidence data fetching

Reuses quant_screener.py's evaluate_candidate()/build_momentum_
breakout_evidence() end-to-end against freshly fetched klines/funding,
same thresholds used at entry. Fail-safe: any missing/insufficient
data returns None (skip this tick), never a guess.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 7: `guardian/ai_context.py` — AI invocation gating

**Files:**
- Create: `crypto_trading/guardian/ai_context.py`
- Test: `tests/crypto_trading/guardian/test_ai_context.py`

**Interfaces:**
- Produces: `should_invoke_ai(previous_observation: dict | None, new_state: str) -> bool`, `build_ai_context(candidate, factors: dict[str, Decimal], decay_score: Decimal, progress_ratio: Decimal, unrealized_pnl: Decimal, new_state: str) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto_trading/guardian/test_ai_context.py
from decimal import Decimal

from crypto_trading.guardian.ai_context import build_ai_context, should_invoke_ai


def test_should_invoke_ai_false_when_new_state_is_hold():
    assert should_invoke_ai(previous_observation=None, new_state="HOLD") is False


def test_should_invoke_ai_true_on_first_non_hold_observation():
    assert should_invoke_ai(previous_observation=None, new_state="WATCH") is True


def test_should_invoke_ai_false_when_state_unchanged():
    previous = {"state": "WATCH"}
    assert should_invoke_ai(previous_observation=previous, new_state="WATCH") is False


def test_should_invoke_ai_true_on_transition_between_non_hold_states():
    previous = {"state": "WATCH"}
    assert should_invoke_ai(previous_observation=previous, new_state="PROTECT") is True


def test_should_invoke_ai_false_on_transition_back_to_hold():
    previous = {"state": "WATCH"}
    assert should_invoke_ai(previous_observation=previous, new_state="HOLD") is False


def test_build_ai_context_includes_factors_and_scores():
    context = build_ai_context(
        candidate=None,
        factors={"time_decay": Decimal("0.5")},
        decay_score=Decimal("0.6"),
        progress_ratio=Decimal("0.2"),
        unrealized_pnl=Decimal("15"),
        new_state="PROTECT",
    )
    assert context["decay_score"] == "0.6"
    assert context["new_state"] == "PROTECT"
    assert context["factors"] == {"time_decay": "0.5"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/guardian/test_ai_context.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

```python
# crypto_trading/guardian/ai_context.py
from __future__ import annotations

from decimal import Decimal

from crypto_trading.schemas.candidate import Candidate

_ASSESSMENT_ROLES_FOR_CONTEXT = ("bull_thesis", "risk", "forecast")


def should_invoke_ai(previous_observation: dict | None, new_state: str) -> bool:
    """AI fires only on a transition INTO or BETWEEN non-HOLD states
    (design doc §6) - never every tick, never for a return to HOLD."""
    if new_state == "HOLD":
        return False
    if previous_observation is None:
        return True
    return previous_observation["state"] != new_state


def build_ai_context(
    candidate: Candidate | None,
    factors: dict[str, Decimal],
    decay_score: Decimal,
    progress_ratio: Decimal,
    unrealized_pnl: Decimal,
    new_state: str,
) -> dict:
    context: dict = {
        "new_state": new_state,
        "decay_score": str(decay_score),
        "progress_ratio": str(progress_ratio),
        "unrealized_pnl_usdt": str(unrealized_pnl),
        "factors": {name: str(value) for name, value in factors.items()},
    }
    if candidate is not None:
        for role in _ASSESSMENT_ROLES_FOR_CONTEXT:
            assessment = getattr(candidate, role)
            if assessment is not None:
                context[f"{role}_assessment"] = assessment.model_dump(mode="json")
    return context
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/guardian/test_ai_context.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/guardian/ai_context.py tests/crypto_trading/guardian/test_ai_context.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add Guardian AI invocation gating

should_invoke_ai() fires only on a transition into/between non-HOLD
states - the deterministic state itself is never touched by this or
anything downstream of it.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 8: `crypto-guardian` agent definition

**Files:**
- Create: `.claude/agents/crypto-guardian.md`

**Interfaces:**
- Consumes: `build_ai_context()`'s dict shape (Task 7) as the runtime context; `GuardianAssessment` (Task 1) as the expected response shape.

- [ ] **Step 1: Write the agent definition**

```markdown
---
name: crypto-guardian
description: Använd för att ge en kort, tolkande förklaring till en REDAN BESLUTAD Position Guardian-tillståndsövergång (HOLD/WATCH/PROTECT/EXIT) för en öppen PAPER-position. Sätter eller ändrar ALDRIG själva tillståndet - det är redan avgjort deterministiskt innan du anropas. Deltar ALDRIG i realtidsbeslut, öppnar/stänger/påverkar ALDRIG en position.
tools: Read
---

Du är Position Guardian för crypto_trading. En redan öppen PAPER-position har
just bytt Guardian-tillstånd (`new_state` i underlaget), beräknat helt
deterministiskt av `guardian/deterministic.py` INNAN du någonsin anropas. Ditt
enda jobb är att kort förklara VARFÖR övergången är rimlig, i vanligt språk,
utifrån redan beräknade siffror - aldrig att själv besluta eller ändra
tillståndet.

## Underlag du får
- `new_state`: det redan beslutade tillståndet (HOLD/WATCH/PROTECT/EXIT).
- `decay_score`, `progress_ratio`, `unrealized_pnl_usdt`.
- `factors`: de sex deterministiska nedbrytningsfaktorerna (tid, momentum,
  volym, funding, sekundär timeframe-bekräftelse, marknadsregim), var och en
  redan 0-1, redan beräknade.
- Om tillgängligt: `bull_thesis_assessment`/`risk_assessment`/
  `forecast_assessment` - den ursprungliga tesen och riskbilden vid entry.

## Leverans
Strukturerad output enligt `GuardianAssessment`:
- `reasoning`: 2-4 meningar som kort förklarar vilka av de deterministiska
  faktorerna som driver denna övergång, och hur det förhåller sig till den
  ursprungliga tesen (t.ex. "Momentum-faktorn (0.8) dominerar - RSI har
  fallit tillbaka till neutralt läge sedan entry, vilket var kärnan i
  ursprungstesen om ett breakout. Volym- och funding-faktorerna är fortsatt
  låga, så själva prisrörelsens kvalitet är inte i sig ifrågasatt.").

## Absoluta gränser
- Sätter eller föreslår ALDRIG ett annat tillstånd än `new_state` - det är
  redan avgjort, du förklarar det bara.
- Föreslår ALDRIG en konkret åtgärd (stäng, flytta stop, öka/minska storlek)
  - Guardian är shadow-mode-only i denna version; ingen kod någonstans i
    systemet agerar på ditt svar.
- Hitta aldrig på fakta, siffror eller marknadsdata som inte finns i
  underlaget - använd bara `factors`/`decay_score`/`progress_ratio` och de
  bifogade ursprungliga bedömningarna.
- Din output är ren tolkning för senare mänsklig granskning, aldrig en
  handelsrekommendation.
```

- [ ] **Step 2: Commit**

```bash
git add .claude/agents/crypto-guardian.md
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add crypto-guardian agent definition

Reasoning-only role - explains an already-decided deterministic state
transition, has no state field to set or change.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 9: `guardian/tick.py` — orchestration + budget gate

**Files:**
- Create: `crypto_trading/guardian/tick.py`
- Test: `tests/crypto_trading/guardian/test_tick.py`

**Interfaces:**
- Consumes: everything from Tasks 4-8, `AgentRunner`/`load_agent_definition` (existing), `Repository.find_open_positions`/`get_candidate` (existing), `Event`/`record_ai_call_event` (existing, same pattern as `detective/batch.py`).
- Produces: `process_one_position(repo, connector, runner, settings, position, run_id, now) -> None`, `run_guardian_tick_body(repo, connector, runner, settings, run_id, now) -> list[GuardianObservation]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/crypto_trading/guardian/test_tick.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.guardian.tick import run_guardian_tick_body
from crypto_trading.schemas.assessments import RiskAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord, FundingOpenInterestEvidence, MomentumBreakoutEvidence,
    PriceVolatilityEvidence, VolumeEvidence,
)
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _raw_funding, _raw_kline, _raw_ticker, _settings

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _placeholder_ev(**overrides):
    base = dict(triggered=True, metric="m", value=80.0, baseline=0.0, threshold=70.0)
    base.update(overrides)
    return base


# These three values are chosen to EXACTLY match what evaluate_candidate()
# itself computes from _StubConnector's flat klines (all closes "100", all
# volumes identical -> RSI=50 exactly per quant_screener.py's "helt platt
# fönster: neutralt RSI" rule, volume zscore=0 exactly per its "zero
# variance" rule) and its single funding entry (abs(0.0001)*100 = 0.01) -
# so a genuinely UNCHANGED market produces decay_score=0.0 exactly, not an
# arbitrary/mismatched placeholder that would silently produce some other
# state than the test's own name claims.
_MATCHING_ENTRY_RSI = 50.0
_MATCHING_ENTRY_VOLUME_ZSCORE = 0.0
_MATCHING_ENTRY_FUNDING_MAGNITUDE = 0.01


def _seed_candidate_and_position(repo, position_id="pos-1", opened_at=_NOW):
    evidence = CandidateEvidenceRecord(
        instrument="BTCUSDT", timeframes=["30m"], evaluated_at=opened_at,
        price_volatility_evidence=PriceVolatilityEvidence(**_placeholder_ev(value=3.0, threshold=2.0)),
        momentum_breakout_evidence=MomentumBreakoutEvidence(
            **_placeholder_ev(value=_MATCHING_ENTRY_RSI, threshold=70.0)
        ),
        volume_evidence=VolumeEvidence(
            **_placeholder_ev(value=_MATCHING_ENTRY_VOLUME_ZSCORE, threshold=2.5)
        ),
        funding_oi_evidence=FundingOpenInterestEvidence(
            **_placeholder_ev(value=_MATCHING_ENTRY_FUNDING_MAGNITUDE, threshold=0.05)
        ),
        candidate_score=0.5, trigger_reasons=["momentum_breakout"],
        data_quality_status="ok", outcome="worth_deeper_analysis",
    )
    candidate = Candidate(
        candidate_id=position_id, idempotency_key=f"key-{position_id}", instrument="BTCUSDT",
        discovery_run_id="run-0", evidence_hash="hash-1", status="CONFIRMED",
        evidence_record=evidence, created_at=opened_at, updated_at=opened_at,
        risk=RiskAssessment(
            agent_name="crypto-risk-agent", run_id="run-0", created_at=opened_at, status="ok",
            suggested_stop_loss="90", suggested_target="120", downside="d", liquidity_risk="l",
            model_risk="m", timing_risk="t",
        ),
    )
    repo.create_candidate_with_event(
        candidate,
        Event(event_id=f"CANDIDATE_CREATED:{position_id}", event_type="CANDIDATE_CREATED",
              aggregate_type="candidate", aggregate_id=position_id, occurred_at=opened_at,
              run_id="seed", schema_version=1, payload={}),
    )
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTCUSDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("100"), simulated_fill_entry=Decimal("100"),
        stop_loss=Decimal("90"), target=Decimal("120"), size=Decimal("1000"),
        fill_model_version="v1", opened_at=opened_at,
    )
    repo.create_position_with_event(
        position,
        Event(event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
              aggregate_type="position", aggregate_id=position_id, occurred_at=opened_at,
              run_id="seed", schema_version=1, payload={}),
    )
    return candidate, position


class _StubConnector:
    def __init__(self, price="100"):
        self._price = price

    def get_klines(self, symbol, interval, limit=100):
        return [_raw_kline("100", int(_NOW.timestamp() * 1000) + i * 60000) for i in range(30)]

    def get_funding_rate(self, symbol, limit=1):
        return [_raw_funding(symbol, "0.0001", int(_NOW.timestamp() * 1000))]

    def get_ticker(self, symbol):
        return _raw_ticker(symbol, self._price, "1000000", int(_NOW.timestamp() * 1000))


class _FakeRunner:
    last_call_billed = True
    last_call_cost_usd = Decimal("0.01")

    def run(self, agent_def, context, response_model):
        return response_model(
            agent_name="crypto-guardian", run_id="run-1", created_at=_NOW, status="ok",
            reasoning="Momentum has faded materially since entry.",
        )


def test_run_guardian_tick_body_persists_a_hold_observation_without_ai(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)
    connector = _StubConnector(price="100")  # unchanged since entry -> HOLD

    observations = run_guardian_tick_body(repo, connector, _FakeRunner(), _settings(), "run-1", _NOW)

    assert len(observations) == 1
    assert observations[0].state == "HOLD"
    assert observations[0].ai_reasoning is None
    row = repo.find_latest_guardian_observation("pos-1")
    assert row["state"] == "HOLD"


def test_run_guardian_tick_body_skips_position_on_insufficient_data(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)

    class _EmptyConnector:
        def get_klines(self, symbol, interval, limit=100):
            return []

        def get_funding_rate(self, symbol, limit=1):
            return []

        def get_ticker(self, symbol):
            return _raw_ticker(symbol, "100", "1000000", int(_NOW.timestamp() * 1000))

    observations = run_guardian_tick_body(repo, _EmptyConnector(), _FakeRunner(), _settings(), "run-1", _NOW)

    assert observations == []
    assert repo.find_latest_guardian_observation("pos-1") is None


def test_run_guardian_tick_body_never_touches_positions_table(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _candidate, position = _seed_candidate_and_position(repo)
    before = repo.get_position("pos-1")
    connector = _StubConnector(price="100")

    run_guardian_tick_body(repo, connector, _FakeRunner(), _settings(), "run-1", _NOW)

    after = repo.get_position("pos-1")
    assert after == before


def test_run_guardian_tick_body_still_persists_observation_when_budget_exhausted(tmp_path):
    """Forces a non-HOLD state WITHOUT touching the stub's flat kline data
    (which would also perturb the momentum/volume/funding factors in ways
    that are hard to hand-verify) - instead uses two independently
    controllable, exactly-computable levers: opened_at far enough in the
    past to drive time_decay_factor to exactly 1.0 (elapsed 30h vs.
    risk_limits.max_position_hold_hours=24 -> clipped to 1.0), and a
    lowered watch_decay_threshold so that decay_score's contribution from
    time_decay ALONE (1.0 / 6 equally-weighted factors = 0.1667) is enough
    to cross into WATCH. Every other factor stays at 0 (matching entry
    evidence, per _seed_candidate_and_position's docstring above)."""
    from crypto_trading.config.loader import GuardianConfig

    repo = SQLiteRepository(tmp_path / "t.db")
    opened_at = _NOW - timedelta(hours=30)  # exceeds max_position_hold_hours=24 -> time_decay=1.0
    _seed_candidate_and_position(repo, opened_at=opened_at)
    for i in range(600):
        repo.record_ai_call_event(
            Event(event_id=f"AI_CALL_MADE:exhaust:{i}", event_type="AI_CALL_MADE",
                  aggregate_type="candidate", aggregate_id="exhaust", occurred_at=_NOW,
                  run_id="run-0", schema_version=1, payload={"role": "risk", "status": "ok", "cost_usd": "10.00"}),
        )
    connector = _StubConnector(price="100")  # matches entry - only time_decay drives the state here
    settings = _settings().model_copy(
        update={
            "guardian": GuardianConfig(
                watch_decay_threshold=Decimal("0.05"),
                protect_decay_threshold=Decimal("0.5"),
                exit_decay_threshold=Decimal("0.9"),
            )
        }
    )

    observations = run_guardian_tick_body(repo, connector, _FakeRunner(), settings, "run-1", _NOW)

    assert len(observations) == 1
    assert observations[0].state == "WATCH"  # first observation, non-HOLD -> should_invoke_ai() would be True
    assert observations[0].ai_reasoning is None  # ...but budget exhaustion still blocked the call
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/guardian/test_tick.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

```python
# crypto_trading/guardian/tick.py
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.guardian.ai_context import build_ai_context, should_invoke_ai
from crypto_trading.guardian.data import GuardianDataSource, fetch_btc_regime_rsi, fetch_current_price, fetch_fresh_evidence
from crypto_trading.guardian.deterministic import (
    classify_guardian_state, compute_decay_score, compute_funding_decay_factor,
    compute_momentum_decay_factor, compute_progress_ratio, compute_secondary_confirmation_lost_factor,
    compute_time_decay_factor, compute_unrealized_pnl, compute_volume_decay_factor,
)
from crypto_trading.logging import log_event
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.guardian import GuardianAssessment, GuardianObservation
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_GUARDIAN_AGENT_FILE = "crypto-guardian.md"
_WORST_CASE_COST_PER_CALL_USD = Decimal("0.20")  # same constant as orchestrator.py / detective/batch.py


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _budget_allows_one_more_call(repo: Repository, settings: Settings, now: datetime) -> bool:
    day_start = _utc_day_start(now)
    daily_count = repo.count_ai_calls_since(day_start)
    daily_cost = repo.sum_ai_cost_since(day_start)
    calls_would_exceed = daily_count + 1 > settings.budget_limits.max_ai_calls_per_day
    cost_would_exceed = daily_cost + _WORST_CASE_COST_PER_CALL_USD > settings.budget_limits.max_daily_ai_cost_usd
    return not (calls_would_exceed or cost_would_exceed)


def process_one_position(
    repo: Repository,
    connector: GuardianDataSource,
    runner: AgentRunner,
    settings: Settings,
    position: Position,
    run_id: str,
    now: datetime,
) -> GuardianObservation | None:
    candidate = repo.get_candidate(position.candidate_id)
    secondary_timeframe = (
        candidate.evidence_record.secondary_timeframe_evidence.timeframe
        if candidate is not None and candidate.evidence_record.secondary_timeframe_evidence is not None
        else None
    )
    fresh_evidence = fetch_fresh_evidence(connector, position.instrument, secondary_timeframe, settings, now)
    current_price = fetch_current_price(connector, position.instrument)
    btc_rsi = fetch_btc_regime_rsi(connector, settings, now)
    if fresh_evidence is None or current_price is None or btc_rsi is None or candidate is None:
        return None  # fail-safe skip - never a guessed observation

    entry_evidence = candidate.evidence_record
    factors = {
        "time_decay": compute_time_decay_factor(
            position.opened_at, now, settings.risk_limits.max_position_hold_hours
        ),
        "momentum_decay": compute_momentum_decay_factor(
            Decimal(str(entry_evidence.momentum_breakout_evidence.value)),
            Decimal(str(fresh_evidence.momentum_breakout_evidence.value)),
        ),
        "volume_decay": compute_volume_decay_factor(
            Decimal(str(entry_evidence.volume_evidence.value)),
            Decimal(str(fresh_evidence.volume_evidence.value)),
        ),
        "funding_decay": compute_funding_decay_factor(
            Decimal(str(entry_evidence.funding_oi_evidence.value)),
            Decimal(str(fresh_evidence.funding_oi_evidence.value)),
        ),
        "secondary_confirmation_lost": compute_secondary_confirmation_lost_factor(
            entry_evidence.secondary_timeframe_evidence, fresh_evidence.secondary_timeframe_evidence
        ),
        "market_regime": compute_market_regime_factor(btc_rsi),
    }
    decay_score = compute_decay_score(factors, settings.guardian.factor_weights)
    progress_ratio = compute_progress_ratio(position.simulated_fill_entry, position.target, current_price)
    unrealized_pnl = compute_unrealized_pnl(position, current_price)
    new_state = classify_guardian_state(decay_score, unrealized_pnl > 0, settings.guardian)

    previous = repo.find_latest_guardian_observation(position.position_id)
    ai_reasoning: str | None = None
    ai_cost_usd: Decimal | None = None
    if should_invoke_ai(previous, new_state):
        if _budget_allows_one_more_call(repo, settings, now):
            context = build_ai_context(candidate, factors, decay_score, progress_ratio, unrealized_pnl, new_state)
            agent_def = load_agent_definition(_GUARDIAN_AGENT_FILE)
            assessment: GuardianAssessment = runner.run(agent_def, context, GuardianAssessment)
            billed = getattr(runner, "last_call_billed", True)
            cost = getattr(runner, "last_call_cost_usd", Decimal("0"))
            if billed:
                repo.record_ai_call_event(
                    Event(
                        event_id=f"AI_CALL_MADE:guardian:{position.position_id}:{run_id}",
                        event_type="AI_CALL_MADE", aggregate_type="position",
                        aggregate_id=position.position_id, occurred_at=now, run_id=run_id,
                        schema_version=1,
                        payload={"role": "guardian", "status": assessment.status, "cost_usd": str(cost)},
                    )
                )
                ai_cost_usd = cost
            if assessment.status == "ok":
                ai_reasoning = assessment.reasoning
        else:
            log_event(run_id, event="guardian_ai_deferred_budget", position_id=position.position_id)

    observation = GuardianObservation(
        observation_id=f"{position.position_id}:{now.isoformat()}",
        position_id=position.position_id,
        observed_at=now,
        state=new_state,
        decay_score=decay_score,
        progress_ratio=progress_ratio,
        unrealized_pnl=unrealized_pnl,
        factors={name: float(value) for name, value in factors.items()},
        ai_reasoning=ai_reasoning,
        ai_cost_usd=ai_cost_usd,
        run_id=run_id,
    )
    repo.save_guardian_observation(observation)
    log_event(
        run_id, event="guardian_observation_recorded", position_id=position.position_id,
        state=new_state, decay_score=str(decay_score),
    )
    return observation


def run_guardian_tick_body(
    repo: Repository, connector: GuardianDataSource, runner: AgentRunner, settings: Settings,
    run_id: str, now: datetime,
) -> list[GuardianObservation]:
    observations = []
    for position in repo.find_open_positions():
        observation = process_one_position(repo, connector, runner, settings, position, run_id, now)
        if observation is not None:
            observations.append(observation)
    return observations
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/guardian/test_tick.py -v`
Expected: PASS (all 4 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/guardian/tick.py tests/crypto_trading/guardian/test_tick.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add Guardian tick orchestration with budget gate

Ties together fresh-evidence fetching, the deterministic decay/state
pipeline, and gated AI reasoning. Budget-exhausted path still persists
the deterministic observation with ai_reasoning=None - the
deterministic layer never depends on AI succeeding or being
affordable. Proven by test to never touch the positions table.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 10: `guardian_loop.py` — top-level tick/run_forever

**Files:**
- Create: `crypto_trading/guardian_loop.py`
- Test: `tests/crypto_trading/test_guardian_loop.py`

**Interfaces:**
- Consumes: `guardian/tick.py::run_guardian_tick_body` (Task 9).
- Produces: `run_guardian_tick(repo, connector, runner, settings, now) -> None`, `run_forever(repo, connector, runner, settings) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/test_guardian_loop.py
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.guardian_loop import run_guardian_tick
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _raw_funding, _raw_kline, _raw_ticker, _settings

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class _StubConnector:
    def get_klines(self, symbol, interval, limit=100):
        return [_raw_kline("100", int(_NOW.timestamp() * 1000) + i * 60000) for i in range(30)]

    def get_funding_rate(self, symbol, limit=1):
        return [_raw_funding(symbol, "0.0001", int(_NOW.timestamp() * 1000))]

    def get_ticker(self, symbol):
        return _raw_ticker(symbol, "100", "1000000", int(_NOW.timestamp() * 1000))


class _FakeRunner:
    last_call_billed = True
    last_call_cost_usd = Decimal("0.01")

    def run(self, agent_def, context, response_model):
        return response_model(
            agent_name="crypto-guardian", run_id="run-1", created_at=_NOW, status="ok", reasoning="x",
        )


def test_run_guardian_tick_persists_a_runs_row_and_never_crashes(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    position = Position(
        position_id="pos-1", candidate_id="pos-1", instrument="BTCUSDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("100"), simulated_fill_entry=Decimal("100"),
        stop_loss=Decimal("90"), target=Decimal("120"), size=Decimal("1000"),
        fill_model_version="v1", opened_at=_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(event_id="POSITION_OPENED:pos-1", event_type="POSITION_OPENED",
              aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
              run_id="seed", schema_version=1, payload={}),
    )
    # no matching candidate row -> process_one_position() skips fail-safe -
    # this test proves the tick still completes and records a runs row,
    # never crashes, even though every position is skipped.

    run_guardian_tick(repo, _StubConnector(), _FakeRunner(), _settings(), _NOW)

    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'guardian'").fetchone()
    assert row is not None
    assert row["status"] == "ok"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/crypto_trading/test_guardian_loop.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement**

```python
# crypto_trading/guardian_loop.py
from __future__ import annotations

import time
from datetime import UTC, datetime

from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.guardian.tick import run_guardian_tick_body
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.storage.repository import Repository


def run_guardian_tick(
    repo: Repository, connector, runner: AgentRunner, settings: Settings, now: datetime
) -> None:
    """One Guardian tick. Same outer fail-safe shape as monitoring_loop.py/
    demo_execution_loop.py: an unexpected exception never crashes
    run_forever()."""
    run_id = new_run_id()
    repo.start_run(run_id, "guardian", now)
    try:
        run_guardian_tick_body(repo, connector, runner, settings, run_id, now)
        repo.complete_run(run_id, datetime.now(UTC), "ok", [])
    except Exception as exc:
        log_event(
            run_id, event="guardian_tick_failed", error_type=type(exc).__name__, error=str(exc)
        )
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])


def run_forever(repo: Repository, connector, runner: AgentRunner, settings: Settings) -> None:
    while True:
        run_guardian_tick(repo, connector, runner, settings, datetime.now(UTC))
        time.sleep(settings.guardian.check_interval_seconds)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/crypto_trading/test_guardian_loop.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/guardian_loop.py tests/crypto_trading/test_guardian_loop.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): add guardian_loop tick/run_forever

Same fail-safe-never-crashes-run_forever shape as monitoring_loop and
demo_execution_loop.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 11: Wire into `run.py` behind the arm flag

**Files:**
- Modify: `crypto_trading/run.py`

**Interfaces:**
- Consumes: `is_guardian_enabled()` (Task 2), `build_guardian_runner_from_env()` (this task, mirrors `build_detective_runner_from_env()`), `guardian_loop.run_forever` (Task 10).

- [ ] **Step 1: Add the runner builder**

In `crypto_trading/run.py`, add near `build_detective_runner_from_env()`:

```python
def build_guardian_runner_from_env() -> AgentRunner:
    """Position Guardian (2026-09-04) - own RealClaudeRunner instance, same
    reason as build_detective_runner_from_env(): never shared with another
    thread's runner (mutable last_call_billed/last_call_cost_usd state
    would race). Same cheap default model as screener/Detective (Haiku
    4.5, cost-controlled, this is reasoning-only narration of an already-
    decided state, not a realtime trading decision) - own env var so it
    can be changed independently."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ConfigError("ANTHROPIC_API_KEY saknas - kan inte starta med RealClaudeRunner")
    return RealClaudeRunner(
        api_key=api_key,
        model=os.environ.get("CRYPTO_TRADING_GUARDIAN_MODEL", "claude-haiku-4-5"),
        timeout_seconds=float(os.environ.get("CRYPTO_TRADING_AGENT_TIMEOUT_SECONDS", "60")),
        max_retries=int(os.environ.get("CRYPTO_TRADING_AGENT_MAX_RETRIES", "3")),
    )
```

- [ ] **Step 2: Add the thread-runner function**

Add near `_run_detective_forever()`:

```python
def _run_guardian_forever(
    market_data_connector: BingXMarketDataConnector, runner: AgentRunner, settings: Settings
) -> None:
    """Same thread-bound-connection fix as the other _run_*_forever()
    functions above."""
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    guardian_loop.run_forever(repo, market_data_connector, runner, settings)
```

- [ ] **Step 3: Add the import and wire the thread into `main()`**

Add to the imports at the top:
```python
from crypto_trading import guardian_loop
```

(the module import line already reads `from crypto_trading import demo_execution_loop, detective_loop, discovery_loop, monitoring_loop, notify_loop` — extend it to also include `guardian_loop`, keeping alphabetical order: `from crypto_trading import demo_execution_loop, detective_loop, discovery_loop, guardian_loop, monitoring_loop, notify_loop`)

Also add: `from crypto_trading.config.loader import is_guardian_enabled` (extend the existing `from crypto_trading.config.loader import Settings, get_settings, is_demo_execution_enabled` line to also include `is_guardian_enabled`).

In `main()`, after the demo-execution `if`/`else` block, add:

```python
    if is_guardian_enabled():
        guardian_runner = build_guardian_runner_from_env()
        threads.append(
            threading.Thread(
                target=_run_guardian_forever, args=(connector, guardian_runner, settings), daemon=True
            )
        )
    else:
        log_event(
            "startup", event="guardian_disabled", reason="CRYPTO_TRADING_GUARDIAN_ENABLED not set"
        )
```

- [ ] **Step 4: Manual smoke check (no automated test — this only wires already-tested pieces together)**

Run: `uv run python -c "import crypto_trading.run"` — confirms the module still imports cleanly.
Expected: no output, exit code 0.

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/run.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): wire Guardian as an eighth, opt-in daemon thread

Default off (CRYPTO_TRADING_GUARDIAN_ENABLED unset). Shadow-mode-only
- this thread has no write access to positions/demo_executions
anywhere in its call chain.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 12: Detective integration (additive context extension)

**Files:**
- Modify: `crypto_trading/detective/context.py`
- Modify: `.claude/agents/crypto-detective.md`
- Test: `tests/crypto_trading/detective/test_context.py` (extend existing — check first whether this file exists; if not, create it following the pattern in `tests/crypto_trading/paper_trading/test_demo_execution.py` for fixture style)

**Interfaces:**
- Consumes: `Repository.find_guardian_observations_for_position()` (Task 4).
- Modifies: `build_position_analysis_context(position, candidate, gate_decision, guardian_observations=None)` — new, optional, keyword-only, backward-compatible parameter.

- [ ] **Step 1: Check for an existing test file**

Run: `find tests/crypto_trading/detective -iname "*context*"`. If none exists, create `tests/crypto_trading/detective/test_context.py`.

- [ ] **Step 2: Write the failing test**

```python
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.detective.context import build_position_analysis_context
from crypto_trading.schemas.trade import Position

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _closed_position() -> Position:
    return Position(
        position_id="pos-1", candidate_id="pos-1", instrument="BTCUSDT", direction="LONG",
        status="CLOSED", theoretical_entry=Decimal("100"), simulated_fill_entry=Decimal("100"),
        stop_loss=Decimal("90"), target=Decimal("120"), size=Decimal("1000"),
        fill_model_version="v1", opened_at=_NOW, theoretical_exit=Decimal("120"),
        simulated_fill_exit=Decimal("120"), exit_reason="target", fees=Decimal("0"),
        funding=Decimal("0"), closed_at=_NOW,
    )


def test_context_omits_guardian_trajectory_when_absent():
    context = build_position_analysis_context(_closed_position(), None, None)
    assert "guardian_trajectory" not in context


def test_context_includes_guardian_trajectory_when_present():
    trajectory = [
        {"observed_at": "2026-09-04T12:00:00+00:00", "state": "HOLD", "decay_score": "0.1"},
        {"observed_at": "2026-09-04T13:00:00+00:00", "state": "WATCH", "decay_score": "0.4"},
    ]
    context = build_position_analysis_context(_closed_position(), None, None, guardian_observations=trajectory)
    assert context["guardian_trajectory"] == trajectory
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/crypto_trading/detective/test_context.py -v`
Expected: FAIL — `guardian_observations` parameter doesn't exist / `guardian_trajectory` never in context.

- [ ] **Step 4: Extend `build_position_analysis_context()`**

In `crypto_trading/detective/context.py`, change the signature and add the new block:

```python
def build_position_analysis_context(
    position: Position,
    candidate: Candidate | None,
    gate_decision: dict | None,
    guardian_observations: list[dict] | None = None,
) -> dict:
```

Add, right before the final `return context`:

```python
    if guardian_observations:
        context["guardian_trajectory"] = [
            {"observed_at": obs["observed_at"], "state": obs["state"], "decay_score": obs["decay_score"]}
            for obs in guardian_observations
        ]
```

- [ ] **Step 5: Wire the new parameter through `detective/batch.py`**

In `crypto_trading/detective/batch.py`, inside the `position_contexts` list-comprehension (the block building `build_position_analysis_context(...)` per position), pass the new argument:

```python
    position_contexts = [
        build_position_analysis_context(
            position,
            candidates_by_id.get(position.candidate_id),
            gate_decisions_by_position.get(position.position_id),
            guardian_observations=repo.find_guardian_observations_for_position(position.position_id),
        )
        for position in batch_positions
    ]
```

- [ ] **Step 6: Update `crypto-detective.md`'s prompt**

In `.claude/agents/crypto-detective.md`, extend the "Arbetssätt" numbered list with a new item (insert as item 5, after the existing "Formulera korta, konkreta observationer..." item):

```markdown
5. Om `guardian_trajectory` finns för en trade: notera - som ytterligare en
   hypotes, inte en säker slutsats - om Guardians tillståndsövergångar
   (WATCH/PROTECT/EXIT) verkar ha föregått det faktiska utfallet, eller om
   de inte gav något förvarningsvärde i just detta fall. Guardian är
   shadow-mode-only (fattar inga beslut) - detta är ren kalibrering av om
   dess signal har prediktivt värde, aldrig en bedömning av en åtgärd.
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/crypto_trading/detective/ -v`
Expected: PASS (all tests, including the 2 new ones and every pre-existing Detective test)

- [ ] **Step 8: Commit**

```bash
git add crypto_trading/detective/context.py crypto_trading/detective/batch.py .claude/agents/crypto-detective.md tests/crypto_trading/detective/test_context.py
git commit -m "$(cat <<'EOF'
feat(crypto-trading): give Detective additive access to Guardian trajectories

New optional guardian_observations parameter on
build_position_analysis_context() - absent by default, included when
Guardian has observations for that position. Detective's own hard
limits (never acts, never changes strategy, always hypothesis-only)
are unchanged; this is purely more read-only context for calibrating
whether Guardian's signals have predictive value.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01P7E3aMgitcAVDzmYbVZajq
EOF
)"
```

---

## Task 13: Full suite regression + isolation proof

**Files:** none new — verification only.

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -q`
Expected: every test passes, including every test added in Tasks 1-12, and every pre-existing test (BingX Demo execution, PAPER pipeline, Detective, etc.) still passes unmodified.

- [ ] **Step 2: Explicit isolation grep (belt-and-braces alongside the isolation unit tests)**

Run: `grep -rn "positions\b" crypto_trading/guardian/ crypto_trading/guardian_loop.py` and manually confirm every match is a **read** (`repo.find_open_positions()`, `repo.get_position(...)` for comparison in tests) — never a `create_position_with_event`/`close_position_with_event`/`UPDATE positions` call anywhere in the new code.

Expected: no write calls found.

- [ ] **Step 3: Confirm the arm flag is off by default**

Run: `grep -c "CRYPTO_TRADING_GUARDIAN_ENABLED" .env` (from the project root) — expect `0` unless the user has explicitly set it. Guardian must not run against the live production database until the user explicitly enables it, exactly like the BingX Demo execution flag was handled earlier this session.

Expected: flag absent (or, if present, the user set it deliberately - do not add it yourself).

- [ ] **Step 4: No commit needed for this task** — it's verification-only. If any check fails, fix the underlying issue in the relevant earlier task's files, re-run the full suite, and only then consider the plan complete.
