# Crypto Trading — Phase 0 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bygga grunden för `crypto_trading/` — scheman, config-lager, SQLite-storage (WAL, append-only event-logg, idempotens), och en deterministisk state machine med crash-safety — helt testbar utan nätverk, utan att röra `intelligence/`.

**Architecture:** Enkelriktade lager, samma princip som Fas 1: `schemas` (beroendefritt) ← `config`/`storage` ← `state_machine`. `state_machine.py` beror bara på `storage.Repository`-**protokollet**, aldrig på `SQLiteRepository` konkret.

**Tech Stack:** Python 3.13, `pydantic` v2, `pyyaml`, `python-dotenv`, `sqlite3` (stdlib), `pytest`. Alla dependencies finns redan i `pyproject.toml` (delas med `intelligence/`) — inga nya tillägg krävs för Phase 0.

**Spec:** `SPEC_CRYPTO.md` (repo root, särskilt §4, §5, §8.1–§8.7, §16) och `PLAN_CRYPTO.md` (Phase 0-avsnittet) — denna plan argumenterar utifrån dessa; vid konflikt gäller SPEC_CRYPTO.md. Konsoliderad Phase 0-design godkänd i konversationshistoriken (inkl. `CorruptCandidateStateError`-justeringen, committad i `c862a34`).

## Global Constraints

- Ingen kod i `crypto_trading/` lägger ordrar, ansluter till mäklarkonton, hanterar broker-credentials eller flyttar pengar — i någon fas (SPEC §1, §19).
- `crypto_trading/` importerar aldrig `intelligence/` och tvärtom; ingen delad runtime-state (SPEC §0).
- Alla pengar-/pris-/PnL-fält är `Decimal`, aldrig `float`, aldrig via float vid serialisering (SPEC-design, denna plan).
- SQLite: `PRAGMA journal_mode=WAL` + konfigurerbar `busy_timeout` på varje anslutning.
- `events`-tabellen är append-only: inga `UPDATE`/`DELETE`-metoder i `Repository`, plus DB-triggers som blockerar det på SQL-nivå.
- Alla domänstatebyten som har ett motsvarande event skrivs atomiskt (samma SQLite-transaktion) via en enda Repository-metod — aldrig två separata anrop.
- En lagrad candidate-rad som inte kan deserialiseras till ett giltigt `Candidate`-objekt — vare sig felet sitter i `status`, `evidence_record` eller en timestamp — bygger **aldrig** ett delvis korrekt `Candidate`-objekt. Repository kastar konsekvent `CorruptCandidateStateError` och skriver ett `CORRUPT_STATE_DETECTED`-event för hela deserialiseringskedjan (SPEC §8.3, uppdaterad; se Task 10).
- Default `pytest`-körning kräver noll nätverk (SPEC §13-mönster från Fas 1).
- `python-target-version` = py313, `ruff` line-length 100, regler `E,F,I,UP,B` (befintlig `pyproject.toml`).

## Implementationsanmärkningar (samtliga avsteg från SPEC §16:s tabellista — läs innan granskning)

SPEC §16 listar tolv tabeller "(minst)": `instruments, market_data_snapshots, candidates, evidence_records, assessments, gate_decisions, positions, trades, forecasts, forecast_outcomes, telegram_events, runs`. Phase 0:s schema (Task 9) skapar åtta: `schema_meta` (ej i SPEC:s lista, ren infrastruktur för schemaversionering), `events` (ej i SPEC:s lista, se §2/§12/§13 — audit-loggen), `candidates`, `assessments`, `gate_decisions`, `positions`, `forecasts`, `runs`. Nedan listas **varje** tabell från SPEC §16 som inte finns oförändrad i Phase 0, vilken fas som äger den, och varför den medvetet inte byggs nu — Phase 0-scopet ändras inte för att fylla listan.

| SPEC §16-tabell | Status i Phase 0 | Äger fasen | Varför uppskjuten |
|---|---|---|---|
| `instruments` | Skapas inte | Phase 1 (BingX Market Data) | Kräver instrumentuniversum från en BingX-connector som inte finns förrän Phase 1. En tom tabell utan skrivväg skulle inte tjäna något syfte i Phase 0. |
| `market_data_snapshots` | Skapas inte | Phase 1 (BingX Market Data) | Kräver samma BingX-connector som `instruments` — ingen marknadsdata hämtas i Phase 0. |
| `evidence_records` (separat tabell) | Skapas inte separat — evidens lagras istället inbäddad som JSON i `candidates.evidence_record` för candidates som faktiskt kvalificerar sig | Phase 2 (Quant Screening) | SPEC §5 anger explicit att evidens för icke-candidates (`outcome="not_a_candidate"`) bara debug-loggas, inte persisteras strukturerat — och ingen Quant Screener finns förrän Phase 2. En separat, candidate-oberoende `evidence_records`-tabell (för att t.ex. granska ALLA screener-utvärderingar, inte bara de som blev candidates) motiveras först när screenern som skulle fylla den existerar. |
| `telegram_events` | Skapas inte | Phase 6 (Telegram) | Inget notifieringssystem finns förrän Phase 6 — en tom tabell utan skrivväg vore för tidig. |
| `positions` + `trades` | Sammanslagna till **en** tabell (`positions`), täcker hela livscykeln öppen→stängd | Schema: Phase 0. Skrivlogik: Phase 4 | Två tabeller för samma underliggande entitet riskerar att representera "samma sanning" på två ställen och driva isär — en rad per position, uppdaterad vid stängning, undviker det helt utan att tappa funktion. Ingen skrivlogik finns i Phase 0 (bara schema, se Decimal-rundturstestet i Task 9 som skriver en enda testrad direkt mot schemat). |
| `forecasts` + `forecast_outcomes` | Sammanslagna till **en** tabell (`forecasts`), med `actual_outcome`/`outcome_timestamp` som nullable-kolumner ifyllda när utfallet är känt | Schema: Phase 0. Skrivlogik: Phase 3 (forecast) / Phase 8 (kalibrering mot utfall) | Samma resonemang som `positions`/`trades` — en rad per forecast, aldrig två tabeller som kan driva isär. |

Om separata tabeller för någon av dessa ändå önskas innan respektive fas börjar skriva till dem, flagga det i granskningen — inget hindrar en framtida schemaändring (spårad via `schema_meta.schema_version`, se Task 9).

---

## Task 1: Paketskelett för Phase 0

**Files:**
- Create: `crypto_trading/__init__.py`
- Create: `crypto_trading/schemas/__init__.py`
- Create: `crypto_trading/config/__init__.py`
- Create: `crypto_trading/storage/__init__.py`
- Modify: `.gitignore`

**Rättad under exekvering:** planen angav ursprungligen även `tests/crypto_trading/__init__.py` + undermappars `__init__.py`. Det skapar en pytest-rootpath-kollision — testmoduler namnges då `crypto_trading.schemas.test_x`, vilket binder `sys.modules['crypto_trading']` till den tomma test-paketversionen istället för det riktiga paketet, och gör alla efterföljande imports av riktig kod från testfiler omöjliga (`ModuleNotFoundError`). Åtgärdat genom att INTE skapa `__init__.py` under `tests/crypto_trading/` alls — matchar `tests/intelligence/`s etablerade mönster (inga paketnivå-`__init__.py` i testträdet). Se separat commit "crypto_trading Phase 0: ta bort tests/crypto_trading/__init__.py-hierarkin".

**Interfaces:**
- Produces: paketet `crypto_trading` är importerbart (`import crypto_trading`).

- [ ] **Step 1: Skapa paketskelettet**

Bara filerna under `crypto_trading/` skapas som tomma (`__init__.py` helt utan innehåll — inga re-exports än, inget att exportera). Skapa INGA `__init__.py` under `tests/crypto_trading/` (se rättningen ovan) — pytest hittar och samlar in testfiler utan dem.

- [ ] **Step 2: Lägg till crypto_trading-databasen i `.gitignore`**

Lägg till i `.gitignore` (efter den befintliga `# Intelligence-systemet`-sektionen):

```
# Crypto trading-systemet
data/crypto_trading.db
data/crypto_trading.db-journal
data/crypto_trading.db-wal
data/crypto_trading.db-shm
```

- [ ] **Step 3: Verifiera att paketet importeras**

Run: `python -c "import crypto_trading"`
Expected: inget fel, ingen output.

- [ ] **Step 4: Commit**

```bash
git add crypto_trading/__init__.py crypto_trading/schemas/__init__.py crypto_trading/config/__init__.py crypto_trading/storage/__init__.py .gitignore
git commit -m "crypto_trading Phase 0 steg 1: paketskelett"
```

---

## Task 2: `schemas/common.py` — delade statustyper

**Files:**
- Create: `crypto_trading/schemas/common.py`
- Test: `tests/crypto_trading/schemas/test_common.py`

**Interfaces:**
- Produces: `CandidateStatus`, `PositionStatus`, `AssessmentStatus`, `DataQualityStatus` — alla `typing.Literal`-typalias, importeras av alla senare scheman.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/schemas/test_common.py
from typing import get_args

from crypto_trading.schemas.common import (
    AssessmentStatus,
    CandidateStatus,
    DataQualityStatus,
    PositionStatus,
)


def test_candidate_status_has_exactly_eight_values():
    assert set(get_args(CandidateStatus)) == {
        "CANDIDATE",
        "DATA_INVALID",
        "BUDGET_LIMITED",
        "UNDER_AI_ANALYSIS",
        "ANALYSIS_INTERRUPTED",
        "REJECTED",
        "NO_TRADE",
        "CONFIRMED",
    }


def test_candidate_status_has_no_unknown_state_member():
    assert "UNKNOWN_STATE" not in get_args(CandidateStatus)


def test_position_status_values():
    assert set(get_args(PositionStatus)) == {"OPEN_POSITION", "CLOSED"}


def test_assessment_status_values():
    assert set(get_args(AssessmentStatus)) == {"ok", "failed", "timeout"}


def test_data_quality_status_values():
    assert set(get_args(DataQualityStatus)) == {"ok", "degraded", "invalid"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/schemas/test_common.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crypto_trading.schemas.common'`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/schemas/common.py
from __future__ import annotations

from typing import Literal

CandidateStatus = Literal[
    "CANDIDATE",
    "DATA_INVALID",
    "BUDGET_LIMITED",
    "UNDER_AI_ANALYSIS",
    "ANALYSIS_INTERRUPTED",
    "REJECTED",
    "NO_TRADE",
    "CONFIRMED",
]

PositionStatus = Literal["OPEN_POSITION", "CLOSED"]

AssessmentStatus = Literal["ok", "failed", "timeout"]

DataQualityStatus = Literal["ok", "degraded", "invalid"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/schemas/test_common.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/schemas/common.py tests/crypto_trading/schemas/test_common.py
git commit -m "crypto_trading Phase 0 steg 2: statustyper (common.py)"
```

---

## Task 3: `schemas/event.py` — event-/audit-loggens schema

**Files:**
- Create: `crypto_trading/schemas/event.py`
- Test: `tests/crypto_trading/schemas/test_event.py`

**Interfaces:**
- Consumes: inget (beroendefritt utöver pydantic/datetime).
- Produces: `Event` (pydantic `BaseModel`) med fälten `event_id, event_type, aggregate_type, aggregate_id, occurred_at, run_id, schema_version, payload`.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/schemas/test_event.py
from datetime import UTC, datetime

from crypto_trading.schemas.event import Event


def test_event_roundtrips_all_fields():
    event = Event(
        event_id="CANDIDATE_CREATED:abc-123",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id="abc-123",
        occurred_at=datetime.now(UTC),
        run_id="run-1",
        schema_version=1,
        payload={"instrument": "BTCUSDT"},
    )
    assert event.event_type == "CANDIDATE_CREATED"
    assert event.payload["instrument"] == "BTCUSDT"


def test_event_run_id_is_optional():
    event = Event(
        event_id="CORRUPT_STATE_DETECTED:abc-123:X",
        event_type="CORRUPT_STATE_DETECTED",
        aggregate_type="candidate",
        aggregate_id="abc-123",
        occurred_at=datetime.now(UTC),
        run_id=None,
        schema_version=1,
        payload={"raw_status": "X"},
    )
    assert event.run_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/schemas/test_event.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/schemas/event.py
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Event(BaseModel):
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: datetime
    run_id: str | None
    schema_version: int
    payload: dict
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/schemas/test_event.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/schemas/event.py tests/crypto_trading/schemas/test_event.py
git commit -m "crypto_trading Phase 0 steg 3: Event-schema"
```

---

## Task 4: `schemas/evidence.py` — CandidateEvidenceRecord + idempotens-hashning

**Files:**
- Create: `crypto_trading/schemas/evidence.py`
- Test: `tests/crypto_trading/schemas/test_evidence.py`

**Interfaces:**
- Consumes: `DataQualityStatus` från `crypto_trading.schemas.common`.
- Produces: `CandidateEvidenceRecord`, `compute_evidence_hash(record) -> str`, `compute_candidate_idempotency_key(instrument, discovery_run_id, evidence_hash) -> str`. Dessa två funktioner används av Task 10 (repository) och av framtida Phase 2 (candidate_engine).

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/schemas/test_evidence.py
from datetime import UTC, datetime

from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
    compute_candidate_idempotency_key,
    compute_evidence_hash,
)


def _make_record(evaluated_at=None) -> CandidateEvidenceRecord:
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h", "4h"],
        evaluated_at=evaluated_at or datetime.now(UTC),
        price_volatility_evidence=PriceVolatilityEvidence(
            triggered=True, metric="pct_change_1h", value=3.2, baseline=0.5, threshold=2.0
        ),
        momentum_breakout_evidence=MomentumBreakoutEvidence(
            triggered=False, metric="rsi", value=55.0, baseline=50.0, threshold=70.0
        ),
        volume_evidence=VolumeEvidence(
            triggered=True, metric="volume_zscore", value=3.1, baseline=1.0, threshold=2.5
        ),
        funding_oi_evidence=FundingOpenInterestEvidence(
            triggered=False, metric="funding_rate", value=0.01, baseline=0.01, threshold=0.05
        ),
        candidate_score=0.71,
        trigger_reasons=["price_volatility", "volume"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def test_evidence_record_outcome_never_a_direction():
    record = _make_record()
    assert record.outcome in ("worth_deeper_analysis", "not_a_candidate")


def test_evidence_hash_is_deterministic_for_identical_content():
    r1 = _make_record(evaluated_at=datetime(2026, 1, 1, tzinfo=UTC))
    r2 = _make_record(evaluated_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC))
    # olika evaluated_at, i övrigt identiskt innehåll -> evaluated_at exkluderas ur hashen
    assert compute_evidence_hash(r1) == compute_evidence_hash(r2)


def test_evidence_hash_changes_with_content():
    r1 = _make_record()
    r2 = _make_record()
    r2.candidate_score = 0.99
    assert compute_evidence_hash(r1) != compute_evidence_hash(r2)


def test_idempotency_key_is_deterministic_and_case_insensitive():
    key1 = compute_candidate_idempotency_key("BTCUSDT", "run-1", "hash-abc")
    key2 = compute_candidate_idempotency_key("btcusdt ", "run-1", "hash-abc")
    assert key1 == key2


def test_idempotency_key_differs_for_different_instruments():
    key1 = compute_candidate_idempotency_key("BTCUSDT", "run-1", "hash-abc")
    key2 = compute_candidate_idempotency_key("ETHUSDT", "run-1", "hash-abc")
    assert key1 != key2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/schemas/test_evidence.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/schemas/evidence.py
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from crypto_trading.schemas.common import DataQualityStatus


class PriceVolatilityEvidence(BaseModel):
    triggered: bool
    metric: str
    value: float
    baseline: float
    threshold: float


class MomentumBreakoutEvidence(BaseModel):
    triggered: bool
    metric: str
    value: float
    baseline: float
    threshold: float


class VolumeEvidence(BaseModel):
    triggered: bool
    metric: str
    value: float
    baseline: float
    threshold: float


class FundingOpenInterestEvidence(BaseModel):
    triggered: bool
    metric: str
    value: float
    baseline: float
    threshold: float


class CandidateEvidenceRecord(BaseModel):
    instrument: str
    timeframes: list[str]
    evaluated_at: datetime
    price_volatility_evidence: PriceVolatilityEvidence
    momentum_breakout_evidence: MomentumBreakoutEvidence
    volume_evidence: VolumeEvidence
    funding_oi_evidence: FundingOpenInterestEvidence
    candidate_score: float
    trigger_reasons: list[str]
    data_quality_status: DataQualityStatus
    outcome: Literal["worth_deeper_analysis", "not_a_candidate"]


def compute_evidence_hash(evidence: CandidateEvidenceRecord) -> str:
    """Hash av evidensens SEMANTISKA innehåll — evaluated_at exkluderas medvetet
    så att två beräkningar av samma underliggande evidens vid olika millisekund
    ger samma hash (SPEC §8.6 / Phase 0-design)."""
    data = evidence.model_dump(exclude={"evaluated_at"}, mode="json")
    canonical = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_candidate_idempotency_key(
    instrument: str, discovery_run_id: str, evidence_hash: str
) -> str:
    normalized_instrument = instrument.strip().upper()
    raw = f"{normalized_instrument}\x1f{discovery_run_id}\x1f{evidence_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/schemas/test_evidence.py -v`
Expected: PASS (5 tests — rättat räknefel upptäckt vid exekvering, testfilen har alltid haft 5 def test_-funktioner)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/schemas/evidence.py tests/crypto_trading/schemas/test_evidence.py
git commit -m "crypto_trading Phase 0 steg 4: CandidateEvidenceRecord + idempotens-hashning"
```

---

## Task 5: `schemas/assessments.py` — sju AssessmentTyper

**Files:**
- Create: `crypto_trading/schemas/assessments.py`
- Test: `tests/crypto_trading/schemas/test_assessments.py`

**Interfaces:**
- Consumes: `AssessmentStatus` från `crypto_trading.schemas.common`.
- Produces: `AssessmentBase` + `NewsSentimentAssessment`, `TechnicalAssessment`, `BullThesisAssessment`, `ForecastAssessment`, `RiskAssessment`, `BearAdversarialAssessment`, `QAAssessment` — konsumeras av Task 6 (`Candidate`).

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/schemas/test_assessments.py
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from crypto_trading.schemas.assessments import (
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)

_BASE_KWARGS = {"agent_name": "test-agent", "run_id": "run-1", "created_at": datetime.now(UTC)}


def test_news_sentiment_separates_fact_claim_interpretation():
    a = NewsSentimentAssessment(
        **_BASE_KWARGS,
        status="ok",
        verified_facts=["BTC traded above 50000"],
        source_claims=["source X claims institutional buying"],
        interpretation="short-term bullish sentiment",
    )
    assert a.verified_facts != a.source_claims


def test_qa_assessment_passed_and_violations():
    a = QAAssessment(**_BASE_KWARGS, status="ok", passed=False, violations=["missing risk field"])
    assert a.passed is False
    assert a.violations == ["missing risk field"]


def test_forecast_scenario_probabilities_must_sum_to_one():
    with pytest.raises(ValidationError):
        ForecastAssessment(
            **_BASE_KWARGS,
            status="ok",
            scenario_probabilities={"bullish": 0.9, "bearish": 0.5},
            horizon="4h",
            forecast_version="v1",
        )


def test_forecast_scenario_probabilities_valid_sum():
    a = ForecastAssessment(
        **_BASE_KWARGS,
        status="ok",
        scenario_probabilities={"bullish": 0.6, "neutral": 0.25, "bearish": 0.15},
        horizon="4h",
        forecast_version="v1",
    )
    assert abs(sum(a.scenario_probabilities.values()) - 1.0) < 0.001


def test_risk_assessment_is_advisory_fields_only():
    a = RiskAssessment(
        **_BASE_KWARGS,
        status="ok",
        suggested_stop_loss="49000",
        suggested_target="53000",
        downside="high volatility",
        liquidity_risk="low",
        model_risk="medium",
        timing_risk="low",
    )
    assert a.suggested_stop_loss == "49000"


def test_bear_adversarial_requires_falsification_conditions():
    a = BearAdversarialAssessment(
        **_BASE_KWARGS,
        status="ok",
        counterarguments=["overbought on 4h RSI"],
        alternative_explanations=["thin weekend liquidity"],
        falsification_conditions="price closes below 48000 on daily",
    )
    assert a.falsification_conditions


def test_technical_and_bull_thesis_construct():
    TechnicalAssessment(**_BASE_KWARGS, status="ok", market_data={"price": 50000}, interpretation="uptrend")
    BullThesisAssessment(
        **_BASE_KWARGS, status="ok", hypothesis="breakout", catalyst="ETF news", setup="range breakout"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/schemas/test_assessments.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/schemas/assessments.py
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator

from crypto_trading.schemas.common import AssessmentStatus


class AssessmentBase(BaseModel):
    agent_name: str
    run_id: str
    created_at: datetime
    status: AssessmentStatus


class NewsSentimentAssessment(AssessmentBase):
    verified_facts: list[str]
    source_claims: list[str]
    interpretation: str


class TechnicalAssessment(AssessmentBase):
    market_data: dict
    interpretation: str


class BullThesisAssessment(AssessmentBase):
    hypothesis: str
    catalyst: str
    setup: str


class ForecastAssessment(AssessmentBase):
    scenario_probabilities: dict[str, float]
    horizon: str
    forecast_version: str

    @field_validator("scenario_probabilities")
    @classmethod
    def probabilities_sum_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if not (0.999 <= total <= 1.001):
            raise ValueError(f"scenario_probabilities must sum to 1.0, got {total}")
        return v


class RiskAssessment(AssessmentBase):
    suggested_stop_loss: str
    suggested_target: str
    downside: str
    liquidity_risk: str
    model_risk: str
    timing_risk: str


class BearAdversarialAssessment(AssessmentBase):
    counterarguments: list[str]
    alternative_explanations: list[str]
    falsification_conditions: str


class QAAssessment(AssessmentBase):
    passed: bool
    violations: list[str]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/schemas/test_assessments.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/schemas/assessments.py tests/crypto_trading/schemas/test_assessments.py
git commit -m "crypto_trading Phase 0 steg 5: sju AssessmentTyper"
```

---

## Task 6: `schemas/candidate.py` — Candidate (aggregat)

**Files:**
- Create: `crypto_trading/schemas/candidate.py`
- Test: `tests/crypto_trading/schemas/test_candidate.py`

**Interfaces:**
- Consumes: `CandidateStatus` (common.py), `CandidateEvidenceRecord` (evidence.py), de sju assessment-typerna (assessments.py).
- Produces: `Candidate` — konsumeras av Task 10/11 (repository).

*Ingen separat "okänt state"-typ finns här. Ett korrupt/orepresenterat lagrat
`status`-värde är aldrig ett domänobjekt — det hanteras uteslutande av
`storage.repository` via `CorruptCandidateStateError` + ett
`CORRUPT_STATE_DETECTED`-event (se Task 10). `CandidateStatus` (Task 2) har
exakt åtta giltiga värden och inget "UNKNOWN_STATE"-medlem.*

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/schemas/test_candidate.py
from datetime import UTC, datetime

from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)


def _make_evidence() -> CandidateEvidenceRecord:
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=datetime.now(UTC),
        price_volatility_evidence=PriceVolatilityEvidence(
            triggered=True, metric="pct_change_1h", value=3.2, baseline=0.5, threshold=2.0
        ),
        momentum_breakout_evidence=MomentumBreakoutEvidence(
            triggered=False, metric="rsi", value=55.0, baseline=50.0, threshold=70.0
        ),
        volume_evidence=VolumeEvidence(
            triggered=True, metric="volume_zscore", value=3.1, baseline=1.0, threshold=2.5
        ),
        funding_oi_evidence=FundingOpenInterestEvidence(
            triggered=False, metric="funding_rate", value=0.01, baseline=0.01, threshold=0.05
        ),
        candidate_score=0.71,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def test_candidate_starts_with_all_assessments_none():
    candidate = Candidate(
        candidate_id="cand-1",
        idempotency_key="key-1",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status="CANDIDATE",
        evidence_record=_make_evidence(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    assert candidate.news_sentiment is None
    assert candidate.technical is None
    assert candidate.bull_thesis is None
    assert candidate.forecast is None
    assert candidate.risk is None
    assert candidate.bear_adversarial is None
    assert candidate.qa is None
    assert candidate.status == "CANDIDATE"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/schemas/test_candidate.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/schemas/candidate.py
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from crypto_trading.schemas.assessments import (
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)
from crypto_trading.schemas.common import CandidateStatus
from crypto_trading.schemas.evidence import CandidateEvidenceRecord


class Candidate(BaseModel):
    candidate_id: str
    idempotency_key: str
    instrument: str
    discovery_run_id: str
    evidence_hash: str
    status: CandidateStatus
    evidence_record: CandidateEvidenceRecord
    created_at: datetime
    updated_at: datetime

    news_sentiment: NewsSentimentAssessment | None = None
    technical: TechnicalAssessment | None = None
    bull_thesis: BullThesisAssessment | None = None
    forecast: ForecastAssessment | None = None
    risk: RiskAssessment | None = None
    bear_adversarial: BearAdversarialAssessment | None = None
    qa: QAAssessment | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/schemas/test_candidate.py -v`
Expected: PASS (1 test)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/schemas/candidate.py tests/crypto_trading/schemas/test_candidate.py
git commit -m "crypto_trading Phase 0 steg 6: Candidate-aggregat"
```

---

## Task 7: `schemas/trade.py` + `schemas/forecast.py` — Decimal-scheman

**Files:**
- Create: `crypto_trading/schemas/trade.py`
- Create: `crypto_trading/schemas/forecast.py`
- Test: `tests/crypto_trading/schemas/test_trade.py`
- Test: `tests/crypto_trading/schemas/test_forecast.py`

**Interfaces:**
- Consumes: `PositionStatus` (common.py).
- Produces: `Position` (med `Decimal`-fält enligt SPEC §11), `ForecastRecord` — konsumeras av Task 9 (db-schema) och senare faser (Phase 4/8).

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/schemas/test_trade.py
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.schemas.trade import Position


def test_position_theoretical_and_simulated_fill_are_separate_decimal_fields():
    position = Position(
        position_id="pos-1",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry=Decimal("50000.00"),
        simulated_fill_entry=Decimal("50005.25"),
        stop_loss=Decimal("49000.00"),
        target=Decimal("53000.00"),
        size=Decimal("0.1"),
        fill_model_version="v1",
        opened_at=datetime.now(UTC),
    )
    assert isinstance(position.theoretical_entry, Decimal)
    assert isinstance(position.simulated_fill_entry, Decimal)
    assert position.theoretical_entry != position.simulated_fill_entry
    assert position.closed_at is None


def test_position_decimal_precision_is_exact_not_float():
    position = Position(
        position_id="pos-2",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        direction="SHORT",
        status="OPEN_POSITION",
        theoretical_entry=Decimal("0.1"),
        simulated_fill_entry=Decimal("0.100001"),
        stop_loss=Decimal("0.11"),
        target=Decimal("0.09"),
        size=Decimal("123456789.123456789"),
        fill_model_version="v1",
        opened_at=datetime.now(UTC),
    )
    # 0.1 som float är inte exakt 0.1 - detta bevisar att Decimal-vägen aldrig passerar float
    assert position.theoretical_entry == Decimal("0.1")
    assert str(position.size) == "123456789.123456789"
```

```python
# tests/crypto_trading/schemas/test_forecast.py
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from crypto_trading.schemas.forecast import ForecastRecord


def test_forecast_record_probabilities_must_sum_to_one():
    with pytest.raises(ValidationError):
        ForecastRecord(
            forecast_id="fc-1",
            candidate_id="cand-1",
            instrument="BTCUSDT",
            forecast_timestamp=datetime.now(UTC),
            horizon="4h",
            scenario_probabilities={"bullish": 0.9, "bearish": 0.9},
            forecast_version="v1",
            market_state_metadata={},
        )


def test_forecast_record_outcome_fields_start_none():
    record = ForecastRecord(
        forecast_id="fc-1",
        candidate_id="cand-1",
        instrument="BTCUSDT",
        forecast_timestamp=datetime.now(UTC),
        horizon="4h",
        scenario_probabilities={"bullish": 0.6, "neutral": 0.25, "bearish": 0.15},
        forecast_version="v1",
        market_state_metadata={"funding_rate": 0.01},
    )
    assert record.actual_outcome is None
    assert record.outcome_timestamp is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/schemas/test_trade.py tests/crypto_trading/schemas/test_forecast.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/schemas/trade.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from crypto_trading.schemas.common import PositionStatus

Direction = Literal["LONG", "SHORT"]


class Position(BaseModel):
    position_id: str
    candidate_id: str
    instrument: str
    direction: Direction
    status: PositionStatus
    theoretical_entry: Decimal
    simulated_fill_entry: Decimal
    stop_loss: Decimal
    target: Decimal
    size: Decimal
    fill_model_version: str
    opened_at: datetime
    theoretical_exit: Decimal | None = None
    simulated_fill_exit: Decimal | None = None
    exit_reason: str | None = None
    fees: Decimal | None = None
    funding: Decimal | None = None
    closed_at: datetime | None = None
```

```python
# crypto_trading/schemas/forecast.py
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator


class ForecastRecord(BaseModel):
    forecast_id: str
    candidate_id: str
    instrument: str
    forecast_timestamp: datetime
    horizon: str
    scenario_probabilities: dict[str, float]
    forecast_version: str
    market_state_metadata: dict
    actual_outcome: str | None = None
    outcome_timestamp: datetime | None = None

    @field_validator("scenario_probabilities")
    @classmethod
    def probabilities_sum_to_one(cls, v: dict[str, float]) -> dict[str, float]:
        total = sum(v.values())
        if not (0.999 <= total <= 1.001):
            raise ValueError(f"scenario_probabilities must sum to 1.0, got {total}")
        return v
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/schemas/test_trade.py tests/crypto_trading/schemas/test_forecast.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/schemas/trade.py crypto_trading/schemas/forecast.py tests/crypto_trading/schemas/test_trade.py tests/crypto_trading/schemas/test_forecast.py
git commit -m "crypto_trading Phase 0 steg 7: Position (Decimal) + ForecastRecord"
```

---

## Task 8: Config-lager — `config/exceptions.py`, `config/loader.py`, YAML-filer

**Files:**
- Create: `crypto_trading/config/exceptions.py`
- Create: `crypto_trading/config/loader.py`
- Create: `crypto_trading/config/pipeline.yaml`
- Create: `crypto_trading/config/risk_limits.yaml`
- Create: `crypto_trading/config/budget_limits.yaml`
- Test: `tests/crypto_trading/config/test_loader.py`

**Interfaces:**
- Produces: `ConfigError`, `PipelineConfig`, `RiskLimitsConfig`, `BudgetLimitsConfig`, `Settings`, `get_settings() -> Settings`. `get_settings()` är entrypointen framtida faser använder.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/config/test_loader.py
from decimal import Decimal

import pytest

from crypto_trading.config.exceptions import ConfigError
from crypto_trading.config.loader import (
    BudgetLimitsConfig,
    PipelineConfig,
    RiskLimitsConfig,
    get_settings,
)


def test_get_settings_loads_real_yaml_files_successfully():
    settings = get_settings()
    assert settings.pipeline.top_n > 0
    assert settings.pipeline.discovery_interval_minutes > 0
    assert settings.risk_limits.starting_capital_usdt > 0
    assert isinstance(settings.risk_limits.starting_capital_usdt, Decimal)
    assert settings.budget_limits.max_ai_calls_per_day > 0


def test_pipeline_config_rejects_zero_top_n():
    with pytest.raises(Exception):
        PipelineConfig(
            discovery_interval_minutes=15,
            monitoring_interval_seconds=30,
            top_n=0,
            cooldown_minutes=60,
            max_data_age_seconds={"ticker": 30},
            min_sample_size_for_calibration=30,
            calibration_preliminary_sample_size=10,
            sqlite_busy_timeout_ms=5000,
        )


def test_risk_limits_config_rejects_risk_pct_over_one():
    with pytest.raises(Exception):
        RiskLimitsConfig(
            starting_capital_usdt=Decimal("10000"),
            risk_per_trade_pct=Decimal("1.5"),
            max_concurrent_positions=5,
            max_total_exposure_pct=Decimal("0.25"),
            spread_pct=Decimal("0.0005"),
            slippage_pct=Decimal("0.0005"),
            fee_pct=Decimal("0.0004"),
        )


def test_budget_limits_config_rejects_zero_calls():
    with pytest.raises(Exception):
        BudgetLimitsConfig(
            max_candidates_per_discovery_run=10,
            max_ai_calls_per_discovery_run=0,
            max_ai_calls_per_day=500,
            warning_threshold_pct=Decimal("0.8"),
        )


def test_missing_config_file_raises_config_error(tmp_path, monkeypatch):
    import crypto_trading.config.loader as loader_module

    monkeypatch.setattr(loader_module, "_CONFIG_DIR", tmp_path)
    with pytest.raises(ConfigError):
        get_settings()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/config/test_loader.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the YAML files**

```yaml
# crypto_trading/config/pipeline.yaml
discovery_interval_minutes: 15
monitoring_interval_seconds: 30
top_n: 30
cooldown_minutes: 60
max_data_age_seconds:
  ticker: 30
  kline: 120
  funding_rate: 3600
  open_interest: 300
min_sample_size_for_calibration: 30
calibration_preliminary_sample_size: 10
sqlite_busy_timeout_ms: 5000
```

```yaml
# crypto_trading/config/risk_limits.yaml
# OBS: alla penningvärden citerade som strängar - Decimal ska ALDRIG passera
# via ett YAML-flyttal (float-precision skulle då redan ha smugit in vid inläsning).
starting_capital_usdt: "10000"
risk_per_trade_pct: "0.01"
max_concurrent_positions: 5
max_total_exposure_pct: "0.25"
spread_pct: "0.0005"
slippage_pct: "0.0005"
fee_pct: "0.0004"
```

```yaml
# crypto_trading/config/budget_limits.yaml
max_candidates_per_discovery_run: 10
max_ai_calls_per_discovery_run: 70
max_ai_calls_per_day: 500
warning_threshold_pct: "0.8"
```

- [ ] **Step 4: Write minimal implementation**

```python
# crypto_trading/config/exceptions.py
from __future__ import annotations


class ConfigError(Exception):
    """Kastas när konfiguration saknas eller inte validerar. Fail-fast vid start —
    aldrig en tyst default för ett SPEC-obligatoriskt värde."""
```

```python
# crypto_trading/config/loader.py
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from crypto_trading.config.exceptions import ConfigError

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "crypto_trading" / "config"


class PipelineConfig(BaseModel):
    discovery_interval_minutes: int = Field(gt=0)
    monitoring_interval_seconds: int = Field(gt=0)
    top_n: int = Field(gt=0)
    cooldown_minutes: int = Field(gt=0)
    max_data_age_seconds: dict[str, int]
    min_sample_size_for_calibration: int = Field(gt=0)
    calibration_preliminary_sample_size: int = Field(gt=0)
    sqlite_busy_timeout_ms: int = Field(gt=0)


class RiskLimitsConfig(BaseModel):
    starting_capital_usdt: Decimal = Field(gt=0)
    risk_per_trade_pct: Decimal = Field(gt=0, le=1)
    max_concurrent_positions: int = Field(gt=0)
    max_total_exposure_pct: Decimal = Field(gt=0, le=1)
    spread_pct: Decimal = Field(ge=0)
    slippage_pct: Decimal = Field(ge=0)
    fee_pct: Decimal = Field(ge=0)


class BudgetLimitsConfig(BaseModel):
    max_candidates_per_discovery_run: int = Field(gt=0)
    max_ai_calls_per_discovery_run: int = Field(gt=0)
    max_ai_calls_per_day: int = Field(gt=0)
    warning_threshold_pct: Decimal = Field(gt=0, le=1)


class Settings(BaseModel):
    db_path: Path
    pipeline: PipelineConfig
    risk_limits: RiskLimitsConfig
    budget_limits: BudgetLimitsConfig


def _load_yaml_model(path: Path, model: type[BaseModel]) -> BaseModel:
    if not path.exists():
        raise ConfigError(f"config file missing: {path}")
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}: {exc}") from exc


def get_settings() -> Settings:
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    db_path_override = os.environ.get("CRYPTO_TRADING_DB_PATH_OVERRIDE")
    db_path = (
        Path(db_path_override)
        if db_path_override
        else _PROJECT_ROOT / "data" / "crypto_trading.db"
    )
    return Settings(
        db_path=db_path,
        pipeline=_load_yaml_model(_CONFIG_DIR / "pipeline.yaml", PipelineConfig),
        risk_limits=_load_yaml_model(_CONFIG_DIR / "risk_limits.yaml", RiskLimitsConfig),
        budget_limits=_load_yaml_model(_CONFIG_DIR / "budget_limits.yaml", BudgetLimitsConfig),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/crypto_trading/config/test_loader.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add crypto_trading/config/exceptions.py crypto_trading/config/loader.py crypto_trading/config/pipeline.yaml crypto_trading/config/risk_limits.yaml crypto_trading/config/budget_limits.yaml tests/crypto_trading/config/test_loader.py
git commit -m "crypto_trading Phase 0 steg 8: config-lager (YAML + Pydantic-validering, fail-fast)"
```

---

## Task 9: `storage/db.py` — schema, WAL, append-only events-triggers

**Files:**
- Create: `crypto_trading/storage/db.py`
- Test: `tests/crypto_trading/storage/test_db.py`

**Interfaces:**
- Produces: `get_connection(path, busy_timeout_ms) -> sqlite3.Connection`, `init_schema(conn)`, `SCHEMA_VERSION` — konsumeras av Task 10/11 (repository). Denna task låser även den konkreta `Decimal -> str`-serialiseringskonventionen (SQLite `TEXT`-kolumner och JSON-payloads) som alla senare faser (särskilt Phase 4:s paper trading) måste följa — testad direkt mot en redan existerande `TEXT`-kolumn i schemat, utan att bygga någon positions-repository-logik i Phase 0.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/storage/test_db.py
import json
import sqlite3
from decimal import Decimal

import pytest

from crypto_trading.storage.db import SCHEMA_VERSION, get_connection


def test_get_connection_enables_wal_mode(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_get_connection_sets_busy_timeout(tmp_path):
    conn = get_connection(tmp_path / "test.db", busy_timeout_ms=1234)
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 1234


def test_init_schema_is_idempotent(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO candidates "
        "(candidate_id, idempotency_key, instrument, discovery_run_id, evidence_hash, "
        "status, evidence_record, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("c1", "k1", "BTCUSDT", "run-1", "hash-1", "CANDIDATE", "{}", "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    # anropa init_schema igen (som en ny get_connection skulle göra) - ska inte kasta eller ta bort data
    from crypto_trading.storage.db import init_schema

    init_schema(conn)
    row = conn.execute("SELECT candidate_id FROM candidates WHERE candidate_id = 'c1'").fetchone()
    assert row is not None


def test_schema_version_is_recorded(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    assert row[0] == str(SCHEMA_VERSION)


def test_events_table_rejects_update(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id, occurred_at, "
        "run_id, schema_version, payload) VALUES (?,?,?,?,?,?,?,?)",
        ("e1", "CANDIDATE_CREATED", "candidate", "c1", "2026-01-01", "run-1", 1, "{}"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE events SET event_type = 'X' WHERE event_id = 'e1'")


def test_events_table_rejects_delete(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id, occurred_at, "
        "run_id, schema_version, payload) VALUES (?,?,?,?,?,?,?,?)",
        ("e2", "CANDIDATE_CREATED", "candidate", "c1", "2026-01-01", "run-1", 1, "{}"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM events WHERE event_id = 'e2'")


def test_events_seq_is_monotonically_increasing(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    for i in range(3):
        conn.execute(
            "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id, occurred_at, "
            "run_id, schema_version, payload) VALUES (?,?,?,?,?,?,?,?)",
            (f"e{i}", "X", "candidate", "c1", "2026-01-01", "run-1", 1, "{}"),
        )
    conn.commit()
    rows = conn.execute("SELECT seq FROM events ORDER BY seq").fetchall()
    seqs = [r[0] for r in rows]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 3


_DECIMAL_ROUNDTRIP_VALUES = [
    Decimal("0.1"),
    Decimal("1.234567890123456789"),  # 18 decimaler - fler signifikanta siffror än float64 (~15-17)
    Decimal("9876543210.21"),  # miljonklass
]


@pytest.mark.parametrize("value", _DECIMAL_ROUNDTRIP_VALUES)
def test_decimal_sqlite_text_roundtrip_is_exact(tmp_path, value):
    """Låser konventionen Decimal -> str -> SQLite TEXT -> str -> Decimal.
    Ingen positions-repository byggs för detta - bara den råa konventionen
    testas direkt mot en TEXT-kolumn som redan finns i schemat (positions.size),
    utan att dra in någon Phase 4-funktionalitet."""
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO positions "
        "(position_id, candidate_id, instrument, direction, status, theoretical_entry, "
        "simulated_fill_entry, stop_loss, target, size, fill_model_version, opened_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "pos-decimal-test",
            "cand-1",
            "BTCUSDT",
            "LONG",
            "OPEN_POSITION",
            "50000",
            "50000",
            "49000",
            "53000",
            str(value),  # canonical: alltid str(Decimal), aldrig float(...)
            "v1",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()

    row = conn.execute(
        "SELECT size FROM positions WHERE position_id = 'pos-decimal-test'"
    ).fetchone()
    stored_text = row["size"]
    assert isinstance(stored_text, str)
    reconstructed = Decimal(stored_text)

    assert reconstructed == value
    assert stored_text == str(value)  # ingen precisionsförlust i strängformen


def test_decimal_high_precision_value_would_lose_precision_via_float_but_not_via_str():
    """Bevisar konkret VARFÖR float aldrig får användas i serialiseringsvägen:
    ett värde med fler signifikanta siffror än float64 klarar av tappar
    precision om det passerar via float, men inte via str(Decimal)."""
    value = Decimal("1.234567890123456789")

    lost_via_float = Decimal(str(float(value)))
    assert lost_via_float != value  # bevisar att float-vägen FAKTISKT tappar precision

    preserved_via_str = Decimal(str(value))
    assert preserved_via_str == value  # str-vägen (den vi faktiskt använder) tappar ingenting


@pytest.mark.parametrize("value", _DECIMAL_ROUNDTRIP_VALUES)
def test_decimal_json_roundtrip_is_exact_never_via_float(value):
    """Låser samma konvention för JSON-payloads (t.ex. events.payload):
    Decimal -> str -> json.dumps -> json.loads -> Decimal, aldrig via float."""
    payload = {"amount": str(value)}
    serialized = json.dumps(payload, default=str)
    deserialized = json.loads(serialized)
    reconstructed = Decimal(deserialized["amount"])

    assert reconstructed == value
    assert isinstance(deserialized["amount"], str)  # aldrig ett JSON-tal/float
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/storage/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/storage/db.py
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    run_id TEXT,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: DELETE is not permitted');
END;

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    instrument TEXT NOT NULL,
    discovery_run_id TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_record TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assessments (
    candidate_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (candidate_id, field_name)
);

CREATE TABLE IF NOT EXISTS gate_decisions (
    candidate_id TEXT PRIMARY KEY,
    decision TEXT NOT NULL,
    reasons TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);

-- positions TÄCKER hela livscykeln öppen->stängd (ingen separat trades-tabell,
-- se "Implementationsanmärkningar" i planens header).
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    theoretical_entry TEXT NOT NULL,
    simulated_fill_entry TEXT NOT NULL,
    stop_loss TEXT NOT NULL,
    target TEXT NOT NULL,
    size TEXT NOT NULL,
    fill_model_version TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    theoretical_exit TEXT,
    simulated_fill_exit TEXT,
    exit_reason TEXT,
    fees TEXT,
    funding TEXT,
    closed_at TEXT
);

-- forecasts har utfallsfälten inbyggda (ingen separat forecast_outcomes-tabell,
-- se "Implementationsanmärkningar" i planens header).
CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    forecast_timestamp TEXT NOT NULL,
    horizon TEXT NOT NULL,
    scenario_probabilities TEXT NOT NULL,
    forecast_version TEXT NOT NULL,
    market_state_metadata TEXT NOT NULL,
    actual_outcome TEXT,
    outcome_timestamp TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT NOT NULL,
    run_type TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    status TEXT,
    errors TEXT
);
"""


def get_connection(path: Path, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/storage/test_db.py -v`
Expected: PASS (14 tester: 7 schema/WAL-tester + 3 Decimal/SQLite-rundtur + 1 float-precisionsbevis + 3 Decimal/JSON-rundtur)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/storage/db.py tests/crypto_trading/storage/test_db.py
git commit -m "crypto_trading Phase 0 steg 9: SQLite-schema, WAL, append-only events-triggers, Decimal-rundtur"
```

---

## Task 10: `storage/repository.py` — Repository-protokoll, candidate-skapande, korrupt-state-hantering

**Files:**
- Create: `crypto_trading/storage/exceptions.py`
- Create: `crypto_trading/storage/repository.py`
- Test: `tests/crypto_trading/storage/test_repository_candidate.py`

**Interfaces:**
- Consumes: `get_connection` (db.py), `Candidate` (candidate.py), `CandidateEvidenceRecord` (evidence.py), `Event` (event.py).
- Produces: `Repository` (Protocol), `SQLiteRepository.create_candidate_with_event(candidate, event) -> bool`, `SQLiteRepository.get_candidate(candidate_id) -> Candidate | None`, `SQLiteRepository.find_candidates_by_status(status) -> list[Candidate]`, `CorruptCandidateStateError` — konsumeras av Task 11 (atomicitet), Task 12/13 (state_machine, inkl. sweepen som förlitar sig på att `find_candidates_by_status` hoppar över korrupta rader istället för att avbryta), Task 14 (samtidighet).

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/storage/test_repository_candidate.py
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.storage.exceptions import CorruptCandidateStateError
from crypto_trading.storage.repository import SQLiteRepository


def _make_evidence() -> CandidateEvidenceRecord:
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=datetime.now(UTC),
        price_volatility_evidence=PriceVolatilityEvidence(
            triggered=True, metric="pct_change_1h", value=3.2, baseline=0.5, threshold=2.0
        ),
        momentum_breakout_evidence=MomentumBreakoutEvidence(
            triggered=False, metric="rsi", value=55.0, baseline=50.0, threshold=70.0
        ),
        volume_evidence=VolumeEvidence(
            triggered=True, metric="volume_zscore", value=3.1, baseline=1.0, threshold=2.5
        ),
        funding_oi_evidence=FundingOpenInterestEvidence(
            triggered=False, metric="funding_rate", value=0.01, baseline=0.01, threshold=0.05
        ),
        candidate_score=0.71,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _make_candidate(candidate_id="cand-1", idempotency_key="key-1", status="CANDIDATE") -> Candidate:
    now = datetime.now(UTC)
    return Candidate(
        candidate_id=candidate_id,
        idempotency_key=idempotency_key,
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status=status,
        evidence_record=_make_evidence(),
        created_at=now,
        updated_at=now,
    )


def _make_event(candidate: Candidate, event_type: str) -> Event:
    return Event(
        event_id=f"{event_type}:{candidate.candidate_id}",
        event_type=event_type,
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=datetime.now(UTC),
        run_id=candidate.discovery_run_id,
        schema_version=1,
        payload={"instrument": candidate.instrument},
    )


def test_create_candidate_with_event_persists_both(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    event = _make_event(candidate, "CANDIDATE_CREATED")

    created = repo.create_candidate_with_event(candidate, event)

    assert created is True
    reloaded = repo.get_candidate("cand-1")
    assert reloaded is not None
    assert reloaded.status == "CANDIDATE"
    row = repo._conn.execute("SELECT event_type FROM events WHERE event_id = ?", (event.event_id,)).fetchone()
    assert row is not None
    assert row["event_type"] == "CANDIDATE_CREATED"


def test_create_candidate_with_event_is_idempotent_on_retry(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    event = _make_event(candidate, "CANDIDATE_CREATED")

    first = repo.create_candidate_with_event(candidate, event)
    second = repo.create_candidate_with_event(candidate, event)

    assert first is True
    assert second is False  # idempotent no-op, ingen dubblett
    count = repo._conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1
    event_count = repo._conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert event_count == 1


def test_get_candidate_returns_none_when_missing(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.get_candidate("does-not-exist") is None


def test_get_candidate_raises_corrupt_state_error_on_unrecognized_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    event = _make_event(candidate, "CANDIDATE_CREATED")
    repo.create_candidate_with_event(candidate, event)

    # simulera datakorruption: skriv ett ogiltigt status-värde direkt
    repo._conn.execute(
        "UPDATE candidates SET status = 'GARBAGE' WHERE candidate_id = 'cand-1'"
    )
    repo._conn.commit()

    with pytest.raises(CorruptCandidateStateError) as exc_info:
        repo.get_candidate("cand-1")

    assert exc_info.value.candidate_id == "cand-1"
    assert exc_info.value.raw_status == "GARBAGE"
    assert exc_info.value.corrupted_field == "status"

    corrupt_event = repo._conn.execute(
        "SELECT payload FROM events WHERE event_type = 'CORRUPT_STATE_DETECTED' "
        "AND aggregate_id = 'cand-1'"
    ).fetchone()
    assert corrupt_event is not None
    assert json.loads(corrupt_event["payload"])["corrupted_field"] == "status"


def test_get_candidate_raises_corrupt_state_error_on_corrupt_evidence_record(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate(candidate_id="cand-2", idempotency_key="key-2")
    event = _make_event(candidate, "CANDIDATE_CREATED")
    repo.create_candidate_with_event(candidate, event)

    # simulera datakorruption i evidence_record-kolumnen, INTE status
    repo._conn.execute(
        "UPDATE candidates SET evidence_record = 'not valid json' WHERE candidate_id = 'cand-2'"
    )
    repo._conn.commit()

    with pytest.raises(CorruptCandidateStateError) as exc_info:
        repo.get_candidate("cand-2")

    assert exc_info.value.candidate_id == "cand-2"
    assert exc_info.value.corrupted_field == "evidence_record"

    corrupt_event = repo._conn.execute(
        "SELECT payload FROM events WHERE event_type = 'CORRUPT_STATE_DETECTED' "
        "AND aggregate_id = 'cand-2'"
    ).fetchone()
    assert corrupt_event is not None
    assert json.loads(corrupt_event["payload"])["corrupted_field"] == "evidence_record"


def test_get_candidate_raises_corrupt_state_error_on_corrupt_timestamp(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate(candidate_id="cand-3", idempotency_key="key-3")
    event = _make_event(candidate, "CANDIDATE_CREATED")
    repo.create_candidate_with_event(candidate, event)

    # simulera datakorruption i created_at-kolumnen
    repo._conn.execute(
        "UPDATE candidates SET created_at = 'not-a-timestamp' WHERE candidate_id = 'cand-3'"
    )
    repo._conn.commit()

    with pytest.raises(CorruptCandidateStateError) as exc_info:
        repo.get_candidate("cand-3")

    assert exc_info.value.candidate_id == "cand-3"
    assert exc_info.value.corrupted_field == "timestamp"

    corrupt_event = repo._conn.execute(
        "SELECT payload FROM events WHERE event_type = 'CORRUPT_STATE_DETECTED' "
        "AND aggregate_id = 'cand-3'"
    ).fetchone()
    assert corrupt_event is not None
    assert json.loads(corrupt_event["payload"])["corrupted_field"] == "timestamp"


def test_find_candidates_by_status_skips_corrupt_rows_and_keeps_valid_ones(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")

    valid = _make_candidate(candidate_id="cand-valid", idempotency_key="key-valid")
    repo.create_candidate_with_event(valid, _make_event(valid, "CANDIDATE_CREATED"))

    corrupt = _make_candidate(candidate_id="cand-corrupt", idempotency_key="key-corrupt")
    repo.create_candidate_with_event(corrupt, _make_event(corrupt, "CANDIDATE_CREATED"))
    # korrumpera evidence_record, INTE status - annars skulle raden inte
    # längre matcha WHERE status = 'CANDIDATE' och testet skulle inte
    # faktiskt exercisera "hittad av statusfrågan men trasig vid full
    # deserialisering".
    repo._conn.execute(
        "UPDATE candidates SET evidence_record = 'not valid json' WHERE candidate_id = 'cand-corrupt'"
    )
    repo._conn.commit()

    result = repo.find_candidates_by_status("CANDIDATE")

    result_ids = {c.candidate_id for c in result}
    assert result_ids == {"cand-valid"}  # korrupt rad hoppades över, avbröt inte resten

    corrupt_event = repo._conn.execute(
        "SELECT 1 FROM events WHERE event_type = 'CORRUPT_STATE_DETECTED' "
        "AND aggregate_id = 'cand-corrupt'"
    ).fetchone()
    assert corrupt_event is not None  # ändå auditerad, trots att den uteslöts ur resultatet


def test_repository_protocol_exposes_no_update_or_delete_event_method():
    assert not hasattr(SQLiteRepository, "update_event")
    assert not hasattr(SQLiteRepository, "delete_event")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/storage/test_repository_candidate.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/storage/exceptions.py
from __future__ import annotations


class CorruptCandidateStateError(Exception):
    """En lagrad candidate-rad kunde inte deserialiseras till ett giltigt
    Candidate-objekt. `corrupted_field` anger var i deserialiseringskedjan
    felet upptäcktes: "evidence_record", "timestamp", eller "status"/
    "candidate" (se SQLiteRepository.get_candidate — Task 10)."""

    def __init__(self, candidate_id: str, raw_status: str, corrupted_field: str):
        self.candidate_id = candidate_id
        self.raw_status = raw_status
        self.corrupted_field = corrupted_field
        super().__init__(
            f"candidate {candidate_id} has corrupt persisted data in field "
            f"{corrupted_field!r} (raw status={raw_status!r})"
        )
```

```python
# crypto_trading/storage/repository.py
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import CandidateEvidenceRecord
from crypto_trading.storage.db import get_connection
from crypto_trading.storage.exceptions import CorruptCandidateStateError


class Repository(Protocol):
    def create_candidate_with_event(self, candidate: Candidate, event: Event) -> bool: ...
    def get_candidate(self, candidate_id: str) -> Candidate | None: ...
    def find_candidates_by_status(self, status: str) -> list[Candidate]: ...
    def transition_candidate_with_event(
        self, candidate_id: str, new_status: str, updated_at: datetime, event: Event
    ) -> None: ...


class SQLiteRepository:
    def __init__(self, path: Path, busy_timeout_ms: int = 5000):
        self._conn = get_connection(path, busy_timeout_ms=busy_timeout_ms)

    def create_candidate_with_event(self, candidate: Candidate, event: Event) -> bool:
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO candidates "
                "(candidate_id, idempotency_key, instrument, discovery_run_id, evidence_hash, "
                "status, evidence_record, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    candidate.candidate_id,
                    candidate.idempotency_key,
                    candidate.instrument,
                    candidate.discovery_run_id,
                    candidate.evidence_hash,
                    candidate.status,
                    candidate.evidence_record.model_dump_json(),
                    candidate.created_at.isoformat(),
                    candidate.updated_at.isoformat(),
                ),
            )
            created = cur.rowcount > 0
            if created:
                self._insert_event(event)
            self._conn.commit()
            return created
        except Exception:
            self._conn.rollback()
            raise

    def transition_candidate_with_event(
        self, candidate_id: str, new_status: str, updated_at: datetime, event: Event
    ) -> None:
        try:
            self._conn.execute(
                "UPDATE candidates SET status = ?, updated_at = ? WHERE candidate_id = ?",
                (new_status, updated_at.isoformat(), candidate_id),
            )
            self._insert_event(event)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _insert_event(self, event: Event) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO events "
            "(event_id, event_type, aggregate_type, aggregate_id, occurred_at, run_id, "
            "schema_version, payload) VALUES (?,?,?,?,?,?,?,?)",
            (
                event.event_id,
                event.event_type,
                event.aggregate_type,
                event.aggregate_id,
                event.occurred_at.isoformat(),
                event.run_id,
                event.schema_version,
                json.dumps(event.payload, default=str),
            ),
        )
        return cur.rowcount > 0

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        """Läser och deserialiserar en candidate-rad.

        Klassas som korrupt persistent state (CorruptCandidateStateError +
        CORRUPT_STATE_DETECTED), ALDRIG som ett delvis konstruerat Candidate:
        - evidence_record: ValidationError eller ValueError (json.JSONDecodeError
          ärver ValueError) vid CandidateEvidenceRecord.model_validate_json().
        - created_at/updated_at: ValueError vid datetime.fromisoformat().
        - övriga fält (i praktiken status, det enda återstående fältet med en
          begränsande typ - Literal): ValidationError vid den slutliga
          Candidate(**data)-konstruktionen.

        Fångar MEDVETET INTE bredare undantagstyper (KeyError, TypeError,
        AttributeError, ...) - de indikerar ett verkligt programmeringsfel
        (t.ex. ett schema/kod-mismatch efter en migrering), inte korrupt
        lagrad data, och ska propagera okontrollerat istället för att
        felaktigt klassas som CorruptCandidateStateError.
        """
        row = self._conn.execute(
            "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        raw_status = data["status"]

        try:
            data["evidence_record"] = CandidateEvidenceRecord.model_validate_json(
                data["evidence_record"]
            )
        except (ValidationError, ValueError) as exc:
            self._insert_corrupt_state_event(candidate_id, raw_status, "evidence_record")
            raise CorruptCandidateStateError(candidate_id, raw_status, "evidence_record") from exc

        try:
            data["created_at"] = datetime.fromisoformat(data["created_at"])
            data["updated_at"] = datetime.fromisoformat(data["updated_at"])
        except ValueError as exc:
            self._insert_corrupt_state_event(candidate_id, raw_status, "timestamp")
            raise CorruptCandidateStateError(candidate_id, raw_status, "timestamp") from exc

        try:
            return Candidate(**data)
        except ValidationError as exc:
            status_error = any(err["loc"] == ("status",) for err in exc.errors())
            corrupted_field = "status" if status_error else "candidate"
            self._insert_corrupt_state_event(candidate_id, raw_status, corrupted_field)
            raise CorruptCandidateStateError(candidate_id, raw_status, corrupted_field) from exc

    def _insert_corrupt_state_event(
        self, candidate_id: str, raw_status: str, corrupted_field: str
    ) -> None:
        event = Event(
            event_id=f"CORRUPT_STATE_DETECTED:{candidate_id}:{corrupted_field}",
            event_type="CORRUPT_STATE_DETECTED",
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            occurred_at=datetime.now(UTC),
            run_id=None,
            schema_version=1,
            payload={"raw_status": raw_status, "corrupted_field": corrupted_field},
        )
        self._insert_event(event)
        self._conn.commit()

    def find_candidates_by_status(self, status: str) -> list[Candidate]:
        """Ett korrupt candidate-state (CorruptCandidateStateError) hoppas
        över - redan auditerat av get_candidate() innan den kastade - och
        avbryter ALDRIG behandlingen av övriga, giltiga candidates i samma
        anrop (SPEC fail-safe-princip: ett trasigt objekt får inte blockera
        resten av systemet)."""
        rows = self._conn.execute(
            "SELECT candidate_id FROM candidates WHERE status = ?", (status,)
        ).fetchall()
        result = []
        for row in rows:
            try:
                candidate = self.get_candidate(row["candidate_id"])
            except CorruptCandidateStateError:
                continue
            if candidate is not None:
                result.append(candidate)
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/storage/test_repository_candidate.py -v`
Expected: PASS (8 tester: create+persist, idempotent retry, missing candidate, korrupt status, korrupt evidence_record, korrupt timestamp, find_candidates_by_status hoppar över korrupt rad, protokollet saknar update/delete-event)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/storage/exceptions.py crypto_trading/storage/repository.py tests/crypto_trading/storage/test_repository_candidate.py
git commit -m "crypto_trading Phase 0 steg 10: Repository — idempotent candidate-skapande, fullständig CorruptCandidateStateError-kedja"
```

---

## Task 11: Repository — atomicitet vid statebyte (rollback-test)

**Files:**
- Modify: `tests/crypto_trading/storage/test_repository_candidate.py` (nytt testfall, ingen produktionskodändring — `transition_candidate_with_event` skrevs redan i Task 10)

**Interfaces:**
- Consumes: `SQLiteRepository.transition_candidate_with_event` (redan implementerad i Task 10).

- [ ] **Step 1: Write the failing test**

```python
# tillägg i tests/crypto_trading/storage/test_repository_candidate.py
import sqlite3
from datetime import UTC, datetime


class _FailingConnection:
    """Wrapper runt en riktig sqlite3.Connection som injicerar ett fel på ett
    specifikt execute()-anrop. sqlite3.Connection är en C-typ vars execute-
    attribut är skrivskyddat per instans - kan INTE monkeypatchas direkt
    (`repo._conn.execute = ...` ger `AttributeError: attribute 'execute' is
    read-only`, upptäckt vid exekvering) - därför den här tunna wrappern
    istället, ibytt på repo._conn (som är ett vanligt Python-attribut)."""

    def __init__(self, real_conn, fail_on_call_number: int):
        self._real_conn = real_conn
        self._fail_on_call_number = fail_on_call_number
        self._call_count = 0

    def execute(self, sql, *args, **kwargs):
        self._call_count += 1
        if self._call_count == self._fail_on_call_number:
            raise sqlite3.OperationalError(
                "simulated failure between state-update and event-insert"
            )
        return self._real_conn.execute(sql, *args, **kwargs)

    def commit(self):
        return self._real_conn.commit()

    def rollback(self):
        return self._real_conn.rollback()


def test_transition_candidate_with_event_rolls_back_atomically_on_failure(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    creation_event = _make_event(candidate, "CANDIDATE_CREATED")
    repo.create_candidate_with_event(candidate, creation_event)

    real_conn = repo._conn
    # anrop 1 = UPDATE candidates, anrop 2 = event-INSERT - fel injiceras exakt där
    repo._conn = _FailingConnection(real_conn, fail_on_call_number=2)
    transition_event = _make_event(candidate, "CANDIDATE_TO_UNDER_ANALYSIS")

    with pytest.raises(sqlite3.OperationalError):
        repo.transition_candidate_with_event(
            "cand-1", "UNDER_AI_ANALYSIS", datetime.now(UTC), transition_event
        )

    repo._conn = real_conn
    reloaded = repo.get_candidate("cand-1")
    assert reloaded.status == "CANDIDATE"  # oförändrat - rollback fungerade
    event_row = repo._conn.execute(
        "SELECT 1 FROM events WHERE event_id = ?", (transition_event.event_id,)
    ).fetchone()
    assert event_row is None  # eventet skrevs aldrig heller
```

Denna test läggs till sist i samma testfil som Task 10 skapade (importerna `pytest`, `sqlite3` och hjälpfunktionerna `_make_candidate`/`_make_event` finns redan där).

- [ ] **Step 2: Run test to verify it already passes**

Run: `pytest tests/crypto_trading/storage/test_repository_candidate.py::test_transition_candidate_with_event_rolls_back_atomically_on_failure -v`
Expected: PASS direkt — implementationen från Task 10 (try/except/rollback i `transition_candidate_with_event`) uppfyller redan kravet. Detta steg **bekräftar** atomiciteten med ett explicit test, ingen ny kod behövs (avsiktligt inte ett rött-grönt TDD-steg, se rubriken).

*(Om testet oväntat FAILAR: det betyder `transition_candidate_with_event`s `try/except: rollback(); raise`-mönster från Task 10 inte fångar rätt — lägg då till samma mönster som redan finns i `create_candidate_with_event`.)*

- [ ] **Step 3: Commit**

```bash
git add tests/crypto_trading/storage/test_repository_candidate.py
git commit -m "crypto_trading Phase 0 steg 11: explicit atomicitetstest för transition_candidate_with_event"
```

---

## Task 12: `state_machine.py` — ALLOWED_TRANSITIONS + can_transition

**Files:**
- Create: `crypto_trading/state_machine.py`
- Test: `tests/crypto_trading/test_state_machine.py`

**Interfaces:**
- Produces: `ALLOWED_TRANSITIONS: dict[str, frozenset[str]]`, `can_transition(current_status: str, target_status: str) -> tuple[bool, str]` — konsumeras av Task 13 (sweep) och alla framtida faser som ändrar `CandidateStatus`.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/test_state_machine.py
import pytest

from crypto_trading.state_machine import can_transition

_ALL_STATUSES = [
    "CANDIDATE",
    "DATA_INVALID",
    "BUDGET_LIMITED",
    "UNDER_AI_ANALYSIS",
    "ANALYSIS_INTERRUPTED",
    "REJECTED",
    "NO_TRADE",
    "CONFIRMED",
]


@pytest.mark.parametrize(
    "current,target",
    [
        ("CANDIDATE", "DATA_INVALID"),
        ("CANDIDATE", "BUDGET_LIMITED"),
        ("CANDIDATE", "UNDER_AI_ANALYSIS"),
        ("UNDER_AI_ANALYSIS", "ANALYSIS_INTERRUPTED"),
        ("UNDER_AI_ANALYSIS", "REJECTED"),
        ("UNDER_AI_ANALYSIS", "NO_TRADE"),
        ("UNDER_AI_ANALYSIS", "CONFIRMED"),
        ("ANALYSIS_INTERRUPTED", "UNDER_AI_ANALYSIS"),
    ],
)
def test_allowed_transitions(current, target):
    allowed, reason = can_transition(current, target)
    assert allowed is True
    assert reason == "ok"


@pytest.mark.parametrize(
    "current,target",
    [
        ("REJECTED", "CONFIRMED"),
        ("NO_TRADE", "CONFIRMED"),
        ("DATA_INVALID", "CONFIRMED"),
        ("BUDGET_LIMITED", "CONFIRMED"),
        ("CONFIRMED", "UNDER_AI_ANALYSIS"),
        ("CANDIDATE", "CONFIRMED"),
        ("CANDIDATE", "NO_TRADE"),
    ],
)
def test_forbidden_transitions(current, target):
    allowed, reason = can_transition(current, target)
    assert allowed is False
    assert reason  # icke-tom förklaring


@pytest.mark.parametrize("status", _ALL_STATUSES)
def test_terminal_statuses_have_no_outgoing_transitions_except_analysis_interrupted(status):
    if status in ("CANDIDATE", "UNDER_AI_ANALYSIS", "ANALYSIS_INTERRUPTED"):
        return  # dessa har giltiga utgångar, testas ovan
    for target in _ALL_STATUSES:
        allowed, _ = can_transition(status, target)
        assert allowed is False


def test_can_transition_is_defensive_against_unknown_source_status():
    """Detta är EN oberoende, defensiv fail-closed-kontroll i can_transition
    själv - inte ett UNKNOWN_STATE-domänvärde och inte samma mekanism som
    CorruptCandidateStateError (Task 10), som är den faktiska vägen för en
    korrupt lagrad candidate-rad (status, evidence_record eller timestamp).
    can_transition ser aldrig ett sådant fall i praktiken - denna gren är
    bälte-och-hängslen för fallet att funktionen anropas direkt med en
    råsträng som inte kommer från ett giltigt Candidate-objekt."""
    allowed, reason = can_transition("TOTALLY_UNRECOGNIZED", "CONFIRMED")
    assert allowed is False
    assert "unknown source state" in reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/test_state_machine.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/state_machine.py (del 1 av 2 — resten läggs till i Task 13)
from __future__ import annotations

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "CANDIDATE": frozenset({"DATA_INVALID", "BUDGET_LIMITED", "UNDER_AI_ANALYSIS"}),
    "UNDER_AI_ANALYSIS": frozenset({"ANALYSIS_INTERRUPTED", "REJECTED", "NO_TRADE", "CONFIRMED"}),
    "ANALYSIS_INTERRUPTED": frozenset({"UNDER_AI_ANALYSIS"}),
    "DATA_INVALID": frozenset(),
    "BUDGET_LIMITED": frozenset(),
    "REJECTED": frozenset(),
    "NO_TRADE": frozenset(),
    "CONFIRMED": frozenset(),
}


def can_transition(current_status: str, target_status: str) -> tuple[bool, str]:
    """Ren, deterministisk gate-funktion: (bool, reason), aldrig en exception.

    OBS: `current_status` typas medvetet som `str`, inte `CandidateStatus` -
    detta är ett andra, oberoende skyddslager (bälte-och-hängslen), inte en
    väg för att hantera korrupt lagrad data. Om `current_status` inte finns i
    `ALLOWED_TRANSITIONS` nekas övergången fail-closed med en förklaring -
    det skapar ALDRIG något "UNKNOWN_STATE"-domänvärde eller något annat
    domänobjekt. En faktiskt korrupt lagrad candidate-rad (oavsett om felet
    sitter i `status`, `evidence_record` eller en timestamp) hanteras
    uteslutande av `storage.repository.SQLiteRepository.get_candidate()` via
    `CorruptCandidateStateError` + ett `CORRUPT_STATE_DETECTED`-event, INNAN
    ett `Candidate`-objekt någonsin skulle kunna nå denna funktion (se Task
    10). Denna funktion ser alltså i praktiken bara redan validerade
    `CandidateStatus`-värden - grenen nedan är ett defensivt nej, inte en
    förväntad körväg."""
    allowed_targets = ALLOWED_TRANSITIONS.get(current_status)
    if allowed_targets is None:
        return False, f"unknown source state: {current_status!r}"
    if target_status not in allowed_targets:
        return False, f"transition {current_status} -> {target_status} is not allowed"
    return True, "ok"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/test_state_machine.py -v`
Expected: PASS (24 tester: 8 tillåtna övergångar + 7 förbjudna övergångar + 8 terminal-status-fall + 1 defensivt fall)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/state_machine.py tests/crypto_trading/test_state_machine.py
git commit -m "crypto_trading Phase 0 steg 12: state machine — ALLOWED_TRANSITIONS + can_transition"
```

---

## Task 13: `state_machine.py` — ANALYSIS_INTERRUPTED startup-sweep

**Files:**
- Modify: `crypto_trading/state_machine.py`
- Test: `tests/crypto_trading/test_state_machine_sweep.py`

**Interfaces:**
- Consumes: `Repository`-protokollet (`storage.repository`), `Event` (`schemas.event`). Förlitar sig på att `find_candidates_by_status` (Task 10) redan hoppar över `CorruptCandidateStateError`-rader istället för att avbryta — sweepen behöver ingen egen felhantering för det.
- Produces: `sweep_interrupted_analyses(repo: Repository, swept_at: datetime, run_id: str) -> list[str]`. En korrupt candidate bland de svepta rapporteras via det `CORRUPT_STATE_DETECTED`-event `get_candidate` redan skrev, men syns inte i returvärdet — den blockerar aldrig behandlingen av övriga giltiga candidates.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/test_state_machine_sweep.py
from datetime import UTC, datetime

from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.state_machine import sweep_interrupted_analyses
from crypto_trading.storage.repository import SQLiteRepository


def _make_evidence() -> CandidateEvidenceRecord:
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=datetime.now(UTC),
        price_volatility_evidence=PriceVolatilityEvidence(
            triggered=True, metric="pct_change_1h", value=3.2, baseline=0.5, threshold=2.0
        ),
        momentum_breakout_evidence=MomentumBreakoutEvidence(
            triggered=False, metric="rsi", value=55.0, baseline=50.0, threshold=70.0
        ),
        volume_evidence=VolumeEvidence(
            triggered=True, metric="volume_zscore", value=3.1, baseline=1.0, threshold=2.5
        ),
        funding_oi_evidence=FundingOpenInterestEvidence(
            triggered=False, metric="funding_rate", value=0.01, baseline=0.01, threshold=0.05
        ),
        candidate_score=0.71,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _seed_candidate(repo: SQLiteRepository, candidate_id: str, status: str) -> None:
    now = datetime.now(UTC)
    candidate = Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{candidate_id}",
        instrument="BTCUSDT",
        discovery_run_id="run-old",
        evidence_hash="hash-1",
        status="CANDIDATE",
        evidence_record=_make_evidence(),
        created_at=now,
        updated_at=now,
    )
    creation_event = Event(
        event_id=f"CANDIDATE_CREATED:{candidate_id}",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        occurred_at=now,
        run_id="run-old",
        schema_version=1,
        payload={},
    )
    repo.create_candidate_with_event(candidate, creation_event)
    if status != "CANDIDATE":
        transition_event = Event(
            event_id=f"MOVE_TO_{status}:{candidate_id}",
            event_type=f"MOVE_TO_{status}",
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            occurred_at=now,
            run_id="run-old",
            schema_version=1,
            payload={},
        )
        repo.transition_candidate_with_event(candidate_id, status, now, transition_event)


def test_sweep_moves_under_analysis_candidates_to_interrupted(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    _seed_candidate(repo, "stuck-1", "UNDER_AI_ANALYSIS")
    _seed_candidate(repo, "stuck-2", "UNDER_AI_ANALYSIS")
    _seed_candidate(repo, "not-stuck", "CANDIDATE")

    swept_at = datetime.now(UTC)
    interrupted_ids = sweep_interrupted_analyses(repo, swept_at, run_id="startup-run-1")

    assert set(interrupted_ids) == {"stuck-1", "stuck-2"}
    assert repo.get_candidate("stuck-1").status == "ANALYSIS_INTERRUPTED"
    assert repo.get_candidate("stuck-2").status == "ANALYSIS_INTERRUPTED"
    assert repo.get_candidate("not-stuck").status == "CANDIDATE"


def test_sweep_writes_one_event_per_interrupted_candidate(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    _seed_candidate(repo, "stuck-1", "UNDER_AI_ANALYSIS")

    sweep_interrupted_analyses(repo, datetime.now(UTC), run_id="startup-run-1")

    row = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE event_type = 'ANALYSIS_INTERRUPTED_DETECTED'"
    ).fetchone()
    assert row["n"] == 1


def test_sweep_never_transitions_analysis_interrupted_back_to_under_analysis(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    _seed_candidate(repo, "already-interrupted", "ANALYSIS_INTERRUPTED")

    interrupted_ids = sweep_interrupted_analyses(repo, datetime.now(UTC), run_id="startup-run-2")

    assert interrupted_ids == []
    assert repo.get_candidate("already-interrupted").status == "ANALYSIS_INTERRUPTED"


def test_sweep_is_idempotent_on_repeated_calls(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    _seed_candidate(repo, "stuck-1", "UNDER_AI_ANALYSIS")

    first = sweep_interrupted_analyses(repo, datetime.now(UTC), run_id="run-a")
    second = sweep_interrupted_analyses(repo, datetime.now(UTC), run_id="run-b")

    assert first == ["stuck-1"]
    assert second == []  # redan ANALYSIS_INTERRUPTED efter första sweepen, inget kvar att svepa


def test_sweep_continues_past_corrupt_candidate_and_still_interrupts_valid_ones(tmp_path):
    """En korrupt candidate bland flera UNDER_AI_ANALYSIS-rader ska aldrig
    blockera sweepen från att behandla övriga, giltiga candidates."""
    repo = SQLiteRepository(tmp_path / "test.db")
    _seed_candidate(repo, "stuck-valid", "UNDER_AI_ANALYSIS")
    _seed_candidate(repo, "stuck-corrupt", "UNDER_AI_ANALYSIS")

    # korrumpera evidence_record, INTE status - så att raden fortfarande
    # matchar UNDER_AI_ANALYSIS-frågan men inte kan deserialiseras fullt ut.
    repo._conn.execute(
        "UPDATE candidates SET evidence_record = 'not valid json' WHERE candidate_id = 'stuck-corrupt'"
    )
    repo._conn.commit()

    interrupted_ids = sweep_interrupted_analyses(repo, datetime.now(UTC), run_id="startup-run-3")

    assert interrupted_ids == ["stuck-valid"]  # korrupt candidate blockerade inte den giltiga
    assert repo.get_candidate("stuck-valid").status == "ANALYSIS_INTERRUPTED"

    corrupt_event = repo._conn.execute(
        "SELECT 1 FROM events WHERE event_type = 'CORRUPT_STATE_DETECTED' "
        "AND aggregate_id = 'stuck-corrupt'"
    ).fetchone()
    assert corrupt_event is not None  # den korrupta candidate:n auditerades ändå
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/test_state_machine_sweep.py -v`
Expected: FAIL with `ImportError: cannot import name 'sweep_interrupted_analyses'`

- [ ] **Step 3: Write minimal implementation**

Lägg till i `crypto_trading/state_machine.py` (efter `can_transition`):

```python
# crypto_trading/state_machine.py (del 2 av 2 — läggs till efter can_transition från Task 12)
from datetime import datetime
from typing import TYPE_CHECKING

from crypto_trading.schemas.event import Event

if TYPE_CHECKING:
    from crypto_trading.storage.repository import Repository


def sweep_interrupted_analyses(
    repo: "Repository", swept_at: datetime, run_id: str
) -> list[str]:
    """Vid start av discovery-processen: varje candidate som redan ligger i
    UNDER_AI_ANALYSIS är per definition föräldralös (denna process skrev den
    inte - den startar precis nu). Sveper dem till ANALYSIS_INTERRUPTED,
    enkelriktat - återupplivar ALDRIG automatiskt (SPEC §8.5, Phase 0-design)."""
    interrupted_ids: list[str] = []
    for candidate in repo.find_candidates_by_status("UNDER_AI_ANALYSIS"):
        allowed, reason = can_transition(candidate.status, "ANALYSIS_INTERRUPTED")
        if not allowed:
            raise AssertionError(f"sweep produced an illegal transition: {reason}")
        event = Event(
            event_id=f"ANALYSIS_INTERRUPTED_DETECTED:{candidate.candidate_id}:{run_id}",
            event_type="ANALYSIS_INTERRUPTED_DETECTED",
            aggregate_type="candidate",
            aggregate_id=candidate.candidate_id,
            occurred_at=swept_at,
            run_id=run_id,
            schema_version=1,
            payload={"previous_status": candidate.status},
        )
        repo.transition_candidate_with_event(
            candidate.candidate_id, "ANALYSIS_INTERRUPTED", swept_at, event
        )
        interrupted_ids.append(candidate.candidate_id)
    return interrupted_ids
```

Notera: importen av `Event` flyttas till toppen av filen (slå ihop med Task 12:s importsektion — `from __future__ import annotations` följt av `from datetime import datetime`, `from typing import TYPE_CHECKING`, `from crypto_trading.schemas.event import Event`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/test_state_machine_sweep.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/state_machine.py tests/crypto_trading/test_state_machine_sweep.py
git commit -m "crypto_trading Phase 0 steg 13: ANALYSIS_INTERRUPTED startup-sweep, enkelriktad"
```

---

## Task 14: Samtidighetstest — WAL + busy_timeout under deterministisk contention

**Files:**
- Create: `tests/crypto_trading/storage/test_repository_concurrency.py`

**Interfaces:**
- Consumes: `SQLiteRepository` (Task 10).

*Testerna tvingar fram verklig lock-contention (en anslutning håller
`BEGIN IMMEDIATE` öppen ett kontrollerat antal sekunder) istället för att
förlita sig på att trådschemaläggning råkar skapa den — deterministiskt,
inte beroende av tur.*

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/storage/test_repository_concurrency.py
import sqlite3
import threading
import time
from datetime import UTC, datetime

import pytest

from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.storage.repository import SQLiteRepository


def _make_evidence() -> CandidateEvidenceRecord:
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=datetime.now(UTC),
        price_volatility_evidence=PriceVolatilityEvidence(
            triggered=True, metric="pct_change_1h", value=3.2, baseline=0.5, threshold=2.0
        ),
        momentum_breakout_evidence=MomentumBreakoutEvidence(
            triggered=False, metric="rsi", value=55.0, baseline=50.0, threshold=70.0
        ),
        volume_evidence=VolumeEvidence(
            triggered=True, metric="volume_zscore", value=3.1, baseline=1.0, threshold=2.5
        ),
        funding_oi_evidence=FundingOpenInterestEvidence(
            triggered=False, metric="funding_rate", value=0.01, baseline=0.01, threshold=0.05
        ),
        candidate_score=0.71,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _make_candidate(candidate_id: str) -> Candidate:
    now = datetime.now(UTC)
    return Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{candidate_id}",
        instrument="BTCUSDT",
        discovery_run_id="run-1",
        evidence_hash="hash-1",
        status="CANDIDATE",
        evidence_record=_make_evidence(),
        created_at=now,
        updated_at=now,
    )


def _make_event(candidate_id: str) -> Event:
    return Event(
        event_id=f"CANDIDATE_CREATED:{candidate_id}",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        occurred_at=datetime.now(UTC),
        run_id="run-1",
        schema_version=1,
        payload={},
    )


def test_busy_timeout_lets_writer_wait_for_lock_and_succeed(tmp_path):
    """Anslutning A tvingas hålla ett skriv-lås öppet i en kontrollerad tid.
    Anslutning B:s skrivning under tiden ska VÄNTA (inte misslyckas direkt)
    och sedan lyckas när A släpper låset - bevisar att busy_timeout faktiskt
    används, deterministiskt, utan att förlita sig på trådschemaläggning."""
    db_path = tmp_path / "concurrent_wait.db"
    repo_b = SQLiteRepository(db_path, busy_timeout_ms=2000)

    hold_seconds = 0.4
    lock_acquired = threading.Event()

    def hold_write_lock():
        # repo_a skapas HÄR, inne i tråden - en sqlite3-anslutning är
        # trådbunden (check_same_thread=True som default) och kan inte
        # skapas i huvudtråden men användas i en annan tråd (upptäckt vid
        # exekvering: "SQLite objects created in a thread can only be used
        # in that same thread").
        repo_a = SQLiteRepository(db_path, busy_timeout_ms=2000)
        repo_a._conn.execute("BEGIN IMMEDIATE")
        lock_acquired.set()
        time.sleep(hold_seconds)
        repo_a._conn.commit()

    holder_thread = threading.Thread(target=hold_write_lock)
    holder_thread.start()
    assert lock_acquired.wait(timeout=2), "connection A never acquired the write lock"

    started_at = time.monotonic()
    created = repo_b.create_candidate_with_event(
        _make_candidate("cand-waits"), _make_event("cand-waits")
    )
    elapsed = time.monotonic() - started_at
    holder_thread.join(timeout=2)

    assert created is True
    # B måste faktiskt ha VÄNTAT på A:s lås - inte misslyckats direkt, inte
    # lyckats innan A ens tog låset.
    assert elapsed >= hold_seconds * 0.5, (
        f"B:s skrivning verkar inte ha väntat på A:s lås (elapsed={elapsed:.3f}s)"
    )
    assert elapsed < 2.0, f"B väntade orimligt länge (elapsed={elapsed:.3f}s)"

    verify_repo = SQLiteRepository(db_path, busy_timeout_ms=2000)
    assert verify_repo.get_candidate("cand-waits") is not None
    count = verify_repo._conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1  # ingen dubblett, ingen korruption


def test_busy_timeout_is_respected_write_fails_after_timeout_elapses(tmp_path):
    """Anslutning A håller låset LÄNGRE än anslutning B:s egen busy_timeout.
    B:s skrivning ska misslyckas efter ungefär B:s busy_timeout - varken
    direkt (vilket skulle bevisa att busy_timeout ignoreras) eller efter hela
    A:s hålltid (vilket skulle bevisa att B väntade på fel/inget villkor)."""
    db_path = tmp_path / "concurrent_timeout.db"
    short_timeout_ms = 200
    repo_b = SQLiteRepository(db_path, busy_timeout_ms=short_timeout_ms)

    hold_seconds = 1.0  # betydligt längre än B:s busy_timeout (0.2s)
    lock_acquired = threading.Event()

    def hold_write_lock():
        # repo_a skapas i tråden - se kommentar i föregående test.
        repo_a = SQLiteRepository(db_path, busy_timeout_ms=2000)
        repo_a._conn.execute("BEGIN IMMEDIATE")
        lock_acquired.set()
        time.sleep(hold_seconds)
        repo_a._conn.commit()

    holder_thread = threading.Thread(target=hold_write_lock)
    holder_thread.start()
    assert lock_acquired.wait(timeout=2), "connection A never acquired the write lock"

    started_at = time.monotonic()
    with pytest.raises(sqlite3.OperationalError):
        repo_b.create_candidate_with_event(
            _make_candidate("cand-times-out"), _make_event("cand-times-out")
        )
    elapsed = time.monotonic() - started_at
    holder_thread.join(timeout=2)

    assert elapsed >= (short_timeout_ms / 1000) * 0.5, (
        f"B misslyckades för snabbt ({elapsed:.3f}s) - busy_timeout verkar ignorerat"
    )
    assert elapsed < hold_seconds, (
        f"B väntade lika länge som A höll låset ({elapsed:.3f}s) - busy_timeout verkar inte styra väntetiden"
    )

    # Ingen korruption: B:s misslyckade skrivning lämnade ingen rad (rollback
    # skedde i create_candidate_with_event); A skrev aldrig något själv.
    verify_repo = SQLiteRepository(db_path, busy_timeout_ms=2000)
    count = verify_repo._conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/storage/test_repository_concurrency.py -v`
Expected: FAIL — inte pga saknad kod (allt finns redan från Task 10), detta är det första samtidighetstestet. Kör det för att se om det passerar direkt eller avslöjar ett låsningsproblem.

- [ ] **Step 3: Åtgärda om det behövs**

Om `test_busy_timeout_lets_writer_wait_for_lock_and_succeed` visar ett `database is locked`-fel trots `busy_timeout`: verifiera att `PRAGMA journal_mode=WAL` faktiskt slog igenom (SQLite kan tysta falla tillbaka till rollback-journal på vissa filsystem) genom att logga `conn.execute("PRAGMA journal_mode").fetchone()`. Om WAL inte aktiveras, undersök filsystemets stöd för memory-mapped I/O i testmiljön — en känd SQLite-begränsning, inte ett kodfel i `crypto_trading/`. Dokumentera fyndet istället för att gissa en fix.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/storage/test_repository_concurrency.py -v`
Expected: PASS (2 tester)

- [ ] **Step 5: Commit**

```bash
git add tests/crypto_trading/storage/test_repository_concurrency.py
git commit -m "crypto_trading Phase 0 steg 14: deterministiskt samtidighetstest för WAL + busy_timeout"
```

---

## Task 15: Import-gräns mot `intelligence/`

**Files:**
- Create: `tests/crypto_trading/test_no_intelligence_coupling.py`

**Interfaces:**
- Consumes: hela `crypto_trading`-paketet (importgranskning).

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/test_no_intelligence_coupling.py
import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _imported_top_level_modules(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_crypto_trading_never_imports_intelligence():
    crypto_trading_files = (_REPO_ROOT / "crypto_trading").rglob("*.py")
    offenders = []
    for py_file in crypto_trading_files:
        if "intelligence" in _imported_top_level_modules(py_file):
            offenders.append(str(py_file))
    assert offenders == [], f"crypto_trading files importing intelligence: {offenders}"


def test_intelligence_never_imports_crypto_trading():
    intelligence_files = (_REPO_ROOT / "intelligence").rglob("*.py")
    offenders = []
    for py_file in intelligence_files:
        if "crypto_trading" in _imported_top_level_modules(py_file):
            offenders.append(str(py_file))
    assert offenders == [], f"intelligence files importing crypto_trading: {offenders}"


def test_crypto_trading_has_no_broker_account_or_order_code():
    forbidden_terms = ("account_balance", "place_order", "broker_credential", "api_secret")
    crypto_trading_files = (_REPO_ROOT / "crypto_trading").rglob("*.py")
    offenders = []
    for py_file in crypto_trading_files:
        content = py_file.read_text(encoding="utf-8").lower()
        for term in forbidden_terms:
            if term in content:
                offenders.append((str(py_file), term))
    assert offenders == [], f"forbidden broker/order terms found: {offenders}"
```

- [ ] **Step 2: Run test to verify it already passes**

Run: `pytest tests/crypto_trading/test_no_intelligence_coupling.py -v`
Expected: PASS direkt (ingen kod skriven hittills bryter mot detta, avsiktligt inte ett rött-grönt TDD-steg) — om det oväntat FAILAR avslöjar det en redan existerande kopplingsbugg i tidigare tasks, åtgärda innan du fortsätter.

- [ ] **Step 3: Commit**

```bash
git add tests/crypto_trading/test_no_intelligence_coupling.py
git commit -m "crypto_trading Phase 0 steg 15: importgräns mot intelligence/ + förbjudna broker-termer"
```

---

## Task 16: `logging.py` — run_id och secret-redaction

**Files:**
- Create: `crypto_trading/logging.py`
- Test: `tests/crypto_trading/test_logging.py`

**Interfaces:**
- Produces: `new_run_id() -> str`, `redact(data: dict) -> dict`, `log_event(run_id: str, **fields) -> None`. Samma kontrakt som `intelligence/logging.py` — egen fil, ingen import från `intelligence/` (Global Constraints).

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/test_logging.py
import json
import logging

from crypto_trading.logging import log_event, new_run_id, redact


def test_new_run_id_returns_unique_uuid_strings():
    a = new_run_id()
    b = new_run_id()
    assert a != b
    assert len(a) == 36  # uuid4 sträng-längd


def test_redact_masks_keys_matching_secret_markers():
    data = {"telegram_bot_token": "abc123", "instrument": "BTCUSDT"}
    out = redact(data)
    assert out["telegram_bot_token"] == "***REDACTED***"
    assert out["instrument"] == "BTCUSDT"


def test_redact_masks_embedded_token_in_string_value():
    data = {"error_message": "request failed: token=abc123&other=1"}
    out = redact(data)
    assert "abc123" not in out["error_message"]
    assert "***REDACTED***" in out["error_message"]


def test_log_event_never_emits_raw_secret(caplog):
    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        log_event("run-1", telegram_bot_token="super-secret-value", instrument="BTCUSDT")
    assert "super-secret-value" not in caplog.text
    assert "run-1" in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/test_logging.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/logging.py
from __future__ import annotations

import json
import logging
import re
import uuid

_SECRET_KEY_MARKERS = ("api_key", "apikey", "token", "secret", "credential")

_SECRET_VALUE_PATTERN = re.compile(r"(?i)(?:api_key|apikey|token)=[^&\s]+")

_logger = logging.getLogger("crypto_trading")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


def new_run_id() -> str:
    return str(uuid.uuid4())


def redact(data: dict) -> dict:
    out = {}
    for key, value in data.items():
        if any(marker in key.lower() for marker in _SECRET_KEY_MARKERS):
            out[key] = "***REDACTED***"
        elif isinstance(value, str):
            out[key] = _SECRET_VALUE_PATTERN.sub("***REDACTED***", value)
        else:
            out[key] = value
    return out


def log_event(run_id: str, **fields) -> None:
    payload = redact({"run_id": run_id, **fields})
    _logger.info(json.dumps(payload, default=str))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/test_logging.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/logging.py tests/crypto_trading/test_logging.py
git commit -m "crypto_trading Phase 0 steg 16: logging.py — run_id och secret-redaction"
```

---

## Task 17: Slutverifiering

**Files:** inga nya — verifierar hela Phase 0.

- [ ] **Step 1: Full testsvit för crypto_trading**

Run: `pytest tests/crypto_trading/ -v`
Expected: alla tester gröna (samtliga Task 1–16).

- [ ] **Step 2: Ruff check + format**

Run: `ruff check crypto_trading/ tests/crypto_trading/`
Expected: inga fel.

Run: `ruff format --check crypto_trading/ tests/crypto_trading/`
Expected: inga diff.

- [ ] **Step 3: Verifiera att intelligence/ fortfarande är orört**

Run: `git diff HEAD -- intelligence/`
Expected: tom output (jämfört mot senaste commit innan Phase 0-arbetet startade).

- [ ] **Step 4: Full repo-testsvit (bekräfta att inget i intelligence/ gick sönder)**

Run: `pytest -v`
Expected: alla tester (både `tests/intelligence/` och `tests/crypto_trading/`) gröna.

- [ ] **Step 5: Uppdatera PLAN_CRYPTO_PHASE0.md**

Kryssa i samtliga `- [ ]` i denna fil till `- [x]` (samma mönster som Fas 1:s avslut) och lägg till en statusbanner högst upp: `> **Status: PHASE 0 AVSLUTAD (datum).**`

---

## Self-review (utfört innan planen sparas)

**Spec-täckning:** projektstruktur (Task 1), config-system (Task 8), Pydantic-scheman (Task 2–7), Repository-protokoll (Task 10–11), SQLite-schema (Task 9), event/audit-logg (Task 3, 9, 10), state machine + transitions (Task 12), ANALYSIS_INTERRUPTED (Task 13), korrupt/okänt lagrat state via `CorruptCandidateStateError` + `CORRUPT_STATE_DETECTED` för hela deserialiseringskedjan (Task 10 — INTE ett `CandidateStatus`-värde, se §4 nedan), fail-closed-batch-hantering av korrupta rader (Task 10 `find_candidates_by_status`, Task 13 sweep), idempotens (Task 4, 10), timestamps/ordering (Task 9), felhantering (Task 8, 10), teststrategi (alla tasks, TDD, samt Task 14 deterministisk samtidighet), mock/in-memory repository (medvetet uteslutet — beslutat i design), konfigurationsvalidering (Task 8), secrets-policy (Task 8 — bara `db_path`-override, inga oanvända secret-fält), logging/redaction (Task 16), migration/versionering (Task 9, `schema_meta`), Decimal-serialiseringskonvention SQLite+JSON (Task 9), fullständig disposition av samtliga SPEC §16-tabeller (planens header). Ingen kvarstående lucka.

**Placeholder-scan:** inga TBD/TODO, inga "add appropriate error handling"-fraser — varje steg har komplett, körbar kod.

**Typkonsekvens:** `Candidate.status: CandidateStatus` (8 värden, inget `UNKNOWN_STATE`-medlem — Task 2, testat explicit rad 114). `can_transition(current_status: str, ...)` (medvetet `str` inte `CandidateStatus` — ett andra, oberoende defensivt skyddslager, inte en väg för korrupt data, se dokumentationen i Task 12:s implementation). `Repository.create_candidate_with_event`/`transition_candidate_with_event`/`find_candidates_by_status`-signaturerna matchar exakt mellan Protocol (Task 10) och `SQLiteRepository`-implementationen samt alla anrop i Task 11/13/14. `Event`-konstruktionen är identisk i alla tasks som skapar ett event. `Candidate` (Task 6) har inget syskonobjekt för korrupt state längre — bara `CorruptCandidateStateError` (Task 10) representerar det fallet, ingen dubblerad state-modell. `RepositoryWriteError` är borttagen i sin helhet (var död kod, refererades felaktigt i Task 10:s "Interfaces"-rad) — verifierat att inga referenser kvarstår.

**§4 — Uppdaterad efter denna gransknings-runda (R1/R2):** ett korrupt lagrat candidate-fält — `status`, `evidence_record` (JSON) eller en timestamp — representeras **uteslutande** av `CorruptCandidateStateError` (kastad av `SQLiteRepository.get_candidate()`, med `corrupted_field` som anger exakt var i kedjan felet upptäcktes) + ett `CORRUPT_STATE_DETECTED`-audit-event, för hela deserialiseringskedjan, inte bara statuskolumnen (Task 10). Fångar medvetet bara `ValidationError`/`ValueError` — aldrig bredare undantag som skulle dölja ett verkligt programmeringsfel (dokumenterat i metodens docstring). `find_candidates_by_status()` och därmed `sweep_interrupted_analyses()` (Task 10, Task 13) hoppar över en `CorruptCandidateStateError`-rad och fortsätter med övriga giltiga candidates — en trasig rad kan aldrig blockera resten av batchen, testat explicit med minst en korrupt och en giltig candidate på båda nivåerna. `CandidateStatus` (Task 2) har exakt åtta giltiga värden. `can_transition()` (Task 12) har en separat, dokumenterat redundant fail-closed-gren för en råsträng den inte känner igen — bälte-och-hängslen, inte en andra representation av samma koncept. Ingen `UnknownStateRecord`- eller annan alternativ state-modell finns någonstans i planen.

---

**Plan complete and saved to `PLAN_CRYPTO_PHASE0.md`** (repo-rot, inte `docs/superpowers/plans/` — matchar projektets etablerade konvention med `SPEC_CRYPTO.md`/`PLAN_CRYPTO.md` i repo-roten).
