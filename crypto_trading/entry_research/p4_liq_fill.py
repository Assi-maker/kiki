"""P4: real Binance-futures liquidations from Tardis.dev's FREE sample days.

Tardis publishes the Binance USD-M `forceOrder` stream (since 2020-01-07) and
lets anyone download the FIRST DAY OF EVERY MONTH without an API key. That is
the only legitimate free source of real (not proxied) liquidation history we
found; it gives 14 sample days spread over the 14 research months. Caveat
(Binance, not Tardis): the stream only carries the LATEST liquidation per
symbol per 1000 ms, so totals are a lower bound in heavy cascades.

side: "sell" = a LONG was liquidated (forced sell), "buy" = a SHORT was.

    python -m crypto_trading.entry_research.p4_liq_fill
"""

from __future__ import annotations

import csv
import gzip
import io
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

DB = Path("data/entry_research/p4_liq.db")
ARCHIVE = Path("data/entry_research/archive.db")
URL = "https://datasets.tardis.dev/v1/binance-futures/liquidations/{y}/{m:02d}/01/{sym}.csv.gz"
DAYS = [(2025, m) for m in range(8, 13)] + [(2026, m) for m in range(1, 10)]


def fetch(client: httpx.Client, sym: str, y: int, m: int) -> tuple[str, str, list[tuple]]:
    day = f"{y}-{m:02d}-01"
    try:
        r = client.get(URL.format(y=y, m=m, sym=sym), timeout=60)
    except httpx.HTTPError as e:
        return sym, day, [("ERR", str(e))]
    if r.status_code != 200:
        return sym, day, [("HTTP", r.status_code)]
    rows = []
    for rec in csv.DictReader(io.StringIO(gzip.decompress(r.content).decode())):
        ts = int(rec["timestamp"]) // 1000  # us -> ms
        rows.append((sym, ts, rec["side"], float(rec["price"]) * float(rec["amount"])))
    return sym, day, rows


def main() -> None:
    syms = [r[0] for r in sqlite3.connect(ARCHIVE).execute("SELECT DISTINCT symbol FROM klines5m")]
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS liq (symbol TEXT, ts INTEGER, side TEXT, usd REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS fetched (symbol TEXT, day TEXT, status TEXT, n INTEGER, PRIMARY KEY(symbol, day))")
    done = {(s, d) for s, d in db.execute("SELECT symbol, day FROM fetched")}
    jobs = [(s, y, m) for s in syms for (y, m) in DAYS if (s, f"{y}-{m:02d}-01") not in done]
    print(f"{len(jobs)} files to fetch", flush=True)
    with httpx.Client() as client, ThreadPoolExecutor(16) as ex:
        for i, (sym, day, rows) in enumerate(ex.map(lambda j: fetch(client, *j), jobs)):
            if rows and rows[0][0] in ("ERR", "HTTP"):
                db.execute("INSERT OR REPLACE INTO fetched VALUES (?,?,?,?)", (sym, day, f"{rows[0][0]}:{rows[0][1]}", 0))
            else:
                db.executemany("INSERT INTO liq VALUES (?,?,?,?)", rows)
                db.execute("INSERT OR REPLACE INTO fetched VALUES (?,?,?,?)", (sym, day, "ok", len(rows)))
            if i % 100 == 0:
                db.commit()
                print(i, flush=True)
    db.execute("CREATE INDEX IF NOT EXISTS liq_sym_ts ON liq(symbol, ts)")
    db.commit()


if __name__ == "__main__":
    main()
