from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

from anthropic import Anthropic, APIError

from crypto_trading.agents.loader import AgentDefinition
from crypto_trading.logging import log_event
from crypto_trading.schemas.assessments import AssessmentBase

T = TypeVar("T", bound=AssessmentBase)

# Modellen svarar ibland (icke-deterministiskt - samma prompt kan ge både
# rå JSON och kodblocksinlindad JSON från anrop till anrop, verifierat
# empiriskt: se root-cause-analysen för bear_adversarial-buggen) med JSON
# inlindat i ett markdown-kodblock (```json ... ``` eller ``` ... ```)
# trots instruktionen att svara ENDAST med JSON. json.loads() förstår inte
# kodblocksmarkörerna och kastar JSONDecodeError på hela svaret, vilket
# tömmer retry-budgeten och ger status="failed" (fail-closed, se
# _failed_assessment) även när modellen faktiskt producerade ett giltigt
# JSON-svar. Ankrad helhetsmatchning (^...$) - stryper ENDAST ett svar som
# är exakt ett kodblock, aldrig en delsträngsextraktion ur fritext - så att
# valideringen inte försvagas: allt som inte är antingen ren JSON eller
# exakt ett JSON-kodblock faller fortfarande igenom till json.loads() och
# misslyckas som tidigare.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    match = _CODE_FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


# Kostnadsoptimering (2026-09-02): grova, hårdkodade $/MTok-priser för
# INTERN kostnadsloggning (agent_call_usage) - inte en faktureringskälla.
# Priser ändras över tid; uppdatera denna tabell vid modellbyte eller
# prisändring hos Anthropic. Okänd modell -> 0.0 (loggar tokens utan att
# gissa en kostnad, hellre än att tysta fel-uppskatta).
_MODEL_PRICE_PER_MTOK_USD: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def _estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = _MODEL_PRICE_PER_MTOK_USD.get(model, (0.0, 0.0))
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price


class AgentRunner(ABC):
    # Kostnadsbudget (2026-09-03, root cause: ett anrop som aldrig nådde
    # modellen - t.ex. HTTP 400 credit exhaustion - förbrukade ändå dagens
    # AI-anropstak/kostnadsbudget, eftersom Orchestrator tidigare räknade
    # varje run()-anrop lika oavsett utfall. Dessa två attribut är den enda
    # sanningskällan för "kostade detta anrop riktiga pengar" - Orchestrator
    # läser dem via getattr(..., default=True/Decimal("0")) direkt efter
    # run() returnerar, så en runner som inte sätter dem (som
    # MockAgentRunner) räknas som fakturerad med $0 kostnad - identiskt med
    # beteendet innan denna ändring, för att inte påverka någon av de
    # befintliga Mock-baserade testerna.
    last_call_billed: bool = True
    last_call_cost_usd: Decimal = Decimal("0")

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
        # SDK-level retries default to 2 (i.e. 3 attempts) and retry timeouts by
        # design, nesting inside our own retry loop below (self._max_retries) and
        # multiplying worst-case wall time to attempts x SDK_attempts x timeout_seconds.
        # We already own retry/backoff here, so disable the SDK's own layer.
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
            f"Svara ENDAST med giltig JSON som matchar detta schema:\n{json.dumps(schema)}\n\n"
            "Svara med ren JSON direkt - inget markdown-kodblock (```), ingen "
            "förklarande text före eller efter."
        )
        run_id = context.get("run_id", "unknown")
        timeout_seconds = self._timeout_overrides.get(agent_def.name, self._timeout_seconds)
        # Kostnadsbudget (2026-09-03): ackumuleras över SAMTLIGA försök inom
        # detta run()-anrop, inte bara det sista - ett försök kan nå
        # modellen (och alltså kosta riktiga pengar) och ändå räknas som
        # misslyckat här om svaret sedan inte går att parsa/validera (se
        # testet .._marks_call_billed_even_when_response_fails_to_parse).
        # Ekonomiskt har Anthropic då redan fakturerat det försöket oavsett
        # vad vår klientkod gör med svaret efteråt.
        billed_this_call = False
        cost_this_call = Decimal("0")
        for attempt in range(self._max_retries):
            try:
                message = self._client.messages.create(
                    model=self._model,
                    max_tokens=16000,
                    system=agent_def.system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                    timeout=timeout_seconds,
                )
                usage = message.usage
                input_tokens = usage.input_tokens
                output_tokens = usage.output_tokens
                estimated_cost = _estimate_cost_usd(self._model, input_tokens, output_tokens)
                billed_this_call = True
                cost_this_call += Decimal(str(estimated_cost))
                log_event(
                    run_id,
                    event="agent_call_usage",
                    agent_name=agent_def.name,
                    model=self._model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cache_creation_input_tokens=(
                        getattr(usage, "cache_creation_input_tokens", 0) or 0
                    ),
                    estimated_cost_usd=estimated_cost,
                )
                text = "".join(b.text for b in message.content if b.type == "text")
                data = json.loads(_strip_code_fence(text))
                if not isinstance(data, dict):
                    raise ValueError("model response was not a JSON object")
                data.setdefault("agent_name", agent_def.name)
                data.setdefault("status", "ok")
                data.setdefault("created_at", datetime.now(UTC).isoformat())
                self.last_call_billed = billed_this_call
                self.last_call_cost_usd = cost_this_call
                return output_schema.model_validate(data)
            except (json.JSONDecodeError, ValueError, TypeError, APIError) as exc:
                # SPEC §10: every retry failure must leave a diagnostic trace —
                # never a silent `continue`. Never interpolate the raw exception
                # or any raw request/response object here, only type name +
                # str(exc), never exc.request/.response (redact() still scrubs
                # defensively).
                log_event(
                    run_id,
                    event="agent_retry_failed",
                    agent_name=agent_def.name,
                    attempt=attempt + 1,
                    max_retries=self._max_retries,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                continue

        self.last_call_billed = billed_this_call
        self.last_call_cost_usd = cost_this_call
        return self._failed_assessment(agent_def, output_schema, run_id)

    def _failed_assessment(
        self, agent_def: AgentDefinition, output_schema: type[T], run_id: str
    ) -> T:
        # Bugfix (reproducerad live 2026-09-02, run_id 19239634...): en
        # ForecastAssessment vars retries tömdes kraschade hela
        # discovery-ticken (okontrollerat, aldrig fångat av run()s egen
        # try/except - se det try-blocket ovan) istället för att, som
        # designat, bara ge DENNA roll status="failed" och låta candidaten
        # gå vidare till Gaten som missing_or_failed_assessment. Två
        # samverkande orsaker: (1) _blank_value() kände bara igen bar
        # `dict`, inte en parametriserad `dict[str, float]` - föll igenom
        # till "" för scenario_probabilities. (2) även med rätt tom {}
        # hade model_validate() ändå kraschat, eftersom
        # ForecastAssessment.probabilities_sum_to_one() (en
        # affärsregel-validator för RIKTIGA modellsvar) aldrig kan vara
        # nöjd av en tom platshållare - summan av en tom dict är 0, aldrig
        # 1.0. En "failed"-platshållare representerar per definition inget
        # verkligt analysresultat (Gaten/downstream behandlar alltid
        # status!="ok" som missing_or_failed_assessment, oavsett
        # fältinnehåll) - model_construct() (skippar all fältvalidering,
        # till skillnad från model_validate()) är därför rätt verktyg här:
        # det försvagar INTE valideringen av riktiga modellsvar, som
        # fortfarande går genom model_validate() ovan i run() och
        # underkastas exakt samma scheman/validators som förut.
        required_fields = {
            name: self._blank_value(field.annotation)
            for name, field in output_schema.model_fields.items()
            if name not in {"agent_name", "run_id", "created_at", "status"}
        }
        return output_schema.model_construct(
            agent_name=agent_def.name,
            run_id=run_id,
            created_at=datetime.now(UTC),
            status="failed",
            **required_fields,
        )

    @staticmethod
    def _blank_value(annotation):
        origin = getattr(annotation, "__origin__", None)
        if origin is list:
            return []
        if origin is dict or annotation is dict:
            return {}
        if annotation is float:
            return 0.0
        if annotation is bool:
            return False
        return ""
