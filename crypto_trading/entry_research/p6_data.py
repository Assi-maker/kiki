"""P6 data: Binance SPOT daily candles for every perp in the P5 daily DB.

The perp `1000PEPEUSDT` trades 1000 PEPE per contract; its spot pair is
PEPEUSDT, so spot prices are multiplied by `mult` to be comparable.
Perps without a spot pair are skipped (no cash-and-carry possible).

Writes table `spot(symbol, t, o, h, l, c, qv)` (symbol = the PERP name,
prices already x mult) into data/entry_research/p5/daily.db.

    python -m crypto_trading.entry_research.p6_data
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.request

from crypto_trading.entry_research.p5_data import DB, START

API = "https://api.binance.com"
DAY = 86_400_000


def get(path: str):
    for a in range(5):
        try:
            with urllib.request.urlopen(API + path, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            if getattr(e, "code", None) == 400:
                return None
            time.sleep(2 * (a + 1))
    return None


def spot_name(perp: str) -> tuple[str, int]:
    for prefix, mult in (("1000000", 1_000_000), ("1000", 1000), ("1M", 1_000_000)):
        if perp.startswith(prefix) and not perp.startswith("1000SATS"):
            return perp[len(prefix):], mult
    if perp.startswith("1000SATS"):
        return "1000SATSUSDT", 1
    return perp, 1


def main() -> None:
    db = sqlite3.connect(DB)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS spot(symbol TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL, qv REAL,
                                        PRIMARY KEY(symbol, t));
        CREATE TABLE IF NOT EXISTS spot_done(symbol TEXT PRIMARY KEY, spot TEXT, mult INTEGER, n INTEGER);""")
    info = get("/api/v3/exchangeInfo")
    spot_syms = {s["symbol"] for s in info["symbols"]} if info else set()
    perps = [r[0] for r in db.execute("SELECT DISTINCT symbol FROM klines ORDER BY symbol")]
    for i, perp in enumerate(perps):
        if db.execute("SELECT 1 FROM spot_done WHERE symbol=?", (perp,)).fetchone():
            continue
        sp, mult = spot_name(perp)
        n = 0
        if sp in spot_syms:
            t = START
            while True:
                rows = get(f"/api/v3/klines?symbol={sp}&interval=1d&limit=1000&startTime={t}")
                if not rows:
                    break
                db.executemany("INSERT OR REPLACE INTO spot VALUES(?,?,?,?,?,?,?)",
                               [(perp, int(r[0]), float(r[1]) * mult, float(r[2]) * mult, float(r[3]) * mult,
                                 float(r[4]) * mult, float(r[7])) for r in rows])
                n += len(rows)
                t = int(rows[-1][0]) + DAY
                if len(rows) < 1000:
                    break
        db.execute("INSERT OR REPLACE INTO spot_done VALUES(?,?,?,?)", (perp, sp, mult, n))
        db.commit()
        if i % 25 == 0:
            print(f"{i}/{len(perps)} {perp} -> {sp} x{mult}: {n}", flush=True)
    print("spot pairs with data:", db.execute("SELECT count(*) FROM spot_done WHERE n > 0").fetchone()[0])


if __name__ == "__main__":
    main()
