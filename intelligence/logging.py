from __future__ import annotations

import json
import logging
import re
import uuid

_SECRET_KEY_MARKERS = ("api_key", "apikey", "token", "secret")

# Defense in depth for Finding #1: even when a value isn't flagged by a secret
# key name (e.g. it comes embedded inside a URL/exception message string, not
# under a dict key like "api_key"), scrub any "apikey=...", "api_key=..." or
# "token=..." query-param-shaped substring out of it too.
_SECRET_VALUE_PATTERN = re.compile(r"(?i)(?:api_key|apikey|token)=[^&\s]+")

_logger = logging.getLogger("intelligence")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
    _logger.setLevel(logging.INFO)


def new_run_id() -> str:
    return str(uuid.uuid4())


def redact(data: dict) -> dict:
    out = {}
    for key, value in data.items():
        if any(marker in key.lower() for marker in _SECRET_KEY_MARKERS):
            out[key] = "***REDACTED***"
        elif isinstance(value, str):
            out[key] = _SECRET_VALUE_PATTERN.sub("***REDACTED***", value)
        else:
            out[key] = value
    return out


def log_event(run_id: str, **fields) -> None:
    payload = redact({"run_id": run_id, **fields})
    _logger.info(json.dumps(payload, default=str))
