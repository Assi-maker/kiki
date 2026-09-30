"""Cross-exchange lead/lag research: Binance (reference) vs BingX (traded) - 2026-09-30.

    python -m crypto_trading.entry_research.cross_exchange feasibility

Read-only research; the bot never imports this module. Public market-data
GETs only (BingX at <= 2 req/s so the bot's own API budget is not touched;
Binance from the static archive).

Step 1 (this file, `feasibility`): before any large lab, verify on a small
TRAIN-only sample that the data has the resolution and coverage needed, and
whether a Binance -> BingX lead exists at all beyond the first minute:
- coverage: share of minutes present on both venues, timestamp alignment;
- basis: log(Binance close / BingX close) in bp, and its deviation from the
  trailing 24 h median (a persistent venue offset is not information);
- lead/lag: correlation of Binance 1m returns at t with BingX 1m returns at
  t + lag, lag = -2 .. +20 min;
- decay: BingX return after a reference move/divergence event, measured from
  t + L to t + L + 5 min for L = 0, 1, 5, 10, 20 min.
All on TRAIN days only - no VALID / TEST / HOLDOUT data is read here.
"""

from __future__ import annotations

import csv
import io
import json
import math
import sys
import time
import urllib.request
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from crypto_trading.entry_research import regime_lab as rl

BINGX_URL = "https://open-api.bingx.com/openApi/swap/v3/quote/klines"
BINANCE_DAILY_1M = "https://data.binance.vision/data/futures/um/daily/klines/{s}/1m/{s}-1m-{d}.zip"
BINGX_MIN_INTERVAL_S = 0.5  # <= 2 req/s
_last_call = [0.0]


def _get_json(url: str) -> dict:
    wait = BINGX_MIN_INTERVAL_S - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


def bingx_1m(symbol: str, day: date) -> dict[int, float]:
    """minute open-time (s) -> close, one UTC day."""
    start = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp() * 1000)
    url = (
        f"{BINGX_URL}?symbol={symbol.replace('USDT', '-USDT')}&interval=1m"
        f"&startTime={start}&endTime={start + 86_400_000 - 1}&limit=1440"
    )
    data = _get_json(url).get("data") or []
    return {int(k["time"]) // 1000: float(k["close"]) for k in data}


def binance_1m(symbol: str, day: date) -> dict[int, float]:
    url = BINANCE_DAILY_1M.format(s=symbol, d=day.isoformat())
    with urllib.request.urlopen(url, timeout=60) as r:
        payload = r.read()
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        text = z.read(z.namelist()[0]).decode()
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if row and row[0][:1].isdigit():
            out[int(row[0]) // 1000] = float(row[4])
    return out


def _corr(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 10:
        return float("nan")
    ma, mb = sum(a) / n, sum(b) / n
    sab = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    saa = sum((x - ma) ** 2 for x in a)
    sbb = sum((y - mb) ** 2 for y in b)
    return sab / math.sqrt(saa * sbb) if saa and sbb else float("nan")


def feasibility(symbols: tuple[str, ...], days: list[date]) -> dict:
    res: dict = {"symbols": symbols, "days": [d.isoformat() for d in days], "per_symbol": {}}
    lags = (-2, -1, 0, 1, 2, 3, 5, 10, 20)
    for sym in symbols:
        bx: dict[int, float] = {}
        bn: dict[int, float] = {}
        for d in days:
            bx.update(bingx_1m(sym, d))
            bn.update(binance_1m(sym, d))
        both = sorted(set(bx) & set(bn))
        allm = set(bx) | set(bn)
        basis = [1e4 * math.log(bn[t] / bx[t]) for t in both]
        # 1m log returns on consecutive minutes present on both venues
        rb = {t: 1e4 * math.log(bn[t] / bn[t - 60]) for t in both if t - 60 in bn}
        rx = {t: 1e4 * math.log(bx[t] / bx[t - 60]) for t in both if t - 60 in bx}
        cc = {}
        for lag in lags:
            pairs = [(rb[t], rx[t + 60 * lag]) for t in rb if t + 60 * lag in rx]
            cc[lag] = round(_corr([p[0] for p in pairs], [p[1] for p in pairs]), 4)
        # decay after a reference-led divergence: Binance moved >= 3x its typical
        # 5m move more than BingX over the last 5 min (dev = lead of Binance)
        lead5 = {}
        for t in both:
            if t - 300 in bn and t - 300 in bx:
                lead5[t] = 1e4 * (math.log(bn[t] / bn[t - 300]) - math.log(bx[t] / bx[t - 300]))
        vals = sorted(abs(v) for v in lead5.values())
        thr = vals[int(len(vals) * 0.99)] if vals else float("inf")  # top 1 % (TRAIN sample)
        decay = {}
        for L in (0, 1, 5, 10, 20):
            fwd = []
            for t, v in lead5.items():
                if abs(v) < thr:
                    continue
                a, b = t + 60 * L, t + 60 * (L + 5)
                if a in bx and b in bx:
                    fwd.append(math.copysign(1, v) * 1e4 * math.log(bx[b] / bx[a]))
            decay[L] = {"n": len(fwd), "mean_bp": round(sum(fwd) / len(fwd), 2) if fwd else None}
        srt = sorted(basis)
        res["per_symbol"][sym] = {
            "minutes_both": len(both),
            "coverage": round(len(both) / len(allm), 4) if allm else 0,
            "basis_bp_p1_p50_p99": [round(srt[int(len(srt) * q)], 2) for q in (0.01, 0.5, 0.99)]
            if srt
            else None,
            "ret_corr_binance_t_vs_bingx_t_plus_lag": cc,
            "lead5_top1pct_threshold_bp": round(thr, 2),
            "bingx_fwd5m_after_lead_event_signed_bp": decay,
        }
        print(sym, json.dumps(res["per_symbol"][sym]), flush=True)
    return res


def main() -> None:
    if sys.argv[1:2] == ["feasibility"]:
        a = datetime.fromtimestamp(rl.PERIODS[0][1], UTC).date()
        days = [a + timedelta(days=18 * i + 3) for i in range(10)]  # spread over TRAIN
        assert all(
            rl.period(int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())) == "TRAIN"
            for d in days
        )
        out = feasibility(("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "WIFUSDT"), days)
        (Path("data/entry_research") / "cross_feasibility.json").write_text(
            json.dumps(out, indent=1), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
