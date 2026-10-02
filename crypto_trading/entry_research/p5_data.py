"""P5 data: Binance USDT-M perpetual DAILY klines + full funding history.

P5 moves to a slower timescale than every earlier lab (P1-P4 held 5m-24h):
days to weeks, where costs are a small fraction of the move and the
literature documents trend / momentum / carry premia in crypto.

Universe: every crypto USDT perpetual in exchangeInfo, INCLUDING delisted
(status SETTLING) contracts, so the cross-section is not survivor-only.
TradFi perpetuals (stocks/fx) are excluded.

Output: data/entry_research/p5/daily.db (sqlite)
  klines(symbol, t, o, h, l, c, qv)   t = day open ms UTC; known at t + 1 day
  funding(symbol, t, rate)            settlement time ms UTC
  symbols(symbol, status, onboard)

    python -m crypto_trading.entry_research.p5_data
Resumable: a symbol is skipped when it is already marked done.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.request
from pathlib import Path

DB = Path("data/entry_research/p5/daily.db")
API = "https://fapi.binance.com"
DAY = 86_400_000
START = 1567296000000  # 2019-09-01


def _get(path: str) -> object:
    for attempt in range(6):
        try:
            with urllib.request.urlopen(API + path, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001 - network retry
            code = getattr(e, "code", None)
            time.sleep(60 if code in (418, 429) else 2 * (attempt + 1))
    raise RuntimeError(f"GET failed: {path}")


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS klines(symbol TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, qv REAL,
                                          PRIMARY KEY(symbol, t));
        CREATE TABLE IF NOT EXISTS funding(symbol TEXT, t INTEGER, rate REAL, PRIMARY KEY(symbol, t));
        CREATE TABLE IF NOT EXISTS symbols(symbol TEXT PRIMARY KEY, status TEXT, onboard INTEGER, done INTEGER DEFAULT 0);
        """
    )
    return db


def fetch_symbol(db: sqlite3.Connection, sym: str, onboard: int) -> None:
    t = max(START, onboard - DAY)
    now = int(time.time() * 1000)
    while t < now:
        rows = _get(f"/fapi/v1/klines?symbol={sym}&interval=1d&limit=1500&startTime={t}")
        if not rows:
            break
        db.executemany(
            "INSERT OR REPLACE INTO klines VALUES(?,?,?,?,?,?,?)",
            [(sym, int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7])) for r in rows],
        )
        t = int(rows[-1][0]) + DAY
        if len(rows) < 1500:
            break
    t = max(START, onboard - DAY)
    while t < now:
        rows = _get(f"/fapi/v1/fundingRate?symbol={sym}&limit=1000&startTime={t}")
        time.sleep(0.65)  # fundingRate: 500 requests / 5 min / IP
        if not rows:
            break
        db.executemany(
            "INSERT OR REPLACE INTO funding VALUES(?,?,?)",
            [(sym, int(r["fundingTime"]), float(r["fundingRate"])) for r in rows],
        )
        t = int(rows[-1]["fundingTime"]) + 1
        if len(rows) < 1000:
            break
    db.execute("UPDATE symbols SET done=1 WHERE symbol=?", (sym,))
    db.commit()


def main() -> None:
    db = connect()
    info = _get("/fapi/v1/exchangeInfo")
    for s in info["symbols"]:
        if s["quoteAsset"] == "USDT" and s["contractType"] == "PERPETUAL":
            db.execute(
                "INSERT OR IGNORE INTO symbols(symbol, status, onboard) VALUES(?,?,?)",
                (s["symbol"], s["status"], int(s["onboardDate"])),
            )
    db.commit()
    todo = db.execute("SELECT symbol, onboard FROM symbols WHERE done=0 ORDER BY onboard").fetchall()
    for i, (sym, onboard) in enumerate(todo):
        fetch_symbol(db, sym, onboard)
        if i % 25 == 0:
            print(f"{i}/{len(todo)} {sym}", flush=True)
    print("done", db.execute("SELECT count(*), count(DISTINCT symbol) FROM klines").fetchone(), flush=True)


if __name__ == "__main__":
    main()
