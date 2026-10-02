"""Trend-gated short-term trading (pre-registered before any result).

Question: does short-term trading ON TOP of the P5 trend signal (the only
strategy that passed an honest test) add daily profit, if it may only trade
long and only while the trend is up?

Trend signal (P5, frozen): BTC/ETH ensemble, sign of the 20- and 50-day
return, long/flat, decided at the daily close from Binance daily data.
Gate for UTC day D = trend weight at the close of D-1 > 0 (no look-ahead).

Data: P4 Binance 5m archive 2025-08-01 .. 2026-09-30 with real funding.
Halves: H1 2025-08..2026-02, H2 2026-03..2026-09.

Benchmark T: the P5 trend strategy itself on 1000 USDT (daily P/L).
Overlays, each 1000 USDT notional per coin, only inside gate-on windows, a
window's open trades are closed at market when the gate turns off:
  G-own   buy-only grid LONG_R1_S0.2_noTF (best tick variant) on BTC and ETH,
          each gated by its OWN trend
  G-mkt   the same grid on all 133 coins, gated by the BTC trend
  S-own   long scalping (TP +2 USDT net, SL -4 net, taker, max 1 h) on BTC
          and ETH, gated by own trend
  S-mkt   the same scalping on all 133 coins, gated by the BTC trend
Engines: grid_lab.run_grid (integer-held fix) and scalp_lab.run (5m bars,
max hold 12 bars). 5m candles overstate long-grid P/L vs 1m (grid_resolution),
so any bias favours the overlays.

"Adds value" only if the overlay's P/L is > 0 in BOTH halves AND T + overlay
has a higher share of winning days than T alone in both halves.

    uv run --with numpy --with pandas python -m crypto_trading.entry_research.combo_lab
"""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.entry_research import grid_lab as G
from crypto_trading.entry_research import grid_lab14 as L14
from crypto_trading.entry_research import scalp_lab as SC

OUT = Path("docs/superpowers/reports/2026-10-02-combo-lab.json")
DAY = 86_400_000
SPLIT_DAY = int(datetime(2026, 3, 1, tzinfo=UTC).timestamp() * 1000) // DAY
GRID = next(v for v in G.VARIANTS if v.name == "LONG_R1_S0.2_noTF")


def trend_gate() -> tuple[dict, dict, dict]:
    """day index -> gate (bool) for BTC and ETH, and T's daily P/L on 1000 USDT."""
    from crypto_trading.entry_research import p5_lab as P5
    from crypto_trading.entry_research.p5_robust import trend_weights

    close, qv, fund = P5.load()
    ret = close.pct_change(fill_method=None)
    w = trend_weights(close, ret, ["BTCUSDT", "ETHUSDT"], [20, 50], "LF")
    s = P5.simulate(w, ret, fund, P5.universe(close, qv, 10))
    gate = {}
    for a in ("BTCUSDT", "ETHUSDT"):
        prev = w[a].shift(1)  # weight decided at the close of D-1 holds over day D
        gate[a] = {int(d.value // 1_000_000) // DAY: bool(x > 0) for d, x in prev.items() if x == x}
    t_daily = {int(d.value // 1_000_000) // DAY: 1000.0 * float(x) for d, x in s.items()}
    return gate["BTCUSDT"], gate["ETHUSDT"], t_daily


def windows(bars, gate: dict) -> list[tuple[int, int]]:
    out, start = [], None
    for i, b in enumerate(bars):
        on = gate.get(b[0] // DAY, False)
        if on and start is None:
            start = i
        elif not on and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(bars)))
    return out


def job(args: tuple[int, str]) -> dict | None:
    k, gate_name = args
    btc, eth, _ = trend_gate()
    sym = json.loads((L14.CACHE / "symbols.json").read_text())[k]
    s = L14.series(k)
    if s is None:
        return None
    bars, fund = s
    gate = {"BTC": btc, "ETH": eth}[gate_name]
    sig = G.sigma_series([b[4] for b in bars], L14.BPD)
    grid_daily, scalp_daily = {}, {}
    SC.MAX_HOLD = 12
    for i0, i1 in windows(bars, gate):
        i0 = max(i0, L14.BPD + 1)
        if i1 - i0 < 12:
            continue
        G.run_grid(bars, fund, sig, i0, i1, GRID, 1000.0, grid_daily, L14.BPD)
        p, t = SC.run(bars[i0:i1], "LONG", -4.0, SC.TAKER, False)
        for v, tt in zip(p, t):
            scalp_daily[tt // DAY] = scalp_daily.get(tt // DAY, 0.0) + v
    return {"symbol": sym, "gate": gate_name, "grid": grid_daily, "scalp": scalp_daily}


def halves(daily: dict) -> tuple[float, float]:
    return (sum(v for d, v in daily.items() if int(d) < SPLIT_DAY),
            sum(v for d, v in daily.items() if int(d) >= SPLIT_DAY))


def win_share(daily: dict, days: list[int]) -> float:
    xs = [daily.get(d, 0.0) for d in days]
    act = [x for x in xs if x != 0]
    return 100 * sum(x > 0 for x in act) / max(len(act), 1)


def main() -> None:
    syms = json.loads((L14.CACHE / "symbols.json").read_text())
    _, _, t_daily = trend_gate()
    jobs = [(syms.index("BTCUSDT"), "BTC"), (syms.index("ETHUSDT"), "ETH")] + [(k, "BTC") for k in range(len(syms))]
    with ProcessPoolExecutor(max_workers=4) as ex:
        res = [r for r in ex.map(job, jobs) if r is not None]
    own, mkt = res[:2], res[2:]
    lo = int(datetime(2025, 8, 2, tzinfo=UTC).timestamp() * 1000) // DAY
    hi = int(datetime(2026, 9, 30, tzinfo=UTC).timestamp() * 1000) // DAY
    days = list(range(lo, hi))
    T = {d: t_daily.get(d, 0.0) for d in days}

    def agg(rs, key, scale):
        out = {}
        for r in rs:
            for d, v in r[key].items():
                out[int(d)] = out.get(int(d), 0.0) + v * scale
        return out

    overlays = {
        "G-own": agg(own, "grid", 1.0), "S-own": agg(own, "scalp", 1.0),
        # market overlays scaled to 2 coins' worth of notional, same as the own overlays
        "G-mkt": agg(mkt, "grid", 2.0 / len(mkt)), "S-mkt": agg(mkt, "scalp", 2.0 / len(mkt)),
    }
    th1, th2 = halves(T)
    split_days = ([d for d in days if d < SPLIT_DAY], [d for d in days if d >= SPLIT_DAY])
    print(f"T (trend alone, 1000 USDT): H1 {th1:+.1f}  H2 {th2:+.1f}  "
          f"win days H1 {win_share(T, split_days[0]):.0f}%  H2 {win_share(T, split_days[1]):.0f}%")
    summary = {"T": {"H1": th1, "H2": th2}}
    for name, ov in overlays.items():
        h1, h2 = halves(ov)
        comb = {d: T.get(d, 0.0) + ov.get(d, 0.0) for d in days}
        w1, w2 = win_share(comb, split_days[0]), win_share(comb, split_days[1])
        adds = h1 > 0 and h2 > 0 and w1 > win_share(T, split_days[0]) and w2 > win_share(T, split_days[1])
        summary[name] = {"H1": h1, "H2": h2, "T_plus_H1": th1 + h1, "T_plus_H2": th2 + h2,
                         "win_H1": w1, "win_H2": w2, "adds_value": adds}
        print(f"{name:6s} overlay H1 {h1:+8.1f}  H2 {h2:+8.1f} | T+overlay H1 {th1 + h1:+8.1f}  H2 {th2 + h2:+8.1f} "
              f" win days {w1:.0f}% / {w2:.0f}%" + ("  ADDS VALUE" if adds else ""))
    OUT.write_text(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
