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
