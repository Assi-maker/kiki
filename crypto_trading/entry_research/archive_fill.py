"""12+ months of Binance USD-M history from the public archive (2026-09-30).

    python -m crypto_trading.entry_research.archive_fill

Source: https://data.binance.vision (static zip files, no API key, no rate
limit on the trading API - the LIVE bot's BingX/Binance quotas are untouched).
Research only - the bot never imports this module.

- klines 5m:  monthly files for full months, daily files for the current month
- funding:    monthly fundingRate files (settled rate, known at calc_time)
- metrics:    daily files, 5m snapshots of open interest, top-trader and
              taker long/short ratios. A snapshot stamped t is only usable at
              T >= t + 5 min (same one-period lag as derivs_data).

Universe: every symbol in the existing research kline cache (the BingX
candidate history + LIVE top-80) that also trades on Binance USD-M. Known
bias: chosen by TODAY's listing, so symbols delisted during the window are
missing (survivorship) - stated in every report built on this data.

Stored in data/entry_research/archive.db (gitignored). Resumable: every
(symbol, kind, period) file is recorded in `fetched` with its status, and a
re-run only fetches what is missing.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import UTC, date, datetime, timedelta

KLINES_DB = "data/entry_research/klines.db"
ARCHIVE_DB = "data/entry_research/archive.db"
BASE = "https://data.binance.vision/data/futures/um"
FIRST_MONTH = date(2025, 8, 1)  # 14 months incl. the current one
# Threads: an uncached archive file takes 5-15 s (CDN origin latency), so
# throughput scales with concurrency; parsing is cheap.
WORKERS = 128  # in-flight window is bounded (2 x WORKERS), so memory stays flat
EXTRA_SYMBOLS = ("BTCUSDT", "ETHUSDT")  # market-regime references, always present


def _months(first: date, last_full: date) -> list[date]:
    out, d = [], first
    while d <= last_full:
        out.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def plan(today: date) -> list[tuple[str, str]]:
    """(kind, period) pairs. The current month has no monthly file yet, so it
    is covered by daily files up to yesterday (today's file is not published)."""
    this_month = today.replace(day=1)
    last_full = date(this_month.year - (this_month.month == 1), (this_month.month - 2) % 12 + 1, 1)
    yesterday = today - timedelta(days=1)
    jobs = [("klines", m.strftime("%Y-%m")) for m in _months(FIRST_MONTH, last_full)]
    jobs += [("klines_d", d.isoformat()) for d in _days(this_month, yesterday)]
    jobs += [("funding", m.strftime("%Y-%m")) for m in _months(FIRST_MONTH, last_full)]
    jobs += [("metrics", d.isoformat()) for d in _days(FIRST_MONTH, yesterday)]
    return jobs


def url(symbol: str, kind: str, period: str) -> str:
    if kind == "klines":
        return f"{BASE}/monthly/klines/{symbol}/5m/{symbol}-5m-{period}.zip"
    if kind == "klines_d":
        return f"{BASE}/daily/klines/{symbol}/5m/{symbol}-5m-{period}.zip"
    if kind == "funding":
        return f"{BASE}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{period}.zip"
    if kind == "metrics":
        return f"{BASE}/daily/metrics/{symbol}/{symbol}-metrics-{period}.zip"
    raise ValueError(kind)


def _rows(payload: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = list(csv.reader(io.StringIO(text)))
    if rows and rows[0] and not rows[0][0][:1].isdigit():  # newer files carry a header
        rows = rows[1:]
    return [r for r in rows if r]


def _ms(ts: str) -> int:
    return int(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp() * 1000)


def _f(x: str) -> float | None:
    return float(x) if x not in ("", None) else None


def parse(kind: str, symbol: str, rows: list[list[str]]) -> list[tuple]:
    if kind in ("klines", "klines_d"):
        # open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,...
        return [
            (
                symbol,
                int(r[0]),
                float(r[1]),
                float(r[2]),
                float(r[3]),
                float(r[4]),
                float(r[5]),
                float(r[7]),
                int(r[8]),
                float(r[9]),
            )
            for r in rows
        ]
    if kind == "funding":
        # calc_time,funding_interval_hours,last_funding_rate
        return [(symbol, int(r[0]), float(r[2])) for r in rows]
    if kind == "metrics":
        # create_time,symbol,sum_open_interest,sum_open_interest_value,
        # count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,
        # count_long_short_ratio,sum_taker_long_short_vol_ratio
        return [
            (symbol, _ms(r[0]), _f(r[2]), _f(r[3]), _f(r[4]), _f(r[5]), _f(r[6]), _f(r[7]))
            for r in rows
        ]
    raise ValueError(kind)


def fetch(symbol: str, kind: str, period: str) -> tuple[str, list[tuple]]:
    """Network + parse only (worker thread). Returns (status, rows)."""
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url(symbol, kind, period), timeout=60) as r:
                return "ok", parse(kind, symbol, _rows(r.read()))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "missing", []  # not listed yet / delisted / not published
            err = f"http_{e.code}"
        except Exception as e:  # noqa: BLE001 - network flakiness, retried
            err = type(e).__name__
        time.sleep(2**attempt)
    return f"error:{err}", []


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS klines5m (symbol TEXT, t INTEGER, o REAL, h REAL,
            l REAL, c REAL, v REAL, qv REAL, n INTEGER, tbv REAL,
            PRIMARY KEY (symbol, t)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS funding (symbol TEXT, t INTEGER, rate REAL,
            PRIMARY KEY (symbol, t)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS metrics (symbol TEXT, t INTEGER, oi REAL,
            oi_value REAL, top_ls_acct REAL, top_ls_pos REAL, ls_acct REAL,
            taker_ls_vol REAL, PRIMARY KEY (symbol, t)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS fetched (symbol TEXT, kind TEXT, period TEXT,
            status TEXT, n INTEGER, PRIMARY KEY (symbol, kind, period));
        """
    )


TABLE = {"klines": "klines5m", "klines_d": "klines5m", "funding": "funding", "metrics": "metrics"}


def universe() -> list[str]:
    cached = [
        s for (s,) in sqlite3.connect(KLINES_DB).execute("SELECT DISTINCT symbol FROM klines")
    ]
    syms = {s.replace("-", "") for s in cached if s.endswith("-USDT")} | set(EXTRA_SYMBOLS)
    return sorted(syms)


def main() -> None:
    today = datetime.now(UTC).date()
    conn = sqlite3.connect(ARCHIVE_DB)
    conn.execute("PRAGMA journal_mode=WAL")  # the lab can read while this writes
    init(conn)
    done = {
        (s, k, p)
        for s, k, p in conn.execute(
            "SELECT symbol, kind, period FROM fetched WHERE status IN ('ok', 'missing')"
        )
    }
    symbols = universe()
    # A symbol absent from the latest daily kline file is not a Binance USD-M
    # perp at all: probe it once and skip the other ~500 files.
    probe_day = (today - timedelta(days=2)).isoformat()
    jobs = [(s, k, p) for s in symbols for k, p in plan(today) if (s, k, p) not in done]
    print(f"{len(symbols)} symbols, {len(jobs)} files to fetch", flush=True)
    missing_symbols: set[str] = set()
    with ThreadPoolExecutor(WORKERS) as pool:
        probes = {pool.submit(fetch, s, "klines_d", probe_day): s for s in symbols}
        for f in as_completed(probes):
            if f.result()[0] == "missing":
                missing_symbols.add(probes[f])
        if missing_symbols:
            print(
                f"not on Binance USD-M ({len(missing_symbols)}): {sorted(missing_symbols)}",
                flush=True,
            )
        jobs = [j for j in jobs if j[0] not in missing_symbols]
        # Bounded window: at most 2 x WORKERS files in flight / in memory, and
        # each result is released as soon as it is written. (Submitting every
        # job at once kept every finished result alive - the run of
        # 2026-09-30 was killed for low memory.)
        pending: dict = {}
        it = iter(jobs)
        i = 0
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
                s, k, p = pending.pop(f)
                status, rows = f.result()
                if rows:
                    ph = ",".join("?" * len(rows[0]))
                    conn.executemany(f"INSERT OR REPLACE INTO {TABLE[k]} VALUES ({ph})", rows)
                conn.execute(
                    "INSERT OR REPLACE INTO fetched VALUES (?,?,?,?,?)",
                    (s, k, p, status, len(rows)),
                )
                del rows
                i += 1
                if i % 500 == 0:
                    conn.commit()
                    print(f"{i}/{len(jobs)} files", flush=True)
    conn.commit()
    summary = conn.execute(
        "SELECT kind, status, COUNT(*), SUM(n) FROM fetched GROUP BY kind, substr(status,1,5)"
    ).fetchall()
    for row in summary:
        print(row, flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
