"""Integrity tests for the novelty / information-gain check (research only)."""

import ast
import io
import random
import zipfile
from pathlib import Path

from crypto_trading.entry_research import book_fill as bf
from crypto_trading.entry_research import novelty as nv
from crypto_trading.entry_research import regime_lab as rl

ROOT = Path(__file__).resolve().parents[3]


def _data(n=4000, seed=1):
    rnd = random.Random(seed)
    d = {c: [] for c in nv.COLS}
    for i in range(n):
        ex = [rnd.gauss(0, 1) for _ in nv.EXISTING]
        new = rnd.gauss(0, 1)
        noise = rnd.gauss(0, 1)
        row = {c: x for c, x in zip(nv.EXISTING, ex, strict=True)}
        row["T"] = rl._ts(2025, 9, 1) + i * 3600
        row["fwd1h"] = 0.5 * ex[0] + 0.3 * new + noise  # new carries information
        row["fwd4h"] = 0.5 * ex[0] + noise
        row["TI_1h"] = new
        row["TI_5m"] = ex[0] + 0.01 * rnd.gauss(0, 1)  # a copy of an existing feature
        for c in nv.COLS:
            d[c].append(row.get(c, rnd.gauss(0, 1)))
    return d


def test_a_copy_of_an_existing_feature_has_no_partial_information_but_a_new_one_does():
    out = nv.evaluate_group(_data(), ("TI_5m", "TI_1h"), {})
    assert out["TI_5m"]["redundancy"] > 0.95
    assert abs(out["TI_5m"]["fwd1h"]["partial_ic"]) < 0.05  # raw IC is large, partial is not
    assert abs(out["TI_5m"]["fwd1h"]["ic"]) > 0.3
    assert out["TI_1h"]["fwd1h"]["partial_ic"] > 0.15
    assert out["TI_1h"]["fwd1h"]["partial_p"] < 0.01
    assert out["TI_1h"]["redundancy"] < 0.1


def test_verdict_needs_valid_confirmation_size_and_non_redundancy():
    good = {
        "n": 10_000,
        "redundancy": 0.2,
        "fwd1h": {"partial_ic": 0.03, "partial_p": 0.001, "decile_spread_pct": 0.3},
        "fwd4h": {"partial_ic": 0.0, "partial_p": 0.5, "decile_spread_pct": 0.0},
    }
    assert nv.verdict(good, good)["pass"]
    flip = {**good, "fwd1h": {**good["fwd1h"], "partial_ic": -0.03, "decile_spread_pct": -0.3}}
    assert not nv.verdict(good, flip)["pass"]  # sign flips in VALID
    small = {**good, "fwd1h": {**good["fwd1h"], "decile_spread_pct": 0.05}}
    assert not nv.verdict(small, small)["pass"]  # below the round-trip cost
    red = {**good, "redundancy": 0.8}
    assert not nv.verdict(red, red)["pass"]


def test_only_train_and_valid_are_ever_loaded():
    assert nv.PERIODS == ("TRAIN", "VALID")
    src = (ROOT / "crypto_trading/entry_research/novelty.py").read_text(encoding="utf-8")
    assert '"TEST"' not in src and '"HOLDOUT"' not in src


def test_book_snapshot_is_used_only_if_at_most_120_s_old_at_the_bar_close():
    lines = ["timestamp,percentage,depth,notional"]
    for ts in ("2026-03-01 00:03:30", "2026-03-01 00:06:50"):
        for pct, notional in ((-1.0, 300), (-0.2, 30), (0.2, 10), (1.0, 100)):
            lines.append(f"{ts},{pct},1,{notional}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "\n".join(lines))
    rows = bf.parse("SOLUSDT", buf.getvalue())
    closes = {r[1] for r in rows}
    base = rl._ts(2026, 3, 1) * 1000
    assert base + 300_000 in closes  # 00:05 close: snapshot 00:03:30 is 90 s old
    assert base + 600_000 not in closes  # 00:10 close: 00:06:50 is 190 s old -> unknown
    r = next(r for r in rows if r[1] == base + 300_000)
    assert abs(r[3] - (300 - 100) / 400) < 1e-12  # imb1 = (bid - ask) / (bid + ask)


def test_ranks_average_ties():
    assert nv.ranks([3.0, 1.0, 3.0, 2.0]) == [2.5, 0.0, 2.5, 1.0]


def test_the_new_modules_import_nothing_that_trades():
    for mod in ("novelty.py", "book_fill.py"):
        tree = ast.parse((ROOT / "crypto_trading/entry_research" / mod).read_text(encoding="utf-8"))
        names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        for name in names:
            if name.startswith("crypto_trading"):
                assert name.split(".")[1] == "entry_research", (mod, name)


def test_old_book_files_without_the_02_band_still_give_the_1_pct_imbalance():
    lines = ["timestamp,percentage,depth,notional"]
    for pct, notional in ((-1, 300), (1, 100), (-2, 500), (2, 400)):
        lines.append(f"2025-10-01 00:04:00,{pct},1,{notional}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "\n".join(lines))
    (row,) = bf.parse("BTCUSDT", buf.getvalue())
    assert row[2] is None and abs(row[3] - 0.5) < 1e-12 and row[4] == 400
