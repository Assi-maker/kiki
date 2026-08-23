from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TypeVar

from anthropic import Anthropic, APIError

from intelligence.agents.loader import AgentDefinition
from intelligence.logging import log_event
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
        # SDK-level retries default to 2 (i.e. 3 attempts) and retry timeouts by
        # design, nesting inside our own retry loop below (self._max_retries) and
        # multiplying worst-case wall time to attempts x SDK_attempts x timeout_seconds.
        # We already own retry/backoff here, so disable the SDK's own layer.
        self._client = Anthropic(api_key=api_key, max_retries=0)
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    def run(self, agent_def: AgentDefinition, context: dict, output_schema: type[T]) -> T:
        schema = output_schema.model_json_schema()
        user_message = (
            "OBS: Det här API-anropet har inga verktyg tillgängliga — varken "
            "webbsökning eller filskrivning. Basera ditt svar på kontexten nedan "
            "och ditt eget resonemang. Hitta aldrig på specifika fakta, källor "
            "eller marknadsdata som varken finns i kontexten eller är allmänt "
            "känd kunskap. Fyll i schemats fält efter bästa förmåga utifrån det "
            "du faktiskt har — sätt status=ok så länge du kan göra det på ett "
            "rimligt sätt. Sätt status=failed bara om kontexten konkret saknar "
            "det du behöver för att fullgöra just din del av uppdraget.\n\n"
            f"Context (JSON): {json.dumps(context, default=str)}\n\n"
            f"Svara ENDAST med giltig JSON som matchar detta schema:\n{json.dumps(schema)}"
        )
        run_id = context.get("run_id", "unknown")
        for attempt in range(self._max_retries):
            try:
                message = self._client.messages.create(
                    model=self._model,
                    max_tokens=16000,
                    system=agent_def.system_prompt,
                    messages=[{"role": "user", "content": user_message}],
                    timeout=self._timeout_seconds,
                )
                text = "".join(block.text for block in message.content if block.type == "text")
                data = json.loads(text)
                if not isinstance(data, dict):
                    raise ValueError("model response was not a JSON object")
                data.setdefault("agent_name", agent_def.name)
                data.setdefault("status", "ok")
                data.setdefault("created_at", datetime.now(UTC).isoformat())
                return output_schema.model_validate(data)
            except (json.JSONDecodeError, ValueError, TypeError, APIError) as exc:
                # SPEC §10: every retry failure must leave a diagnostic trace —
                # never a silent `continue`. Never interpolate the raw exception
                # or any raw request/response object here (Finding #1's class of
                # bug); log_event's redact() still scrubs defensively, but only
                # type name + str(exc) are passed in, never `exc.request`/`.response`.
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

        return self._failed_assessment(agent_def, output_schema, run_id)

    def _failed_assessment(
        self, agent_def: AgentDefinition, output_schema: type[T], run_id: str
    ) -> T:
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
