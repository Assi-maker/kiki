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
