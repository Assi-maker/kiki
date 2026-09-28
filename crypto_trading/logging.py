from __future__ import annotations

import faulthandler
import json
import logging
import re
import sys
import threading
import traceback
import uuid
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

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


def _key_matches_secret_marker(key: str) -> bool:
    # Bugfix (kostnadsoptimering 2026-09-02): en ren substrängsmatchning på
    # "token" träffade även legitima tokenRÄKNINGAR som input_tokens/
    # output_tokens (int, inte hemligheter) och tystade bort den nya
    # agent_call_usage-kostnadsloggningen. Underscore-inramning ("_x_" i
    # "_nyckel_") kräver att markören står som ett eget ord/segment i
    # nyckeln - "_token_" matchar "bot_token" och "access_token_value" men
    # inte "input_tokens" (som bara innehåller "_tokens_", aldrig "_token_")
    # - en riktig hemlighetsnyckel heter alltid singular, en räkning plural.
    padded_key = f"_{key.lower()}_"
    return any(f"_{marker}_" in padded_key for marker in _SECRET_KEY_MARKERS)


def redact(data: dict) -> dict:
    out = {}
    for key, value in data.items():
        if _key_matches_secret_marker(key):
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


# ---------------------------------------------------------------------------
# Persistent logging (2026-09-28). Until now every event went only to the
# console window of start_bot.bat: after the silent process death of
# 2026-09-27 07:13 UTC and the restart that followed, nothing of the running
# bot's history existed on disk. The handler sits on the ROOT logger so both
# our own `crypto_trading` events (which propagate) and any third-party
# warning land in the same rotated file.
# ---------------------------------------------------------------------------

LOG_FILE_NAME = "crypto_trading.log"
FAULT_FILE_NAME = "faulthandler.log"


class _UtcFormatter(logging.Formatter):
    """`<ISO-8601 UTC>Z <LEVEL> <message>` - one line per event, the message
    being the same redacted JSON log_event() already produces."""

    def formatTime(self, record, datefmt=None):  # noqa: N802 - logging API name
        moment = datetime.fromtimestamp(record.created, UTC)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z"


_file_handler: logging.Handler | None = None
_fault_file = None
_previous_hooks: tuple | None = None


def _log_uncaught(thread_name: str, exc_type, exc_value, exc_tb) -> None:
    log_event(
        "uncaught", event="uncaught_exception", thread=thread_name,
        error_type=exc_type.__name__, error=str(exc_value),
        traceback="".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
    )


def _thread_excepthook(args) -> None:
    if args.exc_type is not SystemExit:
        name = args.thread.name if args.thread is not None else "unknown"
        _log_uncaught(name, args.exc_type, args.exc_value, args.exc_traceback)
    if _previous_hooks is not None:
        _previous_hooks[1](args)


def _sys_excepthook(exc_type, exc_value, exc_tb) -> None:
    if not issubclass(exc_type, KeyboardInterrupt):
        _log_uncaught("MainThread", exc_type, exc_value, exc_tb)
    if _previous_hooks is not None:
        _previous_hooks[0](exc_type, exc_value, exc_tb)


def configure_persistent_logging(
    log_dir: Path, max_bytes: int = 20 * 1024 * 1024, backup_count: int = 10
) -> Path:
    """Idempotent. Adds a size-rotated file handler (default 20 MB x 11 files,
    ~220 MB worst case) under `log_dir`, logs every uncaught exception - main
    thread or any loop thread - as an `uncaught_exception` event with its full
    traceback, and points faulthandler at its own file so even a hard
    interpreter crash leaves a stack dump. Returns the active log file path."""
    global _file_handler, _fault_file, _previous_hooks
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / LOG_FILE_NAME
    if _file_handler is not None:
        return Path(_file_handler.baseFilename)
    handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(_UtcFormatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.captureWarnings(True)
    _file_handler = handler
    _previous_hooks = (sys.excepthook, threading.excepthook)
    sys.excepthook = _sys_excepthook
    threading.excepthook = _thread_excepthook
    _fault_file = open(log_dir / FAULT_FILE_NAME, "a", encoding="utf-8")  # noqa: SIM115 - kept open for faulthandler
    faulthandler.enable(file=_fault_file, all_threads=True)
    return log_file


def shutdown_persistent_logging() -> None:
    """Undoes configure_persistent_logging() (tests; a clean process exit
    does not need it)."""
    global _file_handler, _fault_file, _previous_hooks
    if _file_handler is not None:
        logging.getLogger().removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None
    if _previous_hooks is not None:
        sys.excepthook, threading.excepthook = _previous_hooks
        _previous_hooks = None
    if _fault_file is not None:
        faulthandler.disable()
        _fault_file.close()
        _fault_file = None
    logging.captureWarnings(False)
