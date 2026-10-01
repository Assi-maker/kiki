"""P4 external-information families (options + on-chain), same protocol as p4_lab.

    python -m crypto_trading.entry_research.p4_external   # fetch + evaluate
    python -m crypto_trading.entry_research.p4_external combine

PRE-REGISTERED (written before any outcome below was computed)
F7 OPTIONS_IV - Deribit DVOL (30-day implied vol index, BTC and ETH), free
   public API, hourly OHLC; the value of the hour [t, t+1h) is used from the
   5m bar ending at t+1h on. Signals (24 h cooldown each):
     SPIKE_z / CRUSH_z: 24h log change of DVOL, z vs the trailing 30 d of
       hourly-sampled 24h changes, >= +z / <= -z, z in {1.5, 2.5};
     VRP_HI / VRP_LO: DVOL minus the asset's realised vol (24h of 5m returns,
       annualised), in the top / bottom 10 % of its trailing 30 d.
   Targets: OWN (BTC on BTC-DVOL, ETH on ETH-DVOL, pooled) and BASKET (every
   eligible symbol on BTC-DVOL). LONG and SHORT, horizons 5m..24h.
   Mechanism: implied vol is what option dealers charge for risk. A spike /
   high premium = forced hedging demand and fear (risk-premium harvesting ->
   LONG after fear); a crush = complacency.
F8 ONCHAIN_FLOW - Coin Metrics community API, exchange in/out flows (native
   units), BTC and ETH, DAILY only (hourly needs a paid key). Day D's value is
   used from D+1 04:00 UTC on (it is published ~03:00 as "flash").
   Signals: INFLOW_HI / OUTFLOW_HI = flow z >= 2 vs trailing 90 d.
   OWN asset, LONG and SHORT, horizons 5m..24h.
   Mechanism: coins sent to exchanges are sent to be sold (supply overhang);
   withdrawals = accumulation.
Both families join the SAME Benjamini-Hochberg pool as p4_lab (combine step)
and the same S1-S8 criteria.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np

from crypto_trading.entry_research import p4_data as D
from crypto_trading.entry_research import p4_lab as P

RAW = Path("data/entry_research/p4_external_raw.json")
OUT = Path("data/entry_research/p4_external_results.json")
FINAL = Path("data/entry_research/p4_final.json")


def fetch() -> dict:
    if RAW.exists():
        return json.loads(RAW.read_text())
    raw: dict = {"dvol": {}, "flows": {}}
    with httpx.Client(timeout=60) as cl:
        for cur in ("BTC", "ETH"):
            rows, start = [], D.T0 - 40 * 86400_000
            end = D.T0 + D.N * D.STEP
            while start < end:
                stop = min(start + 900 * 3600_000, end)
                r = cl.get(
                    "https://www.deribit.com/api/v2/public/get_volatility_index_data",
                    params={"currency": cur, "start_timestamp": start, "end_timestamp": stop, "resolution": 3600},
                ).json()
                rows += r["result"]["data"]
                start = stop
                time.sleep(0.2)
            raw["dvol"][cur] = sorted({row[0]: row for row in rows}.values())
        for a in ("btc", "eth"):
            r = cl.get(
                "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
                params={"assets": a, "metrics": "FlowInExNtv,FlowOutExNtv", "frequency": "1d", "start_time": "2025-04-01", "end_time": "2026-09-30", "page_size": 10000},
            ).json()
            raw["flows"][a] = [(d["time"], float(d["FlowInExNtv"]), float(d["FlowOutExNtv"])) for d in r["data"]]
    RAW.write_text(json.dumps(raw))
    return raw


def hourly_series(rows: list) -> np.ndarray:
    """DVOL close placed on the 5m bar that ENDS when the hour closes, forward filled."""
    out = np.full(D.N, np.nan)
    for t, _o, _h, _l, c in rows:
        k = (t + 3600_000 - D.T0) // D.STEP - 1
        if 0 <= k < D.N:
            out[k] = c
    m = ~np.isnan(out)
    pos = np.where(m, np.arange(D.N), 0)
    np.maximum.accumulate(pos, out=pos)
    return out[pos]


def trailing_z(x: np.ndarray, hours: int) -> np.ndarray:
    """z of x[k] vs the hourly samples of x over the trailing window (excl. k)."""
    import pandas as pd

    hourly = pd.Series(x[11::12])
    mu = hourly.rolling(hours, min_periods=hours // 2).mean().shift(1)
    sd = hourly.rolling(hours, min_periods=hours // 2).std().shift(1)
    z = np.full(D.N, np.nan)
    z[11::12] = ((hourly - mu) / sd).values
    return z


def trailing_rank(x: np.ndarray, hours: int) -> np.ndarray:
    import pandas as pd

    hourly = pd.Series(x[11::12])
    r = hourly.rolling(hours, min_periods=hours // 2).apply(lambda w: (w[:-1] < w[-1]).mean(), raw=True)
    out = np.full(D.N, np.nan)
    out[11::12] = r.values
    return out


def events(cond: np.ndarray, gap: int) -> np.ndarray:
    c = np.nan_to_num(cond).astype(bool)
    start = np.nonzero(c & ~np.r_[False, c[:-1]])[0]
    return P.cooldown(start, gap)


def build(raw: dict, C, age_ok, syms) -> tuple[list, list, dict]:
    sets, cells, meta = [], [], {"events": {}}
    own = {"BTC": syms.index("BTCUSDT"), "ETH": syms.index("ETHUSDT")}
    sig_ev: dict = {}
    for cur, si in own.items():
        dv = hourly_series(raw["dvol"][cur])
        ch = np.log(dv / np.r_[np.full(288, np.nan), dv[:-288]])
        z = trailing_z(ch, 720)
        lr = np.diff(np.log(C[si].astype("f8")), prepend=np.nan)
        import pandas as pd

        rv = pd.Series(lr).rolling(288, min_periods=200).std().values * np.sqrt(288 * 365) * 100
        rk = trailing_rank(dv - rv, 720)
        for name, cond in (
            ("SPIKE_1.5", z >= 1.5),
            ("SPIKE_2.5", z >= 2.5),
            ("CRUSH_1.5", z <= -1.5),
            ("CRUSH_2.5", z <= -2.5),
            ("VRP_HI", rk >= 0.9),
            ("VRP_LO", rk <= 0.1),
        ):
            sig_ev[(cur, name)] = events(cond, 288)
    for name in ("SPIKE_1.5", "SPIKE_2.5", "CRUSH_1.5", "CRUSH_2.5", "VRP_HI", "VRP_LO"):
        ks_b, ks_e = sig_ev[("BTC", name)], sig_ev[("ETH", name)]
        meta["events"][name] = {"BTC": len(ks_b), "ETH": len(ks_e)}
        s_own = np.r_[np.full(len(ks_b), own["BTC"]), np.full(len(ks_e), own["ETH"])].astype(np.int32)
        k_own = np.r_[ks_b, ks_e].astype(np.int32)
        es = [np.nonzero(age_ok[:, k])[0] for k in ks_b]
        s_bk = np.concatenate(es).astype(np.int32) if es else np.zeros(0, np.int32)
        k_bk = np.concatenate([np.full(len(e), k) for e, k in zip(es, ks_b)]).astype(np.int32) if es else np.zeros(0, np.int32)
        for target, s_arr, k_arr in (("OWN", s_own, k_own), ("BASKET", s_bk, k_bk)):
            p = {"signal": name, "target": target}
            sets.append(P.TradeSet("F7_OPTIONS_IV", p, s_arr, k_arr))
            i = len(sets) - 1
            cells.append(P.Cell("F7_OPTIONS_IV", p, "LONG", [(i, 1)], tuple(P.H_BARS)))
            cells.append(P.Cell("F7_OPTIONS_IV", p, "SHORT", [(i, -1)], tuple(P.H_BARS)))
    flow_ev = {"INFLOW_HI": ([], []), "OUTFLOW_HI": ([], [])}
    for a, cur in (("btc", "BTC"), ("eth", "ETH")):
        rows = raw["flows"][a]
        fin = np.array([r[1] for r in rows])
        fout = np.array([r[2] for r in rows])
        for name, x in (("INFLOW_HI", fin), ("OUTFLOW_HI", fout)):
            for i in range(90, len(rows)):
                w = x[i - 90 : i]
                if w.std() > 0 and (x[i] - w.mean()) / w.std() >= 2:
                    day = datetime.fromisoformat(rows[i][0].replace("Z", "")[:19]).replace(tzinfo=UTC)
                    t = int(day.timestamp() * 1000) + 86400_000 + 4 * 3600_000
                    k = (t - D.T0) // D.STEP - 1
                    if 0 <= k < D.N:
                        flow_ev[name][0].append(own[cur])
                        flow_ev[name][1].append(k)
    for name, (s, k) in flow_ev.items():
        meta["events"][name] = len(k)
        p = {"signal": name, "target": "OWN"}
        sets.append(P.TradeSet("F8_ONCHAIN_FLOW", p, np.array(s, np.int32), np.array(k, np.int32)))
        i = len(sets) - 1
        cells.append(P.Cell("F8_ONCHAIN_FLOW", p, "LONG", [(i, 1)], tuple(P.H_BARS)))
        cells.append(P.Cell("F8_ONCHAIN_FLOW", p, "SHORT", [(i, -1)], tuple(P.H_BARS)))
    return sets, cells, meta


def run() -> None:
    raw = fetch()
    syms = D.symbols()
    C = np.asarray(D.load("c"), dtype="f4")
    Hh = np.asarray(D.load("h"), dtype="f4")
    Ll = np.asarray(D.load("l"), dtype="f4")
    CF = D.load("cf")
    first = np.argmax(np.isfinite(C), axis=1)
    age_ok = ((np.arange(D.N)[None, :] - first[:, None]) >= P.AGE_MIN) & np.isfinite(C)
    sets, cells, meta = build(raw, C, age_ok, syms)
    rows = []
    for hname in P.H_BARS:
        P.evaluate_horizon(sets, hname, C, Hh, Ll, CF)
        for ci, cell in enumerate(cells):
            r = {"id": f"X{ci}:{hname}", "family": cell.family, "params": cell.params, "side": cell.side_label, "h": hname}
            r.update(P.stats_for(cell, sets, hname))
            rows.append(r)
    meta["set_sizes"] = [{"family": s.family, "params": s.params, "n": int(len(s.k))} for s in sets]
    OUT.write_text(json.dumps({"meta": meta, "rows": rows}, default=float))
    print(json.dumps(meta["events"]), len(rows))


def combine() -> None:
    main = json.loads(P.OUT.read_text())
    ext = json.loads(OUT.read_text())
    rows = main["rows"] + ext["rows"]
    sel = P.verdict(rows)
    summary = {}
    for fam in sorted({r["family"] for r in rows}):
        fr = [r for r in rows if r["family"] == fam]
        summary[fam] = {
            "cells": len(fr),
            "train_net_pos": sum(r["TRAIN"]["net"] > 0 for r in fr),
            "train_p05": sum(r["TRAIN"]["p"] < 0.05 for r in fr),
            "S1_bh": sum(r["S1"] for r in fr),
            "S2_valid": sum(r["S2"] for r in fr),
            "S3_oos": sum(r["S3"] for r in fr),
            "S4_all_periods": sum(r["S4"] for r in fr),
            "S7_surviving": sum(r["SURVIVING_EDGE"] for r in fr),
            "bonf_train": sum(r["bonf_train"] < 0.05 for r in fr),
            "gross_bh": sum(r["q_train_gross"] < 0.10 for r in fr),
            "net_pos_all4": sum(all(r[p]["net"] > 0 for p in P.PERIOD_NAMES) for r in fr),
            "gross_pos_all4": sum(all(r[p]["gross"] > 0 for p in P.PERIOD_NAMES) for r in fr),
        }
    FINAL.write_text(json.dumps({"summary": summary, "selection": sel, "meta": {"main": main["meta"], "external": ext["meta"]}, "rows": rows}, default=float))
    print(json.dumps(summary, indent=1))
    print("selected", sel)


if __name__ == "__main__":
    combine() if sys.argv[1:] == ["combine"] else run()
