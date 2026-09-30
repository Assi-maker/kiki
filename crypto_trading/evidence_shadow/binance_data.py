"""Point-in-time Binance USD-M public data for the live evidence classifier.

The Historical Evidence was computed on Binance data (archive), so the live
classification uses the SAME source and the SAME feature code - otherwise a
live candidate would be matched against evidence of a differently measured
signal. Public GETs only (no key), rate-limited, shadow process only.

Temporal contract - every function takes `cutoff` (the decision time, UTC
seconds) and returns ONLY what was known at that time, whatever the API
sends back:
- a 5m bar only if it CLOSED at or before the cutoff (open + 300 <= cutoff);
- a funding rate only if settled at or before the cutoff;
- an open-interest stamp only if <= cutoff (event_engine.Derivs then adds
  its own extra 5 min lag, exactly as in the research).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections.abc import Callable

FAPI = "https://fapi.binance.com"
BAR_S = 300
MIN_INTERVAL_S = 0.12  # <= ~8 req/s, far below the public 2400 weight/min

_lock = threading.Lock()
_last = [0.0]


def http_get_json(url: str) -> object:
    with _lock:
        wait = MIN_INTERVAL_S - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.loads(r.read().decode())


Getter = Callable[[str], object]


def klines_5m(symbol: str, start: int, cutoff: int, get: Getter = http_get_json) -> list[tuple]:
    """(open_s, o, h, l, c, v) for every 5m bar that closed in (start, cutoff]."""
    out: dict[int, tuple] = {}
    t = start
    while t < cutoff:
        url = (
            f"{FAPI}/fapi/v1/klines?symbol={symbol}&interval=5m&startTime={t * 1000}"
            f"&endTime={cutoff * 1000}&limit=1500"
        )
        rows = get(url) or []
        if not rows:
            break
        for r in rows:
            o = int(r[0]) // 1000
            if o + BAR_S <= cutoff:  # closed at or before the decision time
                out[o] = (o, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
        nxt = int(rows[-1][0]) // 1000 + BAR_S
        if nxt <= t or len(rows) < 1500:
            break
        t = nxt
    return [out[k] for k in sorted(out)]


def funding(symbol: str, start: int, cutoff: int, get: Getter = http_get_json) -> list[tuple]:
    """(settle_s, rate) settled in [start, cutoff]."""
    url = (
        f"{FAPI}/fapi/v1/fundingRate?symbol={symbol}&startTime={start * 1000}"
        f"&endTime={cutoff * 1000}&limit=1000"
    )
    rows = get(url) or []
    out = [(int(r["fundingTime"]) // 1000, float(r["fundingRate"])) for r in rows]
    return sorted(x for x in out if x[0] <= cutoff)


def open_interest(symbol: str, start: int, cutoff: int, get: Getter = http_get_json) -> list[tuple]:
    """(stamp_s, oi_value_usdt) with stamp <= cutoff (Binance keeps 30 days)."""
    out: dict[int, float] = {}
    t = max(start, cutoff - 29 * 86400)
    while t < cutoff:
        url = (
            f"{FAPI}/futures/data/openInterestHist?symbol={symbol}&period=5m"
            f"&startTime={t * 1000}&endTime={cutoff * 1000}&limit=500"
        )
        rows = get(url) or []
        if not isinstance(rows, list) or not rows:
            break
        for r in rows:
            s = int(r["timestamp"]) // 1000
            if s <= cutoff:
                out[s] = float(r["sumOpenInterestValue"])
        nxt = int(rows[-1]["timestamp"]) // 1000 + BAR_S
        if nxt <= t or len(rows) < 500:
            break
        t = nxt
    return sorted(out.items())
