import logging

from crypto_trading.logging import log_event, new_run_id, redact, redact_error_list


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


def test_redact_does_not_mask_token_count_fields():
    """Bugfix (kostnadsoptimering 2026-09-02): redact()s tidigare rena
    substrängsmatchning på "token" råkade även träffa input_tokens/
    output_tokens/cache_read_input_tokens/cache_creation_input_tokens -
    legitima tokenRÄKNINGAR (int), inte hemligheter - och tystade bort hela
    poängen med den nya agent_call_usage-kostnadsloggningen. En riktig
    hemlighetsnyckel heter alltid singular "...token" (bot_token,
    access_token); en räkning heter plural "...tokens" - det skiljer dem åt
    utan att öppna ett kryphål för riktiga secrets."""
    data = {
        "input_tokens": 970,
        "output_tokens": 1576,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    out = redact(data)
    assert out == data


def test_redact_still_masks_a_token_field_embedded_among_other_words():
    data = {"access_token_value": "abc123"}
    out = redact(data)
    assert out["access_token_value"] == "***REDACTED***"


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


def test_redact_error_list_masks_key_value_secrets_in_each_string():
    """Fas 6-fynd (code review 2026-08-29): Repository.complete_run()s
    `errors: list[str]` gick tidigare direkt till `runs.errors` UTAN
    redact() (som bara opererar på dict-värden) - en oredigerad secret i
    ett undantagsmeddelande skulle persisteras rått och senare kunna
    visas i klartext av format_debug_error_message() över Telegram."""
    errors = ["request failed: token=abc123&other=1", "harmless error, no secret here"]
    out = redact_error_list(errors)
    assert "abc123" not in out[0]
    assert "***REDACTED***" in out[0]
    assert out[1] == "harmless error, no secret here"


def test_redact_error_list_masks_telegram_bot_url_token_in_each_string():
    errors = [
        "request to https://api.telegram.org/bot123456789:ABCdefGhIJKlmNoPQRsTuVwxYZ/"
        "sendMessage failed"
    ]
    out = redact_error_list(errors)
    assert "123456789:ABCdefGhIJKlmNoPQRsTuVwxYZ" not in out[0]
    assert "***REDACTED***" in out[0]


def test_redact_error_list_returns_empty_list_unchanged():
    assert redact_error_list([]) == []


def test_log_event_never_emits_raw_secret(caplog):
    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        log_event("run-1", telegram_bot_token="super-secret-value", instrument="BTCUSDT")
    assert "super-secret-value" not in caplog.text
    assert "run-1" in caplog.text
