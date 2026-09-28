"""Shadow evaluation report (P3-P6, 2026-09-28).

`python -m crypto_trading.shadow.report [--out FILE]` - read-only.

Rules for calling anything an EDGE (all must hold):
- n >= 30 on each side that matters;
- Benjamini-Hochberg q < 0.05 across EVERY hypothesis in the report;
- the effect has the same sign out-of-sample (OOS cohort if it has >= 10
  records, otherwise the later time half as a walk-forward check).
Otherwise: INSUFFICIENT_DATA (too small), NOISE (not significant) or
NOT_CONFIRMED_OOS. Continuous features are cut into tertiles on the TRAIN
half and those cutpoints are applied unchanged to the TEST half.
"""
from __future__ import annotations

import argparse
import random
import statistics as st

MIN_N = 30
ALPHA = 0.05


def classify(n: int, q: float, oos_same_sign: bool | None) -> str:
    if n < MIN_N:
        return "INSUFFICIENT_DATA"
    if q >= ALPHA:
        return "NOISE"
    if oos_same_sign is not True:
        return "NOT_CONFIRMED_OOS"
    return "EDGE"


def benjamini_hochberg(pvalues: list[float]) -> list[float]:
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, pvalues[i] * m / rank)
        q[i] = prev
    return q


def _perm_p(a: list[float], b: list[float], n: int = 3000, seed: int = 7) -> float:
    if len(a) < 2 or len(b) < 2:
        return 1.0
    rng = random.Random(seed)
    observed = abs(st.mean(a) - st.mean(b))
    pool, hits = a + b, 0
    for _ in range(n):
        rng.shuffle(pool)
        if abs(st.mean(pool[:len(a)]) - st.mean(pool[len(a):])) >= observed - 1e-12:
            hits += 1
    return (hits + 1) / (n + 1)


def _ci(xs: list[float], n: int = 2000, seed: int = 11) -> tuple[float, float] | None:
    if len(xs) < 3:
        return None
    rng = random.Random(seed)
    means = sorted(st.mean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return round(means[int(0.025 * n)], 3), round(means[int(0.975 * n)], 3)


def _r(record: dict) -> float | None:
    outcome = record.get("outcome") or {}
    return outcome.get("r")


def _split(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """TRAIN = in-sample / first time half; TEST = OOS cohort when it has at
    least 10 records, else the second time half (walk-forward)."""
    ordered = sorted(records, key=lambda r: r["decided_at"])
    oos = [r for r in ordered if r.get("cohort") == "OOS"]
    if len(oos) >= 10:
        return [r for r in ordered if r.get("cohort") != "OOS"], oos
    half = len(ordered) // 2
    return ordered[:half], ordered[half:]


def _mean(xs):
    return st.mean(xs) if xs else None


def _fmt(x, d=3):
    return "–" if x is None else f"{x:+.{d}f}"


def _hypothesis(name, group, rest, test_group, test_rest):
    g, o = [x for x in map(_r, group) if x is not None], [x for x in map(_r, rest) if x is not None]
    tg, to = [x for x in map(_r, test_group) if x is not None], [x for x in map(_r, test_rest) if x is not None]
    effect = (_mean(g) - _mean(o)) if g and o else None
    test_effect = (_mean(tg) - _mean(to)) if tg and to else None
    same = None if effect is None or test_effect is None else (effect > 0) == (test_effect > 0)
    return {"name": name, "n": len(g), "n_rest": len(o), "mean": _mean(g), "mean_rest": _mean(o),
            "effect": effect, "ci": _ci(g), "p": _perm_p(g, o), "test_effect": test_effect,
            "oos_same_sign": same}


def build_report(records: list[dict]) -> str:
    usable = [r for r in records if _r(r) is not None]
    train, test = _split(usable)
    hypotheses: list[dict] = []
    sections: dict[str, list[dict]] = {}

    def add(section, name, predicate):
        grp = [r for r in usable if predicate(r) is True]
        rest = [r for r in usable if predicate(r) is False]
        tg = [r for r in test if predicate(r) is True]
        to = [r for r in test if predicate(r) is False]
        h = _hypothesis(name, grp, rest, tg, to)
        hypotheses.append(h)
        sections.setdefault(section, []).append(h)

    rules = sorted({k for r in usable for k in (r.get("veto_flags") or {})})
    for rule in rules:
        add("Veto rules (group = WOULD BLOCK; effect = mean R blocked - kept)", rule,
            lambda r, rule=rule: (r.get("veto_flags") or {}).get(rule))
    triggers = sorted({"+".join(r.get("trigger_reasons") or []) for r in usable})
    for trig in triggers:
        add("Trigger types (group = this trigger; effect vs all others)", f"trigger={trig}",
            lambda r, trig=trig: "+".join(r.get("trigger_reasons") or []) == trig)
    for feature in ("candidate_score", "risk_reward", "forecast_uncertainty", "symbol_vol_1h_pct",
                    "volume_zscore", "btc_ret_4h_pct"):
        def value(r, feature=feature):
            v = r.get(feature) if feature == "candidate_score" else (r.get("features") or {}).get(feature)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        train_values = sorted(v for v in map(value, train) if v is not None)
        if len(train_values) < 9:
            continue
        low, high = train_values[len(train_values) // 3], train_values[2 * len(train_values) // 3]
        add("Continuous features (tertiles cut on TRAIN, applied to TEST)", f"{feature} <= {low:.4g} (bottom third)",
            lambda r, value=value, low=low: None if value(r) is None else value(r) <= low)
        add("Continuous features (tertiles cut on TRAIN, applied to TEST)", f"{feature} > {high:.4g} (top third)",
            lambda r, value=value, high=high: None if value(r) is None else value(r) > high)
    add("GODFATHER entry quality (P6, observe-only)", "GF EQ verdict == TRADE",
        lambda r: None if (r.get("veto_flags") or {}).get("GODFATHER_EQ_NOT_TRADE") is None
        else not r["veto_flags"]["GODFATHER_EQ_NOT_TRADE"])

    # exit variants: paired per candidate (variant R - base R)
    variant_rows = []
    names = sorted({k for r in usable for k in (r.get("variants") or {})})
    for name in names:
        def delta(r, name=name):
            v = (r.get("variants") or {}).get(name)
            return None if not v or v.get("r") is None else v["r"] - _r(r)
        d = [x for x in map(delta, usable) if x is not None]
        dt = [x for x in map(delta, test) if x is not None]
        dtr = [x for x in map(delta, train) if x is not None]
        p = _perm_p(d, [0.0] * len(d)) if d else 1.0
        variant_rows.append({"name": name, "n": len(d), "mean": _mean(d), "ci": _ci(d), "p": p,
                             "train": _mean(dtr), "test": _mean(dt),
                             "oos_same_sign": None if not dt or not dtr else (_mean(dtr) > 0) == (_mean(dt) > 0)})
    all_p = [h["p"] for h in hypotheses] + [v["p"] for v in variant_rows]
    all_q = benjamini_hochberg(all_p) if all_p else []
    for h, q in zip(hypotheses + variant_rows, all_q):
        h["q"] = q
        h["class"] = classify(h["n"], q, h["oos_same_sign"])

    lines = [
        "# Shadow evaluation report",
        "",
        f"Records with an outcome: n={len(usable)} (train {len(train)}, test {len(test)}; "
        f"OOS cohort {sum(1 for r in usable if r.get('cohort') == 'OOS')}). "
        f"{len(all_p)} hypotheses, Benjamini-Hochberg across all of them.",
        f"Baseline: mean R {_fmt(_mean([_r(r) for r in usable]))} CI {_ci([_r(r) for r in usable])}; "
        f"TEST mean R {_fmt(_mean([_r(r) for r in test]))}.",
        "",
        "EDGE requires n >= 30, q < 0.05 and the same sign in TEST. Nothing here changes LIVE.",
    ]
    for section, rows in sections.items():
        lines += ["", f"## {section}", "",
                  "| Hypothesis | n | mean R | n rest | mean R rest | effect | CI (group) | TEST effect | q | class |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for h in rows:
            lines.append(f"| {h['name']} | {h['n']} | {_fmt(h['mean'])} | {h['n_rest']} | {_fmt(h['mean_rest'])} | "
                         f"{_fmt(h['effect'])} | {h['ci']} | {_fmt(h['test_effect'])} | {h['q']:.3f} | {h['class']} |")
    for title, prefix in (("Break-even (P4, shadow)", "BE_"), ("Trailing (P5, shadow)", "TRAIL_")):
        lines += ["", f"## {title}: paired delta R vs the plain bracket", "",
                  "| Variant | n | mean delta R | CI | TRAIN | TEST | q | class |", "|---|---|---|---|---|---|---|---|"]
        for v in variant_rows:
            if v["name"].startswith(prefix):
                lines.append(f"| {v['name']} | {v['n']} | {_fmt(v['mean'])} | {v['ci']} | {_fmt(v['train'])} | "
                             f"{_fmt(v['test'])} | {v['q']:.3f} | {v['class']} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    from crypto_trading.config.loader import get_settings
    from crypto_trading.storage.repository import SQLiteRepository

    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    parser.add_argument("--db")
    args = parser.parse_args()
    settings = get_settings()
    repo = SQLiteRepository(args.db or settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    text = build_report(repo.list_shadow_evaluations())
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
    print(text)


if __name__ == "__main__":
    main()
