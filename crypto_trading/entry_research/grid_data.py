"""Grid lab data: continuous BingX 1m candles + funding for every coin the
bot traded LIVE, over the whole LIVE period (first entry - 1 day -> last exit).

Public market-data endpoints only. Writes data/entry_research/grid.db.

    python -m crypto_trading.entry_research.grid_data
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from crypto_trading.config.loader import get_settings
from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector

BOT_DB = Path("data/crypto_trading.db")
DB = Path("data/entry_research/grid.db")
MIN = 60_000


def live_trades() -> list[dict]:
    db = sqlite3.connect(f"file:{BOT_DB}?mode=ro", uri=True)
    rows = db.execute(
        """SELECT p.position_id, p.instrument, l.claimed_at, l.entry_filled_at, l.closed_at, l.exit_filled_at,
                  l.exchange_fill_entry, l.exchange_fill_exit, l.entry_quantity, l.realized_fees_usdt,
                  l.realized_funding_usdt, l.notional_usdt, l.exit_classification
           FROM live_executions l JOIN positions p USING(position_id)
           WHERE l.phase = 'CLOSED' AND l.exchange_fill_entry IS NOT NULL AND l.exchange_fill_exit IS NOT NULL
           ORDER BY l.claimed_at"""
    ).fetchall()
    keys = ["position_id", "symbol", "claimed_at", "entry_at", "closed_at", "exit_at", "entry", "exit", "qty",
            "fees", "funding", "notional", "exit_class"]
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["start"] = datetime.fromisoformat(d["entry_at"] or d["claimed_at"])
        d["end"] = datetime.fromisoformat(d["exit_at"] or d["closed_at"])
        out.append(d)
    return out


def main() -> None:
    s = get_settings()
    mk = BingXMarketDataConnector(base_url=s.pipeline.bingx_base_url, timeout_seconds=20.0, max_retries=4,
                                  requests_per_second=5.0, cache_ttl_seconds=0.0)
    trades = live_trades()
    t0 = min(t["start"] for t in trades) - timedelta(days=1)
    t1 = max(t["end"] for t in trades) + timedelta(hours=1)
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS k1m(symbol TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL,
                                       PRIMARY KEY(symbol, t)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS funding(symbol TEXT, t INTEGER, rate REAL, PRIMARY KEY(symbol, t));
        CREATE TABLE IF NOT EXISTS done(symbol TEXT PRIMARY KEY, n INTEGER);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);""")
    db.execute("INSERT OR REPLACE INTO meta VALUES('t0', ?), ('t1', ?)", (t0.isoformat(), t1.isoformat()))
    syms = sorted({t["symbol"] for t in trades})
    a, b = int(t0.timestamp() * 1000), int(t1.timestamp() * 1000)
    for i, sym in enumerate(syms):
        if db.execute("SELECT 1 FROM done WHERE symbol=?", (sym,)).fetchone():
            continue
        n = 0
        start = a
        while start < b:
            end = min(start + 1440 * MIN - 1, b)
            try:
                rows = mk.get_klines(sym, "1m", limit=1440, start_time_ms=start, end_time_ms=end) or []
            except Exception as e:  # noqa: BLE001 - a delisted coin returns an error; keep going
                print(sym, "klines error", e, flush=True)
                rows = []
            db.executemany("INSERT OR REPLACE INTO k1m VALUES(?,?,?,?,?,?)",
                           [(sym, int(r["time"]), float(r["open"]), float(r["high"]), float(r["low"]),
                             float(r["close"])) for r in rows])
            n += len(rows)
            start = end + 1
        fstart = a
        while fstart < b:
            try:
                fr = mk.get_funding_rate(sym, limit=1000, start_time_ms=fstart, end_time_ms=b) or []
            except Exception as e:  # noqa: BLE001
                print(sym, "funding error", e, flush=True)
                break
            db.executemany("INSERT OR REPLACE INTO funding VALUES(?,?,?)",
                           [(sym, int(r["fundingTime"]), float(r["fundingRate"])) for r in fr])
            if len(fr) < 1000:
                break
            fstart = max(int(r["fundingTime"]) for r in fr) + 1
        db.execute("INSERT OR REPLACE INTO done VALUES(?,?)", (sym, n))
        db.commit()
        print(f"{i + 1}/{len(syms)} {sym} {n} bars", flush=True)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
