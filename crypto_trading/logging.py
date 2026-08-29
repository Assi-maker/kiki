from __future__ import annotations

import json
import logging
import re
import uuid

_SECRET_KEY_MARKERS = ("api_key", "apikey", "token", "secret", "credential")

_SECRET_VALUE_PATTERN = re.compile(r"(?i)(?:api_key|apikey|token)=[^&\s]+")
# Fas 6, Beslut 3: Telegram Bot API:ets URL-format har token i PATH:en
# (https://api.telegram.org/bot<TOKEN>/sendMessage), inte som en
# key=value-parameter - fångas inte av mönstret ovan. Andra skyddslager om
# disciplinen att aldrig logga hela URL:en (notify/telegram.py) bryts.
_TELEGRAM_BOT_URL_PATTERN = re.compile(r"/bot\d+:[\w-]+")

_logger = logging.getLogger("crypto_trading")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


def new_run_id() -> str:
    return str(uuid.uuid4())


def _redact_string(value: str) -> str:
    masked = _SECRET_VALUE_PATTERN.sub("***REDACTED***", value)
    return _TELEGRAM_BOT_URL_PATTERN.sub("/bot***REDACTED***", masked)


def redact(data: dict) -> dict:
    out = {}
    for key, value in data.items():
        if any(marker in key.lower() for marker in _SECRET_KEY_MARKERS):
            out[key] = "***REDACTED***"
        elif isinstance(value, str):
            out[key] = _redact_string(value)
        else:
            out[key] = value
    return out


def redact_error_list(errors: list[str]) -> list[str]:
    """Samma skyddsmönster som redact(), men för en bar lista med
    felsträngar - dict-formen på redact() passar inte
    Repository.complete_run()s `errors: list[str]`-argument, som
    persisteras till `runs.errors` (och, sedan Fas 6, kan visas i klartext
    via notify/telegram.py::format_debug_error_message() på debug-nivå).
    Upptäckt vid code review 2026-08-29: complete_run() gick tidigare
    förbi redact() helt - ett undantagsmeddelande som råkade innehålla en
    secret (t.ex. ett httpx-undantag som inte fångades av
    TelegramNotifier.send()s egen except-sats) skulle persisteras rått."""
    return [_redact_string(e) for e in errors]


def log_event(run_id: str, **fields) -> None:
    payload = redact({"run_id": run_id, **fields})
    _logger.info(json.dumps(payload, default=str))
