"""Risk-policy counterfactual replay run (2026-09-29) - read-only.

    python -m crypto_trading.entry_research.risk_replay_run [--offline]

Writes data/entry_research/risk_replay.json. Never touches LIVE.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from crypto_trading.entry_research import risk_policy as rp
from crypto_trading.entry_research.dataset import TEST_FROM, VALID_FROM, period_of
from crypto_trading.entry_research.klines import KlineCache
from crypto_trading.entry_research.stats import block_of, cluster_bootstrap_ci, p_mean_positive

OUT = Path("data/entry_research")
PERIODS = ("TRAIN", "VALID", "TEST")

POLICIES = [
    rp.Policy("A_current_10_5_fixed", Decimal("0.10"), Decimal("0.05"), "FIXED"),
    rp.Policy("B1_group_7.5_fixed", Decimal("0.10"), Decimal("0.075"), "FIXED"),
    rp.Policy("B2_group_10_fixed", Decimal("0.10"), Decimal("0.10"), "FIXED"),
    rp.Policy("C_alloc_within_10_5", Decimal("0.10"), Decimal("0.05"), "ALLOC"),
    rp.Policy("C2_fixed_risk_2.5pct_within_10_5", Decimal("0.10"), Decimal("0.05"), "FIXED_RISK",
              risk_per_trade=Decimal("0.025")),
]


def load_opportunities(db: str) -> list[rp.Opportunity]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    eq = {r["candidate_id"]: r["eq_class"] for r in c.execute("SELECT candidate_id, eq_class FROM entry_quality_shadow")}
    feats = {}
    ds = OUT / "dataset.json"
    if ds.exists():
        for r in json.loads(ds.read_text(encoding="utf-8")):
            feats[r["candidate_id"]] = r["feat"]
    out = []
    for r in c.execute(
        "SELECT k.candidate_id, k.instrument, k.created_at, "
        "(SELECT max(occurred_at) FROM events e WHERE e.event_type='AI_CALL_MADE' AND e.aggregate_id=k.candidate_id "
        " AND json_extract(e.payload,'$.role')='qa') AS decided, "
        "(SELECT json_extract(payload,'$.suggested_stop_loss') FROM assessments a WHERE a.candidate_id=k.candidate_id "
        " AND field_name='risk') AS sl, "
        "(SELECT json_extract(payload,'$.suggested_target') FROM assessments a WHERE a.candidate_id=k.candidate_id "
        " AND field_name='risk') AS tp FROM candidates k WHERE k.status='CONFIRMED' ORDER BY k.created_at"
    ):
        try:
            stop, target = float(r["sl"]), float(r["tp"])
        except (TypeError, ValueError):
            continue
        f = dict(feats.get(r["candidate_id"], {}))
        f["eq_class"] = eq.get(r["candidate_id"])
        out.append(rp.Opportunity(r["candidate_id"], r["instrument"], datetime.fromisoformat(r["created_at"]),
                                  datetime.fromisoformat(r["decided"]), stop, target, f))
    return out


def build_paths(opps, cache) -> list[tuple[rp.Opportunity, rp.Path]]:
    out = []
    for o in opps:
        bars = cache.bars(o.symbol, o.decided_at, o.decided_at + rp.HORIZON + timedelta(minutes=10))
        p = rp.simulate_path(bars, o)
        if p is not None:
            out.append((o, p))
    return out


def standalone(pairs, label_fn) -> dict:
    """Each opportunity alone at full size (100 x 10), grouped by label."""
    groups: dict[str, list] = {}
    for o, p in pairs:
        qty = float(rp.MARGIN * rp.LEVERAGE) / p.entry
        risk = float(rp.worst_case_risk_usdt(Decimal(str(qty)), Decimal(str(p.entry)), Decimal(str(o.stop)),
                                             rp._limits(rp.POLICIES_A)))
        pnl = rp.pnl_usdt(p, qty)
        groups.setdefault(label_fn(o, p), []).append({"pnl": pnl, "r": pnl / risk, "t0": p.entry_at,
                                                    "mfe": p.mfe_pct, "mae": p.mae_pct, "risk": risk})
    out = {}
    for k, rows in sorted(groups.items()):
        pnl = [x["pnl"] for x in rows]
        rs = [x["r"] for x in rows]
        cl = [block_of(x["t0"]) for x in rows]
        gains, losses = sum(v for v in pnl if v > 0), -sum(v for v in pnl if v < 0)
        out[k] = {"n": len(rows), "win_rate": round(sum(v > 0 for v in pnl) / len(pnl), 3),
                  "exp_usdt": round(st.mean(pnl), 2), "exp_r": round(st.mean(rs), 3),
                  "total_usdt": round(sum(pnl), 1), "pf": round(gains / losses, 2) if losses else None,
                  "ci_r": [round(v, 3) for v in cluster_bootstrap_ci(rs, cl)] if len(rs) >= 5 and
                  cluster_bootstrap_ci(rs, cl) else None,
                  "p_pos": round(p_mean_positive(rs, cl), 3),
                  "mfe": round(st.mean(x["mfe"] for x in rows), 2), "mae": round(st.mean(x["mae"] for x in rows), 2),
                  "risk_usdt_median": round(st.median(x["risk"] for x in rows), 1)}
    return out


rp.POLICIES_A = POLICIES[0]


def kernel_alone_label(o, p) -> str:
    qty, reason = rp.decide(POLICIES[0], o, p, Decimal("420"), [], p.entry_at)
    return "WOULD_APPROVE_ALONE" if qty > 0 else f"REJECT_ALONE:{reason}"


def train_quality_policy(train_pairs):
    """D: choose ONE pre-trade feature on TRAIN (best monotone R spread
    between its top and bottom tertile); weight 1.5 / 1.0 / 0.5 of a 2.5 %
    risk budget. Frozen, then applied to VALID and TEST unchanged."""
    candidates = ["rr", "candidate_score", "stop_dist_pct", "opp_score", "fc_bull_minus_bear", "volz_30m",
                  "chg_1h", "atr15_pct"]
    best = None
    for f in candidates:
        rows = []
        for o, p in train_pairs:
            v = o.features.get(f)
            if v is None:
                continue
            qty = float(rp.MARGIN * rp.LEVERAGE) / p.entry
            risk = float(rp.worst_case_risk_usdt(Decimal(str(qty)), Decimal(str(p.entry)), Decimal(str(o.stop)),
                                                 rp._limits(POLICIES[0])))
            rows.append((v, rp.pnl_usdt(p, qty) / risk))
        if len(rows) < 30:
            continue
        rows.sort()
        lo_cut, hi_cut = rows[len(rows) // 3][0], rows[2 * len(rows) // 3][0]
        low = [r for v, r in rows if v <= lo_cut]
        high = [r for v, r in rows if v > hi_cut]
        if not low or not high:
            continue
        spread = st.mean(high) - st.mean(low)
        if best is None or abs(spread) > abs(best[1]):
            best = (f, spread, lo_cut, hi_cut)
    if best is None:
        return None, None
    f, spread, lo_cut, hi_cut = best
    sign = 1 if spread > 0 else -1

    def weight(features, f=f, lo=lo_cut, hi=hi_cut, sign=sign):
        v = features.get(f)
        if v is None:
            return 1.0
        top, bottom = (v > hi, v <= lo) if sign > 0 else (v <= lo, v > hi)
        return 1.5 if top else 0.5 if bottom else 1.0

    policy = rp.Policy(f"D_quality_{f}_2.5pct_within_10_5", Decimal("0.10"), Decimal("0.05"), "QUALITY",
                       risk_per_trade=Decimal("0.025"), quality_weight=weight)
    return policy, {"feature": f, "train_spread_r": round(spread, 3), "low_cut": lo_cut, "high_cut": hi_cut,
                    "direction": "higher_is_better" if sign > 0 else "lower_is_better"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(OUT / "snapshot.db"))
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--snapshot", action="store_true")
    args = ap.parse_args()
    if args.snapshot:
        src = sqlite3.connect("file:data/crypto_trading.db?mode=ro", uri=True)
        dst = sqlite3.connect(args.db)
        src.backup(dst)
        dst.close()
        src.close()
    connector = None
    if not args.offline:
        from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector

        connector = BingXMarketDataConnector(base_url="https://open-api.bingx.com", timeout_seconds=10.0,
                                             max_retries=3, requests_per_second=2, cache_ttl_seconds=0)
    cache = KlineCache(OUT / "klines.db", connector)
    opps = load_opportunities(args.db)
    pairs = build_paths(opps, cache)
    by_period = {p: [x for x in pairs if period_of(x[1].entry_at) == p] for p in PERIODS}
    days = {p: len({x[1].entry_at.date() for x in by_period[p]}) for p in PERIODS}

    d_policy, d_info = train_quality_policy(by_period["TRAIN"])
    policies = POLICIES + ([d_policy] if d_policy else [])
    replays = {}
    for pol in policies:
        replays[pol.name] = {per: rp.metrics(rp.replay(pol, by_period[per]), days[per]) for per in PERIODS}
        replays[pol.name]["ALL"] = rp.metrics(rp.replay(pol, pairs), sum(days.values()))

    # what policy A rejected in its own sequential replay, and how those did alone at full size
    res_a = rp.replay(POLICIES[0], pairs)
    approved = {t["candidate_id"] for t in res_a.trades}

    def a_label(o, p):
        if o.candidate_id in approved:
            return "A_APPROVED"
        return "A_REJECTED:" + kernel_alone_label(o, p).split(":", 1)[-1] if not kernel_alone_label(
            o, p).startswith("WOULD") else "A_REJECTED:PORTFOLIO_STATE"

    results = {
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": {"confirmed": len(opps), "with_path": len(pairs), "per_period": {p: len(v) for p, v in by_period.items()},
                   "days": days},
        "standalone_all_by_kernel_alone": standalone(pairs, kernel_alone_label),
        "standalone_by_period_kernel_alone": {
            per: standalone(by_period[per], kernel_alone_label) for per in PERIODS},
        "standalone_by_policy_a_outcome": standalone(pairs, a_label),
        "live_kernel_evaluated": standalone(
            [x for x in pairs if x[1].entry_at >= datetime(2026, 9, 28, 12, tzinfo=UTC)], kernel_alone_label),
        "quality_policy_d": d_info,
        "replays": replays,
        "periods": {"valid_from": VALID_FROM.isoformat(), "test_from": TEST_FROM.isoformat()},
    }
    (OUT / "risk_replay.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    print(json.dumps(results["counts"]))


if __name__ == "__main__":
    main()
