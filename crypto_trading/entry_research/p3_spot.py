"""P3-C at the bot's own time scale: spot/perp features per baseline event.

For every P3 baseline event of a symbol with spot 1m history (p3_data.py),
from data closed at T only:
  spot_lead5/15/30  spot return - perp return over the last 5/15/30 min
                    (positive = spot has moved further up than the perp);
  spot_ti15         spot taker imbalance over the last 15 min;
  ti_gap15          spot taker imbalance - perp taker imbalance (15 min);
  basis             log(perp close / spot close) at T;
  dbasis60          basis change over the last 60 min.
Writes data/entry_research/p3_spot_features.db (read by p3_analysis).

    python -m crypto_trading.entry_research.p3_spot
"""

from __future__ import annotations

import math
import sqlite3

from crypto_trading.entry_research.p3_data import ARCHIVE, SPOT_DB
from crypto_trading.entry_research.p3_lab import EVENTS_DB

OUT_DB = "data/entry_research/p3_spot_features.db"


def _spot(db: sqlite3.Connection, symbol: str) -> dict[int, tuple[float, float, float]]:
    return {
        t // 1000: (c, v, tbv)
        for t, c, v, tbv in db.execute(
            "SELECT t, c, v, tbv FROM spot1m WHERE symbol = ?", (symbol,)
        )
    }


def _perp(db: sqlite3.Connection, symbol: str) -> dict[int, float]:
    return {
        t // 1000: c for t, c in db.execute("SELECT t, c FROM klines5m WHERE symbol = ?", (symbol,))
    }


def features(T: int, perp_ti15: float | None, spot: dict, perp: dict) -> dict | None:
    """Spot close at X = close of the 1m bar opening at X - 60; perp close at
    X = close of the 5m bar opening at X - 300."""

    def sc(x: int) -> float | None:
        bar = spot.get(x - 60)
        return bar[0] if bar else None

    def pc(x: int) -> float | None:
        return perp.get(x - 300)

    s0, p0 = sc(T), pc(T)
    if not s0 or not p0:
        return None
    out: dict = {"basis": math.log(p0 / s0)}
    for m in (5, 15, 30):
        s1, p1 = sc(T - 60 * m), pc(T - 60 * m)
        out[f"spot_lead{m}"] = (s0 / s1 - p0 / p1) if s1 and p1 else None
    s60, p60 = sc(T - 3600), pc(T - 3600)
    out["dbasis60"] = out["basis"] - math.log(p60 / s60) if s60 and p60 else None
    bars = [spot.get(T - 60 * j) for j in range(1, 16)]
    if all(bars) and sum(b[1] for b in bars) > 0:
        vol = sum(b[1] for b in bars)
        out["spot_ti15"] = (2 * sum(b[2] for b in bars) - vol) / vol
        out["ti_gap15"] = out["spot_ti15"] - perp_ti15 if perp_ti15 is not None else None
    else:
        out["spot_ti15"] = out["ti_gap15"] = None
    return out


def main() -> None:
    sdb = sqlite3.connect(f"file:{SPOT_DB}?mode=ro", uri=True)
    adb = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True)
    edb = sqlite3.connect(f"file:{EVENTS_DB}?mode=ro", uri=True)
    symbols = [s for (s,) in sdb.execute("SELECT DISTINCT symbol FROM spot1m")]
    out = sqlite3.connect(OUT_DB)
    out.execute("DROP TABLE IF EXISTS spot_features")
    cols = (
        "spot_lead5",
        "spot_lead15",
        "spot_lead30",
        "spot_ti15",
        "ti_gap15",
        "basis",
        "dbasis60",
    )
    out.execute(f"CREATE TABLE spot_features (symbol, T, {', '.join(cols)})")
    total = 0
    for symbol in symbols:
        spot, perp = _spot(sdb, symbol), _perp(adb, symbol)
        rows = []
        for T, ti15 in edb.execute("SELECT T, ti15 FROM events WHERE symbol = ?", (symbol,)):
            f = features(T, ti15, spot, perp)
            if f is not None:
                rows.append((symbol, T, *(f.get(c) for c in cols)))
        out.executemany(
            f"INSERT INTO spot_features VALUES ({','.join('?' * (len(cols) + 2))})", rows
        )
        out.commit()
        total += len(rows)
        print(symbol, len(rows), flush=True)
    print("done", total, flush=True)


if __name__ == "__main__":
    main()
