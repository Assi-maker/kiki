"""Scalping lab: small fixed net take-profit, small stop, repeat (pre-registered).

The user's idea (2026-10-02): take ~+2 USDT net after fees quickly, cap a loss
small, immediately start the next trade, accumulate over hours.

Data: the user's own BingX 1m candles (grid.db: 66 LIVE coins, 2026-09-05 ..
2026-10-01). Position 1000 USDT notional (100 margin x 10, the LIVE size).

Mechanics per coin, one position at a time, re-enter at the close of the bar
after an exit:
  * TP and SL are set in NET USDT after fees: TP = +2; SL in {-2, -4, -6}
  * fees: entry and exit 0.05 % taker each (1.0 USDT round trip), or a MAKER
    variant (0.02 % entry, 0.05 % exit = 0.7 USDT)
  * fills on 1m candles: TP if the bar trades through the TP price, SL if it
    trades through the SL price; if BOTH are inside one bar the SL is taken
    (pessimistic) - the OPTIMISTIC bound (TP first) is reported as well
  * entry direction rules (all known at the entry close, no look-ahead):
      LONG   always long
      SHORT  always short
      MOM    direction of the last 5-minute return
      REV    against the last 5-minute return
  * max holding 60 min, then exit at the close (taker)
Variants: 4 directions x 3 stops x 2 fee modes = 24.
"Works" only if the PESSIMISTIC result is > 0 in total AND in both halves of
the window AND on a majority of coins.

    python -m crypto_trading.entry_research.scalp_lab
"""

from __future__ import annotations

import itertools
import sqlite3

from crypto_trading.entry_research.grid_data import DB

NOTIONAL = 1000.0
TAKER = 0.0005
MAKER = 0.0002
TP_NET = 2.0
MAX_HOLD = 60


def run(bars, direction: str, sl_net: float, entry_fee: float, optimistic: bool) -> tuple[list[float], list[int]]:
    fees = NOTIONAL * (entry_fee + TAKER)
    tp_move = (TP_NET + fees) / NOTIONAL      # gross price move needed for +2 net
    sl_move = (abs(sl_net) - fees) / NOTIONAL  # gross adverse move that gives sl_net
    pnls, times = [], []
    i = 6
    n = len(bars)
    while i < n - 1:
        c = bars[i][4]
        r5 = c / bars[i - 5][4] - 1
        side = {"LONG": 1, "SHORT": -1, "MOM": 1 if r5 >= 0 else -1, "REV": -1 if r5 >= 0 else 1}[direction]
        if sl_move <= 0:
            return [], []
        tp = c * (1 + side * tp_move)
        sl = c * (1 - side * sl_move)
        j = i + 1
        result = None
        while j < n and j <= i + MAX_HOLD:
            h, lo = bars[j][2], bars[j][3]
            hit_tp = h > tp if side > 0 else lo < tp
            hit_sl = lo < sl if side > 0 else h > sl
            if hit_tp and hit_sl:
                result = TP_NET if optimistic else sl_net
            elif hit_tp:
                result = TP_NET
            elif hit_sl:
                result = sl_net
            if result is not None:
                break
            j += 1
        if result is None:
            j = min(j, n - 1)
            result = side * (bars[j][4] / c - 1) * NOTIONAL - fees
        pnls.append(result)
        times.append(bars[i][0])
        i = j + 1
    return pnls, times


def main() -> None:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    syms = [r[0] for r in db.execute("SELECT symbol FROM done ORDER BY symbol")]
    data = {s: db.execute("SELECT t, o, h, l, c FROM k1m WHERE symbol=? ORDER BY t", (s,)).fetchall()[1440:]
            for s in syms}
    mid = sorted(data[syms[0]])[len(data[syms[0]]) // 2][0]
    print(f"{'variant':26s} {'trades':>7s} {'win%':>5s} {'total':>9s} {'per trade':>9s} {'H1':>8s} {'H2':>8s} "
          f"{'coins+':>6s} | {'optimistic total':>16s}")
    for d, sl, (fname, fee) in itertools.product(("LONG", "SHORT", "MOM", "REV"), (-2.0, -4.0, -6.0),
                                                  (("taker", TAKER), ("maker", MAKER))):
        allp, h1, h2, coins_pos, opt_total = [], 0.0, 0.0, 0, 0.0
        for s in syms:
            p, t = run(data[s], d, sl, fee, False)
            po, _ = run(data[s], d, sl, fee, True)
            allp += p
            h1 += sum(x for x, tt in zip(p, t) if tt < mid)
            h2 += sum(x for x, tt in zip(p, t) if tt >= mid)
            coins_pos += sum(p) > 0
            opt_total += sum(po)
        total = sum(allp)
        works = total > 0 and h1 > 0 and h2 > 0 and coins_pos > len(syms) / 2
        print(f"{d + ' SL' + str(int(sl)) + ' ' + fname:26s} {len(allp):7d} {100 * sum(x > 0 for x in allp) / len(allp):5.1f} "
              f"{total:9.0f} {total / len(allp):9.3f} {h1:8.0f} {h2:8.0f} {coins_pos:3d}/{len(syms)} | {opt_total:16.0f}"
              + ("  WORKS" if works else ""))


if __name__ == "__main__":
    main()
