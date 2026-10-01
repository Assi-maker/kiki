"""P3 (2026-10-01) data: Binance SPOT history for the spot/perp study.

Research only; public data.binance.vision files, nothing account-related.

- spot 1m klines (incl. taker-buy volume) for every archive symbol that has a
  spot pair, 2025-08 -> 2026-09-29: monthly files, daily files for the last
  month -> data/entry_research/p3_spot1m.db (table spot1m)
- a microstructure sample for second-level lead/lag: futures aggTrades and
  spot 1s klines for LEADLAG_SYMBOLS on LEADLAG_DAYS (two days in each of
  TRAIN/VALID/TEST/HOLDOUT) -> data/entry_research/p3_micro.db, reduced to a
  1-second last-price series per market (perp from aggTrades, spot from 1s
  klines), so seconds-level lags can be measured without storing raw trades.

    python -m crypto_trading.entry_research.p3_data spot|micro
"""

from __future__ import annotations

import csv
import io
import sqlite3
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import httpx

ARCHIVE = Path("data/entry_research/archive.db")
SPOT_DB = Path("data/entry_research/p3_spot1m.db")
MICRO_DB = Path("data/entry_research/p3_micro.db")
BASE = "https://data.binance.vision/data"
MONTHS = [f"2025-{m:02d}" for m in range(8, 13)] + [f"2026-{m:02d}" for m in range(1, 9)]
LAST_DAYS = [date(2026, 9, 1) + timedelta(days=i) for i in range(29)]
SPOT_SYMBOLS = 40
LEADLAG_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "LINKUSDT"]
LEADLAG_DAYS = [
    "2025-09-17",
    "2025-12-03",
    "2026-02-18",
    "2026-04-08",
    "2026-05-20",
    "2026-07-15",
    "2026-08-12",
    "2026-09-16",
]


def _get(client: httpx.Client, url: str) -> bytes | None:
    for _ in range(3):
        try:
            r = client.get(url, timeout=60)
        except httpx.HTTPError:
            continue
        if r.status_code == 404:
            return None
        if r.status_code == 200:
            return r.content
    return None


def _rows(blob: bytes):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        with z.open(z.namelist()[0]) as f:
            for row in csv.reader(io.TextIOWrapper(f, encoding="utf-8")):
                if row and row[0][:1].isdigit():
                    yield row


def _ms(x: str) -> int:
    v = int(x)
    return v // 1000 if v > 10**14 else v  # spot files switched to microseconds in 2025


def spot_symbol_files(symbol: str) -> list[str]:
    files = [f"{BASE}/spot/monthly/klines/{symbol}/1m/{symbol}-1m-{m}.zip" for m in MONTHS]
    files += [
        f"{BASE}/spot/daily/klines/{symbol}/1m/{symbol}-1m-{d.isoformat()}.zip" for d in LAST_DAYS
    ]
    return files


def fetch_spot() -> None:
    src = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True)
    # The 40 most liquid archive symbols (median 5m quote volume, last 60 days):
    # ~100k baseline events, plenty for the test; all 135 would take ~5 h.
    since = src.execute("SELECT MAX(t) FROM klines5m").fetchone()[0] - 60 * 86_400_000
    liquid = src.execute(
        "SELECT symbol, AVG(qv) AS q FROM klines5m WHERE t > ? GROUP BY symbol ORDER BY q DESC",
        (since,),
    ).fetchall()
    symbols = [s for s, _ in liquid if not s.startswith("1000")][:SPOT_SYMBOLS]
    db = sqlite3.connect(SPOT_DB)
    db.execute(
        "CREATE TABLE IF NOT EXISTS spot1m (symbol TEXT, t INTEGER, o REAL, h REAL,"
        " l REAL, c REAL, v REAL, tbv REAL, PRIMARY KEY (symbol, t)) WITHOUT ROWID"
    )
    db.execute("CREATE TABLE IF NOT EXISTS done (url TEXT PRIMARY KEY, n INTEGER)")
    done = {u for (u,) in db.execute("SELECT url FROM done")}

    def fetch(url: str) -> tuple[str, bytes | None]:
        with httpx.Client(follow_redirects=True) as client:  # one client per thread
            return url, _get(client, url)

    for i, symbol in enumerate(symbols):
        if symbol.startswith("1000"):
            print(f"{symbol}: no 1:1 spot pair, skipped", flush=True)
            continue
        urls = [u for u in spot_symbol_files(symbol) if u not in done]
        with ThreadPoolExecutor(8) as pool:
            blobs = list(pool.map(fetch, urls))
        n_total = 0
        for url, blob in blobs:
            rows = list(_rows(blob)) if blob else []
            db.executemany(
                "INSERT OR REPLACE INTO spot1m VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        symbol,
                        _ms(r[0]),
                        float(r[1]),
                        float(r[2]),
                        float(r[3]),
                        float(r[4]),
                        float(r[5]),
                        float(r[9]),
                    )
                    for r in rows
                ],
            )
            db.execute("INSERT OR REPLACE INTO done VALUES (?,?)", (url, len(rows)))
            n_total += len(rows)
        db.commit()
        print(f"{i + 1}/{len(symbols)} {symbol} +{n_total}", flush=True)
    print("done", flush=True)


def fetch_micro() -> None:
    db = sqlite3.connect(MICRO_DB)
    db.execute(
        "CREATE TABLE IF NOT EXISTS px1s (market TEXT, symbol TEXT, s INTEGER,"
        " price REAL, qty REAL, buy_qty REAL, PRIMARY KEY (market, symbol, s))"
        " WITHOUT ROWID"
    )
    db.execute("CREATE TABLE IF NOT EXISTS done (url TEXT PRIMARY KEY, n INTEGER)")
    done = {u for (u,) in db.execute("SELECT url FROM done")}
    with httpx.Client(follow_redirects=True) as client:
        for symbol in LEADLAG_SYMBOLS:
            for day in LEADLAG_DAYS:
                perp = f"{BASE}/futures/um/daily/aggTrades/{symbol}/{symbol}-aggTrades-{day}.zip"
                spot = f"{BASE}/spot/daily/klines/{symbol}/1s/{symbol}-1s-{day}.zip"
                if perp not in done:
                    blob = _get(client, perp)
                    secs: dict[int, list[float]] = {}
                    for r in _rows(blob) if blob else []:
                        # agg_id, price, qty, first, last, transact_time, is_buyer_maker
                        s = _ms(r[5]) // 1000
                        px, q = float(r[1]), float(r[2])
                        # buyer is maker -> the aggressor sold
                        buy = 0.0 if r[6].lower() == "true" else q
                        cur = secs.setdefault(s, [px, 0.0, 0.0])
                        cur[0] = px  # last trade of the second
                        cur[1] += q
                        cur[2] += buy
                    db.executemany(
                        "INSERT OR REPLACE INTO px1s VALUES ('perp',?,?,?,?,?)",
                        [(symbol, s, v[0], v[1], v[2]) for s, v in secs.items()],
                    )
                    db.execute("INSERT OR REPLACE INTO done VALUES (?,?)", (perp, len(secs)))
                if spot not in done:
                    blob = _get(client, spot)
                    rows = list(_rows(blob)) if blob else []
                    db.executemany(
                        "INSERT OR REPLACE INTO px1s VALUES ('spot',?,?,?,?,?)",
                        [
                            (symbol, _ms(r[0]) // 1000, float(r[4]), float(r[5]), float(r[9]))
                            for r in rows
                        ],
                    )
                    db.execute("INSERT OR REPLACE INTO done VALUES (?,?)", (spot, len(rows)))
                db.commit()
                print(f"{symbol} {day} ok", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    {"spot": fetch_spot, "micro": fetch_micro}[sys.argv[1]]()
