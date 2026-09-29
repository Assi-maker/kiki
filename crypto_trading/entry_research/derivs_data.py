"""Historical open interest + funding for the event research (2026-09-29).

    python -m crypto_trading.entry_research.derivs_data

Read-only public GETs, research only - nothing here is imported by the bot.
- Open interest: BingX has no OI history, so Binance USD-M
  `futures/data/openInterestHist` (5m, only the last 30 days) is used as a
  cross-venue PROXY. A value stamped `timestamp` is only used at T >= timestamp
  + 5 min (one extra period of lag - point-in-time safe).
- Funding: BingX `quote/fundingRate` history (settled rates). A settled rate
  is known at its fundingTime; it is only used at T >= fundingTime.
Stored in data/entry_research/derivs.db (gitignored).
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

KLINES_DB = "data/entry_research/klines.db"
DERIVS_DB = "data/entry_research/derivs.db"
START_MS = 1788220800000  # 2026-09-01T00:00Z
OI_URL = "https://fapi.binance.com/futures/data/openInterestHist"
FUNDING_URL = "https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate"
OI_STEP_MS = 500 * 300_000  # 500 x 5m per request


def binance_symbol(bingx_symbol: str) -> str:
    return bingx_symbol.replace("-", "")


def _get(url: str) -> object:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def _init(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS oi"
        " (symbol TEXT, t INTEGER, oi_value REAL, PRIMARY KEY (symbol, t))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS funding"
        " (symbol TEXT, t INTEGER, rate REAL, PRIMARY KEY (symbol, t))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fetched"
        " (symbol TEXT, kind TEXT, status TEXT, PRIMARY KEY (symbol, kind))"
    )


def _fetch_symbol(sym: str, now_ms: int, need: set[str]) -> dict:
    """Network only (runs in a worker thread); the main thread writes."""
    out: dict = {}
    if "funding" in need:
        try:
            data = _get(
                f"{FUNDING_URL}?symbol={sym}&startTime={START_MS}&endTime={now_ms}&limit=1000"
            )
            rows = [
                (sym, int(d["fundingTime"]) // 1000, float(d["fundingRate"]))
                for d in data.get("data") or []
            ]
            out["funding"] = (rows, f"OK {len(rows)}")
        except (urllib.error.URLError, KeyError, ValueError) as e:
            out["funding"] = ([], f"ERR {e}"[:200])
    if "oi" in need:
        status, rows = "OK", []
        for start in range(START_MS, now_ms, OI_STEP_MS):
            url = (
                f"{OI_URL}?symbol={binance_symbol(sym)}&period=5m&limit=500"
                f"&startTime={start}&endTime={min(start + OI_STEP_MS, now_ms)}"
            )
            try:
                data = _get(url)
            except urllib.error.HTTPError as e:
                status = f"HTTP {e.code}"  # not listed on Binance - no proxy for this symbol
                break
            except urllib.error.URLError as e:
                status = f"ERR {e}"
                break
            rows += [
                (sym, int(d["timestamp"]) // 1000, float(d["sumOpenInterestValue"])) for d in data
            ]
            time.sleep(1.2)  # 3 workers -> ~2 req/s, under Binance's ~3.3 req/s IP limit
        out["oi"] = (rows, f"{status} {len(rows)}")
    return out


def fetch_all(now_ms: int | None = None, workers: int = 3) -> None:
    from crypto_trading.entry_research.universe_fill import top_by_volume

    now_ms = now_ms or int(time.time() * 1000)
    cached = [
        s
        for (s,) in sqlite3.connect(f"file:{KLINES_DB}?mode=ro", uri=True).execute(
            "SELECT DISTINCT symbol FROM klines"
        )
        if s.endswith("-USDT")
    ]
    symbols = sorted(set(cached) | set(top_by_volume()))
    conn = sqlite3.connect(DERIVS_DB)
    _init(conn)
    done = {(s, k) for s, k in conn.execute("SELECT symbol, kind FROM fetched")}
    todo = {s: {k for k in ("funding", "oi") if (s, k) not in done} for s in symbols}
    todo = {s: need for s, need in todo.items() if need}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_symbol, s, now_ms, need): s for s, need in todo.items()}
        for n, fut in enumerate(as_completed(futures)):
            sym = futures[fut]
            for kind, (rows, status) in fut.result().items():
                table = "funding" if kind == "funding" else "oi"
                conn.executemany(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?)", rows)
                conn.execute("INSERT OR REPLACE INTO fetched VALUES (?,?,?)", (sym, kind, status))
            conn.commit()
            print(f"{n + 1}/{len(todo)} {sym}", flush=True)


if __name__ == "__main__":
    fetch_all()
