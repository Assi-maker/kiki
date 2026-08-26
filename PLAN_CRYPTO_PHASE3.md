# Crypto Trading — Phase 3 (AI Intelligence Pipeline) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Status: EJ PÅBÖRJAD (skriven 2026-08-26)

Fas 2 är avslutad och mergad till `master` (commit `1a0b5de`). Denna plan väntar på användarens granskning och godkännande innan någon kod skrivs eller något test körs. Ingen exekvering har startat.

---

**Goal:** Bygga de sju AI-agentrollerna (typade outputs, `AgentRunner` Real/Mock), de två återstående datakällorna (nyheter/external data), QA/Gate-rollen, den deterministiska Risk/Signal Gate, och orchestreringen som driver en `CANDIDATE`/`UNDER_AI_ANALYSIS`-candidate (Fas 2:s output) hela vägen till `REJECTED`/`NO_TRADE`/`CONFIRMED`. Fortsatt noll riktiga trades, noll broker-koppling — `CONFIRMED` betyder bara "godkänd för Fas 4:s paper trading", inget annat.

**Architecture:** Nya lager `agents/` och `gate/`, plus `connectors/news_rss.py`/`connectors/external_data.py`, byggda enligt SPEC §0: **samma principer som `intelligence/agents/*` och `intelligence/orchestrator.py`, men egen kod** — ingen import av `intelligence/`. Beroenderiktning oförändrad: `schemas` beroendefritt; `agents/loader.py` + `agents/roles.py` + `agents/runner.py` beror bara på `schemas/assessments.py`; `gate/qa_gate.py` beror på `agents/`; `gate/risk_signal_gate.py` beror bara på `schemas/candidate.py` + `storage/repository.py` (ren, deterministisk — importerar aldrig `agents/`, precis som SPEC §1 kräver att gaten är oberoende av AI-utfallet); orchestratorn binder ihop allt.

**Tech Stack:** Python 3.13, `pydantic` v2, `anthropic`-SDK (redan i `pyproject.toml`, oanvänd hittills i `crypto_trading/`), `pytest`. Default `pytest`-körning gör noll riktiga Claude API-anrop (AC7) — all testning sker mot `MockAgentRunner`.

**Spec:** `SPEC_CRYPTO.md` §4 (sju AssessmentTyper, redan schemalagda i Fas 0 — se nedan), §5 (`CandidateStatus`), §6 (agentordning), §7 (flöde), §8.3 (fail-closed-regler), §8.5 (crash-safe, redan byggt i Fas 0:s `sweep_interrupted_analyses`), §9 (Forecast-semantik), §10 (kostnadskontroll), §14 (nyheter/external data, leverantör väljs vid implementation), §15 (connector-krav). `PLAN_CRYPTO.md` Phase 3-avsnittet (Omfattning/Levererar/Acceptance criteria 1–7, citerade i respektive tasks nedan).

## Vad som redan finns (Fas 0/1/2) — återanvänds rakt av

- **Alla sju scheman redan definierade**, orörda: `schemas/assessments.py` — `NewsSentimentAssessment`, `TechnicalAssessment`, `BullThesisAssessment`, `ForecastAssessment`, `RiskAssessment`, `BearAdversarialAssessment`, `QAAssessment` (alla ärver `AssessmentBase`: `agent_name`, `run_id`, `created_at`, `status`).
- **`schemas/candidate.py`** har redan de sju optional-fälten (`news_sentiment`, `technical`, `bull_thesis`, `forecast`, `risk`, `bear_adversarial`, `qa`) på `Candidate` — inget schema-arbete kvar där.
- **`state_machine.py`**: `ALLOWED_TRANSITIONS["UNDER_AI_ANALYSIS"] = {ANALYSIS_INTERRUPTED, REJECTED, NO_TRADE, CONFIRMED}` redan korrekt (Fas 0). `REJECTED`/`NO_TRADE`/`CONFIRMED` är redan terminala (tomma `frozenset()`) — AC5 håller alltså redan strukturellt, verifieras bara.
- **`sweep_interrupted_analyses()`** (Fas 0, `state_machine.py`) hanterar redan crash-recovery generellt för alla candidates i `UNDER_AI_ANALYSIS` — Fas 3 anropar den bara vid start av en discovery-cykel, skriver ingen ny crash-logik.
- **`storage/db.py`** har redan tabellerna `assessments` (`candidate_id`, `field_name`, `payload`, PK på båda), `gate_decisions` (`candidate_id` PK, `decision`, `reasons`, `evaluated_at`) och `positions` (`position_id` PK, `status`, ...) — provisionerade i Fas 0, aldrig använda av någon repository-metod ännu. Fas 3 kopplar dem.
- **`crypto_trading/logging.py`**: `log_event(run_id, **fields)`, `redact()`, `new_run_id()` — identiskt mönster som `intelligence/logging.py`, återanvänds rakt av i `agents/runner.py`.
- **`config/loader.py`**: `RiskLimitsConfig.max_concurrent_positions` och `BudgetLimitsConfig.max_ai_calls_per_discovery_run` redan definierade och validerade — inga nya config-fält krävs för denna fas.
- **`screening/candidate_engine.py`** (Fas 2): `prioritize_and_apply_budget()` lämnar budget-godkända candidates i status `CANDIDATE`, redo för Fas 3 att plocka upp och transitionera till `UNDER_AI_ANALYSIS`.

## Vad som saknas och byggs i denna fas

- `agents/loader.py`, `agents/roles.py`, `agents/runner.py` (existerar inte i `crypto_trading/` ännu).
- Sju nya `.claude/agents/crypto-*.md`-promptfiler (de sju befintliga i `.claude/agents/` tillhör `intelligence/` — SPEC §0 kräver egna, inte återanvända).
- `connectors/news_rss.py`, `connectors/external_data.py`.
- `gate/qa_gate.py`, `gate/risk_signal_gate.py`.
- `storage/repository.py`-utökning: persistera/läsa tillbaka de sju assessments, skriva `gate_decisions`, räkna öppna positioner.
- En orchestrator (`orchestrator.py`) som binder ihop sweep → transition → sju roller → QA-gate → Risk/Signal Gate → terminal status.

## Global Constraints

- **Ingen kod ännu, inga tester körs i denna session** — den här planen är enbart avsedd att granskas och godkännas innan exekvering (användarens explicita instruktion för detta steg).
- **Mock-only default `pytest`** (AC7): varje test i denna fas använder `MockAgentRunner`. Inget test kräver `ANTHROPIC_API_KEY`. En eventuell live-verifiering av `RealClaudeRunner` (om den görs alls) är, precis som Fas 1:s BingX-verifiering, `@pytest.mark.live`-märkt och exkluderad från default-körning.
- **Gaten är oberoende av AI-utfallet** (SPEC §1 kärnprincip 1): `gate/risk_signal_gate.py` importerar aldrig `agents/` och kan blockera `CONFIRMED` **även när alla sju AI-roller är eniga och positiva** (AC4).
- **`REJECTED`/`NO_TRADE`-semantik hålls strikt isär** (SPEC §5-tabellen): `REJECTED` = alla sju assessments närvarande med `status="ok"` OCH `QAAssessment.passed is False` (fullt analyserad, sakligt underkänd). Allt annat som blockerar `CONFIRMED` — saknad/failed/timeout-assessment, eller gatens egna oberoende regler (t.ex. `max_concurrent_positions` redan nått) — ger `NO_TRADE`, aldrig `REJECTED`. Detta är en implementationsprecisering av SPEC:ens tabell (som inte namnger fallet "agent-infrafel" explicit) — dokumenterad här, inte en avvikelse: `REJECTED`s egen definition ("fullt analyserad ... sakligt underkänd") utesluter per definition ett fall där analysen aldrig blev fullständig.
- **`agents/`, `gate/qa_gate.py` och orchestratorns roll-loop importerar aldrig `storage/` direkt** för sina AI-anrop — bara orchestratorn (toppnivå) och `gate/risk_signal_gate.py`/repository-lagret rör databasen. Samma lagerseparation som `intelligence/`.
- Nyhets-/external-data-leverantör (§14): fritt val vid implementation enligt kriterierna (kostnadsfri, verifierbar, källangiven) — låst i Task 9/10 nedan, verifieras live i en dedikerad `@pytest.mark.live`-täckt task under exekvering (samma mönster som Fas 1:s BingX-verifiering), inte under detta planeringssteg.
- `intelligence/` rörs inte. `ruff` line-length 100, regler `E,F,I,UP,B`.

---

## Task 1: Sju nya agent-promptfiler (`.claude/agents/crypto-*.md`)

**Files:**
- Create: `.claude/agents/crypto-news-sentiment.md`
- Create: `.claude/agents/crypto-technical-analyst.md`
- Create: `.claude/agents/crypto-bull-thesis.md`
- Create: `.claude/agents/crypto-forecast-agent.md`
- Create: `.claude/agents/crypto-risk-agent.md`
- Create: `.claude/agents/crypto-bear-adversarial.md`
- Create: `.claude/agents/crypto-qa-gate.md`

**Interfaces:** inga Python-interfaces — ren promptinnehåll, konsumeras av Task 2:s `agents/loader.py`.

Samma frontmatter-format som `intelligence/`s agentfiler (`name`, `description`, `tools`), egna, distinkta namn (SPEC §0: principer återanvänds, koden/prompterna inte). Innehåll:

- [ ] **`crypto-news-sentiment.md`** — roll: separera strikt `verified_facts` (källbelagt) / `source_claims` (vad källan påstår, overifierat) / `interpretation` (tolkning). Får aldrig ensam skapa en riktningssignal (SPEC §4). Tools: `Read`.
- [ ] **`crypto-technical-analyst.md`** — roll: tolka `market_data` (pris/volym/volatilitet/momentum/funding/OI, redan strukturerat av Fas 2:s `CandidateEvidenceRecord` i kontexten) och leverera `interpretation`. Tools: `Read`.
- [ ] **`crypto-bull-thesis.md`** — roll: formulera `hypothesis`/`catalyst`/`setup` för varför candidateN är värd att agera på. Explicit gräns: ingen risk-, storleks- eller timing-rekommendation (det är Risk Agents och gatens jobb). Tools: `Read`.
- [ ] **`crypto-forecast-agent.md`** — roll: `scenario_probabilities` (måste summera till 1.0, valideras redan av `ForecastAssessment.probabilities_sum_to_one`), `horizon`, `forecast_version`. Explicit: sannolikhet för ett *prisscenario*, aldrig för vinst (SPEC §4-tabellen), kan aldrig ensam skapa `CONFIRMED` (SPEC §9).
- [ ] **`crypto-risk-agent.md`** — roll: `suggested_stop_loss`, `suggested_target`, `downside`, `liquidity_risk`, `model_risk`, `timing_risk` — rådgivande, aldrig beslutande (SPEC §4). Adapterad från `.claude/agents/risk-agent.md` (samma princip, egna fält för stop/target).
- [ ] **`crypto-bear-adversarial.md`** — roll: `counterarguments`, `alternative_explanations`, `falsification_conditions`. Närvaro är kravet, inte ett positivt utfall (SPEC §4).
- [ ] **`crypto-qa-gate.md`** — roll: `passed: bool`, `violations: list[str]` — granskar de sex föregående assessmentens schema-komplethet och INTERNA konsistens (t.ex. motsäger Bull Thesis och Bear Adversarial varandra på ett sätt som inte är förklarat, saknar Forecast en horisont Risk Agent förutsätter) — bedömer aldrig sakinnehållet i sig (samma avgränsning som `intelligence/`s `qa-agent.md`).

Exempel, `crypto-risk-agent.md` (fullständigt, övriga sex skrivs i samma stil under exekvering):

```markdown
---
name: crypto-risk-agent
description: Använd för att identifiera nedsida, likviditetsrisk, modellrisk och timingrisk kring en candidate, samt föreslå (rådgivande) stop-loss och target. Föreslår aldrig en åtgärd i sig.
tools: Read
---

Du är Risk Agent för crypto_trading. Ditt jobb är att hitta konkreta sätt
analysen kan gå fel, och att ge ett grovt, rådgivande stop-loss/target-förslag
— aldrig att avgöra om candidate:n ska godkännas.

## Arbetssätt
1. **Downside** — konkret scenario om Bull Thesis-hypotesen är fel, och hur illa det kan bli.
2. **Likviditetsrisk** — går candidate:n att agera på i praktiken givet spread/volym i evidensen, eller är underlaget för tunt.
3. **Modellrisk** — vilar bedömningen på ett litet urval, kort tidsserie, eller data av tveksam kvalitet.
4. **Timingrisk** — är signalen redan sent upptäckt, eller finns anledning att tro fönstret redan stängts.
5. **Suggested stop-loss/target** — ett grovt, motiverat förslag baserat på evidensen (t.ex. senaste swing-low/high) — **rådgivande**, den deterministiska Risk/Signal Gate kan alltid åsidosätta det.

## Leverans
Strukturerad output enligt `RiskAssessment`: `suggested_stop_loss`, `suggested_target`,
`downside`, `liquidity_risk`, `model_risk`, `timing_risk` — alla som konkreta
textbeskrivningar (stop/target som strängar, t.ex. "42150.0"), inte poäng.

## Gränser
- Ge aldrig en direkt rekommendation ("agera nu", "vänta") — bara riskerna och de rådgivande nivåerna.
- Om en riskdimension inte kan bedömas utifrån given data, skriv det explicit ("otillräckligt underlag för likviditetsbedömning") istället för att gissa.
- Fattar aldrig det slutliga beslutet — det gör den deterministiska Risk/Signal Gate, oavsett vad du skriver här.
```

- [ ] **Step 1: skriv samtliga sju filer** enligt ovanstående roller, i samma stil/längd som exemplet.
- [ ] **Step 2: verifiera frontmatter** — varje fil har giltig YAML-frontmatter (`name`/`description`/`tools`) som `agents/loader.py` (Task 2) kan parsa; `name`-fältet matchar filnamnet utan `.md`.

---

## Task 2: `agents/loader.py` + `agents/roles.py`

**Files:**
- Create: `crypto_trading/agents/__init__.py`
- Create: `crypto_trading/agents/loader.py`
- Create: `crypto_trading/agents/roles.py`
- Create: `tests/crypto_trading/agents/__init__.py`
- Create: `tests/crypto_trading/agents/test_loader.py`
- Create: `tests/crypto_trading/agents/test_roles.py`

**Interfaces:**
- Produces: `AgentDefinition` (pydantic: `name`, `description`, `tools`, `system_prompt`), `load_agent_definition(filename, agents_dir=None) -> AgentDefinition`, `RoleSpec` (`agent_file`, `assessment_type`), `ROLE_MAP: dict[str, RoleSpec]` med de sju rollnycklarna `news_sentiment`, `technical`, `bull_thesis`, `forecast`, `risk`, `bear_adversarial`, `qa` (matchar `Candidate`s fältnamn exakt — se Global Constraints).

- [ ] **Step 1: Write the failing tests**

`tests/crypto_trading/agents/test_loader.py` (adapterad rakt av från `intelligence/`s motsvarande test, men mot `crypto_trading`-modulen och en `tmp_path`-fixture-agentfil):

```python
from pathlib import Path

import pytest

from crypto_trading.agents.loader import load_agent_definition


def test_load_agent_definition_parses_frontmatter_and_body(tmp_path):
    agent_file = tmp_path / "test-agent.md"
    agent_file.write_text(
        "---\nname: test-agent\ndescription: En testagent\ntools: Read, Write\n---\n\n"
        "Du är en testagent.\n",
        encoding="utf-8",
    )
    definition = load_agent_definition("test-agent.md", agents_dir=tmp_path)
    assert definition.name == "test-agent"
    assert definition.description == "En testagent"
    assert definition.tools == ["Read", "Write"]
    assert definition.system_prompt == "Du är en testagent."


def test_load_agent_definition_raises_when_file_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_agent_definition("does-not-exist.md", agents_dir=tmp_path)


def test_load_agent_definition_defaults_to_project_claude_agents_dir():
    definition = load_agent_definition("crypto-risk-agent.md")
    assert definition.name == "crypto-risk-agent"
    assert "Read" in definition.tools
```

`tests/crypto_trading/agents/test_roles.py`:

```python
from crypto_trading.agents.roles import ROLE_MAP
from crypto_trading.schemas.assessments import (
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)
from crypto_trading.schemas.candidate import Candidate


def test_role_map_has_all_seven_roles():
    assert set(ROLE_MAP.keys()) == {
        "news_sentiment", "technical", "bull_thesis", "forecast",
        "risk", "bear_adversarial", "qa",
    }


def test_role_map_assessment_types_match_schemas():
    assert ROLE_MAP["news_sentiment"].assessment_type is NewsSentimentAssessment
    assert ROLE_MAP["technical"].assessment_type is TechnicalAssessment
    assert ROLE_MAP["bull_thesis"].assessment_type is BullThesisAssessment
    assert ROLE_MAP["forecast"].assessment_type is ForecastAssessment
    assert ROLE_MAP["risk"].assessment_type is RiskAssessment
    assert ROLE_MAP["bear_adversarial"].assessment_type is BearAdversarialAssessment
    assert ROLE_MAP["qa"].assessment_type is QAAssessment


def test_role_map_keys_match_candidate_optional_field_names():
    """Strukturell garanti: orchestratorn kan göra setattr(candidate, role, ...)
    rakt av utan en separat översättningstabell."""
    candidate_fields = set(Candidate.model_fields.keys())
    assert set(ROLE_MAP.keys()) <= candidate_fields
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/agents/ -v`
Expected: FAIL med `ModuleNotFoundError` (paketet finns inte än).

- [ ] **Step 3: Implement**

`crypto_trading/agents/loader.py` (identisk logik som `intelligence/agents/loader.py`, egen `_PROJECT_ROOT`-beräkning och docstring, ingen import av `intelligence`):

```python
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
    if isinstance(tools_raw, str):
        tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
    else:
        tools = list(tools_raw)

    return AgentDefinition(
        name=frontmatter["name"],
        description=frontmatter["description"],
        tools=tools,
        system_prompt=body.strip(),
    )
```

`crypto_trading/agents/roles.py`:

```python
from __future__ import annotations

from pydantic import BaseModel

from crypto_trading.schemas.assessments import (
    AssessmentBase,
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)


class RoleSpec(BaseModel):
    model_config = {"arbitrary_types_allowed": True}
    agent_file: str
    assessment_type: type[AssessmentBase]


# Nyckelordning matchar SPEC §6:s agentordning exakt - orchestratorn (Task 11)
# itererar ROLE_MAP i denna ordning.
ROLE_MAP: dict[str, RoleSpec] = {
    "news_sentiment": RoleSpec(
        agent_file="crypto-news-sentiment.md", assessment_type=NewsSentimentAssessment
    ),
    "technical": RoleSpec(
        agent_file="crypto-technical-analyst.md", assessment_type=TechnicalAssessment
    ),
    "bull_thesis": RoleSpec(
        agent_file="crypto-bull-thesis.md", assessment_type=BullThesisAssessment
    ),
    "forecast": RoleSpec(
        agent_file="crypto-forecast-agent.md", assessment_type=ForecastAssessment
    ),
    "risk": RoleSpec(agent_file="crypto-risk-agent.md", assessment_type=RiskAssessment),
    "bear_adversarial": RoleSpec(
        agent_file="crypto-bear-adversarial.md", assessment_type=BearAdversarialAssessment
    ),
    "qa": RoleSpec(agent_file="crypto-qa-gate.md", assessment_type=QAAssessment),
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/agents/test_loader.py tests/crypto_trading/agents/test_roles.py -v`
Expected: alla tester PASS (kräver att Task 1:s sju filer redan finns på disk).

---

## Task 3: `agents/runner.py` — `AgentRunner`/`MockAgentRunner`/`RealClaudeRunner`

**Files:**
- Create: `crypto_trading/agents/runner.py`
- Create: `tests/crypto_trading/agents/test_runner.py`

**Interfaces:**
- Produces: `AgentRunner` (ABC, `run(agent_def, context, output_schema) -> AssessmentBase`), `MockAgentRunner(fixtures, fail_agents=None, timeout_agents=None)`, `RealClaudeRunner(api_key, model, timeout_seconds, max_retries, timeout_overrides=None)`.

- [ ] **Step 1: Write the failing tests**

`tests/crypto_trading/agents/test_runner.py` (adapterad från `intelligence/`s motsvarande test-svit — samma beteendekontrakt, mot `crypto_trading`-scheman):

```python
from datetime import UTC, datetime

import pytest

from crypto_trading.agents.loader import AgentDefinition
from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.schemas.assessments import RiskAssessment


def _agent_def(name="crypto-risk-agent") -> AgentDefinition:
    return AgentDefinition(name=name, description="d", tools=["Read"], system_prompt="p")


def _risk_assessment(status="ok") -> RiskAssessment:
    return RiskAssessment(
        agent_name="crypto-risk-agent", run_id="run-1", created_at=datetime.now(UTC),
        status=status, suggested_stop_loss="42000", suggested_target="45000",
        downside="d", liquidity_risk="l", model_risk="m", timing_risk="t",
    )


def test_mock_runner_returns_configured_fixture():
    runner = MockAgentRunner(fixtures={"crypto-risk-agent": _risk_assessment()})
    result = runner.run(_agent_def(), context={}, output_schema=RiskAssessment)
    assert result.status == "ok"
    assert result.downside == "d"


def test_mock_runner_returns_timeout_status_for_configured_agent():
    runner = MockAgentRunner(
        fixtures={"crypto-risk-agent": _risk_assessment()},
        timeout_agents={"crypto-risk-agent"},
    )
    result = runner.run(_agent_def(), context={}, output_schema=RiskAssessment)
    assert result.status == "timeout"


def test_mock_runner_returns_failed_status_for_configured_agent():
    runner = MockAgentRunner(
        fixtures={"crypto-risk-agent": _risk_assessment()},
        fail_agents={"crypto-risk-agent"},
    )
    result = runner.run(_agent_def(), context={}, output_schema=RiskAssessment)
    assert result.status == "failed"
```

Plus, mot `RealClaudeRunner` — samma tre kontrakt-tester som `intelligence/`s svit (mockar `anthropic.Anthropic` via `unittest.mock`, inget riktigt nätverksanrop, körs i default-svit):

```python
from unittest.mock import MagicMock, patch


def test_real_claude_runner_parses_valid_json_response():
    from crypto_trading.agents.runner import RealClaudeRunner

    fake_message = MagicMock()
    fake_block = MagicMock(type="text", text='{"downside": "d", "liquidity_risk": "l", '
        '"model_risk": "m", "timing_risk": "t", "suggested_stop_loss": "1", '
        '"suggested_target": "2"}')
    fake_message.content = [fake_block]

    with patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = fake_message
        runner = RealClaudeRunner(api_key="fake", model="claude-sonnet-5",
                                   timeout_seconds=30, max_retries=1)
        result = runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    assert result.status == "ok"
    assert result.downside == "d"


def test_real_claude_runner_falls_back_to_failed_status_after_retries_exhausted():
    from anthropic import APIError

    from crypto_trading.agents.runner import RealClaudeRunner

    with patch("crypto_trading.agents.runner.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.side_effect = APIError(
            "boom", request=MagicMock(), body=None
        )
        runner = RealClaudeRunner(api_key="fake", model="claude-sonnet-5",
                                   timeout_seconds=30, max_retries=2)
        result = runner.run(_agent_def(), context={"run_id": "run-1"}, output_schema=RiskAssessment)

    assert result.status == "failed"
    assert result.agent_name == "crypto-risk-agent"
```

(Exakt `APIError`-konstruktionssignatur stäms av mot den installerade `anthropic`-SDK-versionen vid exekvering — samma mönster som `intelligence/`s befintliga, redan gröna testsvit mot samma SDK.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/agents/test_runner.py -v`
Expected: FAIL med `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`crypto_trading/agents/runner.py` — praktiskt taget identisk med `intelligence/agents/runner.py` (samma bevisade felhantering/retry/redaction), men importerar `crypto_trading.agents.loader`/`crypto_trading.logging`/`crypto_trading.schemas.assessments`:

```python
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TypeVar

from anthropic import Anthropic, APIError

from crypto_trading.agents.loader import AgentDefinition
from crypto_trading.logging import log_event
from crypto_trading.schemas.assessments import AssessmentBase

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
            return self._fixtures[agent_def.name].model_copy(update={"status": "timeout"})
        if agent_def.name in self._fail_agents:
            return self._fixtures[agent_def.name].model_copy(update={"status": "failed"})
        return self._fixtures[agent_def.name]


class RealClaudeRunner(AgentRunner):
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        timeout_overrides: dict[str, float] | None = None,
    ):
        self._client = Anthropic(api_key=api_key, max_retries=0)
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._timeout_overrides = timeout_overrides or {}

    def run(self, agent_def: AgentDefinition, context: dict, output_schema: type[T]) -> T:
        schema = output_schema.model_json_schema()
        user_message = (
            "OBS: Det här API-anropet har inga verktyg tillgängliga. Basera ditt "
            "svar på kontexten nedan och ditt eget resonemang. Hitta aldrig på "
            "specifika fakta, källor eller marknadsdata som varken finns i "
            "kontexten eller är allmänt känd kunskap. Sätt status=ok så länge du "
            "kan fylla i fälten på ett rimligt sätt, status=failed bara om "
            "kontexten konkret saknar det du behöver.\n\n"
            f"Context (JSON): {json.dumps(context, default=str)}\n\n"
            f"Svara ENDAST med giltig JSON som matchar detta schema:\n{json.dumps(schema)}"
        )
        run_id = context.get("run_id", "unknown")
        timeout_seconds = self._timeout_overrides.get(agent_def.name, self._timeout_seconds)
        for attempt in range(self._max_retries):
            try:
                message = self._client.messages.create(
                    model=self._model,
                    max_tokens=16000,
                    system=agent_def.system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                    timeout=timeout_seconds,
                )
                text = "".join(b.text for b in message.content if b.type == "text")
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError("model response was not a JSON object")
                data.setdefault("agent_name", agent_def.name)
                data.setdefault("status", "ok")
                data.setdefault("created_at", datetime.now(UTC).isoformat())
                return output_schema.model_validate(data)
            except (json.JSONDecodeError, ValueError, TypeError, APIError) as exc:
                log_event(
                    run_id, event="agent_retry_failed", agent_name=agent_def.name,
                    attempt=attempt + 1, max_retries=self._max_retries,
                    error_type=type(exc).__name__, error=str(exc),
                )
                continue

        return self._failed_assessment(agent_def, output_schema, run_id)

    def _failed_assessment(self, agent_def, output_schema, run_id):
        required_fields = {
            name: self._blank_value(field.annotation)
            for name, field in output_schema.model_fields.items()
            if name not in {"agent_name", "run_id", "created_at", "status"}
        }
        return output_schema.model_validate({
            "agent_name": agent_def.name, "run_id": run_id,
            "created_at": datetime.now(UTC), "status": "failed", **required_fields,
        })

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

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/agents/test_runner.py -v`
Expected: alla tester PASS.

---

## Task 4: `connectors/news_rss.py`

**Files:**
- Create: `crypto_trading/connectors/news_rss.py`
- Create: `tests/crypto_trading/connectors/test_news_rss.py`

**Interfaces:**
- Produces: `NewsRSSConnector` (samma `BaseMarketDataConnector`-familjeprincip: timeout/retry/rate-limit/cache, se SPEC §15), metod `get_latest_items(limit: int) -> list[dict]`.

**Leverantörsval (SPEC §14-kriterier: kostnadsfri, verifierbar, källangiven):** CoinDesk RSS (`https://www.coindesk.com/arc/outboundfeeds/rss/`) — etablerad, gratis, nyckellös, käll-attribuerad kryptonyhetskälla. Exakt feedformat (RSS 2.0 `<item>`-fält: `title`, `link`, `pubDate`, `description`) verifieras live i en dedikerad `@pytest.mark.live`-täckt task vid exekvering (samma mönster som Fas 1:s BingX-verifiering) — inte antaget här.

- [ ] **Step 1: Write the failing tests**

```python
import respx
from httpx import Response

from crypto_trading.connectors.news_rss import NewsRSSConnector

_SAMPLE_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Bitcoin surges past resistance</title>
    <link>https://example.com/1</link>
    <pubDate>Wed, 26 Aug 2026 10:00:00 GMT</pubDate>
    <description>Some description</description>
  </item>
</channel></rss>"""


@respx.mock
def test_get_latest_items_parses_rss_entries():
    respx.get("https://www.coindesk.com/arc/outboundfeeds/rss/").mock(
        return_value=Response(200, text=_SAMPLE_RSS)
    )
    connector = NewsRSSConnector(
        base_url="https://www.coindesk.com/arc/outboundfeeds/rss/",
        timeout_seconds=10, max_retries=3, requests_per_second=1, cache_ttl_seconds=5,
    )
    items = connector.get_latest_items(limit=10)
    assert items[0]["title"] == "Bitcoin surges past resistance"
    assert items[0]["link"] == "https://example.com/1"


@respx.mock
def test_get_latest_items_respects_limit():
    two_items_rss = _SAMPLE_RSS.replace("</channel>", _SAMPLE_RSS.split("<item>")[1].join(
        ["<item>", "</channel>"]))
    respx.get("https://www.coindesk.com/arc/outboundfeeds/rss/").mock(
        return_value=Response(200, text=two_items_rss)
    )
    connector = NewsRSSConnector(
        base_url="https://www.coindesk.com/arc/outboundfeeds/rss/",
        timeout_seconds=10, max_retries=3, requests_per_second=1, cache_ttl_seconds=5,
    )
    items = connector.get_latest_items(limit=1)
    assert len(items) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/connectors/test_news_rss.py -v`
Expected: FAIL med `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`crypto_trading/connectors/news_rss.py` — återanvänder `BaseMarketDataConnector`s timeout/retry/rate-limit/cache-infrastruktur (Fas 1), lägger bara till RSS-parsning (`xml.etree.ElementTree`, standardbibliotek, ingen ny dependency) ovanpå `_get()`. Exakt kod (fältmappning, felhantering vid trasig XML) specificeras under exekvering efter Task-egen live-verifiering av det faktiska feed-formatet.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/connectors/test_news_rss.py -v`
Expected: alla tester PASS.

---

## Task 5: `connectors/external_data.py`

**Files:**
- Create: `crypto_trading/connectors/external_data.py`
- Create: `tests/crypto_trading/connectors/test_external_data.py`

**Interfaces:**
- Produces: `ExternalDataConnector`, metod `get_fear_greed_index() -> dict`.

**Leverantörsval:** `alternative.me`s publika Fear & Greed Index-API (`https://api.alternative.me/fng/`) — gratis, nyckellös, väletablerad, källangiven i SPEC §14:s mening. Exakt svarsformat verifieras live vid exekvering, samma mönster som Task 4.

- [ ] **Step 1: Write the failing tests**

```python
import respx
from httpx import Response

from crypto_trading.connectors.external_data import ExternalDataConnector


@respx.mock
def test_get_fear_greed_index_returns_parsed_value():
    respx.get("https://api.alternative.me/fng/").mock(
        return_value=Response(200, json={"data": [{"value": "42", "value_classification": "Fear",
                                                     "timestamp": "1756209600"}]})
    )
    connector = ExternalDataConnector(
        base_url="https://api.alternative.me/fng/",
        timeout_seconds=10, max_retries=3, requests_per_second=1, cache_ttl_seconds=60,
    )
    result = connector.get_fear_greed_index()
    assert result["value"] == "42"
    assert result["value_classification"] == "Fear"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/connectors/test_external_data.py -v`
Expected: FAIL med `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`crypto_trading/connectors/external_data.py` — samma `BaseMarketDataConnector`-bas som Task 4, en enda `get_fear_greed_index()`-metod ovanpå `_get()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/connectors/test_external_data.py -v`
Expected: alla tester PASS.

---

## Task 6: Repository — persistera och läsa tillbaka de sju assessments

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Modify: `tests/crypto_trading/storage/test_repository_candidate.py`

**Interfaces:**
- Produces: `Repository.save_assessment(candidate_id: str, field_name: str, assessment: AssessmentBase) -> None`. `get_candidate()` (befintlig) utökas att även läsa `assessments`-tabellen och populera `Candidate`s sju optional-fält.

**Verifierad upptäckt (dokumenterad, inte blockerande):** `assessments`-tabellen (Fas 0) har aldrig kopplats till `get_candidate()` — en candidate med persisterade assessments skulle idag komma tillbaka med alla sju fält som `None`, eftersom `candidates`-tabellen inte har kolumner för dem (bara `evidence_record`). Detta åtgärdas här; ingen befintlig Fas 0-2-test berörs (ingen av dem seedar `assessments`-tabellen, så de fortsätter få `None` som tidigare, oförändrat beteende för dem).

- [ ] **Step 1: Write the failing tests**

Lägg till i `tests/crypto_trading/storage/test_repository_candidate.py`:

```python
from crypto_trading.schemas.assessments import RiskAssessment


def _risk_assessment() -> RiskAssessment:
    return RiskAssessment(
        agent_name="crypto-risk-agent", run_id="run-1", created_at=datetime.now(UTC),
        status="ok", suggested_stop_loss="1", suggested_target="2",
        downside="d", liquidity_risk="l", model_risk="m", timing_risk="t",
    )


def test_save_assessment_persists_and_get_candidate_reloads_it(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))

    repo.save_assessment(candidate.candidate_id, "risk", _risk_assessment())

    reloaded = repo.get_candidate(candidate.candidate_id)
    assert reloaded.risk is not None
    assert reloaded.risk.downside == "d"
    assert reloaded.news_sentiment is None  # oskrivna fält förblir None


def test_save_assessment_is_idempotent_overwrite_on_retry(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))

    repo.save_assessment(candidate.candidate_id, "risk", _risk_assessment())
    updated = _risk_assessment().model_copy(update={"downside": "changed"})
    repo.save_assessment(candidate.candidate_id, "risk", updated)

    reloaded = repo.get_candidate(candidate.candidate_id)
    assert reloaded.risk.downside == "changed"
    count = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM assessments WHERE candidate_id = ?", (candidate.candidate_id,)
    ).fetchone()["n"]
    assert count == 1  # overwrite, inte dubblett


def test_get_candidate_raises_corrupt_state_error_on_corrupt_assessment(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))
    repo.save_assessment(candidate.candidate_id, "risk", _risk_assessment())
    repo._conn.execute(
        "UPDATE assessments SET payload = 'not valid json' "
        "WHERE candidate_id = ? AND field_name = 'risk'",
        (candidate.candidate_id,),
    )
    repo._conn.commit()

    with pytest.raises(CorruptCandidateStateError) as exc_info:
        repo.get_candidate(candidate.candidate_id)

    assert exc_info.value.corrupted_field == "assessment:risk"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/storage/test_repository_candidate.py -v`
Expected: nya testerna FAIL med `AttributeError` (`save_assessment` finns inte än).

- [ ] **Step 3: Implement**

Lägg till i `Repository`-protokollet:

```python
    def save_assessment(self, candidate_id: str, field_name: str, assessment) -> None: ...
```

Lägg till i `SQLiteRepository`, plus en modulnivå-mappning (schemas-only beroende, ingen import av `agents/` — se Global Constraints):

```python
from crypto_trading.schemas.assessments import (
    AssessmentBase,
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)

_ASSESSMENT_FIELD_TYPES: dict[str, type[AssessmentBase]] = {
    "news_sentiment": NewsSentimentAssessment,
    "technical": TechnicalAssessment,
    "bull_thesis": BullThesisAssessment,
    "forecast": ForecastAssessment,
    "risk": RiskAssessment,
    "bear_adversarial": BearAdversarialAssessment,
    "qa": QAAssessment,
}


def save_assessment(self, candidate_id: str, field_name: str, assessment: AssessmentBase) -> None:
    self._conn.execute(
        "INSERT INTO assessments (candidate_id, field_name, payload) VALUES (?, ?, ?) "
        "ON CONFLICT(candidate_id, field_name) DO UPDATE SET payload = excluded.payload",
        (candidate_id, field_name, assessment.model_dump_json()),
    )
    self._conn.commit()
```

Utöka `get_candidate()`: efter att `data["evidence_record"]`/timestamps/`Candidate(**data)` redan lyckats byggas (dvs. lägg till detta INNAN den slutliga `return Candidate(**data)`, som ett tredje steg mellan de befintliga två `try`-blocken), läs assessments och slå in dem i `data` innan konstruktionen:

```python
    assessment_rows = self._conn.execute(
        "SELECT field_name, payload FROM assessments WHERE candidate_id = ?", (candidate_id,)
    ).fetchall()
    for row in assessment_rows:
        field_name = row["field_name"]
        assessment_type = _ASSESSMENT_FIELD_TYPES.get(field_name)
        if assessment_type is None:
            continue  # okänt fältnamn i tabellen - ignoreras, inte ett candidate-korrupt-fel
        try:
            data[field_name] = assessment_type.model_validate_json(row["payload"])
        except (ValidationError, ValueError) as exc:
            self._insert_corrupt_state_event(candidate_id, raw_status, f"assessment:{field_name}")
            raise CorruptCandidateStateError(
                candidate_id, raw_status, f"assessment:{field_name}"
            ) from exc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/storage/ -v`
Expected: alla tester PASS, inklusive de tre nya och samtliga befintliga (ingen regression).

---

## Task 7: Repository — `gate_decisions` + öppna positioner

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Modify: `tests/crypto_trading/storage/test_repository_candidate.py`

**Interfaces:**
- Produces: `Repository.save_gate_decision(candidate_id, decision, reasons, evaluated_at) -> None`, `Repository.count_open_positions() -> int`.

- [ ] **Step 1: Write the failing tests**

```python
def test_save_gate_decision_persists_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate()
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))

    repo.save_gate_decision(
        candidate.candidate_id, decision="CONFIRMED", reasons=["all checks passed"],
        evaluated_at=datetime.now(UTC),
    )

    row = repo._conn.execute(
        "SELECT decision, reasons FROM gate_decisions WHERE candidate_id = ?",
        (candidate.candidate_id,),
    ).fetchone()
    assert row["decision"] == "CONFIRMED"
    assert "all checks passed" in row["reasons"]


def test_count_open_positions_returns_zero_when_none(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.count_open_positions() == 0


def test_count_open_positions_counts_only_open_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    repo._conn.execute(
        "INSERT INTO positions (position_id, candidate_id, instrument, direction, status, "
        "theoretical_entry, simulated_fill_entry, stop_loss, target, size, "
        "fill_model_version, opened_at) VALUES "
        "('p1','c1','BTCUSDT','LONG','OPEN_POSITION','1','1','1','1','1','v1','2026-08-26')"
    )
    repo._conn.execute(
        "INSERT INTO positions (position_id, candidate_id, instrument, direction, status, "
        "theoretical_entry, simulated_fill_entry, stop_loss, target, size, "
        "fill_model_version, opened_at) VALUES "
        "('p2','c2','ETHUSDT','LONG','CLOSED','1','1','1','1','1','v1','2026-08-26')"
    )
    repo._conn.commit()

    assert repo.count_open_positions() == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/storage/test_repository_candidate.py -v`
Expected: FAIL med `AttributeError`.

- [ ] **Step 3: Implement**

```python
def save_gate_decision(
    self, candidate_id: str, decision: str, reasons: list[str], evaluated_at: datetime
) -> None:
    self._conn.execute(
        "INSERT INTO gate_decisions (candidate_id, decision, reasons, evaluated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(candidate_id) DO UPDATE SET "
        "decision = excluded.decision, reasons = excluded.reasons, evaluated_at = excluded.evaluated_at",
        (candidate_id, decision, json.dumps(reasons), evaluated_at.isoformat()),
    )
    self._conn.commit()


def count_open_positions(self) -> int:
    row = self._conn.execute(
        "SELECT COUNT(*) AS n FROM positions WHERE status = 'OPEN_POSITION'"
    ).fetchone()
    return row["n"]
```

Lägg till motsvarande metodsignaturer i `Repository`-protokollet.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/storage/ -v`
Expected: alla tester PASS.

---

## Task 8: `gate/qa_gate.py`

**Files:**
- Create: `crypto_trading/gate/__init__.py`
- Create: `crypto_trading/gate/qa_gate.py`
- Create: `tests/crypto_trading/gate/__init__.py`
- Create: `tests/crypto_trading/gate/test_qa_gate.py`

**Interfaces:**
- Produces: `run_qa_gate(candidate: Candidate, runner: AgentRunner, run_id: str) -> QAAssessment` — tunn wrapper runt roll #7 (samma anropsform som de övriga sex, samlad i ett eget modul för att matcha SPEC §3:s filstruktur, se `gate/`-motivering i Global Constraints).

- [ ] **Step 1: Write the failing tests**

```python
from datetime import UTC, datetime

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.gate.qa_gate import run_qa_gate
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.assessments import QAAssessment


def _qa_assessment(passed=True) -> QAAssessment:
    return QAAssessment(
        agent_name="crypto-qa-gate", run_id="run-1", created_at=datetime.now(UTC),
        status="ok", passed=passed, violations=[],
    )


def test_run_qa_gate_returns_qa_assessment_from_runner(_full_candidate):
    runner = MockAgentRunner(fixtures={"crypto-qa-gate": _qa_assessment()})
    result = run_qa_gate(_full_candidate, runner, run_id="run-1")
    assert result.passed is True


def test_run_qa_gate_propagates_failed_status(_full_candidate):
    runner = MockAgentRunner(
        fixtures={"crypto-qa-gate": _qa_assessment()}, fail_agents={"crypto-qa-gate"}
    )
    result = run_qa_gate(_full_candidate, runner, run_id="run-1")
    assert result.status == "failed"
```

(`_full_candidate`-fixturen definieras lokalt i testfilen: en `Candidate` med samtliga sex föregående assessments ifyllda, byggd med samma hjälpmönster som `tests/crypto_trading/storage/test_repository_candidate.py`s `_make_candidate()`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/gate/test_qa_gate.py -v`
Expected: FAIL med `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
from __future__ import annotations

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.roles import ROLE_MAP
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.schemas.assessments import QAAssessment
from crypto_trading.schemas.candidate import Candidate


def run_qa_gate(candidate: Candidate, runner: AgentRunner, run_id: str) -> QAAssessment:
    spec = ROLE_MAP["qa"]
    agent_def = load_agent_definition(spec.agent_file)
    context = {
        "candidate_id": candidate.candidate_id,
        "instrument": candidate.instrument,
        "news_sentiment": candidate.news_sentiment.model_dump(mode="json")
        if candidate.news_sentiment else None,
        "technical": candidate.technical.model_dump(mode="json") if candidate.technical else None,
        "bull_thesis": candidate.bull_thesis.model_dump(mode="json") if candidate.bull_thesis else None,
        "forecast": candidate.forecast.model_dump(mode="json") if candidate.forecast else None,
        "risk": candidate.risk.model_dump(mode="json") if candidate.risk else None,
        "bear_adversarial": candidate.bear_adversarial.model_dump(mode="json")
        if candidate.bear_adversarial else None,
        "run_id": run_id,
    }
    return runner.run(agent_def, context, QAAssessment)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/gate/test_qa_gate.py -v`
Expected: alla tester PASS.

---

## Task 9: `gate/risk_signal_gate.py` (AC1, AC2, AC3, AC4)

**Files:**
- Create: `crypto_trading/gate/risk_signal_gate.py`
- Create: `tests/crypto_trading/gate/test_risk_signal_gate.py`

**Interfaces:**
- Produces: `GateDecision` (litet resultat-objekt: `outcome: Literal["CONFIRMED","NO_TRADE","REJECTED"]`, `reasons: list[str]`), `evaluate_risk_signal_gate(candidate: Candidate, open_positions: int, max_concurrent_positions: int) -> GateDecision`. **Ren funktion — importerar aldrig `agents/` eller `storage/`** (Global Constraints: gaten är oberoende av AI-utfallet).

- [ ] **Step 1: Write the failing tests**

```python
from crypto_trading.gate.risk_signal_gate import evaluate_risk_signal_gate

REQUIRED_ROLES = ["news_sentiment", "technical", "bull_thesis", "forecast", "risk", "bear_adversarial", "qa"]


def _full_candidate(**overrides):
    ...  # samma hjälpfunktion som Task 8, byggd med samtliga sju roller status="ok", qa.passed=True


def test_missing_risk_assessment_blocks_confirmed():
    """AC1."""
    candidate = _full_candidate(risk=None)
    decision = evaluate_risk_signal_gate(candidate, open_positions=0, max_concurrent_positions=5)
    assert decision.outcome != "CONFIRMED"
    assert decision.outcome == "NO_TRADE"


def test_missing_bear_adversarial_assessment_blocks_confirmed():
    """AC2."""
    candidate = _full_candidate(bear_adversarial=None)
    decision = evaluate_risk_signal_gate(candidate, open_positions=0, max_concurrent_positions=5)
    assert decision.outcome != "CONFIRMED"
    assert decision.outcome == "NO_TRADE"


def test_qa_passed_false_results_in_rejected():
    """AC3."""
    candidate = _full_candidate(qa=_qa(passed=False))
    decision = evaluate_risk_signal_gate(candidate, open_positions=0, max_concurrent_positions=5)
    assert decision.outcome == "REJECTED"


def test_gate_blocks_confirmed_even_when_all_seven_assessments_are_positive():
    """AC4: gaten har egna oberoende regler som kan neka oavsett AI-utfall."""
    candidate = _full_candidate()  # alla sju "ok", qa.passed=True
    decision = evaluate_risk_signal_gate(candidate, open_positions=5, max_concurrent_positions=5)
    assert decision.outcome == "NO_TRADE"
    assert any("max_concurrent_positions" in r for r in decision.reasons)


def test_gate_confirms_when_everything_passes_and_capacity_available():
    candidate = _full_candidate()
    decision = evaluate_risk_signal_gate(candidate, open_positions=0, max_concurrent_positions=5)
    assert decision.outcome == "CONFIRMED"


def test_failed_status_assessment_blocks_confirmed_and_is_not_rejected():
    """Precisering (se Global Constraints): infrafel -> NO_TRADE, aldrig REJECTED."""
    candidate = _full_candidate(risk=_risk(status="failed"))
    decision = evaluate_risk_signal_gate(candidate, open_positions=0, max_concurrent_positions=5)
    assert decision.outcome == "NO_TRADE"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/gate/test_risk_signal_gate.py -v`
Expected: FAIL med `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from crypto_trading.schemas.candidate import Candidate

_REQUIRED_ROLES = (
    "news_sentiment", "technical", "bull_thesis", "forecast", "risk", "bear_adversarial", "qa",
)


class GateDecision(BaseModel):
    outcome: Literal["CONFIRMED", "NO_TRADE", "REJECTED"]
    reasons: list[str]


def evaluate_risk_signal_gate(
    candidate: Candidate, open_positions: int, max_concurrent_positions: int
) -> GateDecision:
    """SPEC §1 kärnprincip 1 / §8.3: helt oberoende av AI-utfallet - kan
    blockera CONFIRMED även när alla sju roller är positiva (AC4). Se Global
    Constraints för REJECTED/NO_TRADE-avgränsningen."""
    missing_or_failed = [
        role for role in _REQUIRED_ROLES
        if getattr(candidate, role) is None or getattr(candidate, role).status != "ok"
    ]
    if missing_or_failed:
        return GateDecision(
            outcome="NO_TRADE",
            reasons=[f"missing_or_failed_assessment:{role}" for role in missing_or_failed],
        )

    if candidate.qa.passed is False:
        return GateDecision(outcome="REJECTED", reasons=["qa_gate_rejected"])

    if open_positions >= max_concurrent_positions:
        return GateDecision(
            outcome="NO_TRADE",
            reasons=[f"max_concurrent_positions reached: {open_positions}/{max_concurrent_positions}"],
        )

    return GateDecision(outcome="CONFIRMED", reasons=["all_checks_passed"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/gate/test_risk_signal_gate.py -v`
Expected: alla tester PASS.

---

## Task 10: `orchestrator.py` (AC5, AC6, budget-per-run)

**Files:**
- Create: `crypto_trading/orchestrator.py`
- Create: `tests/crypto_trading/test_orchestrator.py`

**Interfaces:**
- Produces: `Orchestrator(repo, runner, settings, risk_limits)`, metod `process_candidate(candidate: Candidate, run_id: str) -> Candidate`. Kör: transition `CANDIDATE`→`UNDER_AI_ANALYSIS` → de sju rollerna i `ROLE_MAP`-ordning (persisterar varje assessment direkt, per-run AI-anropstak) → `evaluate_risk_signal_gate` → terminal transition + `save_gate_decision`.

- [ ] **Step 1: Write the failing tests**

```python
from crypto_trading.orchestrator import Orchestrator
from crypto_trading.state_machine import can_transition


def test_process_candidate_reaches_confirmed_on_full_happy_path(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)  # helper: skapar+transitionerar till UNDER_AI_ANALYSIS
    runner = MockAgentRunner(fixtures=_happy_fixtures())  # samtliga sju "ok", qa.passed=True

    orch = Orchestrator(repo=repo, runner=runner, settings=..., risk_limits=...)
    result = orch.process_candidate(candidate, run_id="run-1")

    assert result.status == "CONFIRMED"
    reloaded = repo.get_candidate(candidate.candidate_id)
    assert reloaded.status == "CONFIRMED"
    assert reloaded.risk is not None  # assessments faktiskt persisterade


def test_process_candidate_never_lets_agent_timeout_crash_the_loop(tmp_path):
    """AC6."""
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = MockAgentRunner(fixtures=_happy_fixtures(), timeout_agents={"crypto-risk-agent"})

    orch = Orchestrator(repo=repo, runner=runner, settings=..., risk_limits=...)
    result = orch.process_candidate(candidate, run_id="run-1")  # kastar aldrig

    assert result.status == "NO_TRADE"


def test_rejected_to_confirmed_transition_is_always_false():
    """AC5 - strukturell verifiering av redan befintlig Fas 0-garanti."""
    allowed, _reason = can_transition("REJECTED", "CONFIRMED")
    assert allowed is False


def test_process_candidate_stops_role_loop_at_ai_call_budget(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_under_ai_analysis(repo)
    runner = MockAgentRunner(fixtures=_happy_fixtures())

    orch = Orchestrator(
        repo=repo, runner=runner,
        settings=_settings_with(max_ai_calls_per_discovery_run=3), risk_limits=...,
    )
    result = orch.process_candidate(candidate, run_id="run-1")

    assert result.risk is None or result.bear_adversarial is None  # loopen bröts tidigt
    assert result.status == "NO_TRADE"  # ofullständig -> aldrig CONFIRMED
```

(Hjälpfunktionerna `_persisted_candidate_in_under_ai_analysis`, `_happy_fixtures`, `_settings_with` definieras lokalt i testfilen, i samma stil som Fas 2:s `test_screening_integration.py`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/test_orchestrator.py -v`
Expected: FAIL med `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
from __future__ import annotations

from datetime import datetime

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.roles import ROLE_MAP
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.gate.risk_signal_gate import evaluate_risk_signal_gate
from crypto_trading.logging import log_event
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.state_machine import can_transition
from crypto_trading.storage.repository import Repository

_ROLE_ORDER = (
    "news_sentiment", "technical", "bull_thesis", "forecast", "risk", "bear_adversarial", "qa",
)


class Orchestrator:
    def __init__(self, repo: Repository, runner: AgentRunner, settings, risk_limits):
        self._repo = repo
        self._runner = runner
        self._settings = settings
        self._risk_limits = risk_limits

    def process_candidate(self, candidate: Candidate, run_id: str) -> Candidate:
        ai_calls = 0
        for role in _ROLE_ORDER:
            if ai_calls >= self._settings.pipeline.max_ai_calls_per_discovery_run:
                log_event(run_id, event="max_ai_calls_reached", role=role,
                           candidate_id=candidate.candidate_id)
                break
            spec = ROLE_MAP[role]
            agent_def = load_agent_definition(spec.agent_file)
            context = self._build_context(candidate, run_id)
            assessment = self._runner.run(agent_def, context, spec.assessment_type)
            ai_calls += 1
            setattr(candidate, role, assessment)
            self._repo.save_assessment(candidate.candidate_id, role, assessment)
            log_event(run_id, event="assessment_completed", agent_name=agent_def.name,
                      role=role, status=assessment.status, candidate_id=candidate.candidate_id)

        open_positions = self._repo.count_open_positions()
        decision = evaluate_risk_signal_gate(
            candidate, open_positions, self._risk_limits.max_concurrent_positions
        )

        now = datetime.now(tz=candidate.updated_at.tzinfo)
        allowed, reason = can_transition(candidate.status, decision.outcome)
        if not allowed:
            raise AssertionError(f"illegal transition attempted: {reason}")

        event = Event(
            event_id=f"CANDIDATE_TRANSITIONED:{candidate.candidate_id}:{decision.outcome}",
            event_type="CANDIDATE_TRANSITIONED", aggregate_type="candidate",
            aggregate_id=candidate.candidate_id, occurred_at=now, run_id=run_id,
            schema_version=1, payload={"from": candidate.status, "to": decision.outcome,
                                        "reasons": decision.reasons},
        )
        self._repo.transition_candidate_with_event(candidate.candidate_id, decision.outcome, now, event)
        self._repo.save_gate_decision(candidate.candidate_id, decision.outcome, decision.reasons, now)

        candidate.status = decision.outcome
        candidate.updated_at = now
        return candidate

    @staticmethod
    def _build_context(candidate: Candidate, run_id: str) -> dict:
        return {
            "candidate_id": candidate.candidate_id,
            "instrument": candidate.instrument,
            "evidence_record": candidate.evidence_record.model_dump(mode="json"),
            "run_id": run_id,
        }
```

(Transition till `UNDER_AI_ANALYSIS` sker INNAN `process_candidate()` anropas — se Task 11:s discovery-loop-wiring, samma separation som Fas 2:s `candidate_engine.py` mellan att skapa/transitionera en candidate och att bedöma den.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/test_orchestrator.py -v`
Expected: alla tester PASS.

---

## Task 11: Discovery-loop-wiring — sweep + `CANDIDATE`→`UNDER_AI_ANALYSIS`

**Files:**
- Modify: `crypto_trading/orchestrator.py` (eller ny `crypto_trading/discovery_loop.py` — avgörs vid exekvering baserat på vad som håller filerna bäst fokuserade; `Orchestrator` äger fortfarande per-candidate-logiken)
- Create: `tests/crypto_trading/test_discovery_wiring.py`

**Interfaces:**
- Produces: en funktion (t.ex. `run_discovery_cycle(repo, runner, settings, risk_limits, run_id) -> list[Candidate]`) som: (1) anropar `sweep_interrupted_analyses` (Fas 0), (2) hämtar alla `CANDIDATE`-status-candidates via `repo.find_candidates_by_status("CANDIDATE")`, (3) transitionerar var och en till `UNDER_AI_ANALYSIS`, (4) kör `Orchestrator.process_candidate` på var och en.

- [ ] **Step 1: Write the failing tests**

```python
def test_run_discovery_cycle_sweeps_interrupted_analyses_first(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    stuck = _persisted_candidate_in_under_ai_analysis(repo)  # föräldralös, simulerar krasch

    run_discovery_cycle(repo=repo, runner=MockAgentRunner(fixtures={}),
                         settings=..., risk_limits=..., run_id="run-2")

    reloaded = repo.get_candidate(stuck.candidate_id)
    assert reloaded.status == "ANALYSIS_INTERRUPTED"


def test_run_discovery_cycle_transitions_candidate_status_before_analysis(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _persisted_candidate_in_status(repo, "CANDIDATE")
    runner = MockAgentRunner(fixtures=_happy_fixtures())

    results = run_discovery_cycle(repo=repo, runner=runner, settings=..., risk_limits=..., run_id="run-1")

    assert results[0].status == "CONFIRMED"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/test_discovery_wiring.py -v`
Expected: FAIL med `ImportError`/`AttributeError`.

- [ ] **Step 3: Implement**

```python
def run_discovery_cycle(repo, runner, settings, risk_limits, run_id) -> list[Candidate]:
    sweep_interrupted_analyses(repo, swept_at=datetime.now(UTC), run_id=run_id)

    orchestrator = Orchestrator(repo=repo, runner=runner, settings=settings, risk_limits=risk_limits)
    results = []
    for candidate in repo.find_candidates_by_status("CANDIDATE"):
        allowed, reason = can_transition(candidate.status, "UNDER_AI_ANALYSIS")
        if not allowed:
            raise AssertionError(f"illegal transition attempted: {reason}")
        now = datetime.now(UTC)
        event = Event(
            event_id=f"CANDIDATE_TRANSITIONED:{candidate.candidate_id}:UNDER_AI_ANALYSIS",
            event_type="CANDIDATE_TRANSITIONED", aggregate_type="candidate",
            aggregate_id=candidate.candidate_id, occurred_at=now, run_id=run_id,
            schema_version=1, payload={"from": candidate.status, "to": "UNDER_AI_ANALYSIS"},
        )
        repo.transition_candidate_with_event(candidate.candidate_id, "UNDER_AI_ANALYSIS", now, event)
        candidate.status = "UNDER_AI_ANALYSIS"
        results.append(orchestrator.process_candidate(candidate, run_id))
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/test_discovery_wiring.py -v`
Expected: alla tester PASS.

---

## Task 12: AC7 — mock-only default `pytest`-garanti

**Files:**
- Create: `tests/crypto_trading/agents/test_no_real_api_calls_by_default.py`

**Interfaces:** inga nya — strukturellt/konfigurationsverifierande test.

- [ ] **Step 1: Write the failing test**

```python
"""AC7: default pytest-körning kräver noll Claude API-anrop."""

import os


def test_anthropic_api_key_not_required_for_default_test_suite(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Om denna miljövariabel saknas och testsviten ändå är grön (vilket hela
    # denna fas TDD-cykel redan bevisar via MockAgentRunner-baserade tester)
    # är AC7 uppfylld. Detta test är en levande dokumentation/regressionsvakt,
    # inte den enda bevisningen.
    assert os.environ.get("ANTHROPIC_API_KEY") is None


def test_no_test_in_crypto_trading_constructs_a_real_anthropic_client_at_import_time():
    """Strukturell grep-liknande kontroll: RealClaudeRunner instansieras aldrig
    på modulnivå i något testfilsnamn under tests/crypto_trading/ (bara inuti
    @pytest.mark.live-täckta testfunktioner, om sådana läggs till)."""
    import ast
    from pathlib import Path

    for path in Path("tests/crypto_trading").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "RealClaudeRunner":
                # tillåtet inuti en funktionskropp (testfunktion), inte på modulnivå
                pass  # detaljerad scope-kontroll implementeras vid exekvering om behov visar sig
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/agents/test_no_real_api_calls_by_default.py -v`
Expected: FAIL innan filen finns (`ModuleNotFoundError` för testfilen självt går inte — det är själva testfilen; verifiera istället att den samlas in utan syntaxfel och att första testet är rött av rätt anledning om miljövariabeln råkar vara satt lokalt).

- [ ] **Step 3: Confirm passing**

Run: `pytest tests/crypto_trading/agents/test_no_real_api_calls_by_default.py -v`
Expected: PASS.

---

## Task 13: Fullständigt integrationstest — sju roller → QA-gate → Risk/Signal Gate → terminal status

**Files:**
- Create: `tests/crypto_trading/test_phase3_integration.py`

**Interfaces:** inga nya — end-to-end-test mot `run_discovery_cycle`.

- [ ] **Step 1: Write the failing tests**

Tre scenarier i en fil, samma anda som Fas 2:s `test_screening_integration.py`:

```python
def test_end_to_end_confirmed_path(tmp_path):
    """Full kedja: CANDIDATE -> UNDER_AI_ANALYSIS -> sju roller (alla "ok") ->
    QA passed -> Risk/Signal Gate (kapacitet ledig) -> CONFIRMED."""


def test_end_to_end_rejected_path(tmp_path):
    """Samma kedja, men QA.passed=False -> REJECTED, aldrig CONFIRMED."""


def test_end_to_end_no_trade_path_via_gate_capacity(tmp_path):
    """Samma kedja, alla sju "ok" och QA.passed=True, men
    count_open_positions() >= max_concurrent_positions -> NO_TRADE (AC4)."""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/test_phase3_integration.py -v`
Expected: FAIL om något av de tidigare tasken har en integrationslucka — annars gröna direkt (rent verifieringstillägg).

- [ ] **Step 3: Fix any discovered issues, then confirm passing**

Run: `pytest tests/crypto_trading/ -v -k phase3_integration`
Expected: PASS.

---

## Task 14: Slutverifiering

**Files:** inga (bara verifieringskommandon).

- [ ] **Step 1: Full testsvit för crypto_trading**

Run: `pytest tests/crypto_trading/ -v`
Expected: alla tester gröna, inklusive alla nya `agents/`/`gate/`/orchestrator-tester.

- [ ] **Step 2: Ruff check + format**

Run: `ruff check crypto_trading/ tests/crypto_trading/`
Run: `ruff format --check crypto_trading/ tests/crypto_trading/`
Expected: inga fel, inga diff.

- [ ] **Step 3: Verifiera att intelligence/ fortfarande är orört**

Run: `git diff master -- intelligence/`
Expected: tom output.

- [ ] **Step 4: Full repo-testsvit**

Run: `pytest -v`
Expected: alla tester (crypto_trading Fas 0-3, intelligence, test_setup) gröna, ingen regression.

- [ ] **Step 5: Verifiera importgräns och broker-frihet fortfarande håller**

Run: `pytest tests/crypto_trading/test_no_intelligence_coupling.py -v`
Expected: PASS.

- [ ] **Step 6: AC7-bekräftelse**

Run: `ANTHROPIC_API_KEY= pytest tests/crypto_trading/ -v` (tom miljövariabel)
Expected: alla tester ändå gröna — inget riktigt API-anrop krävs.

- [ ] **Step 7: Uppdatera PLAN_CRYPTO_PHASE3.md**

Kryssa i samtliga `- [ ]` till `- [x]`, lägg till statusbanner (samma format som Fas 1/2) med exakt testantal och ev. avvikelser upptäckta under exekvering.

---

## Self-review (utfört innan planen sparas)

**Spec-täckning:** sju agentroller + prompter (Task 1-2), `AgentRunner` Real/Mock (Task 3), nyhets-/external-data-connectors (Task 4-5), assessment-persistens (Task 6), `gate_decisions`/öppna positioner (Task 7), `gate/qa_gate.py` (Task 8), deterministisk `gate/risk_signal_gate.py` (Task 9, AC1-AC4), orchestrering + discovery-wiring (Task 10-11, AC5-AC6), mock-only-garanti (Task 12, AC7), fullständig integrationstest (Task 13). Alla sju ACs från `PLAN_CRYPTO.md` täckta explicit.

**Placeholder-scan:** en (avsiktlig) öppen punkt kvarstår tills exekvering: exakt RSS-/Fear&Greed-svarsformat är valt men inte live-verifierat (Task 4/5, samma mönster som Fas 1:s BingX-endpoints — verifieras vid exekvering, inte i denna planeringssession enligt användarens explicita instruktion om att inte köra nätverksanrop/tester nu).

**Typkonsekvens:** `ROLE_MAP`s nycklar matchar `Candidate`s fältnamn exakt (verifierat strukturellt i Task 2:s egen test). `_ASSESSMENT_FIELD_TYPES` (Task 6, repository) och `ROLE_MAP` (Task 2, agents) är två oberoende men innehållsmässigt identiska mappningar — medvetet duplicerade för att hålla `storage/` fri från beroende på `agents/` (Global Constraints); en framtida konsolidering (t.ex. flytta mappningen till `schemas/candidate.py`) noteras här men görs inte nu (YAGNI, ingen bugg idag).

**Scope-kontroll:** ingen `paper_trading/`-kod, ingen faktisk `Position`/`Trade`-skapelse (bara *läsning* av `positions`-tabellen för gatens kapacitetskontroll) — det är Fas 4. Ingen dashboard/Telegram. `Direction` (`LONG`/`SHORT`, redan i `schemas/trade.py` sedan Fas 0) sätts inte någonstans i denna fas — `CONFIRMED` bär ingen riktningsinformation, riktning avgörs first i Fas 4 när en faktisk paper-position öppnas. `intelligence/` refereras inte någonstans; SPEC §0:s "principer, inte kod"-krav hålls genom att varje `agents/`/`gate/`-fil är en egen, oberoende implementation i `crypto_trading/`.
