"""P4 data cache: the Binance 5m archive as aligned float arrays on one grid.

Grid: bar k covers [T0 + 5m*k, T0 + 5m*(k+1)), T0 = 2025-08-01 00:00Z,
N = 122 400 bars (-> 2026-09-30 00:00Z). A bar's values are known at its END.
Missing bars are NaN. Arrays (symbols x N):
  c, h, l   - float32 prices
  qv        - float32 quote volume (USDT)
  oiv       - float32 open-interest value (USDT) from the 5m `metrics` file,
              stamped at the metric time t; used at bar k only if t <= end of
              bar k - 5 min (one bar of publication lag), i.e. oiv[k] holds the
              value stamped at the START of bar k.
  tlsp      - float32 top-trader position long/short ratio (same lag rule)
  lsa       - float32 all-account long/short ratio (same lag rule)
  cf        - float64 cumulative funding: sum of rates settled at or before
              the END of bar k (a long pays cf[x] - cf[e] holding e -> x).
  fr_last   - float32 last settled funding rate known at the end of bar k.

    python -m crypto_trading.entry_research.p4_data
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

ARCHIVE = Path("data/entry_research/archive.db")
DIR = Path("data/entry_research/p4_cache")
T0 = 1754006400000
STEP = 300_000
N = 122_400
ARRAYS = {"c": "f4", "h": "f4", "l": "f4", "qv": "f4", "oiv": "f4", "tlsp": "f4", "lsa": "f4", "cf": "f8", "fr_last": "f4"}


def symbols() -> list[str]:
    return json.loads((DIR / "symbols.json").read_text())


def load(name: str) -> np.ndarray:
    return np.load(DIR / f"{name}.npy", mmap_mode="r")


def build() -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(f"file:{ARCHIVE}?mode=ro", uri=True)
    syms = [r[0] for r in db.execute("SELECT symbol FROM klines5m GROUP BY symbol HAVING count(*) > 50000 ORDER BY symbol")]
    (DIR / "symbols.json").write_text(json.dumps(syms))
    S = len(syms)
    arr = {n: np.lib.format.open_memmap(DIR / f"{n}.npy", mode="w+", dtype=d, shape=(S, N)) for n, d in ARRAYS.items()}
    for n in arr:
        arr[n][:] = np.nan
    for i, s in enumerate(syms):
        k = np.array(db.execute("SELECT t, c, h, l, qv FROM klines5m WHERE symbol=?", (s,)).fetchall(), dtype="f8")
        idx = ((k[:, 0] - T0) // STEP).astype(int)
        ok = (idx >= 0) & (idx < N)
        for j, n in enumerate(("c", "h", "l", "qv"), start=1):
            arr[n][i, idx[ok]] = k[ok, j]
        m = np.array(db.execute("SELECT t, oi_value, top_ls_pos, ls_acct FROM metrics WHERE symbol=?", (s,)).fetchall(), dtype="f8")
        if len(m):
            # value stamped at t is used from the bar STARTING at t on (known at that bar's end)
            idx = ((m[:, 0] - T0) // STEP).astype(int)
            ok = (idx >= 0) & (idx < N)
            for j, n in enumerate(("oiv", "tlsp", "lsa"), start=1):
                row = np.full(N, np.nan)
                row[idx[ok]] = m[ok, j]
                arr[n][i] = row
        f = np.array(db.execute("SELECT t, rate FROM funding WHERE symbol=? ORDER BY t", (s,)).fetchall(), dtype="f8")
        cf = np.zeros(N)
        last = np.full(N, np.nan)
        if len(f):
            # settled at t -> counted from the bar whose END is >= t
            idx = np.ceil((f[:, 0] - T0) / STEP).astype(int) - 1
            ok = (idx >= 0) & (idx < N)
            np.add.at(cf, idx[ok], f[ok, 1])
            cf = np.cumsum(cf)
            last[idx[ok]] = f[ok, 1]
            # forward fill last known rate
            mask = ~np.isnan(last)
            pos = np.where(mask, np.arange(N), 0)
            np.maximum.accumulate(pos, out=pos)
            last = last[pos]
        arr["cf"][i] = cf
        arr["fr_last"][i] = last
        print(i, s, flush=True)
    for a in arr.values():
        a.flush()


if __name__ == "__main__":
    build()
