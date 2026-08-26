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
