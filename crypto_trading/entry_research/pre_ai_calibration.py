"""Pre-AI feasibility: calibration (TRAIN only) + historical evaluation.

    python -m crypto_trading.entry_research.pre_ai_calibration [--write-config]

Resolution: 30m bars that CLOSED before the signal - the timeframe discovery
itself uses - rebuilt from the 1m cache on UTC half-hour boundaries (BingX
30m candles). Ground truth "fits": the Safety Kernel's own size_entry with
the Risk Agent's ACTUAL stop at the reference price, equity 420 and an empty
portfolio (the per-trade question); for the 2026-09-28/29 night's kernel
decisions the recorded equity and open portfolio/group risk are used.
Writes data/entry_research/pre_ai_eval.json (and, with --write-config, the
frozen calibration to crypto_trading/config/pre_ai_feasibility.json).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from crypto_trading.config.loader import SafetyKernelConfig
from crypto_trading.entry_research.dataset import closed_before, period_of, resample
from crypto_trading.entry_research.klines import KlineCache
from crypto_trading.safety_kernel import Exposure, size_entry
from crypto_trading.shadow import pre_ai_feasibility as paf

OUT = Path("data/entry_research")
LIMITS = SafetyKernelConfig(max_portfolio_risk_pct=Decimal("0.10"), max_group_risk_pct=Decimal("0.05"))
COST_PER_ANALYSIS_USD = 0.133
EQUITY = Decimal("420")
NIGHT_FROM = datetime(2026, 9, 28, 22, 15, tzinfo=UTC)


def atr30(cache, symbol: str, t0: datetime, bars_n: int = 14) -> float | None:
    bars = closed_before(cache.bars(symbol, t0 - timedelta(hours=bars_n / 2 + 2), t0), t0)
    m30 = [b for b in resample(bars, 30) if b.t + timedelta(minutes=30) <= t0][-bars_n:]
    return paf.atr_pct(m30) if len(m30) >= bars_n - 4 else None


def fits(price: float, stop: float, equity: Decimal, exposures: list[Exposure], symbol: str) -> tuple[bool, list]:
    last = Decimal(str(price))
    base = (Decimal(1000) / last).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
    d = size_entry(symbol=symbol, equity=equity, last_price=last, stop_loss=Decimal(str(stop)),
                   target=last * 2, leverage=10, base_quantity=base, quantity_precision=3,
                   min_notional=Decimal("2"), open_exposures=exposures, limits=LIMITS)
    return d.action == "APPROVE", d.reasons


def quantile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))]


def evaluate(rows: list[dict], method: str) -> dict:
    """method: 'bound' (pre_ai_feasible) or 'estimate' (shadow point estimate)."""
    def verdict(r):
        a = r["assessment"]
        if method == "bound":
            return a["pre_ai_feasible"]
        est = a.get("shadow_estimate")
        return paf.UNKNOWN if est is None else (paf.FEASIBLE if est["fits"] else paf.INFEASIBLE)

    inf = [r for r in rows if verdict(r) == paf.INFEASIBLE]
    unk = [r for r in rows if verdict(r) == paf.UNKNOWN]
    fea = [r for r in rows if verdict(r) == paf.FEASIBLE]
    fitting = [r for r in rows if r["fits"]]
    wrong = [r for r in inf if r["fits"]]
    good_wrong = [r for r in wrong if (r.get("own_r") or 0) > 0]
    pos_among_inf = [r for r in inf if (r.get("own_r") or 0) > 0]
    return {
        "n": len(rows), "infeasible": len(inf), "unknown": len(unk), "feasible": len(fea),
        "actually_fit": len(fitting),
        "precision_infeasible": round(sum(not r["fits"] for r in inf) / len(inf), 3) if inf else None,
        "false_rejections": len(wrong),
        "false_rejection_rate_of_fitting": round(len(wrong) / len(fitting), 3) if fitting else None,
        "good_fitting_wrongly_infeasible": len(good_wrong),
        "positive_outcome_among_infeasible": len(pos_among_inf),
        "ai_cost_saved_usd": round(len(inf) * COST_PER_ANALYSIS_USD, 2),
        "still_reach_ai": len(rows) - len(inf),
        "fitting_classified_feasible": sum(1 for r in fea if r["fits"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(OUT / "snapshot.db"))
    ap.add_argument("--write-config", action="store_true")
    args = ap.parse_args()
    cache = KlineCache(OUT / "klines.db")
    data = json.loads((OUT / "dataset.json").read_text(encoding="utf-8"))
    rows = []
    for r in data:
        f = r["feat"]
        ref, stop_pct = r.get("reference_price"), f.get("stop_dist_pct")
        if not ref or not stop_pct or not r.get("own_stop"):
            continue
        t0 = datetime.fromisoformat(r["t0"])
        rows.append({"candidate_id": r["candidate_id"], "symbol": r["symbol"], "t0": t0,
                     "period": period_of(t0), "cohort": r["cohort"], "status": r["status"],
                     "price": float(ref), "stop": float(r["own_stop"]), "stop_pct": stop_pct,
                     "atr30": atr30(cache, r["symbol"], t0),
                     "own_r": (r.get("own") or {}).get("r")})
    train = [x for x in rows if x["period"] == "TRAIN" and x["atr30"]]
    ratios = [x["stop_pct"] / x["atr30"] for x in train]
    calib = paf.Calibration(
        timeframe="30m", atr_bars=14, ratio_lower=round(quantile(ratios, 0.01), 4),
        ratio_median=round(quantile(ratios, 0.50), 4), ratio_upper=round(quantile(ratios, 0.99), 4),
        stop_floor_pct=round(min(x["stop_pct"] for x in train), 4), frozen_on="TRAIN 2026-09-01..12",
    )
    for x in rows:
        x["fits"], x["kernel_reasons"] = fits(x["price"], x["stop"], EQUITY, [], x["symbol"])
        x["assessment"] = paf.assess(
            symbol=x["symbol"], price=x["price"], atr30_pct=x["atr30"], equity=EQUITY, open_exposures=[],
            open_symbols=set(), max_positions=4, margin_usdt=Decimal(100), leverage=10, limits=LIMITS, calib=calib)

    # the night's kernel decisions with their REAL equity/exposure
    c = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    night = []
    for k in c.execute(
        "SELECT s.detail_json, p.instrument, p.candidate_id, k.created_at FROM safety_kernel_decisions s "
        "JOIN positions p USING(position_id) JOIN candidates k ON k.candidate_id = p.candidate_id "
        "WHERE s.decided_at >= ?", (NIGHT_FROM.isoformat(),)
    ):
        d = json.loads(k["detail_json"])
        eq = Decimal(d["equity"])
        port_before, grp_before = Decimal(d.get("portfolio_risk_before", "0")), Decimal(d.get("group_risk_before", "0"))
        exposures = []
        if grp_before:
            exposures.append(Exposure("OPEN-SAME-GROUP", d["group"], grp_before, Decimal(0)))
        if port_before - grp_before > 0:
            exposures.append(Exposure("OPEN-OTHER", "other", port_before - grp_before, Decimal(0)))
        t0 = datetime.fromisoformat(k["created_at"])
        price = float(d["last_price"])
        a = paf.assess(symbol=k["instrument"], price=price, atr30_pct=atr30(cache, k["instrument"], t0), equity=eq,
                       open_exposures=exposures, open_symbols=set(), max_positions=4, margin_usdt=Decimal(100),
                       leverage=10, limits=LIMITS, calib=calib)
        night.append({"candidate_id": k["candidate_id"], "symbol": k["instrument"], "period": "NIGHT",
                      "fits": d["action"] == "APPROVE", "kernel": d["action"], "kernel_reasons": d.get("reasons"),
                      "assessment": a, "own_r": None,
                      "stop_pct": round((price - float(d["stop_loss"])) / price * 100, 3)})

    confirmed = [x for x in rows if x["status"] == "CONFIRMED"]
    result = {
        "calibration": calib.__dict__,
        "ratio_quantiles_train": {q: round(quantile(ratios, q), 3) for q in (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)},
        "n_rows": len(rows), "n_with_atr30": sum(1 for x in rows if x["atr30"]),
        "evaluation": {},
        "night_rows": [{k: v for k, v in x.items() if k != "assessment"} | {
            "pre_ai_feasible": x["assessment"]["pre_ai_feasible"], "reason": x["assessment"]["reason"],
            "stop_interval": x["assessment"].get("estimated_stop_pct_interval"),
            "estimate_fits": (x["assessment"].get("shadow_estimate") or {}).get("fits")} for x in night],
    }
    for method in ("bound", "estimate"):
        ev = {"ALL_ANALYSED": evaluate(rows, method), "CONFIRMED": evaluate(confirmed, method),
              "NIGHT_KERNEL_DECISIONS": evaluate(night, method)}
        for per in ("TRAIN", "VALID", "TEST"):
            ev[per] = evaluate([x for x in rows if x["period"] == per], method)
            ev[f"CONFIRMED_{per}"] = evaluate([x for x in confirmed if x["period"] == per], method)
        result["evaluation"][method] = ev
    (OUT / "pre_ai_eval.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    if args.write_config:
        paf.CALIBRATION_PATH.write_text(json.dumps({
            "note": ("Pre-AI feasibility calibration - SHADOW ONLY. Frozen on TRAIN (2026-09-01..12); "
                     "never re-fitted automatically. Produced by crypto_trading.entry_research.pre_ai_calibration."),
            "calibration": calib.__dict__}, indent=1), encoding="utf-8")
    print(json.dumps({"calibration": calib.__dict__, "n": len(rows)}))


if __name__ == "__main__":
    main()
