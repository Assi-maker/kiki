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
