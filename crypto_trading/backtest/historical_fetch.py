from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from crypto_trading.schemas.market import FundingRate, Kline

_MAX_CANDLES_PER_CALL = 1440  # BingX server-enforced hard cap, verified live 2026-09-12


class HistoricalMarketDataSource(Protocol):
    def get_klines(
        self, symbol: str, interval: str, limit: int = 100,
        start_time_ms: int | None = None, end_time_ms: int | None = None,
    ) -> list[dict]: ...
    def get_funding_rate(
        self, symbol: str, limit: int = 1,
        start_time_ms: int | None = None, end_time_ms: int | None = None,
    ) -> list[dict]: ...


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _cache_path(cache_dir: Path, kind: str, symbol: str, interval: str, start: datetime, end: datetime) -> Path:
    key = f"{kind}:{symbol}:{interval}:{_ms(start)}:{_ms(end)}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return cache_dir / f"{kind}_{symbol}_{digest}.json"


def fetch_historical_klines(
    connector: HistoricalMarketDataSource, symbol: str, interval: str,
    start: datetime, end: datetime, cache_dir: Path,
) -> list[Kline]:
    """Every real network call this function makes is startTime/endTime-
    bounded and paginated in <=1440-candle chunks (the real, live-verified
    BingX server limit) walking forward from `start`. Results are cached
    to `cache_dir` keyed by (symbol, interval, start, end) so a second
    call with the same arguments never re-fetches - this is what makes
    the replay engine deterministic across repeated runs (Global
    Constraints)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, "klines", symbol, interval, start, end)
    if path.exists():
        raw_all = json.loads(path.read_text())
    else:
        raw_all = []
        cursor = start
        while cursor < end:
            page_end = min(cursor + timedelta(minutes=_MAX_CANDLES_PER_CALL), end)
            raw = connector.get_klines(
                symbol, interval, limit=_MAX_CANDLES_PER_CALL,
                start_time_ms=_ms(cursor), end_time_ms=_ms(page_end),
            )
            raw_all.extend(raw)
            cursor = page_end
        path.write_text(json.dumps(raw_all))
    return sorted(
        (Kline.from_raw(r, symbol, interval) for r in raw_all),
        key=lambda k: k.observed_at,
    )


def fetch_historical_funding(
    connector: HistoricalMarketDataSource, symbol: str,
    start: datetime, end: datetime, cache_dir: Path,
) -> list[FundingRate]:
    """Funding events occur only every 8h - a single call per position
    window is always sufficient (never approaches any pagination limit
    at the scale this plan operates at), but the same cache convention as
    klines is used for consistency and determinism."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, "funding", symbol, "8h", start, end)
    if path.exists():
        raw = json.loads(path.read_text())
    else:
        raw = connector.get_funding_rate(
            symbol, limit=100, start_time_ms=_ms(start), end_time_ms=_ms(end)
        )
        path.write_text(json.dumps(raw))
    return sorted((FundingRate.from_raw(r) for r in raw), key=lambda f: f.observed_at)
