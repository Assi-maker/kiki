"""Local point-in-time data for the evidence classifier (shadow only).

The live classifier fetched ~33 days of Binance 5m history per candidate and
the whole research universe per hour from the public API (~3 min per
candidate). The same Binance data is already on disk:

- `data/entry_research/archive.db`: klines5m and metrics (open interest),
  keys in milliseconds; funding up to 2026-08-31;
- `data/entry_research/derivs.db`: funding and 5m open interest for
  September, keys in seconds;
- `data/evidence_shadow_pit.db` (this module's own gap store): whatever was
  published after the research files end, fetched from the same public
  endpoints (binance_data.py).

`LocalSource` answers exactly the questions binance_data answers, from these
files, under the SAME temporal contract: a 5m bar only if it closed at or
before the cutoff, a funding rate only if settled at or before it, an OI
stamp only if <= it. The classifier then applies its own cut-offs again
(defence in depth). A symbol that is not on disk falls back to the API.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from crypto_trading.evidence_shadow import binance_data as bd
from crypto_trading.evidence_shadow import classifier as cl

ARCHIVE = "data/entry_research/archive.db"
DERIVS = "data/entry_research/derivs.db"
GAP = "data/evidence_shadow_pit.db"
BAR_S = 300

GAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS klines5m (symbol TEXT, t INTEGER, o REAL, h REAL, l REAL,
    c REAL, v REAL, PRIMARY KEY (symbol, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS funding (symbol TEXT, t INTEGER, rate REAL,
    PRIMARY KEY (symbol, t)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS oi (symbol TEXT, t INTEGER, v REAL,
    PRIMARY KEY (symbol, t)) WITHOUT ROWID;
"""


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True,
                           check_same_thread=False)


def open_gap(path: str = GAP) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(GAP_SCHEMA)
    return c


class LocalSource:
    def __init__(self, archive: str = ARCHIVE, derivs: str = DERIVS, gap: str = GAP) -> None:
        self.a = _ro(archive)
        self.d = _ro(derivs)
        self.g = open_gap(gap)
        self.symbols = {s for (s,) in self.a.execute("SELECT DISTINCT symbol FROM metrics")}
        self.api_fallbacks = 0

    # ---------------------------------------------------------------- reads

    def klines_5m(self, sym: str, start: int, cutoff: int) -> list[tuple]:
        out: dict[int, tuple] = {}
        for t, o, h, lo, c, v in self.a.execute(
            "SELECT t, o, h, l, c, v FROM klines5m WHERE symbol = ? AND t > ? AND t <= ?",
            (sym, start * 1000, (cutoff - BAR_S) * 1000),
        ):
            out[t // 1000] = (t // 1000, o, h, lo, c, v)
        for row in self.g.execute(
            "SELECT t, o, h, l, c, v FROM klines5m WHERE symbol = ? AND t > ? AND t <= ?",
            (sym, start, cutoff - BAR_S),
        ):
            out.setdefault(row[0], tuple(row))
        return [out[k] for k in sorted(out) if k + BAR_S <= cutoff]

    def funding(self, sym: str, start: int, cutoff: int) -> list[tuple]:
        out: dict[int, float] = {}
        for t, r in self.a.execute(
            "SELECT t, rate FROM funding WHERE symbol = ? AND t >= ? AND t <= ?",
            (sym, start * 1000, cutoff * 1000 + 999),
        ):
            out[t // 1000] = r
        for db in (self.d, self.g):
            for t, r in db.execute(
                "SELECT t, rate FROM funding WHERE symbol = ? AND t >= ? AND t <= ?",
                (sym, start, cutoff),
            ):
                out.setdefault(t, r)
        return sorted((t, r) for t, r in out.items() if t <= cutoff)

    def open_interest(self, sym: str, start: int, cutoff: int) -> list[tuple]:
        out: dict[int, float] = {}
        for t, v in self.a.execute(
            "SELECT t, oi_value FROM metrics WHERE symbol = ? AND t >= ? AND t <= ?",
            (sym, start * 1000, cutoff * 1000),
        ):
            if v is not None:
                out[t // 1000] = v
        for t, v in self.d.execute(
            "SELECT t, oi_value FROM oi WHERE symbol = ? AND t >= ? AND t <= ?",
            (sym, start, cutoff),
        ):
            out.setdefault(t, v)
        for t, v in self.g.execute(
            "SELECT t, v FROM oi WHERE symbol = ? AND t >= ? AND t <= ?", (sym, start, cutoff)
        ):
            out.setdefault(t, v)
        return sorted((t, v) for t, v in out.items() if t <= cutoff)

    # --------------------------------------------- the classifier's interface

    def fetch_symbol(self, sym: str, cutoff: int, history_s: int = cl.HISTORY_S) -> cl.SymbolData:
        if sym not in self.symbols:
            self.api_fallbacks += 1
            return cl.fetch_symbol(sym, cutoff, history_s)
        start = cutoff - history_s
        return cl.SymbolData(
            self.klines_5m(sym, start, cutoff),
            self.funding(sym, cutoff - 10 * cl.DAY, cutoff),
            self.open_interest(sym, cutoff - 2 * cl.DAY, cutoff),
        )

    def fetch_universe_at(self, symbols: list[str], H: int) -> dict[str, cl.SymbolData]:
        return {
            s: cl.SymbolData(
                self.klines_5m(s, H - 5 * cl.HOUR, H), self.funding(s, H - 10 * cl.HOUR, H), []
            )
            for s in symbols
            if s in self.symbols
        }

    # ------------------------------------------------------------- gap fill

    def _last(self, sym: str, kind: str) -> int:
        """Newest stamp on disk for a symbol (seconds), across all files."""
        if kind == "klines":
            qs = [(self.a, "SELECT MAX(t)/1000 FROM klines5m WHERE symbol = ?"),
                  (self.g, "SELECT MAX(t) FROM klines5m WHERE symbol = ?")]
        elif kind == "funding":
            qs = [(self.a, "SELECT MAX(t)/1000 FROM funding WHERE symbol = ?"),
                  (self.d, "SELECT MAX(t) FROM funding WHERE symbol = ?"),
                  (self.g, "SELECT MAX(t) FROM funding WHERE symbol = ?")]
        else:
            qs = [(self.a, "SELECT MAX(t)/1000 FROM metrics WHERE symbol = ?"),
                  (self.d, "SELECT MAX(t) FROM oi WHERE symbol = ?"),
                  (self.g, "SELECT MAX(t) FROM oi WHERE symbol = ?")]
        return max((db.execute(q, (sym,)).fetchone()[0] or 0) for db, q in qs)

    def fill_gaps(self, until: int, symbols: list[str] | None = None) -> int:
        """Append what Binance published after the files end, up to `until`.
        Only ever adds rows; each row still carries its own timestamp and is
        cut at every decision time by the readers above."""
        n = 0
        for sym in sorted(symbols or self.symbols):
            k0 = self._last(sym, "klines")
            if until - k0 > BAR_S:
                rows = bd.klines_5m(sym, k0, until)
                self.g.executemany("INSERT OR IGNORE INTO klines5m VALUES (?,?,?,?,?,?,?)",
                                   [(sym, *r) for r in rows])
                n += len(rows)
            f0 = self._last(sym, "funding")
            if until - f0 > 8 * 3600:
                rows = bd.funding(sym, f0 + 1, until)
                self.g.executemany("INSERT OR IGNORE INTO funding VALUES (?,?,?)",
                                   [(sym, *r) for r in rows])
                n += len(rows)
            o0 = self._last(sym, "oi")
            if until - o0 > BAR_S:
                rows = bd.open_interest(sym, o0 + 1, until)
                self.g.executemany("INSERT OR IGNORE INTO oi VALUES (?,?,?)",
                                   [(sym, *r) for r in rows])
                n += len(rows)
            self.g.commit()
        return n


if __name__ == "__main__":
    t0 = time.time()
    src = LocalSource()
    print("rows added:", src.fill_gaps(int(time.time())), f"in {time.time() - t0:.0f}s")
