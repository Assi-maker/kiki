"""Archive exchange 1m klines for every traded window (Fas 2A.1).

GODFATHER's analysis layer may not talk to the exchange (its isolation
suite forbids a connector import), so this small module does it on its
behalf: for every closed position whose window is not yet covered, it
fetches the real 1m candles from the position's decision time to its
close - paged forward with startTime/endTime at the BingX per-call cap -
and stores exactly what the exchange returned in `exchange_klines_1m`.

With that history GODFATHER can (a) verify every booked exit against
what the market actually did, whatever gaps monitoring or Guardian had,
and (b) reconstruct the price path after activation from real candles.
A minute the exchange does not return is simply not stored; nothing is
interpolated.

Read-only with respect to trading: it never touches `positions` or any
order path; the only table it writes is the archive.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.schemas.market import Kline
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

_INTERVAL = "1m"
_PAGE_CANDLES = 1440  # BingX per-call cap, live-verified 2026-09-12
# Covered when at most this share of the window's minutes is missing.
_COVERED_FRACTION = 0.98
_PAD_AFTER = timedelta(minutes=4)
_EDGE_TOLERANCE = timedelta(minutes=2)


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def position_window(
    position: Position, now: datetime, live_closed_at: datetime | None = None
) -> tuple[datetime, datetime]:
    """From the paper decision time (the earliest timestamp the trade has)
    to just after the LATER of the paper and the LIVE close - the LIVE
    position can outlive its paper twin."""
    closes = [c for c in (position.closed_at, live_closed_at) if c is not None]
    end = (max(closes) if closes else now) + _PAD_AFTER
    return position.opened_at, min(end, now)


def _live_closed_at(repo: Repository, position: Position) -> datetime | None:
    execution = repo.get_live_execution(position.position_id)
    if execution and execution.get("closed_at"):
        return datetime.fromisoformat(execution["closed_at"])
    return None


def is_covered(
    repo: Repository, position: Position, now: datetime, live_closed_at: datetime | None = None
) -> bool:
    """Covered = enough candles AND both ends of the window reached. A
    count alone passed a window whose last minutes were never fetched."""
    start, end = position_window(position, now, live_closed_at)
    rows = repo.find_exchange_klines(position.instrument, start, end)
    expected = max(1, int((end - start).total_seconds() // 60))
    if len(rows) < expected * _COVERED_FRACTION or not rows:
        return False
    first = datetime.fromisoformat(rows[0]["open_time"])
    last = datetime.fromisoformat(rows[-1]["open_time"])
    return first - start <= _EDGE_TOLERANCE and end - last <= _EDGE_TOLERANCE


def fetch_window(connector, instrument: str, start: datetime, end: datetime) -> list[dict]:
    rows: dict[datetime, dict] = {}
    cursor = start
    while cursor < end:
        page_end = min(cursor + timedelta(minutes=_PAGE_CANDLES), end)
        raw = connector.get_klines(
            instrument, _INTERVAL, limit=_PAGE_CANDLES,
            start_time_ms=_ms(cursor), end_time_ms=_ms(page_end),
        )
        for item in raw:
            kline = Kline.from_raw(item, instrument, _INTERVAL)
            if start <= kline.observed_at <= end:
                rows[kline.observed_at] = {
                    "open_time": kline.observed_at, "open": kline.open, "high": kline.high,
                    "low": kline.low, "close": kline.close, "volume": kline.volume,
                }
        cursor = page_end
    return [rows[moment] for moment in sorted(rows)]


def archive_positions(
    connector, repo: Repository, now: datetime, limit: int | None = None,
    pause_seconds: float = 0.0,
) -> dict:
    """Archive every closed position (with exposure) whose window is not
    yet covered. Never raises for one instrument's failure."""
    run_id = new_run_id()
    archived = skipped = failed = 0
    for position in repo.find_closed_positions():
        if limit is not None and archived >= limit:
            break
        if position.size == 0 or position.closed_at is None:
            continue
        live_closed = _live_closed_at(repo, position)
        if is_covered(repo, position, now, live_closed):
            skipped += 1
            continue
        start, end = position_window(position, now, live_closed)
        try:
            rows = fetch_window(connector, position.instrument, start, end)
        except ConnectorUnavailableError as exc:
            failed += 1
            log_event(run_id, event="kline_archive_fetch_failed",
                      instrument=position.instrument, error=str(exc))
            continue
        repo.save_exchange_klines(position.instrument, rows, now)
        archived += 1
        if pause_seconds:
            time.sleep(pause_seconds)
    summary = {"archived": archived, "already_covered": skipped, "failed": failed}
    log_event(run_id, event="kline_archive_tick", **summary)
    return summary


def main() -> None:
    from crypto_trading.config.loader import get_settings
    from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
    from crypto_trading.storage.repository import SQLiteRepository

    settings = get_settings()
    connector = BingXMarketDataConnector(
        base_url=settings.pipeline.bingx_base_url, timeout_seconds=10.0,
        max_retries=settings.pipeline.bingx_max_retries, requests_per_second=2,
        cache_ttl_seconds=0,
    )
    repo = SQLiteRepository(settings.db_path)
    print(archive_positions(connector, repo, datetime.now(UTC), pause_seconds=0.2))


if __name__ == "__main__":
    main()
