"""Opportunity Detection v2 - selection research (2026-09-29), read-only.

    python -m crypto_trading.entry_research.opportunity_v2

LIVE analyses at most ~3 candidates per discovery cycle (the free-slot cap);
the rest become BUDGET_LIMITED. So "which 3" is the lever. Every historical
discovery run is replayed with the SAME cap and different, pre-registered
selection rules; outcome = the entry-research standardized R (entry +23 min,
2/3 ATR15, 6 h, real costs). The ranking feature for rule R_best is chosen
on TRAIN only and then applied unchanged to VALID and TEST.

Rules:
  CURRENT        top-k by candidate_score (today's LIVE ranking)
  R_<feature>    top-k by one pre-trade feature (TRAIN picks the best)
  DIVERSE        CURRENT, but a symbol already selected in the last 2 h is skipped
  NO_MOMENTUM_X  CURRENT, but edge-lab "no 4 h momentum" candidates are skipped
  COMBO          R_best + DIVERSE + NO_MOMENTUM_X
Writes data/entry_research/opportunity_v2.json.
"""

from __future__ import annotations

import json
import statistics as st
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from crypto_trading.entry_research import edge_lab as el
from crypto_trading.entry_research.dataset import period_of
from crypto_trading.entry_research.stats import bh, block_of, cluster_bootstrap_ci, p_diff_negative
from crypto_trading.shadow.pre_ai_feasibility import no_momentum_4h

OUT = Path("data/entry_research")
K = 3
DIVERSITY_WINDOW = timedelta(hours=2)
RANK_FEATURES = [
    "candidate_score",
    "chg_30m",
    "chg_1h",
    "volz_30m",
    "volz_1h",
    "ret_1h",
    "ret_4h",
    "rsi_30m",
    "atr15_pct",
    "pos_4h",
    "move30",
    "accel",
]


def load() -> list[dict]:
    raw = json.loads((OUT / "dataset.json").read_text(encoding="utf-8"))
    minutes = el.load_minutes(str(OUT / "klines.db"))
    pre = {s: el.precompute30(m) for s, m in minutes.items()}
    rows = []
    for r in raw:
        if not r.get("bars_ok") or not (r.get("outcomes") or {}).get("primary"):
            continue
        r["t0"] = datetime.fromisoformat(r["t0"])
        m = minutes.get(r["symbol"])
        bars = el.bars30_at(m, int(r["t0"].timestamp()), pre[r["symbol"]]) if m else None
        if bars:
            f = el.features_from_bars(bars)
            r["feat"]["move30"], r["feat"]["accel"] = f["move30"], f["accel"]
            r["no_mom"] = no_momentum_4h([b[3] for b in bars])["flag"]
        else:
            r["no_mom"] = None
        rows.append(r)
    return rows


def select(runs: dict, key, diverse=False, skip_no_mom=False, k=K) -> list[dict]:
    chosen, last_pick = [], {}
    for run_id in sorted(runs, key=lambda x: runs[x][0]["t0"]):
        cands = (
            [c for c in runs[run_id] if c["feat"].get(key) is not None]
            if key
            else list(runs[run_id])
        )
        cands.sort(key=lambda c: c["feat"].get(key) or 0, reverse=True)
        picked = []
        for c in cands:
            if len(picked) >= k:
                break
            if skip_no_mom and c.get("no_mom") is True:
                continue
            if (
                diverse
                and c["symbol"] in last_pick
                and c["t0"] - last_pick[c["symbol"]] < DIVERSITY_WINDOW
            ):
                continue
            picked.append(c)
        for c in picked:
            last_pick[c["symbol"]] = c["t0"]
        chosen += picked
    return chosen


def _m(sel):
    rs = [c["outcomes"]["primary"]["r"] for c in sel]
    if not rs:
        return {"n": 0}
    ci = cluster_bootstrap_ci(rs, [block_of(c["t0"]) for c in sel]) if len(rs) >= 10 else None
    return {
        "n": len(rs),
        "mean_r": round(st.mean(rs), 4),
        "win": round(sum(r > 0 for r in rs) / len(rs), 3),
        "ci": [round(x, 3) for x in ci] if ci else None,
        "symbols": len({c["symbol"] for c in sel}),
        "sl_rate": round(
            sum(c["outcomes"]["primary"]["reason"] == "SL" for c in sel) / len(sel), 3
        ),
    }


def main() -> None:
    rows = load()
    by_period = {
        p: [r for r in rows if period_of(r["t0"]) == p] for p in ("TRAIN", "VALID", "TEST")
    }

    def runs_of(rs):
        d = defaultdict(list)
        for r in rs:
            d[r["discovery_run_id"]].append(r)
        return d

    runs = {p: runs_of(v) for p, v in by_period.items()}
    # 1. pick the ranking feature on TRAIN (higher is better OR lower is better)
    train_scores = {}
    for f in RANK_FEATURES:
        for direction in (1, -1):

            def key_rows(rs, f=f, d=direction):
                return {
                    rid: [
                        dict(
                            c,
                            feat={
                                **c["feat"],
                                "_k": (c["feat"].get(f) * d)
                                if c["feat"].get(f) is not None
                                else None,
                            },
                        )
                        for c in cs
                    ]
                    for rid, cs in rs.items()
                }

            sel = select(key_rows(runs["TRAIN"]), "_k")
            if len(sel) >= 30:
                train_scores[(f, direction)] = st.mean(c["outcomes"]["primary"]["r"] for c in sel)
    best = max(train_scores, key=train_scores.get)
    bf, bd = best

    def keyed(rs):
        return {
            rid: [
                dict(
                    c,
                    feat={
                        **c["feat"],
                        "_k": (c["feat"].get(bf) * bd) if c["feat"].get(bf) is not None else None,
                    },
                )
                for c in cs
            ]
            for rid, cs in rs.items()
        }

    rules = {
        "CURRENT": lambda rs: select(rs, "candidate_score"),
        f"R_BEST({bf},{'desc' if bd > 0 else 'asc'})": lambda rs: select(keyed(rs), "_k"),
        "DIVERSE": lambda rs: select(rs, "candidate_score", diverse=True),
        "NO_MOMENTUM_X": lambda rs: select(rs, "candidate_score", skip_no_mom=True),
        "COMBO": lambda rs: select(keyed(rs), "_k", diverse=True, skip_no_mom=True),
        "ALL_CANDIDATES": lambda rs: [c for cs in rs.values() for c in cs],
    }
    result = {
        "k": K,
        "train_ranking_scores": {
            f"{f}:{'desc' if d > 0 else 'asc'}": round(v, 4)
            for (f, d), v in sorted(train_scores.items(), key=lambda x: -x[1])
        },
        "best_feature": {"feature": bf, "direction": "desc" if bd > 0 else "asc"},
        "rules": {},
    }
    tests = []
    for name, rule in rules.items():
        result["rules"][name] = {}
        for p in ("TRAIN", "VALID", "TEST"):
            sel = rule(runs[p])
            result["rules"][name][p] = _m(sel)
            if name not in ("CURRENT", "ALL_CANDIDATES") and p != "TRAIN":
                cur = rules["CURRENT"](runs[p])
                pv = p_diff_negative(
                    [c["outcomes"]["primary"]["r"] for c in cur],
                    [block_of(c["t0"]) for c in cur],
                    [c["outcomes"]["primary"]["r"] for c in sel],
                    [block_of(c["t0"]) for c in sel],
                )
                tests.append((name, p, pv))
                result["rules"][name][p]["p_better_than_current"] = round(pv, 4)
    for (name, p, _), q in zip(tests, bh([t[2] for t in tests]), strict=True):
        result["rules"][name][p]["q"] = round(q, 4)
    # no-momentum share and outcome among ALL candidates (does it flag the bad ones?)
    for p, rs in by_period.items():
        fl = [r for r in rs if r.get("no_mom") is True]
        nf = [r for r in rs if r.get("no_mom") is False]
        result.setdefault("no_momentum_on_candidates", {})[p] = {
            "flagged": _m(fl),
            "not_flagged": _m(nf),
        }
    (OUT / "opportunity_v2.json").write_text(
        json.dumps(result, indent=1, default=str), encoding="utf-8"
    )
    print(json.dumps(result["best_feature"]))


if __name__ == "__main__":
    main()
