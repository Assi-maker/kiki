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


def test_redact_hides_apikey_key_without_underscore():
    data = {"apikey": "SUPER-SECRET-KEY-123", "note": "hello"}
    out = redact(data)
    assert out["apikey"] == "***REDACTED***"
    assert out["note"] == "hello"


def test_redact_scrubs_secret_value_embedded_in_a_url_string():
    # Finding #1: httpx exception messages embed the full request URL, including
    # ?apikey=... — the value can carry the secret even when the dict key
    # holding it (e.g. "error") isn't itself a secret-flagged key name.
    data = {
        "error": (
            "Alpha Vantage otillgänglig: Client error '429' for url "
            "'https://www.alphavantage.co/query?function=GLOBAL_QUOTE"
            "&symbol=IBM&apikey=SUPER-SECRET-KEY-123'"
        )
    }
    out = redact(data)
    assert "SUPER-SECRET-KEY-123" not in out["error"]
    assert "***REDACTED***" in out["error"]


def test_redact_scrubs_api_key_and_token_query_param_shapes():
    data = {
        "a": "https://x.example/?api_key=abc123&other=1",
        "b": "https://x.example/?token=zzz999",
    }
    out = redact(data)
    assert "abc123" not in out["a"]
    assert "zzz999" not in out["b"]


def test_log_event_never_leaks_apikey_query_param_from_exception_message(caplog):
    caplog.set_level(logging.INFO)
    fake_key = "SUPER-SECRET-KEY-123"
    error_message = (
        f"Alpha Vantage otillgänglig: Client error '429 Too Many Requests' for url "
        f"'https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=IBM&apikey={fake_key}'"
    )
    log_event(
        run_id="r1", event="connector_unavailable", source_id="alpha_vantage", error=error_message
    )
    combined = "\n".join(caplog.messages)
    assert fake_key not in combined
