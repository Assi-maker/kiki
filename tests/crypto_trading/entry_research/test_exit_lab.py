"""Integrity tests for the exit / holding-model lab (research only)."""

import ast
from pathlib import Path

from crypto_trading.entry_research import exit_lab as xl
from crypto_trading.entry_research import regime_lab as rl

ROOT = Path(__file__).resolve().parents[3]
V = {name: i for i, name in enumerate(xl.VNAMES)}
ENTRY, RISK = 100.0, 1.0  # 1 R = 1 price unit


def _run(path, sig=None, mom=None, fund=None, fee_r=0.0, atr_live=None):
    n = len(path)
    return xl.simulate(
        path,
        ENTRY,
        RISK,
        1,
        0.25,
        fee_r,
        fund or [0.0] * n,
        sig if sig is not None else [True] * n,
        mom or [True] * n,
        atr_live or [None] * n,
    )


def _flat(n, hi=0.1, lo=-0.1):
    return [(hi, lo, 0.0, 0.0) for _ in range(n)]


def test_fixed_hits_target_and_stop_is_checked_first_within_a_bar():
    path = _flat(3) + [(2.0, -0.2, 1.0, 1.0)] + _flat(10)
    assert _run(path)[V["FIXED"]][:2] == (xl.TP_R, 4)
    both = _flat(2) + [(2.0, -1.5, 0.0, 0.0)] + _flat(10)
    r, bars, _ = _run(both)[V["FIXED"]]
    assert bars == 3 and r < -1.0  # stop first, plus slippage


def test_a_trailing_stop_moves_at_the_close_and_applies_only_from_the_next_bar():
    # bar 0 runs to +1.2 R and falls back to -0.9 R inside the SAME bar: the
    # trail (peak - 0.5 R) must not fire there; it is armed for bar 1.
    path = [(1.2, -0.9, 0.0, 0.0), (0.8, 0.6, 0.7, 0.7)] + _flat(10, 0.9, 0.8)
    r, bars, _ = _run(path)[V["NOTP_TRAIL0.5"]]
    assert bars == 2  # 1.2 - 0.5 = 0.7 stop, touched by bar 1's low 0.6
    slip = rl.STOP_SLIP * (ENTRY + 0.7 * RISK) / RISK  # 0.15 % of the stop price, in R
    assert abs(r - (0.7 - slip)) < 1e-9


def test_break_even_then_trailing():
    path = [(0.6, -0.1, 0.5, 0.5), (0.5, -0.05, 0.0, 0.0)] + _flat(20, 0.2, 0.1)
    r, bars, _ = _run(path, fee_r=0.1)[V["BE0.5_TRAIL"]]
    assert bars == 2 and r < 0.01  # stopped at BE (+fees) minus slippage and fees


def test_time_exit_at_the_close_of_the_nth_bar():
    path = [(0.2, -0.1, 0.1 * (m + 1), 0.0) for m in range(20)]
    r, bars, _ = _run(path)[V["TIME15m"]]
    assert bars == 3 and abs(r - 0.3) < 1e-9


def test_signal_exit_decides_at_a_close_and_fills_at_the_next_open_not_before_bar_3():
    path = [(0.2, -0.1, 0.1, 0.15) for _ in range(20)]
    sig = [False, False, False] + [True] * 17  # false from the start
    r, bars, _ = _run(path, sig=sig)[V["SIG_OFF"]]
    assert bars == 3 and abs(r - 0.15) < 1e-9  # decided at bar index 2's close


def test_baseline_has_no_signal_exit():
    n = 20
    out = xl.simulate(_flat(n), ENTRY, RISK, 1, 0.25, 0.0, [0.0] * n, None, [True] * n, [None] * n)
    assert out[V["SIG_OFF"]] is None
    assert out[V["FIXED"]] is not None


def test_funding_paid_up_to_the_exit_bar_is_charged():
    path = [(0.2, -0.1, 0.0, 0.0)] * 5 + [(2.0, 0.0, 1.0, 1.0)]
    fund = [0.0, 0.0, 0.05, 0.05, 0.05, 0.05]
    assert abs(_run(path, fund=fund)[V["FIXED"]][0] - (xl.TP_R - 0.05)) < 1e-9


def test_every_variant_ends_within_24_hours():
    out = _run(_flat(xl.HORIZON_BARS))
    assert all(o is None or o[1] <= xl.HORIZON_BARS for o in out)


def test_the_selection_never_reads_test_or_holdout():
    tab = xl.ExitTable()
    ch = xl.new_chunk()
    tr0, va0, te0 = rl._ts(2025, 9, 1), rl._ts(2026, 2, 10), rl._ts(2026, 5, 10)

    def add(T, typ, r_fixed, r_other):
        outs = [(r_other, 10, -0.2)] * xl.NV
        outs[V["FIXED"]] = (r_fixed, 10, -0.2)
        xl.add_row(ch, f"S{T % 50}", T, typ, "LONG", 1.0, T + 300, 1.0, outs, {})

    for i in range(300):
        add(tr0 + i * 7200, "BRK_4H", 0.8 if i % 3 else -0.4, -0.5)
    for i in range(60):
        add(va0 + i * 7200, "BRK_4H", 0.8 if i % 3 else -0.4, -0.5)
    for i in range(100):
        add(te0 + i * 7200, "BRK_4H", -1.0, 5.0)  # other exits look great ONLY in TEST
    tab.extend(ch)
    survivors, _ = xl.select(tab, xl.build_cells(tab))
    assert {s["variant"] for s in survivors} == {"FIXED"}


def test_no_bot_module_imports_the_exit_lab():
    offenders = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "crypto_trading").rglob("*.py")
        if not p.relative_to(ROOT).as_posix().startswith("crypto_trading/entry_research/")
        and (
            "entry_research.exit_lab" in p.read_text(encoding="utf-8")
            or "import exit_lab" in p.read_text(encoding="utf-8")
        )
    ]
    assert offenders == []


def test_the_exit_lab_imports_nothing_that_trades():
    tree = ast.parse(
        (ROOT / "crypto_trading/entry_research/exit_lab.py").read_text(encoding="utf-8")
    )
    names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    for name in names:
        if name.startswith("crypto_trading"):
            assert name.split(".")[1] == "entry_research", name


def test_compact_r_storage_keeps_nan_and_0001_resolution():
    q = xl.QArr()
    q.extend([0.12345, float("nan"), -1.2, 99.0])
    assert q[0] == 0.123 and q[1] != q[1] and q[2] == -1.2 and q[3] == 32.767


def test_checkpoint_roundtrip_resume_and_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr(xl, "CHECKPOINT_DIR", tmp_path)
    chunk = xl.new_chunk()
    outs = [(0.1, 3, -0.2)] * xl.NV
    xl.add_row(chunk, "AAAUSDT", rl._ts(2025, 9, 1), "BRK_4H", "LONG", 1.0, 0, 1.0, outs, {})
    monkeypatch.setattr(xl, "scan_symbol", lambda sym: (chunk, [(1, 0.1, None)]))
    assert xl.scan_and_checkpoint("AAAUSDT", "code-A") == "AAAUSDT"
    assert not list(tmp_path.glob("*.tmp"))  # written atomically
    ch, xs = xl.load_checkpoint("AAAUSDT", "code-A")
    assert ch["sym"] == ["AAAUSDT"] and xs == [(1, 0.1, None)]
    assert xl.load_checkpoint("AAAUSDT", "code-B") is None  # other code -> recompute
    assert xl.load_checkpoint("BBBUSDT", "code-A") is None  # never scanned
    (tmp_path / "BBBUSDT.pkl").write_bytes((tmp_path / "AAAUSDT.pkl").read_bytes())
    assert xl.load_checkpoint("BBBUSDT", "code-A") is None  # file of another symbol


def test_code_hash_is_stable():
    assert xl.code_hash() == xl.code_hash()
