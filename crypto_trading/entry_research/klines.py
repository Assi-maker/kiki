"""1m kline cache for entry research - public BingX GET only, stored in its
own SQLite file (never the production DB). Fetched per (symbol, UTC day), so
overlapping candidate windows cost one request per day."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from crypto_trading.shadow.evaluation import Bar

_DAY = timedelta(days=1)


class KlineCache:
    def __init__(self, path: Path, connector=None) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS klines (symbol TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL,"
            " v REAL, PRIMARY KEY (symbol, t))"
        )
        # status: OK | UNAVAILABLE (exchange refused - e.g. delisted/paused symbol)
        self._db.execute("CREATE TABLE IF NOT EXISTS days (symbol TEXT, day TEXT, status TEXT,"
                         " PRIMARY KEY (symbol, day))")
        self._connector = connector

    def _ensure_day(self, symbol: str, day: datetime) -> bool:
        key = day.date().isoformat()
        row = self._db.execute("SELECT status FROM days WHERE symbol=? AND day=?", (symbol, key)).fetchone()
        if row is not None:
            return row[0] == "OK"
        if self._connector is None:
            return False
        from crypto_trading.kline_archive import fetch_window

        end = min(day + _DAY - timedelta(minutes=1), datetime.now(UTC) - timedelta(minutes=2))
        try:
            rows = fetch_window(self._connector, symbol, day, end)
        except Exception:  # noqa: BLE001 - a refused symbol is data, not a crash
            self._db.execute("INSERT OR REPLACE INTO days VALUES (?,?,?)", (symbol, key, "UNAVAILABLE"))
            self._db.commit()
            return False
        self._db.executemany(
            "INSERT OR REPLACE INTO klines VALUES (?,?,?,?,?,?,?)",
            [(symbol, int(r["open_time"].timestamp()), float(r["open"]), float(r["high"]),
              float(r["low"]), float(r["close"]), float(r["volume"])) for r in rows],
        )
        complete = end >= day + _DAY - timedelta(minutes=1)
        if complete:  # a partial (today) day is re-fetched next time
            self._db.execute("INSERT OR REPLACE INTO days VALUES (?,?,?)", (symbol, key, "OK"))
        self._db.commit()
        return True

    def bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        day = datetime(start.year, start.month, start.day, tzinfo=UTC)
        while day <= end:
            self._ensure_day(symbol, day)
            day += _DAY
        rows = self._db.execute(
            "SELECT t,o,h,l,c FROM klines WHERE symbol=? AND t>=? AND t<=? ORDER BY t",
            (symbol, int(start.timestamp()), int(end.timestamp())),
        ).fetchall()
        return [Bar(datetime.fromtimestamp(t, UTC), o, h, l, c) for t, o, h, l, c in rows]
