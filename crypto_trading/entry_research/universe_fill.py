"""Fill the 1m kline cache for the event engine (2026-09-29).

    python -m crypto_trading.entry_research.universe_fill

Every UTC day from 2026-09-01 for: every symbol already cached (candidate
history) + today's top-80 USDT perpetuals by 24 h quote volume (the LIVE
top-N). Public BingX GETs at 2 req/s into data/entry_research/klines.db
(gitignored); research only - the bot never imports this module.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from datetime import UTC, datetime, timedelta

from crypto_trading.entry_research.klines import KlineCache

KLINES_DB = "data/entry_research/klines.db"
FIRST_DAY = datetime(2026, 9, 1, tzinfo=UTC)
TOP_N = 80
TICKER_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"


def top_by_volume(n: int = TOP_N) -> list[str]:
    with urllib.request.urlopen(TICKER_URL, timeout=30) as r:
        data = json.loads(r.read().decode())["data"]
    usdt = [d for d in data if str(d.get("symbol", "")).endswith("-USDT")]
    usdt.sort(key=lambda d: float(d.get("quoteVolume") or 0), reverse=True)
    return [d["symbol"] for d in usdt[:n]]


def main() -> None:
    from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector

    connector = BingXMarketDataConnector(
        base_url="https://open-api.bingx.com",
        timeout_seconds=10.0,
        max_retries=3,
        requests_per_second=2,
        cache_ttl_seconds=0,
    )
    cached = [
        s
        for (s,) in sqlite3.connect(KLINES_DB).execute("SELECT DISTINCT symbol FROM klines")
        if s.endswith("-USDT")
    ]
    symbols = sorted(set(cached) | set(top_by_volume()))
    cache = KlineCache(KLINES_DB, connector)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    for n, sym in enumerate(symbols):
        day = FIRST_DAY
        while day <= today:
            cache._ensure_day(sym, day)
            day += timedelta(days=1)
        print(f"{n + 1}/{len(symbols)} {sym}", flush=True)


if __name__ == "__main__":
    main()
