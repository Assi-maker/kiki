"""Every candidate bot on the SAME last 15 days (2026-09-17 .. 2026-10-01 UTC).

Day-by-day P/L in USDT for 1000 USDT, so the user can see what each bot
would actually have done if run the way the original bot was run.

  ORIGINAL   the real LIVE bot: actual BingX fills, fees and funding of every
             trade CLOSED in the window (bot DB, read-only). Its own capital.
  TREND      P5 trend (BTC+ETH, 20/50 d, long/flat, vol target), Binance daily
  CARRY      BTC/ETH cash-and-carry (long spot + short perp), real funding,
             half the capital per asset, 2 legs of costs on entry/exit
  TREND+CARRY  trend; capital runs the carry on days the trend is flat
  GRID       buy-only grid LONG_R1_S0.2_noTF (best tick variant), BingX 1m
  SCALP      +2 USDT net take-profit, -2 USDT net stop, always long, taker
  STRADDLE   long + short together, TP 1 % / SL 0.5 % each, taker
GRID / SCALP / STRADDLE run on each of the user's 66 LIVE coins with 1000 USDT
notional; the reported day is the AVERAGE coin (= one typical coin of yours
traded with 1000 USDT), so every row is per 1000 USDT.

    uv run --with numpy --with pandas python -m crypto_trading.entry_research.last15
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pandas as pd

from crypto_trading.entry_research import grid_lab as G
from crypto_trading.entry_research import hedge_lab as HL
from crypto_trading.entry_research import scalp_lab as SC
from crypto_trading.entry_research.grid_data import DB as GRID_DB

D0, D1 = date(2026, 9, 17), date(2026, 10, 1)
DAYS = [D0 + timedelta(days=i) for i in range((D1 - D0).days + 1)]
DAY_MS = 86_400_000


def ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp() * 1000)


def original() -> dict[date, float]:
    db = sqlite3.connect("file:data/crypto_trading.db?mode=ro", uri=True)
    out = {d: 0.0 for d in DAYS}
    for closed, e, x, q, fee, fund in db.execute(
            """SELECT closed_at, exchange_fill_entry, exchange_fill_exit, entry_quantity, realized_fees_usdt,
                      realized_funding_usdt FROM live_executions WHERE phase='CLOSED'
               AND exchange_fill_entry IS NOT NULL AND exchange_fill_exit IS NOT NULL"""):
        d = datetime.fromisoformat(closed).astimezone(UTC).date()
        if D0 <= d <= D1:
            e, x, q = float(e), float(x), float(q)
            f = float(fee) if fee is not None else e * q * 0.001
            out[d] += (x - e) * q - f - float(fund or 0)
    return out


def daily_models() -> dict[str, dict[date, float]]:
    from crypto_trading.entry_research import p5_lab as L
    from crypto_trading.entry_research.p5_robust import trend_weights

    close, qv, fund = L.load()
    ret = close.pct_change(fill_method=None)
    w = trend_weights(close, ret, ["BTCUSDT", "ETHUSDT"], [20, 50], "LF")
    trend = L.simulate(w, ret, fund, L.universe(close, qv, 10))
    out, _ = L.strategies(close, qv, fund)
    carry = (out["F5_BTC_thr0"] + out["F5_ETH_thr0"]) / 2
    off = w[["BTCUSDT", "ETHUSDT"]].sum(axis=1).shift(1).fillna(0) == 0
    switch = off.astype(int).diff().abs().fillna(0)
    combo = trend.where(~off, carry) - switch * 0.0008
    res = {}
    for name, s in (("TREND", trend), ("CARRY", carry), ("TREND+CARRY", combo)):
        res[name] = {d: 1000.0 * float(s.get(pd.Timestamp(d), 0.0)) for d in DAYS}
    return res


def intraday_models() -> dict[str, dict[date, float]]:
    db = sqlite3.connect(f"file:{GRID_DB}?mode=ro", uri=True)
    syms = [r[0] for r in db.execute("SELECT symbol FROM done ORDER BY symbol")]
    grid = {d: 0.0 for d in DAYS}
    scalp = {d: 0.0 for d in DAYS}
    strad = {d: 0.0 for d in DAYS}
    v = next(x for x in G.VARIANTS if x.name == "LONG_R1_S0.2_noTF")
    a, b = ms(D0), ms(D1) + DAY_MS
    SC.MAX_HOLD = 60
    for sym in syms:
        bars, fund = G.load(sym, db)
        sig = G.sigma_series([x[4] for x in bars])
        i0 = next(i for i, x in enumerate(bars) if x[0] >= a)
        i1 = next((i for i, x in enumerate(bars) if x[0] >= b), len(bars))
        daily = {}
        G.run_grid(bars, fund, sig, i0, i1, v, 1000.0, daily)
        for k, val in daily.items():
            d = datetime.fromtimestamp(int(k) * 86400, UTC).date()
            if d in grid:
                grid[d] += val / len(syms)
        p, t = SC.run(bars[i0 - 6:i1], "LONG", -2.0, SC.TAKER, False)
        for val, tt in zip(p, t):
            d = datetime.fromtimestamp(tt / 1000, UTC).date()
            if d in scalp:
                scalp[d] += val / len(syms)
        p, t = HL.straddle(bars[i0:i1], 0.01, 0.005)
        for val, tt in zip(p, t):
            d = datetime.fromtimestamp(tt / 1000, UTC).date()
            if d in strad:
                strad[d] += val / len(syms)
    return {"GRID": grid, "SCALP": scalp, "STRADDLE": strad}


def main() -> None:
    bots = {"ORIGINAL (LIVE)": original(), **daily_models(), **intraday_models()}
    df = pd.DataFrame(bots).loc[DAYS]
    df.index = [d.strftime("%m-%d") for d in DAYS]
    pd.set_option("display.width", 250)
    print("USDT per day (per 1000 USDT; ORIGINAL = its real LIVE account)")
    print(df.round(1).to_string())
    print()
    rows = []
    for name, s in df.items():
        wins, losses = s[s > 0].sum(), -s[s < 0].sum()
        cum = s.cumsum()
        rows.append({"bot": name, "SUM 15 d": s.sum(), "avg/day": s.mean(),
                     "win days": f"{(s > 0).sum()}/{(s != 0).sum()}",
                     "sum of wins": wins, "sum of losses": -losses,
                     "losses eat % of wins": 100 * losses / wins if wins > 0 else float("inf"),
                     "best day": s.max(), "worst day": s.min(), "lowest point": cum.min()})
    print(pd.DataFrame(rows).set_index("bot").round(1).to_string())


if __name__ == "__main__":
    main()
