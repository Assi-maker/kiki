import logging

from crypto_trading.logging import log_event, new_run_id, redact


def test_new_run_id_returns_unique_uuid_strings():
    a = new_run_id()
    b = new_run_id()
    assert a != b
    assert len(a) == 36  # uuid4 sträng-längd


def test_redact_masks_keys_matching_secret_markers():
    data = {"telegram_bot_token": "abc123", "instrument": "BTCUSDT"}
    out = redact(data)
    assert out["telegram_bot_token"] == "***REDACTED***"
    assert out["instrument"] == "BTCUSDT"


def test_redact_masks_embedded_token_in_string_value():
    data = {"error_message": "request failed: token=abc123&other=1"}
    out = redact(data)
    assert "abc123" not in out["error_message"]
    assert "***REDACTED***" in out["error_message"]


def test_redact_masks_telegram_bot_token_embedded_in_url_path():
    """Fas 6, Beslut 3: Telegram Bot API:ets URL-format
    (https://api.telegram.org/bot<TOKEN>/sendMessage) har token i PATH:en,
    inte som en key=value-parameter - _SECRET_VALUE_PATTERN (byggd för
    api_key=.../token=... i frågesträngar) fångar inte detta mönster.
    Andra skyddslager om disciplinen att aldrig logga hela URL:en
    (notify/telegram.py::TelegramNotifier.send()) någonsin bryts."""
    data = {
        "error_message": (
            "request to https://api.telegram.org/bot123456789:ABCdefGhIJKlmNoPQRsTuVwxYZ/"
            "sendMessage failed"
        )
    }
    out = redact(data)
    assert "123456789:ABCdefGhIJKlmNoPQRsTuVwxYZ" not in out["error_message"]
    assert "***REDACTED***" in out["error_message"]
    assert "sendMessage" in out["error_message"]  # resten av URL:en/meddelandet kvar, oskadat


def test_log_event_never_emits_raw_secret(caplog):
    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        log_event("run-1", telegram_bot_token="super-secret-value", instrument="BTCUSDT")
    assert "super-secret-value" not in caplog.text
    assert "run-1" in caplog.text
