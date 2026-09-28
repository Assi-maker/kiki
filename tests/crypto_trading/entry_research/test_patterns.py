import random
from datetime import UTC, datetime, timedelta

from crypto_trading.entry_research import patterns as pt

START = datetime(2026, 9, 1, tzinfo=UTC)


def _rows(n, start, edge: bool, seed: int):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        feat = {f: None for f in pt.NUMERIC}
        feat.update({f: None for f in pt.BOOLEAN})
        feat["ret_4h"] = rng.uniform(-5, 5)
        feat["volz_30m"] = rng.uniform(-3, 8)
        feat["btc_ret_4h"] = rng.uniform(-2, 2)
        feat["trig_volume"] = rng.random() < 0.5
        r = rng.gauss(-0.1, 1.0)
        if edge and feat["ret_4h"] < -1.7 and feat["volz_30m"] > 3.2:
            r += 1.2  # planted edge
        rows.append({"t0": start + timedelta(hours=i * 0.5), "feat": feat,
                     "outcomes": {"primary": {"r": r, "mfe_pct": 1, "mae_pct": -1,
                                              "minutes_to_mfe": 5, "risk_pct": 2}}})
    return rows


def _run(edge: bool, seed: int):
    train = _rows(900, START, edge, seed)
    valid = _rows(700, START + timedelta(days=20), edge, seed + 1)
    test = _rows(700, START + timedelta(days=40), edge, seed + 2)
    cuts = pt.tertile_cuts(train)
    selected, _ = pt.discover(train, cuts)
    return pt.evaluate(selected, valid, test)


def test_protocol_recovers_a_planted_two_feature_edge():
    items = _run(edge=True, seed=3)
    winners = [pt.label(i["pattern"]) for i in items if i["class"] in ("EDGE", "WEAK_EDGE")]
    assert any("ret_4h<=" in w and "volz_30m>" in w for w in winners)


def test_protocol_does_not_crown_pure_noise():
    for seed in (5, 17, 29):
        items = _run(edge=False, seed=seed)
        assert not [i for i in items if i["class"] == "EDGE"]


def test_tertile_conditions_are_exclusive_and_exhaustive():
    cond = [("x", op, 1.0, 2.0) for op in ("LOW", "MID", "HIGH")]
    for v in (0.5, 1.0, 1.5, 2.0, 2.5):
        assert sum(pt.holds(c, {"x": v}) for c in cond) == 1
    assert not any(pt.holds(c, {"x": None}) for c in cond)
