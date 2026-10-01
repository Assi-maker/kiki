"""Path cache for the 2026-10-01 trading-system review.

Fetches the real BingX 1m candles around every quant candidate the bot has
produced (24 h before the decision for entry-context features, 8 h after it
for the outcome - the LIVE hold limit is 6 h) plus 5m candles for a fixed
large-cap universe (BTC/ETH regime and market breadth). Everything goes to
`data/entry_research/trade_review.db`, never to the bot DB, which is opened
read-only.

Public market-data endpoints only: no account, no order path.

    python -m crypto_trading.entry_research.trade_review_data
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from crypto_trading.config.loader import get_settings
from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
from crypto_trading.connectors.exceptions import ConnectorUnavailableError

BOT_DB = Path("data/crypto_trading.db")
CACHE_DB = Path("data/entry_research/trade_review.db")
PRE = timedelta(hours=24)
POST = timedelta(hours=8)
PAGE = 1440
UNIVERSE_5M = [
    "BTC-USDT",
    "ETH-USDT",
    "SOL-USDT",
    "BNB-USDT",
    "XRP-USDT",
    "DOGE-USDT",
    "ADA-USDT",
    "AVAX-USDT",
    "LINK-USDT",
    "DOT-USDT",
    "LTC-USDT",
    "BCH-USDT",
    "NEAR-USDT",
    "APT-USDT",
    "ARB-USDT",
    "OP-USDT",
    "SUI-USDT",
    "TON-USDT",
    "TRX-USDT",
    "UNI-USDT",
    "AAVE-USDT",
    "INJ-USDT",
    "TIA-USDT",
    "SEI-USDT",
    "WLD-USDT",
    "FIL-USDT",
    "ATOM-USDT",
    "HBAR-USDT",
    "ENA-USDT",
    "PEPE-USDT",
    "WIF-USDT",
    "ONDO-USDT",
    "JUP-USDT",
    "RENDER-USDT",
    "ETC-USDT",
    "XLM-USDT",
    "ICP-USDT",
    "STX-USDT",
    "CRV-USDT",
    "LDO-USDT",
]


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def open_cache(path: Path = CACHE_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS k1m (symbol TEXT, t INTEGER, o REAL, h REAL, l REAL,"
        " c REAL, v REAL, PRIMARY KEY (symbol, t)) WITHOUT ROWID"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS k5m (symbol TEXT, t INTEGER, o REAL, h REAL, l REAL,"
        " c REAL, v REAL, PRIMARY KEY (symbol, t)) WITHOUT ROWID"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS fetched (symbol TEXT, interval TEXT, start INTEGER,"
        " end INTEGER, n INTEGER, PRIMARY KEY (symbol, interval, start))"
    )
    return db


def merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def candidate_windows(now: datetime) -> dict[str, list[tuple[int, int]]]:
    bot = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)
    out: dict[str, list[tuple[int, int]]] = {}
    for symbol, created in bot.execute("SELECT instrument, created_at FROM candidates"):
        t0 = datetime.fromisoformat(created)
        out.setdefault(symbol, []).append((_ms(t0 - PRE), _ms(min(t0 + POST, now))))
    return {s: merge_windows(w) for s, w in out.items()}


def fetch_range(
    connector: BingXMarketDataConnector,
    db: sqlite3.Connection,
    table: str,
    interval: str,
    symbol: str,
    start: int,
    end: int,
    step_ms: int,
) -> int:
    done = db.execute(
        "SELECT 1 FROM fetched WHERE symbol=? AND interval=? AND start=? AND end>=?",
        (symbol, interval, start, end),
    ).fetchone()
    if done:
        return 0
    cursor, total = start, 0
    while cursor < end:
        page_end = min(end, cursor + PAGE * step_ms - 1)
        try:
            rows = connector.get_klines(
                symbol, interval, limit=PAGE, start_time_ms=cursor, end_time_ms=page_end
            )
        except ConnectorUnavailableError as exc:
            print(f"  {symbol} {interval} {cursor}: {exc}", flush=True)
            rows = []
        db.executemany(
            f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?)",
            [
                (
                    symbol,
                    int(r["time"]),
                    float(r["open"]),
                    float(r["high"]),
                    float(r["low"]),
                    float(r["close"]),
                    float(r["volume"]),
                )
                for r in rows or []
            ],
        )
        total += len(rows or [])
        cursor = page_end + 1
    db.execute(
        "INSERT OR REPLACE INTO fetched VALUES (?,?,?,?,?)", (symbol, interval, start, end, total)
    )
    db.commit()
    return total


def main() -> None:
    settings = get_settings()
    connector = BingXMarketDataConnector(
        base_url=settings.pipeline.bingx_base_url,
        timeout_seconds=15.0,
        max_retries=4,
        requests_per_second=8.0,
        cache_ttl_seconds=0.0,
    )
    db = open_cache()
    now = datetime.now(UTC)
    start_5m = _ms(datetime(2026, 8, 31, tzinfo=UTC))
    for i, symbol in enumerate(UNIVERSE_5M):
        n = fetch_range(connector, db, "k5m", "5m", symbol, start_5m, _ms(now), 300_000)
        print(f"5m {i + 1}/{len(UNIVERSE_5M)} {symbol} +{n}", flush=True)
    windows = candidate_windows(now)
    for i, (symbol, ranges) in enumerate(sorted(windows.items())):
        n = sum(fetch_range(connector, db, "k1m", "1m", symbol, s, e, 60_000) for s, e in ranges)
        print(f"1m {i + 1}/{len(windows)} {symbol} ranges={len(ranges)} +{n}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    sys.exit(main())
