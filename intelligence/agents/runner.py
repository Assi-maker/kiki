from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TypeVar

from anthropic import Anthropic, APIError

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
                if not isinstance(data, dict):
                    raise ValueError("model response was not a JSON object")
                data.setdefault("agent_name", agent_def.name)
                data.setdefault("status", "ok")
                data.setdefault("created_at", datetime.now(UTC).isoformat())
                return output_schema.model_validate(data)
            except (json.JSONDecodeError, ValueError, TypeError, APIError):
                continue

        return self._failed_assessment(agent_def, output_schema, context.get("run_id", "unknown"))

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
