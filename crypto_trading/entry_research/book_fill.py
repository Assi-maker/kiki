"""Order-book depth sample from the Binance public archive (2026-09-30).

    python -m crypto_trading.entry_research.book_fill [--periods TRAIN,VALID] [--every 3]

Source: data.binance.vision `futures/um/daily/bookDepth` - snapshots every
~30 s of the cumulative bid (negative %) and ask (positive %) depth within
0.2 / 1 / 2 / 3 / 4 / 5 % of mid. Research only; the bot never imports this.

Stored per 5m bar CLOSE (point-in-time): the last snapshot at or before the
bar close, if it is at most 120 s old -> table book5m(symbol, t = bar close
ms, imb02, imb1, depth1). imb = (bid - ask) / (bid + ask) of the notional
within the band; depth1 = bid + ask notional within 1 %.

Cost control (user, 2026-09-30): the novelty check runs on TRAIN + VALID
only, on the 30 most liquid symbols and every 3rd day. TEST / HOLDOUT days
are only fetched if a feature passes the novelty check.
"""

from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, date, datetime, timedelta

from crypto_trading.entry_research import regime_lab as rl

ARCHIVE_DB = rl.ARCHIVE_DB
BASE = "https://data.binance.vision/data/futures/um/daily/bookDepth"
TOP_N = 30
WORKERS = 32
MAX_AGE_MS = 120_000


def top_symbols(conn: sqlite3.Connection, n: int = TOP_N) -> list[str]:
    """Most liquid by quote volume over TRAIN (chosen on TRAIN only)."""
    a, b = rl.PERIODS[0][1] * 1000, rl.PERIODS[0][2] * 1000
    rows = conn.execute(
        "SELECT symbol, SUM(qv) FROM klines5m WHERE t >= ? AND t < ? GROUP BY symbol"
        " ORDER BY 2 DESC LIMIT ?",
        (a, b, n),
    ).fetchall()
    return [s for s, _ in rows]


def days_for(periods: list[str], every: int) -> list[date]:
    out = []
    for name, a, b in rl.PERIODS:
        if name not in periods:
            continue
        d = datetime.fromtimestamp(a, UTC).date()
        end = datetime.fromtimestamp(b, UTC).date()
        while d < end:
            out.append(d)
            d += timedelta(days=every)
    return out


def _ms(ts: str) -> int:
    return int(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp() * 1000)


def parse(sym: str, payload: bytes) -> list[tuple]:
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        text = z.read(z.namelist()[0]).decode()
    snaps: dict[int, dict[float, float]] = {}
    for row in csv.reader(io.StringIO(text)):
        if not row or not row[0][:1].isdigit():
            continue
        snaps.setdefault(_ms(row[0]), {})[float(row[1])] = float(row[3])
    times = sorted(snaps)
    out = []
    if not times:
        return out
    first_close = times[0] - times[0] % 300_000 + 300_000
    j = 0
    t = first_close
    while t <= times[-1] + 300_000:
        while j + 1 < len(times) and times[j + 1] <= t:
            j += 1
        ts = times[j]
        if ts <= t and t - ts <= MAX_AGE_MS:
            s = snaps[ts]
            b02, a02, b1, a1 = s.get(-0.2), s.get(0.2), s.get(-1.0), s.get(1.0)
            # files before ~2026-01 have no +-0.2 % band: imb02 is then unknown
            if b1 and a1:
                imb02 = (b02 - a02) / (b02 + a02) if b02 and a02 else None
                out.append((sym, t, imb02, (b1 - a1) / (b1 + a1), b1 + a1))
        t += 300_000
    return out


def fetch(sym: str, day: date) -> tuple[str, list[tuple]]:
    url = f"{BASE}/{sym}/{sym}-bookDepth-{day.isoformat()}.zip"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return "ok", parse(sym, r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "missing", []
            err = f"http_{e.code}"
        except Exception as e:  # noqa: BLE001 - network flakiness, retried
            err = type(e).__name__
        time.sleep(2**attempt)
    return f"error:{err}", []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--periods", default="TRAIN,VALID")
    ap.add_argument("--every", type=int, default=3)
    args = ap.parse_args()
    conn = sqlite3.connect(ARCHIVE_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS book5m (symbol TEXT, t INTEGER, imb02 REAL, imb1 REAL,
            depth1 REAL, PRIMARY KEY (symbol, t)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS book_fetched (symbol TEXT, day TEXT, status TEXT, n INTEGER,
            PRIMARY KEY (symbol, day));
        """
    )
    # 'ok' with 0 rows = parsed with the pre-fix parser (no +-0.2 % band) -> refetch
    done = {
        (s, d)
        for s, d in conn.execute(
            "SELECT symbol, day FROM book_fetched"
            " WHERE status = 'missing' OR (status = 'ok' AND n > 0)"
        )
    }
    symbols = top_symbols(conn)
    days = days_for(args.periods.split(","), args.every)
    jobs = [(s, d) for s in symbols for d in days if (s, d.isoformat()) not in done]
    print(f"{len(symbols)} symbols x {len(days)} days, {len(jobs)} files to fetch", flush=True)
    with ThreadPoolExecutor(WORKERS) as pool:
        pending: dict = {}
        it = iter(jobs)
        n = 0
        while True:
            while len(pending) < 2 * WORKERS:
                j = next(it, None)
                if j is None:
                    break
                pending[pool.submit(fetch, *j)] = j
            if not pending:
                break
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for f in finished:
                s, d = pending.pop(f)
                status, rows = f.result()
                if rows:
                    conn.executemany("INSERT OR REPLACE INTO book5m VALUES (?,?,?,?,?)", rows)
                conn.execute(
                    "INSERT OR REPLACE INTO book_fetched VALUES (?,?,?,?)",
                    (s, d.isoformat(), status, len(rows)),
                )
                n += 1
                if n % 200 == 0:
                    conn.commit()
                    print(f"{n}/{len(jobs)} files", flush=True)
    conn.commit()
    print(
        conn.execute("SELECT status, COUNT(*), SUM(n) FROM book_fetched GROUP BY 1").fetchall(),
        flush=True,
    )


if __name__ == "__main__":
    main()
