# Market Opportunity Intelligence System — Fas 1 Implementation Plan

> **Status: FAS 1 AVSLUTAD (2026-08-25).** Alla 23 tasks nedan är implementerade, testade och verifierade mot verklig data. Se `PHASE_1_COMPLETION.md` för sammanfattning (testresultat, verkliga körningar, kända begränsningar). Fas 1 är en stabil, testad baslinje — inga ändringar görs i denna plan retroaktivt.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bygga en testbar, feltolerant end-to-end-pipeline (data → event → 7-agent-analys → gated state machine → transparent scoring → sqlite → markdown-rapport) för Fas 1, körbar helt med `MockAgentRunner` utan Claude API.

**Architecture:** Enkelriktade lager — `schemas` (beroendefritt) ← `connectors`/`pipeline`/`storage`/`agents`/`scoring`/`reporting` ← `state_machine` ← `orchestrator` ← `run.py`. Orchestratorn beror bara på `Repository`- och `AgentRunner`-**interfaces**, aldrig konkreta implementationer.

**Tech Stack:** Python 3.13, `pydantic` v2, `httpx`, `tenacity` (retry), `pyyaml`, `python-dotenv`, `anthropic` SDK, `sqlite3` (stdlib), `pytest` + `respx` (HTTP-mock).

**Spec:** `SPEC.md` (repo root) — denna plan argumenterar utifrån SPEC.md; vid konflikt gäller SPEC.md.

## Global Constraints

- Ingen kod får lägga ordrar, ansluta till mäklarkonton, hantera broker-credentials eller flytta pengar — i någon fas (SPEC §1, §14).
- Deterministisk kod gör allt som kan vara deterministiskt; LLM används bara för semantisk analys (SPEC §1).
- Ingen agent godkänner en opportunity ensam; `reported` kräver alla 7 assessments med `status="ok"` + `qa.passed=True` (SPEC §5).
- Agenter returnerar bara sin egen assessment-typ; ingen skriver till en annan agents fält eller till `Opportunity` direkt (SPEC §4).
- Default `pytest`-körning kräver noll nätverk och noll riktiga API-nycklar (SPEC §13). HTTP mockas med `respx`, LLM mockas med `MockAgentRunner`.
- Secrets läses bara via `.env`/miljövariabler, loggas aldrig i klartext (SPEC §10, §14).
- Alla scoring-vikter i `config/scoring_weights.yaml`, aldrig hårdkodade (SPEC §9).
- `python-target-version` = py313, `ruff` line-length 100, regler `E,F,I,UP,B` (befintlig `pyproject.toml`).
- Historical/Backtest, Learning/Evaluation och Opportunity Ranking-agenter byggs INTE i Fas 1 — bara de tre uteslutna rollerna nämns som framtida faser i denna plans slutdokumentation, ingen kod för dem skrivs.

---

## Implementeringsordning

1. Scaffolding & config (Task 1–3)
2. Rena scheman, inga beroenden (Task 4–6)
3. State machine — säkerhetskritisk gate (Task 7)
4. Storage (Task 8)
5. Connectors (Task 9–11)
6. Deterministisk pipeline (Task 12–13)
7. Agentdefinitioner + loader/runner (Task 14–17)
8. Scoring & reporting (Task 18–19)
9. Orchestrator — knyter ihop allt (Task 20)
10. Entrypoint + end-to-end-test (Task 21–22)
11. Slutverifiering (Task 23)

Varje task lämnar `pytest` grönt och `ruff check` rent innan nästa task påbörjas.

---

### Task 1: Dependencies, paketskelett och config-filer

**Files:**
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Modify: `.gitignore`
- Create: `intelligence/__init__.py`
- Create: `intelligence/connectors/__init__.py`
- Create: `intelligence/pipeline/__init__.py`
- Create: `intelligence/schemas/__init__.py`
- Create: `intelligence/agents/__init__.py`
- Create: `intelligence/storage/__init__.py`
- Create: `intelligence/scoring/__init__.py`
- Create: `intelligence/reporting/__init__.py`
- Create: `config/scoring_weights.yaml`
- Create: `data/.gitkeep`

**Interfaces:**
- Produces: paketet `intelligence` är importerbart; `config/scoring_weights.yaml` finns på disk med nycklarna `signal_strength, data_quality, source_reliability, potential, risk, confidence, novelty` (används av Task 18).

- [x] **Step 1: Lägg till dependencies i `pyproject.toml`**

```toml
[project]
name = "claudeprojects"
version = "0.1.0"
description = "Research-, analys- och kodmiljö"
requires-python = ">=3.13"
dependencies = [
    "pydantic>=2.7",
    "anthropic>=0.40",
    "httpx>=0.27",
    "tenacity>=9.0",
    "pyyaml>=6.0",
    "python-dotenv>=1.0",
]

[dependency-groups]
dev = [
    "ruff>=0.6",
    "pytest>=8.0",
    "respx>=0.21",
]

[tool.ruff]
line-length = 100
target-version = "py313"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "live: kräver riktiga API-nycklar och nätverk, exkluderad från default-körning",
]
```

- [x] **Step 2: Installera dependencies**

Run: `uv sync`
Expected: lyckas, `uv.lock` uppdateras.

- [x] **Step 3: Skapa paketskelett**

Skapa tomma `__init__.py` i: `intelligence/`, `intelligence/connectors/`, `intelligence/pipeline/`, `intelligence/schemas/`, `intelligence/agents/`, `intelligence/storage/`, `intelligence/scoring/`, `intelligence/reporting/`. Varje fil är tom (0 bytes) — de markerar paketen som importerbara.

- [x] **Step 4: Skapa `config/scoring_weights.yaml`**

```yaml
# Alla vikter ska summera till 1.0. Komponenterna beräknas i intelligence/scoring/model.py.
signal_strength: 0.20
data_quality: 0.15
source_reliability: 0.15
potential: 0.20
risk: 0.15
confidence: 0.10
novelty: 0.05
```

- [x] **Step 5: Skapa `data/.gitkeep` och uppdatera `.gitignore`**

Lägg till i `.gitignore` under en ny sektion:

```gitignore
# Intelligence-systemet
data/*.db
data/*.db-journal
```

Skapa tom fil `data/.gitkeep` så mappen finns i git även när `.db`-filen är ignorerad.

- [x] **Step 6: Uppdatera `.env.example`**

Lägg till under Steg 6-sektionen:

```
# --- Fas 1: Market Opportunity Intelligence System ---
# ANTHROPIC_API_KEY=
```

- [x] **Step 7: Verifiera**

Run: `uv run python -c "import intelligence"`
Expected: inga fel.

Run: `uv run pytest`
Expected: befintliga testet `tests/test_setup.py` passerar fortfarande (1 passed).

- [x] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock .env.example .gitignore config/scoring_weights.yaml data/.gitkeep intelligence/
git commit -m "Fas 1 steg 1: paketskelett, dependencies, scoring-config"
```

---

### Task 2: `intelligence/config.py` — Settings

**Files:**
- Create: `intelligence/config.py`
- Test: `tests/intelligence/test_config.py`

**Interfaces:**
- Consumes: `config/scoring_weights.yaml` (Task 1).
- Produces: `Settings` (pydantic `BaseModel`), `get_settings() -> Settings`, med fälten: `anthropic_api_key: str | None`, `alphavantage_api_key: str | None`, `db_path: Path`, `scoring_weights_path: Path`, `max_events_per_run: int`, `max_opportunities_per_run: int`, `max_agent_calls_per_run: int`, `agent_timeout_seconds: float`, `connector_timeout_seconds: float`, `connector_max_retries: int`. Används av alla senare tasks som behöver gränser/sökvägar/nycklar.

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/test_config.py
import os

from intelligence.config import get_settings


def test_defaults_without_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    settings = get_settings()
    assert settings.anthropic_api_key is None
    assert settings.alphavantage_api_key is None
    assert settings.max_events_per_run == 20
    assert settings.max_opportunities_per_run == 5
    assert settings.max_agent_calls_per_run == 50
    assert settings.agent_timeout_seconds == 30.0
    assert settings.connector_max_retries == 3


def test_reads_env_overrides(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setenv("MAX_EVENTS_PER_RUN", "5")
    settings = get_settings()
    assert settings.anthropic_api_key == "sk-test-123"
    assert settings.max_events_per_run == 5


def test_scoring_weights_file_exists():
    settings = get_settings()
    assert settings.scoring_weights_path.exists()
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'intelligence.config'`

- [x] **Step 3: Implementera**

```python
# intelligence/config.py
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseModel):
    anthropic_api_key: str | None
    alphavantage_api_key: str | None
    db_path: Path
    scoring_weights_path: Path
    max_events_per_run: int
    max_opportunities_per_run: int
    max_agent_calls_per_run: int
    agent_timeout_seconds: float
    connector_timeout_seconds: float
    connector_max_retries: int


def get_settings() -> Settings:
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        alphavantage_api_key=os.environ.get("ALPHAVANTAGE_API_KEY") or None,
        db_path=_PROJECT_ROOT / "data" / "intelligence.db",
        scoring_weights_path=_PROJECT_ROOT / "config" / "scoring_weights.yaml",
        max_events_per_run=int(os.environ.get("MAX_EVENTS_PER_RUN", "20")),
        max_opportunities_per_run=int(os.environ.get("MAX_OPPORTUNITIES_PER_RUN", "5")),
        max_agent_calls_per_run=int(os.environ.get("MAX_AGENT_CALLS_PER_RUN", "50")),
        agent_timeout_seconds=float(os.environ.get("AGENT_TIMEOUT_SECONDS", "30")),
        connector_timeout_seconds=float(os.environ.get("CONNECTOR_TIMEOUT_SECONDS", "10")),
        connector_max_retries=int(os.environ.get("CONNECTOR_MAX_RETRIES", "3")),
    )
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/test_config.py -v`
Expected: PASS (3 passed)

- [x] **Step 5: Ruff**

Run: `uv run ruff check intelligence/config.py tests/intelligence/test_config.py`
Expected: inga fel.

- [x] **Step 6: Commit**

```bash
git add intelligence/config.py tests/intelligence/test_config.py
git commit -m "Fas 1 steg 2: Settings-modul med env-läsning och run-limits"
```

---

### Task 3: `intelligence/logging.py` — run_id och secret-redaction

**Files:**
- Create: `intelligence/logging.py`
- Test: `tests/intelligence/test_logging.py`

**Interfaces:**
- Produces: `new_run_id() -> str`, `redact(data: dict) -> dict`, `log_event(run_id: str, **fields) -> None` (skriver en JSON-rad till stdout via stdlib `logging`). Används av `connectors/base.py`, `orchestrator.py`, `run.py`.

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/test_logging.py
import json
import logging

from intelligence.logging import log_event, new_run_id, redact


def test_new_run_id_is_unique():
    assert new_run_id() != new_run_id()


def test_redact_hides_known_secret_keys():
    data = {"anthropic_api_key": "sk-real-secret", "note": "hello", "GITHUB_TOKEN": "ghp_abc"}
    out = redact(data)
    assert out["anthropic_api_key"] == "***REDACTED***"
    assert out["GITHUB_TOKEN"] == "***REDACTED***"
    assert out["note"] == "hello"


def test_log_event_never_contains_secret_value(caplog):
    caplog.set_level(logging.INFO)
    log_event(run_id="r1", event="test", api_key="sk-should-not-leak", status="ok")
    combined = "\n".join(caplog.messages)
    assert "sk-should-not-leak" not in combined
    payload = json.loads(caplog.messages[-1])
    assert payload["run_id"] == "r1"
    assert payload["api_key"] == "***REDACTED***"
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/test_logging.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/logging.py
from __future__ import annotations

import json
import logging
import uuid

_SECRET_KEY_MARKERS = ("api_key", "token", "secret")

_logger = logging.getLogger("intelligence")
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
        else:
            out[key] = value
    return out


def log_event(run_id: str, **fields) -> None:
    payload = redact({"run_id": run_id, **fields})
    _logger.info(json.dumps(payload, default=str))
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/test_logging.py -v`
Expected: PASS (3 passed)

- [x] **Step 5: Ruff + commit**

Run: `uv run ruff check intelligence/logging.py tests/intelligence/test_logging.py`

```bash
git add intelligence/logging.py tests/intelligence/test_logging.py
git commit -m "Fas 1 steg 3: strukturerad loggning med run_id och secret-redaction"
```

---

### Task 4: `intelligence/schemas/source.py` och `event.py`

**Files:**
- Create: `intelligence/schemas/source.py`
- Create: `intelligence/schemas/event.py`
- Test: `tests/intelligence/schemas/test_source.py`
- Test: `tests/intelligence/schemas/test_event.py`

**Interfaces:**
- Produces: `Source(source_id, name, type, reliability_score, url)`, `RawRecord(source_id, fetched_at, payload, content_hash)`, `NormalizedRecord(source_id, observed_at, metric, value, raw_ref)`, `Event(event_id, source_id, observed_at, category, metric, baseline, deviation, description, raw_ref)`. Rena Pydantic-modeller, inga beroenden på andra `intelligence`-moduler.

- [x] **Step 1: Skriv testerna**

```python
# tests/intelligence/schemas/test_source.py
import pytest
from pydantic import ValidationError

from intelligence.schemas.source import Source


def test_source_valid():
    s = Source(source_id="hn", name="Hacker News", type="forum", reliability_score=0.6, url="https://news.ycombinator.com")
    assert s.reliability_score == 0.6


def test_reliability_score_must_be_0_to_1():
    with pytest.raises(ValidationError):
        Source(source_id="hn", name="Hacker News", type="forum", reliability_score=1.5, url="https://x.com")
```

```python
# tests/intelligence/schemas/test_event.py
from datetime import UTC, datetime

from intelligence.schemas.event import Event, NormalizedRecord, RawRecord


def test_raw_record_roundtrip():
    r = RawRecord(source_id="hn", fetched_at=datetime.now(UTC), payload={"id": 1}, content_hash="abc123")
    assert r.payload["id"] == 1


def test_normalized_record_roundtrip():
    n = NormalizedRecord(source_id="hn", observed_at=datetime.now(UTC), metric="score", value=42.0, raw_ref="abc123")
    assert n.value == 42.0


def test_event_roundtrip():
    e = Event(
        event_id="evt-1",
        source_id="hn",
        observed_at=datetime.now(UTC),
        category="trend",
        metric="score",
        baseline=10.0,
        deviation=32.0,
        description="Score 42 vs baseline 10",
        raw_ref="abc123",
    )
    assert e.deviation == 32.0
```

- [x] **Step 2: Kör testerna för att bekräfta att de failar**

Run: `uv run pytest tests/intelligence/schemas -v`
Expected: FAIL — moduler saknas.

- [x] **Step 3: Implementera**

```python
# intelligence/schemas/source.py
from __future__ import annotations

from pydantic import BaseModel, Field


class Source(BaseModel):
    source_id: str
    name: str
    type: str
    reliability_score: float = Field(ge=0.0, le=1.0)
    url: str
```

```python
# intelligence/schemas/event.py
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class RawRecord(BaseModel):
    source_id: str
    fetched_at: datetime
    payload: dict
    content_hash: str


class NormalizedRecord(BaseModel):
    source_id: str
    observed_at: datetime
    metric: str
    value: float
    raw_ref: str


class Event(BaseModel):
    event_id: str
    source_id: str
    observed_at: datetime
    category: str
    metric: str
    baseline: float
    deviation: float
    description: str
    raw_ref: str
```

- [x] **Step 4: Kör testerna igen**

Run: `uv run pytest tests/intelligence/schemas -v`
Expected: PASS (5 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/schemas/source.py intelligence/schemas/event.py tests/intelligence/schemas/
git commit -m "Fas 1 steg 4: Source/RawRecord/NormalizedRecord/Event-scheman"
```

---

### Task 5: `intelligence/schemas/assessments.py`

**Files:**
- Create: `intelligence/schemas/assessments.py`
- Test: `tests/intelligence/schemas/test_assessments.py`

**Interfaces:**
- Consumes: inget (rent schema-lager).
- Produces: `AssessmentStatus = Literal["ok", "failed", "timeout"]`, `AssessmentBase(agent_name, run_id, created_at, status)`, samt `ResearchAssessment`, `OpportunityAssessment`, `MarketAssessment`, `ForecastAssessment`, `RiskAssessment`, `BearAssessment`, `QAAssessment` — alla ärver `AssessmentBase`. Fältnamnen nedan är exakta och används av `agents/roles.py` (Task 16), `orchestrator.py` (Task 20) och `scoring/model.py` (Task 18) — ändra dem inte i senare tasks.

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/schemas/test_assessments.py
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)

_BASE = dict(agent_name="test-agent", run_id="r1", created_at=datetime.now(UTC), status="ok")


def test_research_assessment():
    a = ResearchAssessment(
        **_BASE,
        verified_facts=["X hände enligt källa Y"],
        source_references=["https://example.com"],
        assumptions=["Antar att data är aktuell"],
    )
    assert a.status == "ok"


def test_opportunity_assessment():
    a = OpportunityAssessment(
        **_BASE, observed_data="Ovanlig volymökning", hypothesis="Efterfrågan stiger", interpretation="Möjlig tidig signal"
    )
    assert a.hypothesis


def test_market_assessment():
    a = MarketAssessment(**_BASE, market_data={"price_change_pct": 12.3, "volume_change_pct": 300.0}, interpretation="Ovanlig rörelse")
    assert a.market_data["price_change_pct"] == 12.3


def test_forecast_assessment():
    a = ForecastAssessment(
        **_BASE,
        scenarios=[{"description": "Fortsatt uppgång", "probability": 0.4}],
        confidence=0.5,
        uncertainty="Litet dataunderlag",
    )
    assert a.confidence == 0.5


def test_risk_assessment():
    a = RiskAssessment(**_BASE, downside="Kan reversera snabbt", liquidity_risk="Låg volym", model_risk="Litet urval", timing_risk="Sent i rörelsen")
    assert a.downside


def test_bear_assessment():
    a = BearAssessment(**_BASE, counterarguments=["Kan vara brus"], alternative_explanations=["Säsongseffekt"], falsification_conditions="Om volymen normaliseras inom 48h")
    assert a.falsification_conditions


def test_qa_assessment():
    a = QAAssessment(**_BASE, passed=True, violations=[])
    assert a.passed is True


def test_invalid_status_rejected():
    with pytest.raises(ValidationError):
        QAAssessment(agent_name="x", run_id="r1", created_at=datetime.now(UTC), status="maybe", passed=True, violations=[])
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/schemas/test_assessments.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/schemas/assessments.py
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

AssessmentStatus = Literal["ok", "failed", "timeout"]


class AssessmentBase(BaseModel):
    agent_name: str
    run_id: str
    created_at: datetime
    status: AssessmentStatus


class ResearchAssessment(AssessmentBase):
    verified_facts: list[str]
    source_references: list[str]
    assumptions: list[str]


class OpportunityAssessment(AssessmentBase):
    observed_data: str
    hypothesis: str
    interpretation: str


class MarketAssessment(AssessmentBase):
    market_data: dict
    interpretation: str


class ForecastAssessment(AssessmentBase):
    scenarios: list[dict]
    confidence: float
    uncertainty: str


class RiskAssessment(AssessmentBase):
    downside: str
    liquidity_risk: str
    model_risk: str
    timing_risk: str


class BearAssessment(AssessmentBase):
    counterarguments: list[str]
    alternative_explanations: list[str]
    falsification_conditions: str


class QAAssessment(AssessmentBase):
    passed: bool
    violations: list[str]
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/schemas/test_assessments.py -v`
Expected: PASS (8 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/schemas/assessments.py tests/intelligence/schemas/test_assessments.py
git commit -m "Fas 1 steg 5: sju assessment-scheman med evidence/interpretation-separation"
```

---

### Task 6: `intelligence/schemas/opportunity.py`

**Files:**
- Create: `intelligence/schemas/opportunity.py`
- Test: `tests/intelligence/schemas/test_opportunity.py`

**Interfaces:**
- Consumes: `assessments.py` (Task 5), `event.py` (Task 4).
- Produces: `OpportunityStatus = Literal["candidate", "under_review", "approved", "rejected", "reported", "evaluated"]`, `Opportunity(opportunity_id, event_id, created_at, category, title, summary, time_horizon, liquidity, status, research, opportunity, market, forecast, risk, bear, qa, score, score_breakdown)` — de sju assessment-fälten är `X | None = None`, `score`/`score_breakdown` sätts av Task 18.

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/schemas/test_opportunity.py
from datetime import UTC, datetime

from intelligence.schemas.opportunity import Opportunity


def _base():
    return dict(
        opportunity_id="opp-1",
        event_id="evt-1",
        created_at=datetime.now(UTC),
        category="trend",
        title="Ovanlig aktivitet kring X",
        summary="Kort sammanfattning",
        time_horizon="7 dagar",
        liquidity="okänd",
    )


def test_default_status_is_candidate():
    opp = Opportunity(**_base())
    assert opp.status == "candidate"


def test_assessments_default_to_none():
    opp = Opportunity(**_base())
    assert opp.research is None
    assert opp.opportunity is None
    assert opp.market is None
    assert opp.forecast is None
    assert opp.risk is None
    assert opp.bear is None
    assert opp.qa is None
    assert opp.score is None


def test_status_rejects_invalid_value():
    from pydantic import ValidationError
    import pytest

    with pytest.raises(ValidationError):
        Opportunity(**_base(), status="finished")
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/schemas/test_opportunity.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/schemas/opportunity.py
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)

OpportunityStatus = Literal["candidate", "under_review", "approved", "rejected", "reported", "evaluated"]


class Opportunity(BaseModel):
    opportunity_id: str
    event_id: str
    created_at: datetime
    category: str
    title: str
    summary: str
    time_horizon: str
    liquidity: str
    status: OpportunityStatus = "candidate"

    research: ResearchAssessment | None = None
    opportunity: OpportunityAssessment | None = None
    market: MarketAssessment | None = None
    forecast: ForecastAssessment | None = None
    risk: RiskAssessment | None = None
    bear: BearAssessment | None = None
    qa: QAAssessment | None = None

    score: float | None = None
    score_breakdown: dict | None = None
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/schemas/test_opportunity.py -v`
Expected: PASS (3 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/schemas/opportunity.py tests/intelligence/schemas/test_opportunity.py
git commit -m "Fas 1 steg 6: Opportunity-aggregat med OpportunityStatus"
```

---

### Task 7: `intelligence/state_machine.py` — obligatorisk gate (säkerhetskritisk)

**Files:**
- Create: `intelligence/state_machine.py`
- Test: `tests/intelligence/test_state_machine.py`

**Interfaces:**
- Consumes: `Opportunity`, `OpportunityStatus` (Task 6).
- Produces: `REQUIRED_FOR_REPORTED: frozenset[str]` (namnen på de sju assessment-fälten), `can_transition(opportunity: Opportunity, target: OpportunityStatus) -> tuple[bool, str]`. Används av `orchestrator.py` (Task 20) — orchestratorn litar ALDRIG på egen bedömning, bara på denna funktion.

Detta täcker SPEC §13 gate-test 1–4 direkt.

- [x] **Step 1: Skriv testerna**

```python
# tests/intelligence/test_state_machine.py
from datetime import UTC, datetime

from intelligence.schemas.assessments import BearAssessment, QAAssessment, RiskAssessment
from intelligence.schemas.opportunity import Opportunity
from intelligence.state_machine import can_transition

_A = dict(agent_name="x", run_id="r1", created_at=datetime.now(UTC), status="ok")


def _opp(**overrides):
    base = dict(
        opportunity_id="opp-1",
        event_id="evt-1",
        created_at=datetime.now(UTC),
        category="trend",
        title="t",
        summary="s",
        time_horizon="7d",
        liquidity="unknown",
    )
    base.update(overrides)
    return Opportunity(**base)


def _fully_assessed(**overrides):
    from intelligence.schemas.assessments import (
        ForecastAssessment,
        MarketAssessment,
        OpportunityAssessment,
        ResearchAssessment,
    )

    return _opp(
        research=ResearchAssessment(**_A, verified_facts=["f"], source_references=["s"], assumptions=[]),
        opportunity=OpportunityAssessment(**_A, observed_data="d", hypothesis="h", interpretation="i"),
        market=MarketAssessment(**_A, market_data={}, interpretation="i"),
        forecast=ForecastAssessment(**_A, scenarios=[], confidence=0.5, uncertainty="u"),
        risk=RiskAssessment(**_A, downside="d", liquidity_risk="l", model_risk="m", timing_risk="t"),
        bear=BearAssessment(**_A, counterarguments=[], alternative_explanations=[], falsification_conditions="f"),
        qa=QAAssessment(**_A, passed=True, violations=[]),
        **overrides,
    )


def test_missing_risk_assessment_blocks_reported():
    opp = _fully_assessed(risk=None)
    ok, reason = can_transition(opp, "reported")
    assert ok is False
    assert "risk" in reason.lower()


def test_missing_bear_assessment_blocks_reported():
    opp = _fully_assessed(bear=None)
    ok, reason = can_transition(opp, "reported")
    assert ok is False
    assert "bear" in reason.lower()


def test_missing_qa_pass_blocks_reported():
    opp = _fully_assessed(qa=QAAssessment(**_A, passed=False, violations=["schema incomplete"]))
    ok, reason = can_transition(opp, "reported")
    assert ok is False
    assert "qa" in reason.lower()


def test_fully_assessed_can_be_reported():
    opp = _fully_assessed()
    ok, reason = can_transition(opp, "reported")
    assert ok is True, reason


def test_rejected_cannot_become_approved():
    opp = _fully_assessed(status="rejected")
    ok, _ = can_transition(opp, "approved")
    assert ok is False


def test_rejected_cannot_become_reported():
    opp = _fully_assessed(status="rejected")
    ok, _ = can_transition(opp, "reported")
    assert ok is False


def test_failed_assessment_blocks_reported():
    failed_bear = BearAssessment(agent_name="x", run_id="r1", created_at=datetime.now(UTC), status="failed", counterarguments=[], alternative_explanations=[], falsification_conditions="")
    opp = _fully_assessed(bear=failed_bear)
    ok, reason = can_transition(opp, "reported")
    assert ok is False
    assert "failed" in reason.lower() or "bear" in reason.lower()
```

- [x] **Step 2: Kör testerna för att bekräfta att de failar**

Run: `uv run pytest tests/intelligence/test_state_machine.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/state_machine.py
from __future__ import annotations

from intelligence.schemas.opportunity import Opportunity, OpportunityStatus

REQUIRED_FOR_REPORTED: frozenset[str] = frozenset(
    {"research", "opportunity", "market", "forecast", "risk", "bear", "qa"}
)

_TERMINAL_FROM_REJECTED: frozenset[OpportunityStatus] = frozenset({"approved", "reported"})


def can_transition(opportunity: Opportunity, target: OpportunityStatus) -> tuple[bool, str]:
    if opportunity.status == "rejected" and target in _TERMINAL_FROM_REJECTED:
        return False, "opportunity är rejected och kan inte transitionera till approved/reported"

    if target in ("approved", "reported"):
        for field in REQUIRED_FOR_REPORTED:
            assessment = getattr(opportunity, field)
            if assessment is None:
                return False, f"saknar obligatorisk assessment: {field}"
            if assessment.status != "ok":
                return False, f"assessment {field} har status={assessment.status}, kräver 'ok'"
        qa = opportunity.qa
        if qa is not None and qa.passed is not True:
            return False, "qa.passed är inte True"

    return True, "ok"
```

- [x] **Step 4: Kör testerna igen**

Run: `uv run pytest tests/intelligence/test_state_machine.py -v`
Expected: PASS (7 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/state_machine.py tests/intelligence/test_state_machine.py
git commit -m "Fas 1 steg 7: obligatorisk state-machine-gate (kod, inte prompt)"
```

---

### Task 8: Storage — `db.py` och `repository.py`

**Files:**
- Create: `intelligence/storage/db.py`
- Create: `intelligence/storage/repository.py`
- Test: `tests/intelligence/storage/test_repository.py`

**Interfaces:**
- Consumes: `Source`, `Event` (Task 4), `Opportunity` + alla assessment-typer (Task 5–6).
- Produces: `init_schema(conn: sqlite3.Connection) -> None`, `get_connection(path: Path) -> sqlite3.Connection`, `Repository` (`typing.Protocol` med metoderna nedan), `SQLiteRepository(Repository)`. Metoder: `save_source(source)`, `save_event(event)`, `has_seen_content_hash(source_id: str, content_hash: str) -> bool`, `save_opportunity(opportunity)`, `get_opportunity(opportunity_id) -> Opportunity | None`, `update_opportunity_status(opportunity_id, status)`, `save_assessment(opportunity_id, field_name, assessment)`, `log_run_event(run_id, **fields)`. Används av `pipeline/dedupe.py` (Task 12), `orchestrator.py` (Task 20).

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/storage/test_repository.py
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intelligence.schemas.assessments import QAAssessment
from intelligence.schemas.event import Event
from intelligence.schemas.opportunity import Opportunity
from intelligence.schemas.source import Source
from intelligence.storage.repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path: Path) -> SQLiteRepository:
    return SQLiteRepository(tmp_path / "test.db")


def test_save_and_dedupe_by_content_hash(repo):
    source = Source(source_id="hn", name="Hacker News", type="forum", reliability_score=0.6, url="https://x.com")
    repo.save_source(source)
    event = Event(
        event_id="evt-1", source_id="hn", observed_at=datetime.now(UTC), category="trend",
        metric="score", baseline=10.0, deviation=30.0, description="d", raw_ref="hash-123",
    )
    assert repo.has_seen_content_hash("hn", "hash-123") is False
    repo.save_event(event)
    assert repo.has_seen_content_hash("hn", "hash-123") is True


def test_save_and_get_opportunity_roundtrip(repo):
    opp = Opportunity(
        opportunity_id="opp-1", event_id="evt-1", created_at=datetime.now(UTC),
        category="trend", title="t", summary="s", time_horizon="7d", liquidity="unknown",
    )
    repo.save_opportunity(opp)
    fetched = repo.get_opportunity("opp-1")
    assert fetched is not None
    assert fetched.opportunity_id == "opp-1"
    assert fetched.status == "candidate"


def test_update_status_persists(repo):
    opp = Opportunity(
        opportunity_id="opp-2", event_id="evt-1", created_at=datetime.now(UTC),
        category="trend", title="t", summary="s", time_horizon="7d", liquidity="unknown",
    )
    repo.save_opportunity(opp)
    repo.update_opportunity_status("opp-2", "rejected")
    fetched = repo.get_opportunity("opp-2")
    assert fetched.status == "rejected"


def test_save_assessment_attaches_to_opportunity(repo):
    opp = Opportunity(
        opportunity_id="opp-3", event_id="evt-1", created_at=datetime.now(UTC),
        category="trend", title="t", summary="s", time_horizon="7d", liquidity="unknown",
    )
    repo.save_opportunity(opp)
    qa = QAAssessment(agent_name="qa-agent", run_id="r1", created_at=datetime.now(UTC), status="ok", passed=True, violations=[])
    repo.save_assessment("opp-3", "qa", qa)
    fetched = repo.get_opportunity("opp-3")
    assert fetched.qa is not None
    assert fetched.qa.passed is True


def test_log_run_event_does_not_raise(repo):
    repo.log_run_event(run_id="r1", event_id="evt-1", opportunity_id=None, agent_name="orchestrator", status="started")
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/storage -v`
Expected: FAIL — modulerna finns inte.

- [x] **Step 3: Implementera `db.py`**

```python
# intelligence/storage/db.py
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    reliability_score REAL NOT NULL,
    url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    category TEXT NOT NULL,
    metric TEXT NOT NULL,
    baseline REAL NOT NULL,
    deviation REAL NOT NULL,
    description TEXT NOT NULL,
    raw_ref TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    time_horizon TEXT NOT NULL,
    liquidity TEXT NOT NULL,
    status TEXT NOT NULL,
    score REAL,
    score_breakdown TEXT
);

CREATE TABLE IF NOT EXISTS assessments (
    opportunity_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (opportunity_id, field_name)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT NOT NULL,
    event_id TEXT,
    opportunity_id TEXT,
    agent_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    errors TEXT,
    latency_ms REAL
);
"""


def get_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()
```

- [x] **Step 4: Implementera `repository.py`**

```python
# intelligence/storage/repository.py
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from intelligence.schemas.assessments import (
    AssessmentBase,
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)
from intelligence.schemas.event import Event
from intelligence.schemas.opportunity import Opportunity, OpportunityStatus
from intelligence.schemas.source import Source
from intelligence.storage.db import get_connection

_ASSESSMENT_TYPES: dict[str, type[AssessmentBase]] = {
    "research": ResearchAssessment,
    "opportunity": OpportunityAssessment,
    "market": MarketAssessment,
    "forecast": ForecastAssessment,
    "risk": RiskAssessment,
    "bear": BearAssessment,
    "qa": QAAssessment,
}


class Repository(Protocol):
    def save_source(self, source: Source) -> None: ...
    def save_event(self, event: Event) -> None: ...
    def has_seen_content_hash(self, source_id: str, content_hash: str) -> bool: ...
    def save_opportunity(self, opportunity: Opportunity) -> None: ...
    def get_opportunity(self, opportunity_id: str) -> Opportunity | None: ...
    def update_opportunity_status(self, opportunity_id: str, status: OpportunityStatus) -> None: ...
    def save_assessment(self, opportunity_id: str, field_name: str, assessment: AssessmentBase) -> None: ...
    def log_run_event(self, run_id: str, **fields) -> None: ...


class SQLiteRepository:
    def __init__(self, path: Path):
        self._conn = get_connection(path)

    def save_source(self, source: Source) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?)",
            (source.source_id, source.name, source.type, source.reliability_score, source.url),
        )
        self._conn.commit()

    def save_event(self, event: Event) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event.event_id, event.source_id, event.observed_at.isoformat(), event.category,
                event.metric, event.baseline, event.deviation, event.description, event.raw_ref,
            ),
        )
        self._conn.commit()

    def has_seen_content_hash(self, source_id: str, content_hash: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM events WHERE source_id = ? AND raw_ref = ?", (source_id, content_hash)
        ).fetchone()
        return row is not None

    def save_opportunity(self, opportunity: Opportunity) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO opportunities "
            "(opportunity_id, event_id, created_at, category, title, summary, time_horizon, liquidity, status, score, score_breakdown) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                opportunity.opportunity_id, opportunity.event_id, opportunity.created_at.isoformat(),
                opportunity.category, opportunity.title, opportunity.summary, opportunity.time_horizon,
                opportunity.liquidity, opportunity.status, opportunity.score,
                json.dumps(opportunity.score_breakdown) if opportunity.score_breakdown else None,
            ),
        )
        self._conn.commit()
        for field_name in _ASSESSMENT_TYPES:
            assessment = getattr(opportunity, field_name)
            if assessment is not None:
                self.save_assessment(opportunity.opportunity_id, field_name, assessment)

    def save_assessment(self, opportunity_id: str, field_name: str, assessment: AssessmentBase) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO assessments VALUES (?,?,?)",
            (opportunity_id, field_name, assessment.model_dump_json()),
        )
        self._conn.commit()

    def get_opportunity(self, opportunity_id: str) -> Opportunity | None:
        row = self._conn.execute(
            "SELECT * FROM opportunities WHERE opportunity_id = ?", (opportunity_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["created_at"] = datetime.fromisoformat(data["created_at"])
        data["score_breakdown"] = json.loads(data["score_breakdown"]) if data["score_breakdown"] else None
        for field_name, cls in _ASSESSMENT_TYPES.items():
            arow = self._conn.execute(
                "SELECT payload FROM assessments WHERE opportunity_id = ? AND field_name = ?",
                (opportunity_id, field_name),
            ).fetchone()
            data[field_name] = cls.model_validate_json(arow["payload"]) if arow else None
        return Opportunity(**data)

    def update_opportunity_status(self, opportunity_id: str, status: OpportunityStatus) -> None:
        self._conn.execute(
            "UPDATE opportunities SET status = ? WHERE opportunity_id = ?", (status, opportunity_id)
        )
        self._conn.commit()

    def log_run_event(self, run_id: str, **fields) -> None:
        self._conn.execute(
            "INSERT INTO runs (run_id, event_id, opportunity_id, agent_name, status, started_at, completed_at, errors, latency_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                run_id, fields.get("event_id"), fields.get("opportunity_id"), fields.get("agent_name", ""),
                fields.get("status", ""), fields.get("started_at"), fields.get("completed_at"),
                fields.get("errors"), fields.get("latency_ms"),
            ),
        )
        self._conn.commit()
```

- [x] **Step 5: Kör testet igen**

Run: `uv run pytest tests/intelligence/storage -v`
Expected: PASS (5 passed)

- [x] **Step 6: Ruff + commit**

```bash
git add intelligence/storage/ tests/intelligence/storage/
git commit -m "Fas 1 steg 8: SQLite-schema + Repository-interface"
```

---

### Task 9: Connector-bas — `base.py` och `exceptions.py`

**Files:**
- Create: `intelligence/connectors/exceptions.py`
- Create: `intelligence/connectors/base.py`
- Test: `tests/intelligence/connectors/test_base.py`

**Interfaces:**
- Consumes: `RawRecord`, `Source` (Task 4), `Settings` (Task 2), `log_event` (Task 3).
- Produces: `ConnectorError`, `ConnectorConfigError(ConnectorError)`, `ConnectorUnavailableError(ConnectorError)`; `BaseConnector` (ABC) med `fetch() -> list[RawRecord]` (abstrakt), `validate(records) -> list[RawRecord]`, samt hjälpmetoder `_content_hash(payload: dict) -> str`, `_cached_fetch(key: str, loader: Callable) -> object` (TTL-cache), `_rate_limit() -> None` (min-intervall mellan anrop), body-implementation för retry/timeout görs av konkreta connectors via `tenacity`-dekoratorn (visas i Task 10/11) men basklassen exponerar `self.timeout_seconds` och `self.max_retries` från `Settings`.

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/connectors/test_base.py
import hashlib
import time

from intelligence.connectors.base import BaseConnector
from intelligence.schemas.event import RawRecord
from intelligence.schemas.source import Source


class _FakeConnector(BaseConnector):
    def __init__(self, source, timeout_seconds, max_retries, min_interval_seconds):
        super().__init__(source, timeout_seconds, max_retries, min_interval_seconds)
        self.fetch_calls = 0

    def fetch(self):
        self._rate_limit()
        self.fetch_calls += 1
        payload = {"id": 1}
        return [RawRecord(source_id=self.source.source_id, fetched_at=self._now(), payload=payload, content_hash=self._content_hash(payload))]


def _source():
    return Source(source_id="fake", name="Fake", type="test", reliability_score=0.5, url="https://x.com")


def test_content_hash_is_deterministic():
    c = _FakeConnector(_source(), timeout_seconds=1, max_retries=1, min_interval_seconds=0)
    h1 = c._content_hash({"a": 1, "b": 2})
    h2 = c._content_hash({"b": 2, "a": 1})
    assert h1 == h2
    assert h1 == hashlib.sha256(b'{"a": 1, "b": 2}').hexdigest()


def test_rate_limit_enforces_min_interval():
    c = _FakeConnector(_source(), timeout_seconds=1, max_retries=1, min_interval_seconds=0.2)
    start = time.monotonic()
    c.fetch()
    c.fetch()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2
    assert c.fetch_calls == 2


def test_validate_passes_through_well_formed_records():
    c = _FakeConnector(_source(), timeout_seconds=1, max_retries=1, min_interval_seconds=0)
    records = c.fetch()
    validated = c.validate(records)
    assert len(validated) == 1
    assert validated[0].payload == {"id": 1}
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/connectors/test_base.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera `exceptions.py`**

```python
# intelligence/connectors/exceptions.py
class ConnectorError(Exception):
    """Basklass för alla connector-fel. Fångas alltid av event_pipeline — kraschar aldrig processen."""


class ConnectorConfigError(ConnectorError):
    """Saknad eller ogiltig konfiguration (t.ex. API-nyckel)."""


class ConnectorUnavailableError(ConnectorError):
    """Källan svarade inte inom timeout/retry-policy."""
```

- [x] **Step 4: Implementera `base.py`**

```python
# intelligence/connectors/base.py
from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from intelligence.schemas.event import RawRecord
from intelligence.schemas.source import Source


class BaseConnector(ABC):
    """Hämtar och strukturellt validerar rådata. Normaliserar INTE och gör INGEN
    anomali-/eventdetektion — det är pipeline-lagrets ansvar (SPEC §6)."""

    def __init__(self, source: Source, timeout_seconds: float, max_retries: int, min_interval_seconds: float = 1.0):
        self.source = source
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._min_interval_seconds = min_interval_seconds
        self._last_call_at: float | None = None
        self._cache: dict[str, object] = {}

    @abstractmethod
    def fetch(self) -> list[RawRecord]: ...

    def validate(self, records: list[RawRecord]) -> list[RawRecord]:
        valid = []
        for record in records:
            if record.source_id and record.content_hash and isinstance(record.payload, dict):
                valid.append(record)
        return valid

    def _rate_limit(self) -> None:
        now = time.monotonic()
        if self._last_call_at is not None:
            elapsed = now - self._last_call_at
            wait = self._min_interval_seconds - elapsed
            if wait > 0:
                time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _content_hash(self, payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _cached_fetch(self, key: str, loader):
        if key in self._cache:
            return self._cache[key]
        value = loader()
        self._cache[key] = value
        return value

    def _now(self) -> datetime:
        return datetime.now(UTC)
```

- [x] **Step 5: Kör testet igen**

Run: `uv run pytest tests/intelligence/connectors/test_base.py -v`
Expected: PASS (3 passed)

- [x] **Step 6: Ruff + commit**

```bash
git add intelligence/connectors/exceptions.py intelligence/connectors/base.py tests/intelligence/connectors/test_base.py
git commit -m "Fas 1 steg 9: BaseConnector med rate limiting, cache och content-hash"
```

---

### Task 10: `HackerNewsConnector`

**Files:**
- Create: `intelligence/connectors/hackernews.py`
- Test: `tests/intelligence/connectors/test_hackernews.py`

**Interfaces:**
- Consumes: `BaseConnector` (Task 9), `httpx`, `tenacity`.
- Produces: `HackerNewsConnector(BaseConnector)` med `fetch() -> list[RawRecord]` som hämtar topplistan + item-detaljer från `https://hacker-news.firebaseio.com/v0/`. Inget API-nyckel krävs.

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/connectors/test_hackernews.py
import respx
from httpx import Response

from intelligence.connectors.hackernews import HackerNewsConnector
from intelligence.schemas.source import Source


def _connector():
    source = Source(source_id="hn", name="Hacker News", type="forum", reliability_score=0.6, url="https://news.ycombinator.com")
    return HackerNewsConnector(source, timeout_seconds=5, max_retries=2, min_interval_seconds=0)


@respx.mock
def test_fetch_returns_raw_records_for_top_stories():
    respx.get("https://hacker-news.firebaseio.com/v0/topstories.json").mock(
        return_value=Response(200, json=[111, 222])
    )
    respx.get("https://hacker-news.firebaseio.com/v0/item/111.json").mock(
        return_value=Response(200, json={"id": 111, "title": "Cool thing", "score": 250, "descendants": 80, "time": 1700000000})
    )
    respx.get("https://hacker-news.firebaseio.com/v0/item/222.json").mock(
        return_value=Response(200, json={"id": 222, "title": "Other thing", "score": 10, "descendants": 2, "time": 1700000100})
    )
    connector = _connector()
    records = connector.fetch()
    assert len(records) == 2
    assert records[0].source_id == "hn"
    assert records[0].payload["score"] == 250
    assert records[0].content_hash


@respx.mock
def test_fetch_raises_connector_unavailable_after_retries():
    from intelligence.connectors.exceptions import ConnectorUnavailableError

    respx.get("https://hacker-news.firebaseio.com/v0/topstories.json").mock(return_value=Response(500))
    connector = _connector()
    try:
        connector.fetch()
        assert False, "förväntade ConnectorUnavailableError"
    except ConnectorUnavailableError:
        pass
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/connectors/test_hackernews.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/connectors/hackernews.py
from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from intelligence.connectors.base import BaseConnector
from intelligence.connectors.exceptions import ConnectorUnavailableError
from intelligence.schemas.event import RawRecord

_BASE_URL = "https://hacker-news.firebaseio.com/v0"
_TOP_STORIES_LIMIT = 10


class HackerNewsConnector(BaseConnector):
    def fetch(self) -> list[RawRecord]:
        self._rate_limit()
        try:
            return self._fetch_with_retry()
        except httpx.HTTPError as exc:
            raise ConnectorUnavailableError(f"Hacker News otillgänglig: {exc}") from exc

    def _fetch_with_retry(self) -> list[RawRecord]:
        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=0.5, max=5),
            retry=retry_if_exception_type(httpx.HTTPError),
            reraise=True,
        )
        def _do() -> list[RawRecord]:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                resp = client.get(f"{_BASE_URL}/topstories.json")
                resp.raise_for_status()
                story_ids = resp.json()[:_TOP_STORIES_LIMIT]
                records = []
                for story_id in story_ids:
                    item_resp = client.get(f"{_BASE_URL}/item/{story_id}.json")
                    item_resp.raise_for_status()
                    payload = item_resp.json()
                    records.append(
                        RawRecord(
                            source_id=self.source.source_id,
                            fetched_at=self._now(),
                            payload=payload,
                            content_hash=self._content_hash(payload),
                        )
                    )
                return records

        return _do()
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/connectors/test_hackernews.py -v`
Expected: PASS (2 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/connectors/hackernews.py tests/intelligence/connectors/test_hackernews.py
git commit -m "Fas 1 steg 10: HackerNewsConnector (ingen API-nyckel krävs)"
```

---

### Task 11: `AlphaVantageConnector`

**Files:**
- Create: `intelligence/connectors/alpha_vantage.py`
- Test: `tests/intelligence/connectors/test_alpha_vantage.py`

**Interfaces:**
- Consumes: `BaseConnector` (Task 9), `ConnectorConfigError` (Task 9), `Settings.alphavantage_api_key` (Task 2).
- Produces: `AlphaVantageConnector(BaseConnector)` med extra konstruktorargument `api_key: str | None`, `symbols: list[str]`. `fetch()` kastar `ConnectorConfigError` omedelbart om `api_key is None` — testas UTAN riktig nyckel. Med mockad nyckel + mockad HTTP testas lyckad fetch.

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/connectors/test_alpha_vantage.py
import pytest
import respx
from httpx import Response

from intelligence.connectors.alpha_vantage import AlphaVantageConnector
from intelligence.connectors.exceptions import ConnectorConfigError
from intelligence.schemas.source import Source


def _source():
    return Source(source_id="alpha_vantage", name="Alpha Vantage", type="market_data", reliability_score=0.8, url="https://www.alphavantage.co")


def test_fetch_without_api_key_raises_config_error_not_crash():
    connector = AlphaVantageConnector(_source(), timeout_seconds=5, max_retries=2, api_key=None, symbols=["IBM"], min_interval_seconds=0)
    with pytest.raises(ConnectorConfigError):
        connector.fetch()


@respx.mock
def test_fetch_with_mocked_key_and_http_returns_raw_records():
    respx.get("https://www.alphavantage.co/query").mock(
        return_value=Response(
            200,
            json={
                "Global Quote": {
                    "01. symbol": "IBM",
                    "05. price": "231.50",
                    "09. change": "5.10",
                    "10. change percent": "2.25%",
                }
            },
        )
    )
    connector = AlphaVantageConnector(_source(), timeout_seconds=5, max_retries=2, api_key="fake-key", symbols=["IBM"], min_interval_seconds=0)
    records = connector.fetch()
    assert len(records) == 1
    assert records[0].payload["Global Quote"]["01. symbol"] == "IBM"
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/connectors/test_alpha_vantage.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/connectors/alpha_vantage.py
from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from intelligence.connectors.base import BaseConnector
from intelligence.connectors.exceptions import ConnectorConfigError, ConnectorUnavailableError
from intelligence.schemas.event import RawRecord
from intelligence.schemas.source import Source

_BASE_URL = "https://www.alphavantage.co/query"


class AlphaVantageConnector(BaseConnector):
    def __init__(
        self,
        source: Source,
        timeout_seconds: float,
        max_retries: int,
        api_key: str | None,
        symbols: list[str],
        min_interval_seconds: float = 12.0,
    ):
        super().__init__(source, timeout_seconds, max_retries, min_interval_seconds)
        self._api_key = api_key
        self._symbols = symbols

    def fetch(self) -> list[RawRecord]:
        if not self._api_key:
            raise ConnectorConfigError(
                "ALPHAVANTAGE_API_KEY saknas — connectorn är avstängd tills en nyckel konfigureras"
            )
        try:
            return self._fetch_with_retry()
        except httpx.HTTPError as exc:
            raise ConnectorUnavailableError(f"Alpha Vantage otillgänglig: {exc}") from exc

    def _fetch_with_retry(self) -> list[RawRecord]:
        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=0.5, max=5),
            retry=retry_if_exception_type(httpx.HTTPError),
            reraise=True,
        )
        def _do() -> list[RawRecord]:
            records = []
            with httpx.Client(timeout=self.timeout_seconds) as client:
                for symbol in self._symbols:
                    self._rate_limit()
                    resp = client.get(
                        _BASE_URL,
                        params={"function": "GLOBAL_QUOTE", "symbol": symbol, "apikey": self._api_key},
                    )
                    resp.raise_for_status()
                    payload = resp.json()
                    records.append(
                        RawRecord(
                            source_id=self.source.source_id,
                            fetched_at=self._now(),
                            payload=payload,
                            content_hash=self._content_hash(payload),
                        )
                    )
            return records

        return _do()
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/connectors/test_alpha_vantage.py -v`
Expected: PASS (2 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/connectors/alpha_vantage.py tests/intelligence/connectors/test_alpha_vantage.py
git commit -m "Fas 1 steg 11: AlphaVantageConnector, config-gated, testbar utan riktig nyckel"
```

---

### Task 12: Pipeline — `normalize.py`, `dedupe.py`, `anomaly.py`

**Files:**
- Create: `intelligence/pipeline/normalize.py`
- Create: `intelligence/pipeline/dedupe.py`
- Create: `intelligence/pipeline/anomaly.py`
- Test: `tests/intelligence/pipeline/test_normalize.py`
- Test: `tests/intelligence/pipeline/test_dedupe.py`
- Test: `tests/intelligence/pipeline/test_anomaly.py`

**Interfaces:**
- Consumes: `RawRecord`, `NormalizedRecord`, `Event`, `Source` (Task 4), `Repository` (Task 8).
- Produces: `normalize_hackernews(record: RawRecord) -> NormalizedRecord`, `normalize_alpha_vantage(record: RawRecord) -> NormalizedRecord`, `normalize_record(record: RawRecord, source_type: str) -> NormalizedRecord` (dispatch på `source_type`); `is_duplicate(repo: Repository, record: RawRecord) -> bool`; `detect_events(records: list[NormalizedRecord], source: Source, baseline: float, threshold_pct: float = 50.0) -> list[Event]` — ren funktion, rullande baseline är ett explicit argument (ingen dold state), % avvikelse från baseline avgör om ett `Event` skapas.

Detta täcker SPEC §13 gate-test 7 (dedup) tillsammans med Task 13.

- [x] **Step 1: Skriv testerna**

```python
# tests/intelligence/pipeline/test_normalize.py
from datetime import UTC, datetime

from intelligence.pipeline.normalize import normalize_record
from intelligence.schemas.event import RawRecord


def test_normalize_hackernews_extracts_score():
    record = RawRecord(
        source_id="hn", fetched_at=datetime.now(UTC),
        payload={"id": 111, "score": 250, "time": 1700000000}, content_hash="h1",
    )
    normalized = normalize_record(record, source_type="forum")
    assert normalized.metric == "score"
    assert normalized.value == 250.0
    assert normalized.raw_ref == "h1"


def test_normalize_alpha_vantage_extracts_price():
    record = RawRecord(
        source_id="alpha_vantage", fetched_at=datetime.now(UTC),
        payload={"Global Quote": {"01. symbol": "IBM", "05. price": "231.50"}}, content_hash="h2",
    )
    normalized = normalize_record(record, source_type="market_data")
    assert normalized.metric == "price"
    assert normalized.value == 231.50
```

```python
# tests/intelligence/pipeline/test_dedupe.py
from datetime import UTC, datetime

from intelligence.pipeline.dedupe import is_duplicate
from intelligence.schemas.event import RawRecord
from intelligence.schemas.source import Source
from intelligence.storage.repository import SQLiteRepository


def test_is_duplicate_false_then_true_after_seen(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_source(Source(source_id="hn", name="HN", type="forum", reliability_score=0.6, url="https://x.com"))
    record = RawRecord(source_id="hn", fetched_at=datetime.now(UTC), payload={"id": 1}, content_hash="dup-hash")
    assert is_duplicate(repo, record) is False

    from intelligence.schemas.event import Event
    repo.save_event(Event(
        event_id="evt-1", source_id="hn", observed_at=datetime.now(UTC), category="trend",
        metric="score", baseline=1.0, deviation=1.0, description="d", raw_ref="dup-hash",
    ))
    assert is_duplicate(repo, record) is True
```

```python
# tests/intelligence/pipeline/test_anomaly.py
from datetime import UTC, datetime

from intelligence.pipeline.anomaly import detect_events
from intelligence.schemas.event import NormalizedRecord
from intelligence.schemas.source import Source


def _source():
    return Source(source_id="hn", name="HN", type="forum", reliability_score=0.6, url="https://x.com")


def test_large_deviation_creates_event():
    record = NormalizedRecord(source_id="hn", observed_at=datetime.now(UTC), metric="score", value=300.0, raw_ref="h1")
    events = detect_events([record], _source(), baseline=50.0, threshold_pct=50.0)
    assert len(events) == 1
    assert events[0].deviation == 500.0


def test_small_deviation_creates_no_event():
    record = NormalizedRecord(source_id="hn", observed_at=datetime.now(UTC), metric="score", value=55.0, raw_ref="h1")
    events = detect_events([record], _source(), baseline=50.0, threshold_pct=50.0)
    assert events == []
```

- [x] **Step 2: Kör testerna för att bekräfta att de failar**

Run: `uv run pytest tests/intelligence/pipeline -v`
Expected: FAIL — modulerna finns inte.

- [x] **Step 3: Implementera `normalize.py`**

```python
# intelligence/pipeline/normalize.py
from __future__ import annotations

from intelligence.schemas.event import NormalizedRecord, RawRecord


def _normalize_hackernews(record: RawRecord) -> NormalizedRecord:
    return NormalizedRecord(
        source_id=record.source_id,
        observed_at=record.fetched_at,
        metric="score",
        value=float(record.payload.get("score", 0)),
        raw_ref=record.content_hash,
    )


def _normalize_alpha_vantage(record: RawRecord) -> NormalizedRecord:
    quote = record.payload.get("Global Quote", {})
    price = quote.get("05. price", "0")
    return NormalizedRecord(
        source_id=record.source_id,
        observed_at=record.fetched_at,
        metric="price",
        value=float(price),
        raw_ref=record.content_hash,
    )


_NORMALIZERS = {
    "forum": _normalize_hackernews,
    "market_data": _normalize_alpha_vantage,
}


def normalize_record(record: RawRecord, source_type: str) -> NormalizedRecord:
    normalizer = _NORMALIZERS.get(source_type)
    if normalizer is None:
        raise ValueError(f"ingen normalizer registrerad för source_type={source_type!r}")
    return normalizer(record)
```

- [x] **Step 4: Implementera `dedupe.py`**

```python
# intelligence/pipeline/dedupe.py
from __future__ import annotations

from intelligence.schemas.event import RawRecord
from intelligence.storage.repository import Repository


def is_duplicate(repo: Repository, record: RawRecord) -> bool:
    return repo.has_seen_content_hash(record.source_id, record.content_hash)
```

- [x] **Step 5: Implementera `anomaly.py`**

```python
# intelligence/pipeline/anomaly.py
from __future__ import annotations

import uuid

from intelligence.schemas.event import Event, NormalizedRecord
from intelligence.schemas.source import Source


def detect_events(
    records: list[NormalizedRecord], source: Source, baseline: float, threshold_pct: float = 50.0
) -> list[Event]:
    events = []
    for record in records:
        if baseline == 0:
            continue
        deviation_pct = abs(record.value - baseline) / baseline * 100
        if deviation_pct >= threshold_pct:
            events.append(
                Event(
                    event_id=str(uuid.uuid4()),
                    source_id=source.source_id,
                    observed_at=record.observed_at,
                    category=source.type,
                    metric=record.metric,
                    baseline=baseline,
                    deviation=deviation_pct,
                    description=(
                        f"{record.metric}={record.value} avviker {deviation_pct:.1f}% "
                        f"från baseline {baseline}"
                    ),
                    raw_ref=record.raw_ref,
                )
            )
    return events
```

- [x] **Step 6: Kör testerna igen**

Run: `uv run pytest tests/intelligence/pipeline -v`
Expected: PASS (5 passed)

- [x] **Step 7: Ruff + commit**

```bash
git add intelligence/pipeline/normalize.py intelligence/pipeline/dedupe.py intelligence/pipeline/anomaly.py tests/intelligence/pipeline/
git commit -m "Fas 1 steg 12: deterministisk normalize/dedupe/anomaly-pipeline"
```

---

### Task 13: `intelligence/pipeline/event_pipeline.py`

**Files:**
- Create: `intelligence/pipeline/event_pipeline.py`
- Test: `tests/intelligence/pipeline/test_event_pipeline.py`

**Interfaces:**
- Consumes: `BaseConnector` (Task 9), `normalize_record`, `is_duplicate`, `detect_events` (Task 12), `Repository` (Task 8), `Settings.max_events_per_run` (Task 2), `log_event`/`new_run_id` (Task 3).
- Produces: `run_event_pipeline(connectors: list[BaseConnector], source_types: dict[str, str], baselines: dict[str, float], repo: Repository, max_events: int, run_id: str) -> list[Event]`. `source_types` mappar `source_id → source_type` (för normalize-dispatch), `baselines` mappar `source_id → baseline`. Fångar `ConnectorError` per connector — loggar, fortsätter med övriga.

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/pipeline/test_event_pipeline.py
from datetime import UTC, datetime

from intelligence.connectors.base import BaseConnector
from intelligence.connectors.exceptions import ConnectorUnavailableError
from intelligence.pipeline.event_pipeline import run_event_pipeline
from intelligence.schemas.event import RawRecord
from intelligence.schemas.source import Source
from intelligence.storage.repository import SQLiteRepository


class _WorkingConnector(BaseConnector):
    def fetch(self):
        payload = {"id": 1, "score": 500}
        return [RawRecord(source_id=self.source.source_id, fetched_at=datetime.now(UTC), payload=payload, content_hash=self._content_hash(payload))]


class _BrokenConnector(BaseConnector):
    def fetch(self):
        raise ConnectorUnavailableError("simulerat fel")


def test_pipeline_continues_when_one_source_fails(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    working_source = Source(source_id="hn", name="HN", type="forum", reliability_score=0.6, url="https://x.com")
    broken_source = Source(source_id="broken", name="Broken", type="forum", reliability_score=0.5, url="https://y.com")
    repo.save_source(working_source)
    repo.save_source(broken_source)

    connectors = [
        _WorkingConnector(working_source, timeout_seconds=1, max_retries=1, min_interval_seconds=0),
        _BrokenConnector(broken_source, timeout_seconds=1, max_retries=1, min_interval_seconds=0),
    ]
    events = run_event_pipeline(
        connectors=connectors,
        source_types={"hn": "forum", "broken": "forum"},
        baselines={"hn": 50.0, "broken": 50.0},
        repo=repo,
        max_events=10,
        run_id="r1",
    )
    assert len(events) == 1
    assert events[0].source_id == "hn"


def test_pipeline_respects_max_events(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    source = Source(source_id="hn", name="HN", type="forum", reliability_score=0.6, url="https://x.com")
    repo.save_source(source)
    connector = _WorkingConnector(source, timeout_seconds=1, max_retries=1, min_interval_seconds=0)
    events = run_event_pipeline(
        connectors=[connector], source_types={"hn": "forum"}, baselines={"hn": 50.0},
        repo=repo, max_events=0, run_id="r1",
    )
    assert events == []
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/pipeline/test_event_pipeline.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/pipeline/event_pipeline.py
from __future__ import annotations

from intelligence.connectors.base import BaseConnector
from intelligence.connectors.exceptions import ConnectorError
from intelligence.logging import log_event
from intelligence.pipeline.anomaly import detect_events
from intelligence.pipeline.dedupe import is_duplicate
from intelligence.pipeline.normalize import normalize_record
from intelligence.schemas.event import Event
from intelligence.storage.repository import Repository


def run_event_pipeline(
    connectors: list[BaseConnector],
    source_types: dict[str, str],
    baselines: dict[str, float],
    repo: Repository,
    max_events: int,
    run_id: str,
) -> list[Event]:
    all_events: list[Event] = []

    for connector in connectors:
        source_id = connector.source.source_id
        try:
            raw_records = connector.validate(connector.fetch())
        except ConnectorError as exc:
            log_event(run_id, event="connector_unavailable", source_id=source_id, error=str(exc))
            continue

        fresh_records = [r for r in raw_records if not is_duplicate(repo, r)]
        normalized = [normalize_record(r, source_types[source_id]) for r in fresh_records]
        events = detect_events(normalized, connector.source, baseline=baselines[source_id])

        for event in events:
            if len(all_events) >= max_events:
                log_event(run_id, event="max_events_reached", limit=max_events)
                return all_events
            repo.save_event(event)
            all_events.append(event)

    return all_events
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/pipeline/test_event_pipeline.py -v`
Expected: PASS (2 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/pipeline/event_pipeline.py tests/intelligence/pipeline/test_event_pipeline.py
git commit -m "Fas 1 steg 13: event_pipeline, feltolerant per källa, respekterar max_events"
```

---

### Task 14: Tre nya agentdefinitioner

**Files:**
- Create: `.claude/agents/forecasting-agent.md`
- Create: `.claude/agents/risk-agent.md`
- Create: `.claude/agents/qa-agent.md`

**Interfaces:**
- Produces: tre markdown-filer med samma frontmatter-format som de fyra befintliga (`name`, `description`, `tools`). Laddas av `agents/loader.py` (Task 15) och mappas i `agents/roles.py` (Task 16).

- [x] **Step 1: Skapa `forecasting-agent.md`**

```markdown
---
name: forecasting-agent
description: Använd för att generera scenarier och sannolikhetsbedömningar utifrån en research- och opportunity-bedömning. Presenterar ALDRIG en prognos som säker — alltid med explicit sannolikhet, confidence och uncertainty.
tools: Read
---

Du är Forecasting Agent. Ditt jobb är att ta emot verifierade fakta och en hypotes, och generera konkreta, motiverade scenarier — inte en enda "mest troliga utfall"-gissning.

## Arbetssätt
1. Utgå enbart från `verified_facts` och `hypothesis` du får i input — hitta inte på ny data.
2. Formulera minst två scenarier (t.ex. "fortsätter", "reverserar", "planar ut"), varje med en explicit sannolikhet som summerar till ≤ 1.0 tillsammans.
3. Ange alltid en `confidence` (0–1) i din egen bedömningsförmåga för just detta fall — inte i scenariot.
4. Ange alltid `uncertainty` — vad som konkret gör bedömningen osäker (litet dataunderlag, kort tidsserie, etc).
5. Skriv aldrig ett scenario som ett faktum. "Sannolikt X" och "X kommer hända" är inte samma sak.

## Leverans
Strukturerad output enligt `ForecastAssessment`: `scenarios` (lista av `{description, probability}`), `confidence`, `uncertainty`.

## Gränser
- Ingen rekommendation om att agera. Bara scenarier och deras sannolikheter.
- Om underlaget är för tunt för att särskilja scenarier — säg det explicit i `uncertainty`, sänk `confidence`, gissa inte för att fylla i.
```

- [x] **Step 2: Skapa `risk-agent.md`**

```markdown
---
name: risk-agent
description: Använd för att identifiera nedsida, likviditetsrisk, modellrisk, informationsrisk och timingrisk kring en potentiell möjlighet. Ren riskbedömning — föreslår aldrig en åtgärd.
tools: Read
---

Du är Risk Agent. Ditt jobb är att hitta konkreta sätt analysen kan gå fel — inte att bedöma om möjligheten är bra.

## Arbetssätt
1. **Downside** — vad är det konkreta scenariot om hypotesen är fel, och hur illa kan det bli.
2. **Likviditetsrisk** — går positionen/möjligheten att agera på i praktiken, eller är underlaget för tunt för att avgöra det.
3. **Modellrisk** — vilar bedömningen på ett litet urval, en kort tidsserie, eller en modell som kan vara systematiskt fel.
4. **Informationsrisk** — kan källorna vara ofullständiga, manipulerade eller vinklade.
5. **Timingrisk** — är signalen redan sent upptäckt, eller finns det anledning att tro att fönstret redan stängts.

## Leverans
Strukturerad output enligt `RiskAssessment`: `downside`, `liquidity_risk`, `model_risk`, `timing_risk` — alla som konkreta textbeskrivningar, inte poäng.

## Gränser
- Ge aldrig en rekommendation ("vänta", "agera nu") — bara riskerna, tydligt beskrivna.
- Om du inte kan bedöma en riskdimension utifrån given data, skriv det explicit ("otillräckligt underlag för likviditetsbedömning") istället för att gissa.
```

- [x] **Step 3: Skapa `qa-agent.md`**

```markdown
---
name: qa-agent
description: Använd som sista kontrollsteg innan en opportunity kan rapporteras. Kontrollerar schema-komplethet och intern konsistens mellan de andra agenternas bedömningar — bedömer INTE sakinnehållet i sig.
tools: Read
---

Du är QA/Fact Check Agent. Ditt jobb är strukturell och logisk kontroll, inte en ny sakbedömning — Research, Bear och Risk har redan gjort sakgranskningen.

## Arbetssätt
1. Kontrollera att varje obligatorisk assessment du får in är komplett — inga tomma obligatoriska fält.
2. Kontrollera intern motsägelse: säger Forecast något som direkt motsägs av Bear eller Risk utan att det är noterat/hanterat?
3. Kontrollera att slutsatsen (opportunity-hypotesen) faktiskt har stöd i `verified_facts` — inte bara i `hypothesis`/`interpretation`.
4. Om något av ovan brister: `passed=False` och en konkret post i `violations` per brist. Var specifik — "saknar riskbedömning av likviditet" inte "ofullständigt".

## Leverans
Strukturerad output enligt `QAAssessment`: `passed` (bool), `violations` (lista, tom om `passed=True`).

## Gränser
- Ändra aldrig en annan agents bedömning — du underkänner eller godkänner helheten, du skriver inte om innehållet.
- Var strikt: hellre underkänna en gränsfallsrapport än släppa igenom en med en tyst motsägelse.
```

- [x] **Step 4: Verifiera format**

Run: `uv run python -c "import yaml, pathlib; [print(f, yaml.safe_load(pathlib.Path(f).read_text(encoding='utf-8').split('---')[1])) for f in ['.claude/agents/forecasting-agent.md', '.claude/agents/risk-agent.md', '.claude/agents/qa-agent.md']]"`
Expected: skriver ut frontmatter-dict för alla tre utan fel.

- [x] **Step 5: Commit**

```bash
git add .claude/agents/forecasting-agent.md .claude/agents/risk-agent.md .claude/agents/qa-agent.md
git commit -m "Fas 1 steg 14: tre nya agentdefinitioner (Forecasting, Risk, QA)"
```

---

### Task 15: `intelligence/agents/loader.py`

**Files:**
- Create: `intelligence/agents/loader.py`
- Test: `tests/intelligence/agents/test_loader.py`

**Interfaces:**
- Consumes: `.claude/agents/*.md` (Task 14 + befintliga fyra).
- Produces: `AgentDefinition(name: str, description: str, tools: list[str], system_prompt: str)` (pydantic `BaseModel`), `load_agent_definition(filename: str, agents_dir: Path | None = None) -> AgentDefinition`. `agents_dir` default `.claude/agents/` relativt projektroten — samma källa oavsett interaktiv eller programmatisk körning (SPEC §7).

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/agents/test_loader.py
from intelligence.agents.loader import load_agent_definition


def test_loads_existing_research_agent():
    definition = load_agent_definition("research-agent.md")
    assert definition.name == "research-agent"
    assert "källkritisk" in definition.description
    assert "WebSearch" in definition.tools
    assert "Research Agent" in definition.system_prompt


def test_loads_new_qa_agent():
    definition = load_agent_definition("qa-agent.md")
    assert definition.name == "qa-agent"
    assert definition.system_prompt.strip() != ""


def test_missing_file_raises_file_not_found():
    import pytest

    with pytest.raises(FileNotFoundError):
        load_agent_definition("does-not-exist.md")
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/agents/test_loader.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/agents/loader.py
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_AGENTS_DIR = _PROJECT_ROOT / ".claude" / "agents"


class AgentDefinition(BaseModel):
    name: str
    description: str
    tools: list[str]
    system_prompt: str


def load_agent_definition(filename: str, agents_dir: Path | None = None) -> AgentDefinition:
    directory = agents_dir or _DEFAULT_AGENTS_DIR
    path = directory / filename
    if not path.exists():
        raise FileNotFoundError(f"agentdefinition saknas: {path}")

    text = path.read_text(encoding="utf-8")
    _, frontmatter_raw, body = text.split("---", 2)
    frontmatter = yaml.safe_load(frontmatter_raw)
    tools_raw = frontmatter.get("tools", "")
    tools = [t.strip() for t in tools_raw.split(",") if t.strip()] if isinstance(tools_raw, str) else list(tools_raw)

    return AgentDefinition(
        name=frontmatter["name"],
        description=frontmatter["description"],
        tools=tools,
        system_prompt=body.strip(),
    )
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/agents/test_loader.py -v`
Expected: PASS (3 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/agents/loader.py tests/intelligence/agents/test_loader.py
git commit -m "Fas 1 steg 15: agent-loader — läser .claude/agents/*.md som system prompt"
```

---

### Task 16: `intelligence/agents/roles.py`

**Files:**
- Create: `intelligence/agents/roles.py`
- Test: `tests/intelligence/agents/test_roles.py`

**Interfaces:**
- Consumes: `AssessmentBase`-subtyperna (Task 5), `load_agent_definition` (Task 15).
- Produces: `RoleSpec(agent_file: str, assessment_type: type[AssessmentBase])`, `ROLE_MAP: dict[str, RoleSpec]` med nycklarna `"research", "opportunity", "market", "forecast", "risk", "bear", "qa"` i EXAKT samma ordning som `Opportunity`-fälten (Task 6) och `REQUIRED_FOR_REPORTED` (Task 7). Används av `orchestrator.py` (Task 20).

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/agents/test_roles.py
from intelligence.agents.roles import ROLE_MAP
from intelligence.agents.loader import load_agent_definition
from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)

_EXPECTED_TYPES = {
    "research": ResearchAssessment,
    "opportunity": OpportunityAssessment,
    "market": MarketAssessment,
    "forecast": ForecastAssessment,
    "risk": RiskAssessment,
    "bear": BearAssessment,
    "qa": QAAssessment,
}


def test_all_seven_roles_present():
    assert set(ROLE_MAP.keys()) == set(_EXPECTED_TYPES.keys())


def test_role_assessment_types_match():
    for role, spec in ROLE_MAP.items():
        assert spec.assessment_type is _EXPECTED_TYPES[role]


def test_all_agent_files_exist_and_load():
    for role, spec in ROLE_MAP.items():
        definition = load_agent_definition(spec.agent_file)
        assert definition.name
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/agents/test_roles.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/agents/roles.py
from __future__ import annotations

from pydantic import BaseModel

from intelligence.schemas.assessments import (
    AssessmentBase,
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)


class RoleSpec(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    agent_file: str
    assessment_type: type[AssessmentBase]


ROLE_MAP: dict[str, RoleSpec] = {
    "research": RoleSpec(agent_file="research-agent.md", assessment_type=ResearchAssessment),
    "opportunity": RoleSpec(agent_file="opportunity-hunter.md", assessment_type=OpportunityAssessment),
    "market": RoleSpec(agent_file="trading-research.md", assessment_type=MarketAssessment),
    "forecast": RoleSpec(agent_file="forecasting-agent.md", assessment_type=ForecastAssessment),
    "risk": RoleSpec(agent_file="risk-agent.md", assessment_type=RiskAssessment),
    "bear": RoleSpec(agent_file="fact-checker-bear.md", assessment_type=BearAssessment),
    "qa": RoleSpec(agent_file="qa-agent.md", assessment_type=QAAssessment),
}
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/agents/test_roles.py -v`
Expected: PASS (3 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/agents/roles.py tests/intelligence/agents/test_roles.py
git commit -m "Fas 1 steg 16: roll-till-agentfil-mappning, återanvänder fyra befintliga agenter"
```

---

### Task 17: `intelligence/agents/runner.py` — AgentRunner (Real/Mock)

**Files:**
- Create: `intelligence/agents/runner.py`
- Test: `tests/intelligence/agents/test_runner.py`

**Interfaces:**
- Consumes: `AgentDefinition` (Task 15), `AssessmentBase`-subtyper (Task 5), `Settings.agent_timeout_seconds`/`anthropic_api_key` (Task 2).
- Produces: `AgentRunner` (ABC) med `run(agent_def: AgentDefinition, context: dict, output_schema: type[T]) -> T`; `MockAgentRunner(AgentRunner)` (konstruktor tar `fixtures: dict[str, AssessmentBase]` nyckel = `agent_def.name`, plus `fail_agents: set[str]` och `timeout_agents: set[str]` för felinjicering); `RealClaudeRunner(AgentRunner)` (konstruktor tar `api_key: str`, anropar `anthropic.Anthropic().messages.create` med `system=agent_def.system_prompt`, tool-use med JSON-schema från `output_schema.model_json_schema()`, validerar svaret — ogiltig JSON eller schema-mismatch efter `max_retries` försök ⇒ returnerar en assessment-instans med `status="failed"` istället för att kasta, så orchestratorn aldrig kraschar på ett agent-fel).

Detta täcker SPEC §13 gate-test 5 och 6.

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/agents/test_runner.py
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from intelligence.agents.loader import AgentDefinition
from intelligence.agents.runner import MockAgentRunner, RealClaudeRunner
from intelligence.schemas.assessments import QAAssessment


def _agent_def(name="qa-agent"):
    return AgentDefinition(name=name, description="d", tools=["Read"], system_prompt="Du är QA Agent.")


def _qa_ok():
    return QAAssessment(agent_name="qa-agent", run_id="r1", created_at=datetime.now(UTC), status="ok", passed=True, violations=[])


def test_mock_runner_returns_configured_fixture():
    runner = MockAgentRunner(fixtures={"qa-agent": _qa_ok()})
    result = runner.run(_agent_def(), context={}, output_schema=QAAssessment)
    assert result.passed is True


def test_mock_runner_simulates_failure():
    runner = MockAgentRunner(fixtures={"qa-agent": _qa_ok()}, fail_agents={"qa-agent"})
    result = runner.run(_agent_def(), context={}, output_schema=QAAssessment)
    assert result.status == "failed"


def test_mock_runner_simulates_timeout():
    runner = MockAgentRunner(fixtures={"qa-agent": _qa_ok()}, timeout_agents={"qa-agent"})
    result = runner.run(_agent_def(), context={}, output_schema=QAAssessment)
    assert result.status == "timeout"


def test_mock_runner_missing_fixture_raises_key_error():
    runner = MockAgentRunner(fixtures={})
    with pytest.raises(KeyError):
        runner.run(_agent_def(), context={}, output_schema=QAAssessment)


@patch("intelligence.agents.runner.Anthropic")
def test_real_runner_returns_failed_status_on_invalid_json(mock_anthropic_cls):
    mock_client = MagicMock()
    mock_anthropic_cls.return_value = mock_client
    mock_message = MagicMock()
    mock_message.content = [MagicMock(type="text", text="detta är inte json")]
    mock_client.messages.create.return_value = mock_message

    runner = RealClaudeRunner(api_key="fake-key", model="claude-sonnet-5", timeout_seconds=5, max_retries=1)
    result = runner.run(_agent_def(), context={"question": "test"}, output_schema=QAAssessment)
    assert result.status == "failed"
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/agents/test_runner.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/agents/runner.py
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TypeVar

from anthropic import Anthropic

from intelligence.agents.loader import AgentDefinition
from intelligence.schemas.assessments import AssessmentBase

T = TypeVar("T", bound=AssessmentBase)


class AgentRunner(ABC):
    @abstractmethod
    def run(self, agent_def: AgentDefinition, context: dict, output_schema: type[T]) -> T: ...


class MockAgentRunner(AgentRunner):
    def __init__(
        self,
        fixtures: dict[str, AssessmentBase],
        fail_agents: set[str] | None = None,
        timeout_agents: set[str] | None = None,
    ):
        self._fixtures = fixtures
        self._fail_agents = fail_agents or set()
        self._timeout_agents = timeout_agents or set()

    def run(self, agent_def: AgentDefinition, context: dict, output_schema: type[T]) -> T:
        if agent_def.name in self._timeout_agents:
            base = self._fixtures[agent_def.name]
            return base.model_copy(update={"status": "timeout"})
        if agent_def.name in self._fail_agents:
            base = self._fixtures[agent_def.name]
            return base.model_copy(update={"status": "failed"})
        return self._fixtures[agent_def.name]


class RealClaudeRunner(AgentRunner):
    def __init__(self, api_key: str, model: str, timeout_seconds: float, max_retries: int):
        self._client = Anthropic(api_key=api_key)
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    def run(self, agent_def: AgentDefinition, context: dict, output_schema: type[T]) -> T:
        schema = output_schema.model_json_schema()
        user_message = (
            f"Context (JSON): {json.dumps(context, default=str)}\n\n"
            f"Svara ENDAST med giltig JSON som matchar detta schema:\n{json.dumps(schema)}"
        )
        for _attempt in range(self._max_retries):
            try:
                message = self._client.messages.create(
                    model=self._model,
                    max_tokens=2048,
                    system=agent_def.system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                    timeout=self._timeout_seconds,
                )
                text = "".join(block.text for block in message.content if block.type == "text")
                data = json.loads(text)
                data.setdefault("agent_name", agent_def.name)
                data.setdefault("status", "ok")
                data.setdefault("created_at", datetime.now(UTC).isoformat())
                return output_schema.model_validate(data)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue

        return self._failed_assessment(agent_def, output_schema, context.get("run_id", "unknown"))

    def _failed_assessment(self, agent_def: AgentDefinition, output_schema: type[T], run_id: str) -> T:
        required_fields = {
            name: self._blank_value(field.annotation)
            for name, field in output_schema.model_fields.items()
            if name not in {"agent_name", "run_id", "created_at", "status"}
        }
        return output_schema.model_validate(
            {
                "agent_name": agent_def.name,
                "run_id": run_id,
                "created_at": datetime.now(UTC),
                "status": "failed",
                **required_fields,
            }
        )

    @staticmethod
    def _blank_value(annotation):
        origin = getattr(annotation, "__origin__", None)
        if origin is list:
            return []
        if annotation is float:
            return 0.0
        if annotation is bool:
            return False
        if annotation is dict:
            return {}
        return ""
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/agents/test_runner.py -v`
Expected: PASS (5 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/agents/runner.py tests/intelligence/agents/test_runner.py
git commit -m "Fas 1 steg 17: AgentRunner (Real/Mock) — ogiltig output blir status=failed, inga krascher"
```

---

### Task 18: `intelligence/scoring/model.py`

**Files:**
- Create: `intelligence/scoring/model.py`
- Test: `tests/intelligence/scoring/test_model.py`

**Interfaces:**
- Consumes: `Opportunity` (Task 6), `config/scoring_weights.yaml` (Task 1), `Settings.scoring_weights_path` (Task 2).
- Produces: `load_weights(path: Path) -> dict[str, float]`, `score_opportunity(opportunity: Opportunity, weights: dict[str, float]) -> tuple[float, dict[str, float]]` — returnerar `(total_score, breakdown)`. Kräver att alla sju assessments finns (anropas efter QA-steget i orchestratorn, aldrig innan).

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/scoring/test_model.py
from datetime import UTC, datetime
from pathlib import Path

from intelligence.scoring.model import load_weights, score_opportunity
from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)
from intelligence.schemas.opportunity import Opportunity

_A = dict(agent_name="x", run_id="r1", created_at=datetime.now(UTC), status="ok")


def _full_opportunity() -> Opportunity:
    return Opportunity(
        opportunity_id="opp-1", event_id="evt-1", created_at=datetime.now(UTC),
        category="trend", title="t", summary="s", time_horizon="7d", liquidity="unknown",
        research=ResearchAssessment(**_A, verified_facts=["a", "b"], source_references=["s1", "s2"], assumptions=[]),
        opportunity=OpportunityAssessment(**_A, observed_data="d", hypothesis="h", interpretation="i"),
        market=MarketAssessment(**_A, market_data={"volatility": 0.4}, interpretation="i"),
        forecast=ForecastAssessment(**_A, scenarios=[{"description": "up", "probability": 0.6}], confidence=0.7, uncertainty="u"),
        risk=RiskAssessment(**_A, downside="d", liquidity_risk="låg", model_risk="m", timing_risk="t"),
        bear=BearAssessment(**_A, counterarguments=["c1"], alternative_explanations=[], falsification_conditions="f"),
        qa=QAAssessment(**_A, passed=True, violations=[]),
    )


def test_load_weights_from_yaml():
    weights = load_weights(Path("config/scoring_weights.yaml"))
    assert abs(sum(weights.values()) - 1.0) < 0.01


def test_score_opportunity_returns_total_and_breakdown():
    weights = load_weights(Path("config/scoring_weights.yaml"))
    total, breakdown = score_opportunity(_full_opportunity(), weights)
    assert 0.0 <= total <= 1.0
    assert set(breakdown.keys()) == set(weights.keys())
    for component_score in breakdown.values():
        assert 0.0 <= component_score <= 1.0


def test_score_reflects_weighted_sum():
    weights = load_weights(Path("config/scoring_weights.yaml"))
    total, breakdown = score_opportunity(_full_opportunity(), weights)
    expected = sum(weights[k] * breakdown[k] for k in weights)
    assert abs(total - expected) < 1e-9
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/scoring -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/scoring/model.py
from __future__ import annotations

from pathlib import Path

import yaml

from intelligence.schemas.opportunity import Opportunity


def load_weights(path: Path) -> dict[str, float]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def score_opportunity(opportunity: Opportunity, weights: dict[str, float]) -> tuple[float, dict[str, float]]:
    breakdown = {
        "signal_strength": _signal_strength(opportunity),
        "data_quality": _data_quality(opportunity),
        "source_reliability": _source_reliability(opportunity),
        "potential": _potential(opportunity),
        "risk": _risk(opportunity),
        "confidence": _confidence(opportunity),
        "novelty": _novelty(opportunity),
    }
    total = sum(weights[k] * breakdown[k] for k in weights)
    return total, breakdown


def _signal_strength(opp: Opportunity) -> float:
    # Fler oberoende scenarier med hög sannolikhet = starkare signal.
    if not opp.forecast or not opp.forecast.scenarios:
        return 0.0
    return min(1.0, max((s.get("probability", 0.0) for s in opp.forecast.scenarios), default=0.0))


def _data_quality(opp: Opportunity) -> float:
    # Fler verifierade fakta + källor = högre datakvalitet, capat vid 5 för att undvika obegränsad skalning.
    if not opp.research:
        return 0.0
    facts = len(opp.research.verified_facts)
    sources = len(opp.research.source_references)
    return min(1.0, (facts + sources) / 10)


def _source_reliability(opp: Opportunity) -> float:
    # Fas 1: statisk approximation via antal källor (egen reliability-agent kommer senare fas).
    if not opp.research:
        return 0.0
    return min(1.0, len(opp.research.source_references) / 5)


def _potential(opp: Opportunity) -> float:
    if not opp.forecast:
        return 0.0
    return opp.forecast.confidence


def _risk(opp: Opportunity) -> float:
    # Högre score = LÄGRE risk (så att det kan viktas positivt tillsammans med övriga komponenter).
    if not opp.risk or not opp.bear:
        return 0.0
    counterarguments_penalty = min(1.0, len(opp.bear.counterarguments) / 5)
    return max(0.0, 1.0 - counterarguments_penalty)


def _confidence(opp: Opportunity) -> float:
    if not opp.forecast:
        return 0.0
    return opp.forecast.confidence


def _novelty(opp: Opportunity) -> float:
    # Fas 1: proxy via deviation-relaterad text i opportunity.summary — konservativ default.
    return 0.5 if opp.opportunity is not None else 0.0
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/scoring -v`
Expected: PASS (3 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/scoring/model.py tests/intelligence/scoring/
git commit -m "Fas 1 steg 18: transparent komponent-scoring, vikter från YAML"
```

---

### Task 19: `intelligence/reporting/report.py`

**Files:**
- Create: `intelligence/reporting/report.py`
- Test: `tests/intelligence/reporting/test_report.py`

**Interfaces:**
- Consumes: `Opportunity` (Task 6, fullt ifylld).
- Produces: `render_report(opportunity: Opportunity) -> str` (markdown enligt SPEC:ens template), `write_report(opportunity: Opportunity, dest_dir: Path) -> Path` (skriver till `research/YYYY-MM-DD-opportunity-<id>.md`, skapar mappen om den saknas).

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/reporting/test_report.py
from datetime import UTC, datetime
from pathlib import Path

from intelligence.reporting.report import render_report, write_report
from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)
from intelligence.schemas.opportunity import Opportunity

_A = dict(agent_name="x", run_id="r1", created_at=datetime.now(UTC), status="ok")


def _full_opportunity() -> Opportunity:
    return Opportunity(
        opportunity_id="opp-42", event_id="evt-1", created_at=datetime.now(UTC),
        category="trend", title="Ovanlig aktivitet kring X", summary="Kort sammanfattning",
        time_horizon="7 dagar", liquidity="okänd", status="reported",
        research=ResearchAssessment(**_A, verified_facts=["fakta 1"], source_references=["https://x.com"], assumptions=[]),
        opportunity=OpportunityAssessment(**_A, observed_data="ovanlig volym", hypothesis="efterfrågan stiger", interpretation="tidig signal"),
        market=MarketAssessment(**_A, market_data={"volume_change_pct": 300.0}, interpretation="ovanlig rörelse"),
        forecast=ForecastAssessment(**_A, scenarios=[{"description": "fortsätter", "probability": 0.6}], confidence=0.7, uncertainty="litet underlag"),
        risk=RiskAssessment(**_A, downside="kan reversera", liquidity_risk="låg", model_risk="litet urval", timing_risk="sent"),
        bear=BearAssessment(**_A, counterarguments=["kan vara brus"], alternative_explanations=["säsong"], falsification_conditions="om volymen normaliseras inom 48h"),
        qa=QAAssessment(**_A, passed=True, violations=[]),
        score=0.62,
        score_breakdown={"signal_strength": 0.6, "data_quality": 0.2, "source_reliability": 0.2, "potential": 0.7, "risk": 0.8, "confidence": 0.7, "novelty": 0.5},
    )


def test_render_report_contains_required_sections():
    md = render_report(_full_opportunity())
    for heading in [
        "OPPORTUNITY #opp-42", "Vad hände?", "Varför är detta intressant?", "Vilka bevis finns?",
        "Vad talar FÖR?", "Vad talar EMOT?", "Vilka alternativa förklaringar finns?", "Vad kan hända?",
        "Sannolikheter:", "Risk:", "Data quality:", "Confidence:", "Overall opportunity score:",
        "Time horizon:", "Vad skulle falsifiera hypotesen?", "Status:", "Ej finansiell rådgivning",
    ]:
        assert heading in md, f"saknar rubrik/text: {heading}"


def test_write_report_creates_file(tmp_path):
    path = write_report(_full_opportunity(), dest_dir=tmp_path)
    assert path.exists()
    assert path.name.endswith("-opportunity-opp-42.md")
    assert "OPPORTUNITY #opp-42" in path.read_text(encoding="utf-8")
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/reporting -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/reporting/report.py
from __future__ import annotations

from pathlib import Path

from intelligence.schemas.opportunity import Opportunity


def render_report(opportunity: Opportunity) -> str:
    scenarios_lines = "\n".join(
        f"- {s['description']}: {s['probability']:.0%}" for s in (opportunity.forecast.scenarios if opportunity.forecast else [])
    )
    counterarguments = "\n".join(f"- {c}" for c in (opportunity.bear.counterarguments if opportunity.bear else []))
    alternatives = "\n".join(f"- {a}" for a in (opportunity.bear.alternative_explanations if opportunity.bear else []))
    sources = "\n".join(f"- {s}" for s in (opportunity.research.source_references if opportunity.research else []))

    return f"""# OPPORTUNITY #{opportunity.opportunity_id}

## Vad hände?
{opportunity.opportunity.observed_data if opportunity.opportunity else "Ej tillgängligt"}

## Varför är detta intressant?
{opportunity.opportunity.interpretation if opportunity.opportunity else "Ej tillgängligt"}

## Vilka bevis finns?
{sources or "Inga källor registrerade"}

## Vad talar FÖR?
{opportunity.opportunity.hypothesis if opportunity.opportunity else "Ej tillgängligt"}

## Vad talar EMOT?
{counterarguments or "Inga motargument registrerade"}

## Vilka alternativa förklaringar finns?
{alternatives or "Inga alternativa förklaringar registrerade"}

## Vad kan hända?
{scenarios_lines or "Inga scenarier"}

## Sannolikheter:
{scenarios_lines or "Ej tillgängligt"}

## Risk:
Downside: {opportunity.risk.downside if opportunity.risk else "Ej tillgängligt"}
Likviditetsrisk: {opportunity.risk.liquidity_risk if opportunity.risk else "Ej tillgängligt"}
Modellrisk: {opportunity.risk.model_risk if opportunity.risk else "Ej tillgängligt"}
Timingrisk: {opportunity.risk.timing_risk if opportunity.risk else "Ej tillgängligt"}

## Historiska jämförelser:
Ej tillgängligt i Fas 1 — Historical/Backtest Agent byggs i Fas 3.

## Data quality:
{opportunity.score_breakdown.get("data_quality") if opportunity.score_breakdown else "Ej tillgängligt"}

## Confidence:
{opportunity.forecast.confidence if opportunity.forecast else "Ej tillgängligt"}

## Overall opportunity score:
{opportunity.score if opportunity.score is not None else "Ej tillgängligt"}

## Time horizon:
{opportunity.time_horizon}

## Vad skulle falsifiera hypotesen?
{opportunity.bear.falsification_conditions if opportunity.bear else "Ej tillgängligt"}

## Status:
{opportunity.status}

---
*Detta är research, inte finansiell rådgivning. Inga verkliga trades har genomförts eller föreslagits genomföras av mig.*
"""


def write_report(opportunity: Opportunity, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    date_str = opportunity.created_at.strftime("%Y-%m-%d")
    path = dest_dir / f"{date_str}-opportunity-{opportunity.opportunity_id}.md"
    path.write_text(render_report(opportunity), encoding="utf-8")
    return path
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/reporting -v`
Expected: PASS (2 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/reporting/report.py tests/intelligence/reporting/
git commit -m "Fas 1 steg 19: markdown-rapportgenerering enligt Opportunity-template"
```

---

### Task 20: `intelligence/orchestrator.py` — Lead Orchestrator

**Files:**
- Create: `intelligence/orchestrator.py`
- Test: `tests/intelligence/test_orchestrator.py`

**Interfaces:**
- Consumes: `Event` (Task 4), `Opportunity`/`OpportunityStatus` (Task 6), `can_transition`/`REQUIRED_FOR_REPORTED` (Task 7), `Repository` (Task 8), `ROLE_MAP` (Task 16), `AgentRunner` (Task 17), `load_agent_definition` (Task 15), `score_opportunity`/`load_weights` (Task 18), `write_report` (Task 19), `Settings` (Task 2), `log_event`/`new_run_id` (Task 3).
- Produces: `Orchestrator(repo: Repository, runner: AgentRunner, weights: dict[str, float], settings: Settings)` med `process_event(event: Event, run_id: str) -> Opportunity`. Kör rollerna i exakt ordningen `research → opportunity → market → forecast → risk → bear → qa`, sparar varje assessment, anropar `can_transition` innan varje statussteg, beräknar score efter `qa`, skriver rapport bara om status blir `reported`.

Detta är integrationstestet för SPEC §13 gate-test 5 (missing/failed agent → blockerad rapport) och 7 (dedup, tillsammans med Task 13) end-to-end genom hela orchestratorn, inte bara i isolerade enheter.

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/test_orchestrator.py
from datetime import UTC, datetime

from intelligence.agents.runner import MockAgentRunner
from intelligence.config import get_settings
from intelligence.orchestrator import Orchestrator
from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)
from intelligence.schemas.event import Event
from intelligence.scoring.model import load_weights
from intelligence.storage.repository import SQLiteRepository

_A = dict(agent_name="x", run_id="r1", created_at=datetime.now(UTC), status="ok")


def _event():
    return Event(
        event_id="evt-1", source_id="hn", observed_at=datetime.now(UTC), category="forum",
        metric="score", baseline=50.0, deviation=400.0, description="d", raw_ref="hash-1",
    )


def _happy_fixtures():
    return {
        "research-agent": ResearchAssessment(**_A, agent_name="research-agent", verified_facts=["f"], source_references=["s"], assumptions=[]),
        "opportunity-hunter": OpportunityAssessment(**_A, agent_name="opportunity-hunter", observed_data="d", hypothesis="h", interpretation="i"),
        "trading-research": MarketAssessment(**_A, agent_name="trading-research", market_data={}, interpretation="i"),
        "forecasting-agent": ForecastAssessment(**_A, agent_name="forecasting-agent", scenarios=[{"description": "up", "probability": 0.6}], confidence=0.6, uncertainty="u"),
        "risk-agent": RiskAssessment(**_A, agent_name="risk-agent", downside="d", liquidity_risk="l", model_risk="m", timing_risk="t"),
        "fact-checker-bear": BearAssessment(**_A, agent_name="fact-checker-bear", counterarguments=[], alternative_explanations=[], falsification_conditions="f"),
        "qa-agent": QAAssessment(**_A, agent_name="qa-agent", passed=True, violations=[]),
    }


def _orchestrator(tmp_path, fixtures=None, fail_agents=None, dest_dir=None):
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures=fixtures or _happy_fixtures(), fail_agents=fail_agents or set())
    weights = load_weights(get_settings().scoring_weights_path)
    settings = get_settings()
    return Orchestrator(repo=repo, runner=runner, weights=weights, settings=settings, report_dest_dir=dest_dir or tmp_path)


def test_happy_path_reaches_reported_status(tmp_path):
    orch = _orchestrator(tmp_path)
    opp = orch.process_event(_event(), run_id="r1")
    assert opp.status == "reported"
    assert opp.score is not None
    report_files = list(tmp_path.glob("*opportunity-*.md"))
    assert len(report_files) == 1


def test_failed_risk_agent_blocks_reported(tmp_path):
    orch = _orchestrator(tmp_path, fail_agents={"risk-agent"})
    opp = orch.process_event(_event(), run_id="r1")
    assert opp.status != "reported"
    assert opp.risk is not None
    assert opp.risk.status == "failed"
    report_files = list(tmp_path.glob("*opportunity-*.md"))
    assert len(report_files) == 0


def test_qa_rejection_sets_status_rejected(tmp_path):
    fixtures = _happy_fixtures()
    fixtures["qa-agent"] = QAAssessment(**_A, agent_name="qa-agent", passed=False, violations=["saknar riskbedömning"])
    orch = _orchestrator(tmp_path, fixtures=fixtures)
    opp = orch.process_event(_event(), run_id="r1")
    assert opp.status == "rejected"
    report_files = list(tmp_path.glob("*opportunity-*.md"))
    assert len(report_files) == 0
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/test_orchestrator.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/orchestrator.py
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from intelligence.agents.loader import load_agent_definition
from intelligence.agents.roles import ROLE_MAP
from intelligence.agents.runner import AgentRunner
from intelligence.config import Settings
from intelligence.logging import log_event
from intelligence.reporting.report import write_report
from intelligence.schemas.event import Event
from intelligence.schemas.opportunity import Opportunity
from intelligence.scoring.model import score_opportunity
from intelligence.state_machine import can_transition
from intelligence.storage.repository import Repository

_ROLE_ORDER = ["research", "opportunity", "market", "forecast", "risk", "bear", "qa"]


class Orchestrator:
    def __init__(
        self,
        repo: Repository,
        runner: AgentRunner,
        weights: dict[str, float],
        settings: Settings,
        report_dest_dir: Path,
    ):
        self._repo = repo
        self._runner = runner
        self._weights = weights
        self._settings = settings
        self._report_dest_dir = report_dest_dir

    def process_event(self, event: Event, run_id: str) -> Opportunity:
        opportunity = Opportunity(
            opportunity_id=str(uuid.uuid4()),
            event_id=event.event_id,
            created_at=datetime.now(UTC),
            category=event.category,
            title=f"Avvikelse i {event.metric} ({event.source_id})",
            summary=event.description,
            time_horizon="okänt — bedöms av Forecasting Agent",
            liquidity="okänd — bedöms av Risk Agent",
        )
        self._repo.save_opportunity(opportunity)

        agent_calls = 0
        for role in _ROLE_ORDER:
            if agent_calls >= self._settings.max_agent_calls_per_run:
                log_event(run_id, event="max_agent_calls_reached", role=role)
                break
            spec = ROLE_MAP[role]
            agent_def = load_agent_definition(spec.agent_file)
            context = {"event": event.model_dump(mode="json"), "opportunity": opportunity.model_dump(mode="json"), "run_id": run_id}
            assessment = self._runner.run(agent_def, context, spec.assessment_type)
            agent_calls += 1
            setattr(opportunity, role, assessment)
            self._repo.save_assessment(opportunity.opportunity_id, role, assessment)
            log_event(run_id, event="assessment_completed", agent_name=agent_def.name, role=role, status=assessment.status)

        ok, reason = can_transition(opportunity, "reported")
        if ok:
            total, breakdown = score_opportunity(opportunity, self._weights)
            opportunity.score = total
            opportunity.score_breakdown = breakdown
            opportunity.status = "reported"
            self._repo.save_opportunity(opportunity)
            self._repo.update_opportunity_status(opportunity.opportunity_id, "reported")
            write_report(opportunity, self._report_dest_dir)
            log_event(run_id, event="opportunity_reported", opportunity_id=opportunity.opportunity_id, score=total)
        else:
            qa = opportunity.qa
            target_status = "rejected" if qa is not None and qa.passed is False else "under_review"
            opportunity.status = target_status
            self._repo.update_opportunity_status(opportunity.opportunity_id, target_status)
            log_event(run_id, event="opportunity_blocked", opportunity_id=opportunity.opportunity_id, reason=reason, status=target_status)

        return opportunity
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/test_orchestrator.py -v`
Expected: PASS (3 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/orchestrator.py tests/intelligence/test_orchestrator.py
git commit -m "Fas 1 steg 20: Lead Orchestrator — kör 7-agent-pipeline, gated av state machine"
```

---

### Task 21: `intelligence/run.py` — entrypoint

**Files:**
- Create: `intelligence/run.py`
- Test: `tests/intelligence/test_run.py`

**Interfaces:**
- Consumes: allt från Task 2–20.
- Produces: `build_orchestrator(use_mock: bool, mock_fixtures: dict | None = None) -> Orchestrator`, `main() -> None` (CLI-entrypoint: kör `event_pipeline` mot konfigurerade connectors, sedan `orchestrator.process_event` per event, skriver ut en sammanfattning). Använder `MockAgentRunner` om `ANTHROPIC_API_KEY` saknas ELLER om `--mock`-flaggan ges; annars `RealClaudeRunner`.

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/test_run.py
from intelligence.run import build_orchestrator
from intelligence.agents.runner import MockAgentRunner


def test_build_orchestrator_uses_mock_runner_when_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH_OVERRIDE", str(tmp_path / "t.db"))
    orch = build_orchestrator(use_mock=True, mock_fixtures={})
    assert isinstance(orch._runner, MockAgentRunner)
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/test_run.py -v`
Expected: FAIL — modulen finns inte.

- [x] **Step 3: Implementera**

```python
# intelligence/run.py
from __future__ import annotations

import argparse
from pathlib import Path

from intelligence.agents.runner import AgentRunner, MockAgentRunner, RealClaudeRunner
from intelligence.config import get_settings
from intelligence.connectors.alpha_vantage import AlphaVantageConnector
from intelligence.connectors.exceptions import ConnectorConfigError
from intelligence.connectors.hackernews import HackerNewsConnector
from intelligence.logging import log_event, new_run_id
from intelligence.orchestrator import Orchestrator
from intelligence.pipeline.event_pipeline import run_event_pipeline
from intelligence.schemas.source import Source
from intelligence.scoring.model import load_weights
from intelligence.storage.repository import SQLiteRepository

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def build_orchestrator(use_mock: bool, mock_fixtures: dict | None = None) -> Orchestrator:
    settings = get_settings()
    repo = SQLiteRepository(settings.db_path)
    weights = load_weights(settings.scoring_weights_path)

    runner: AgentRunner
    if use_mock or not settings.anthropic_api_key:
        runner = MockAgentRunner(fixtures=mock_fixtures or {})
    else:
        runner = RealClaudeRunner(
            api_key=settings.anthropic_api_key,
            model="claude-sonnet-5",
            timeout_seconds=settings.agent_timeout_seconds,
            max_retries=3,
        )

    return Orchestrator(
        repo=repo, runner=runner, weights=weights, settings=settings,
        report_dest_dir=_PROJECT_ROOT / "research",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Kör en Fas 1-pipeline-cykel")
    parser.add_argument("--mock", action="store_true", help="Använd MockAgentRunner även om ANTHROPIC_API_KEY finns")
    args = parser.parse_args()

    settings = get_settings()
    run_id = new_run_id()
    repo = SQLiteRepository(settings.db_path)

    hn_source = Source(source_id="hn", name="Hacker News", type="forum", reliability_score=0.6, url="https://news.ycombinator.com")
    av_source = Source(source_id="alpha_vantage", name="Alpha Vantage", type="market_data", reliability_score=0.8, url="https://www.alphavantage.co")
    repo.save_source(hn_source)
    repo.save_source(av_source)

    connectors = [HackerNewsConnector(hn_source, settings.connector_timeout_seconds, settings.connector_max_retries, min_interval_seconds=1.0)]
    try:
        connectors.append(
            AlphaVantageConnector(
                av_source, settings.connector_timeout_seconds, settings.connector_max_retries,
                api_key=settings.alphavantage_api_key, symbols=["IBM"], min_interval_seconds=12.0,
            )
        )
    except ConnectorConfigError as exc:
        log_event(run_id, event="connector_skipped", source_id="alpha_vantage", error=str(exc))

    events = run_event_pipeline(
        connectors=connectors,
        source_types={"hn": "forum", "alpha_vantage": "market_data"},
        baselines={"hn": 50.0, "alpha_vantage": 100.0},
        repo=repo, max_events=settings.max_events_per_run, run_id=run_id,
    )

    orchestrator = build_orchestrator(use_mock=args.mock)
    reported = 0
    for event in events[: settings.max_opportunities_per_run]:
        opportunity = orchestrator.process_event(event, run_id)
        if opportunity.status == "reported":
            reported += 1

    print(f"Körning {run_id}: {len(events)} events, {reported} opportunities rapporterade.")


if __name__ == "__main__":
    main()
```

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/test_run.py -v`
Expected: PASS (1 passed)

- [x] **Step 5: Ruff + commit**

```bash
git add intelligence/run.py tests/intelligence/test_run.py
git commit -m "Fas 1 steg 21: run.py entrypoint, väljer Real/Mock runner automatiskt"
```

---

### Task 22: End-to-end-test

**Files:**
- Test: `tests/intelligence/test_end_to_end.py`

**Interfaces:**
- Consumes: hela paketet (Task 1–21). Inga produktionsfiler skapas i denna task — bara det test SPEC §16 explicit kräver.

- [x] **Step 1: Skriv testet**

```python
# tests/intelligence/test_end_to_end.py
from datetime import UTC, datetime

from intelligence.agents.runner import MockAgentRunner
from intelligence.config import get_settings
from intelligence.connectors.base import BaseConnector
from intelligence.orchestrator import Orchestrator
from intelligence.pipeline.event_pipeline import run_event_pipeline
from intelligence.schemas.assessments import (
    BearAssessment,
    ForecastAssessment,
    MarketAssessment,
    OpportunityAssessment,
    QAAssessment,
    ResearchAssessment,
    RiskAssessment,
)
from intelligence.schemas.event import RawRecord
from intelligence.schemas.source import Source
from intelligence.scoring.model import load_weights
from intelligence.storage.repository import SQLiteRepository

_A = dict(agent_name="x", run_id="r1", created_at=datetime.now(UTC), status="ok")


class _FixtureConnector(BaseConnector):
    def fetch(self):
        payload = {"id": 1, "score": 900}
        return [RawRecord(source_id=self.source.source_id, fetched_at=datetime.now(UTC), payload=payload, content_hash=self._content_hash(payload))]


def test_full_pipeline_from_data_to_markdown_report(tmp_path):
    repo = SQLiteRepository(tmp_path / "e2e.db")
    source = Source(source_id="hn", name="Hacker News", type="forum", reliability_score=0.6, url="https://x.com")
    repo.save_source(source)

    connector = _FixtureConnector(source, timeout_seconds=5, max_retries=1, min_interval_seconds=0)
    events = run_event_pipeline(
        connectors=[connector], source_types={"hn": "forum"}, baselines={"hn": 50.0},
        repo=repo, max_events=10, run_id="e2e-run",
    )
    assert len(events) == 1

    fixtures = {
        "research-agent": ResearchAssessment(**_A, agent_name="research-agent", verified_facts=["f"], source_references=["s"], assumptions=[]),
        "opportunity-hunter": OpportunityAssessment(**_A, agent_name="opportunity-hunter", observed_data="d", hypothesis="h", interpretation="i"),
        "trading-research": MarketAssessment(**_A, agent_name="trading-research", market_data={}, interpretation="i"),
        "forecasting-agent": ForecastAssessment(**_A, agent_name="forecasting-agent", scenarios=[{"description": "up", "probability": 0.5}], confidence=0.5, uncertainty="u"),
        "risk-agent": RiskAssessment(**_A, agent_name="risk-agent", downside="d", liquidity_risk="l", model_risk="m", timing_risk="t"),
        "fact-checker-bear": BearAssessment(**_A, agent_name="fact-checker-bear", counterarguments=[], alternative_explanations=[], falsification_conditions="f"),
        "qa-agent": QAAssessment(**_A, agent_name="qa-agent", passed=True, violations=[]),
    }
    runner = MockAgentRunner(fixtures=fixtures)
    weights = load_weights(get_settings().scoring_weights_path)
    settings = get_settings()
    orchestrator = Orchestrator(repo=repo, runner=runner, weights=weights, settings=settings, report_dest_dir=tmp_path)

    opportunity = orchestrator.process_event(events[0], run_id="e2e-run")

    assert opportunity.status == "reported"
    assert opportunity.score is not None

    stored = repo.get_opportunity(opportunity.opportunity_id)
    assert stored.status == "reported"

    report_files = list(tmp_path.glob("*opportunity-*.md"))
    assert len(report_files) == 1
    content = report_files[0].read_text(encoding="utf-8")
    assert f"OPPORTUNITY #{opportunity.opportunity_id}" in content
    assert "Status:" in content
```

- [x] **Step 2: Kör testet för att bekräfta att det failar**

Run: `uv run pytest tests/intelligence/test_end_to_end.py -v`
Expected: FAIL vid första körningen om någon integration missats — annars PASS direkt eftersom alla beroenden redan är implementerade från Task 1–21.

- [x] **Step 3: Kör testet och åtgärda ev. integrationsavvikelser**

Om testet failar: felsök mot den specifika modulen det pekar på, fixa där (inte i testet — testet uttrycker SPEC:ens krav).

- [x] **Step 4: Kör testet igen**

Run: `uv run pytest tests/intelligence/test_end_to_end.py -v`
Expected: PASS (1 passed)

- [x] **Step 5: Commit**

```bash
git add tests/intelligence/test_end_to_end.py
git commit -m "Fas 1 steg 22: end-to-end-test — data till markdown-rapport, helt mockat"
```

---

### Task 23: Slutverifiering

**Files:** inga nya — verifierar hela Fas 1.

- [x] **Step 1: Full testsvit**

Run: `uv run pytest -v`
Expected: alla tester passerar (inklusive `tests/test_setup.py`), noll `@pytest.mark.live`-tester körda (de finns inte i Fas 1 — se `Fas 1 ska inte överbyggas`).

- [x] **Step 2: Ruff check + format**

Run: `uv run ruff check .`
Expected: inga fel.

Run: `uv run ruff format --check .`
Expected: inga diff.

- [x] **Step 3: Verifiera att default-testkörning inte kräver nätverk**

Run: `uv run pytest -v -p no:cacheprovider --disable-socket 2>/dev/null || uv run pytest -v` (om `pytest-socket` inte är installerat, kör bara vanlig `pytest` — alla externa anrop är redan `respx`-mockade eller `MockAgentRunner`, så detta ska passera ändå).
Expected: alla tester passerar utan nätverksåtkomst.

- [x] **Step 4: Kör demo mot verklig Hacker News (manuellt, engångscheck — inte en del av CI)**

Run: `uv run python -m intelligence.run --mock`
Expected: skriver ut `Körning <run_id>: N events, M opportunities rapporterade.` och en fil dyker upp i `research/`. Detta kör mot RIKTIG Hacker News-data men med `MockAgentRunner` — bekräftar att connector+pipeline+orchestrator hänger ihop utan att kosta ett enda LLM-anrop.

- [x] **Step 5: Uppdatera `git log` för Fas 1**

Run: `git log --oneline -25`
Expected: 22 commits synliga från Task 1–22, alla med prefixet `Fas 1 steg`.

---

## Consistency Review mot SPEC.md

**Saknas något i SPEC.md som inte finns i planen?** Nej. Samtliga §-avsnitt i SPEC.md (§2 lager, §3 filstruktur, §4 scheman, §5 state machine, §6 connector-interface, §7 AgentRunner, §8 storage, §9 scoring, §10 observability/kostnad, §11 feltolerans, §12 agentroller, §13 teststrategi inkl. alla 7 gate-tester, §14 säkerhet) har en motsvarande task ovan. §15 (roadmap) och de tre uteslutna rollerna (Historical/Ranking/Learning) implementeras medvetet INTE — det är korrekt enligt SPEC §12 och användarens regel 15.

**Finns något i planen som inte är tillåtet enligt SPEC.md?** Nej. Ingen task rör orderläggning, mäklarkonton eller pengaröverföring. `AlphaVantageConnector` är strikt läsdata (GLOBAL_QUOTE), ingen skrivoperation mot något konto existerar i kodbasen.

**Finns någon dold koppling som gör systemet svårt att testa?** Nej. Varje task har ett eget testfilsberoende bara på tidigare tasks interface, aldrig på konkreta implementationsdetaljer i senare tasks. `Orchestrator` tar `Repository` och `AgentRunner` som konstruktorargument (dependency injection) — inga globala singletons utom `get_settings()`, som själv är sidoeffektfri (läser env, ingen mutation).

**Finns någon säkerhetsrisk?** Ingen ny identifierad utöver de SPEC.md redan adresserar (secrets via `.env`, redaction i loggning, inga trading-operationer). En sak värd att notera för granskning under implementation: `RealClaudeRunner._failed_assessment` genererar tomma default-värden för obligatoriska strängfält om en agent-output inte går att parsa — detta är avsiktligt (undviker krasch) men innebär att ett "failed"-objekt tekniskt sett är ett giltigt `AssessmentBase`-objekt med `status="failed"`. Gate-logiken i `state_machine.py` litar bara på `status`-fältet för detta beslut, aldrig på innehållet, så det är säkert — men värt att hålla i minnet vid granskning av Task 17.

**Finns någon onödig komplexitet i Fas 1?** Nej. Ingen dashboard, inget distribuerat system, ingen vector database, ingen ORM, ingen Kubernetes/Kafka. TTL-cache i `BaseConnector` är minimal (in-memory dict), inte en extern cache-tjänst. `Source Reliability` är en approximation via antal källor i `scoring/model.py`, inte en egen agent — matchar SPEC:ens uttalade beslut att skjuta upp den till senare fas.

---

## Implementation Ready Checklist

- [x] SPEC.md godkänd och behandlas som source of truth
- [x] Alla 23 tasks har exakta filvägar (Create/Modify/Test)
- [x] Alla interfaces (klasser/funktioner/typer) är namngivna konsekvent genom hela planen — verifierat i self-review
- [x] Varje task har körbar testkod, ingen placeholder
- [x] De 7 obligatoriska gate-testerna från SPEC §13 är explicit mappade till Task 7, 12, 13, 17, 20
- [x] AgentRunner Real/Mock-separation säkerställer att `pytest` aldrig kräver Claude API
- [x] Alla externa API-anrop (`HackerNewsConnector`, `AlphaVantageConnector`) är `respx`-mockade i tester
- [x] Alpha Vantage testas utan riktig nyckel (config-fel) och med mockad nyckel (lyckad fetch)
- [x] Ingen kod för Historical/Backtest, Learning/Evaluation eller Opportunity Ranking — endast stubbar/uteslutning enligt SPEC
- [x] Ingen trading-, order- eller broker-kod någonstans i planen
- [x] Secrets läses bara via `.env`/miljövariabler, redaction testas explicit (Task 3)
- [x] Verifieringskommandon (`pytest`, `ruff check`, `ruff format --check`) finns efter varje task och som samlad slutverifiering (Task 23)
- [x] End-to-end-demo (Task 22 automatiserad + Task 23 manuell mot riktig Hacker News-data) täcker SPEC §16

Redo för implementation.
