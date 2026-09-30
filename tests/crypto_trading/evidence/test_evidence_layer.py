"""Historical Evidence Layer: temporal isolation, honest verdicts, read-only
access and isolation from every decision path."""

import ast
import dataclasses
import math
import random
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from crypto_trading.entry_research import evidence_builder as eb
from crypto_trading.entry_research import exit_lab as xl
from crypto_trading.entry_research import regime_lab as rl
from crypto_trading.evidence import store, verdict

ROOT = Path(__file__).resolve().parents[3]
FIX = xl.VNAMES.index("FIXED")


def _table(rows):
    """rows: (sym, T, typ, side, r_fixed, hold_bars, reg dict)."""
    ch = xl.new_chunk()
    for sym, T, typ, side, r, hold, reg in rows:
        outs = [(r, hold, -0.5)] * xl.NV
        xl.add_row(ch, sym, T, typ, side, 1.0, T + 300, 1.2, outs, reg)
    tab = xl.ExitTable()
    tab.extend(ch)
    return tab


def _cells(tab, as_of, accepted=frozenset()):
    cells, verdicts = eb.build_snapshot(tab, as_of, eb.pooled_flags(tab), set(accepted))
    by_key = {c[:6]: dict(zip(eb.CELL_STAT_COLS, c[6:], strict=True)) for c in cells}
    return by_key, {v[:4]: v[4:] for v in verdicts}


VALID_T = rl._ts(2026, 3, 2)


# ------------------------------------------------------------------ temporal isolation


def test_an_outcome_still_open_at_as_of_is_invisible():
    tab = _table([("S0", VALID_T, "BRK_4H", "LONG", 0.9, 100, {})])  # exits after 500 min
    exit_time = datetime.fromtimestamp(VALID_T + 300 + 100 * 300, UTC)
    before, _ = _cells(tab, exit_time)  # exit == as_of -> not yet known
    after, _ = _cells(tab, exit_time + timedelta(seconds=1))
    key = ("BRK_4H", "LONG", "ALL", "ALL", "FIXED", "VALID")
    assert key not in before
    assert after[key]["n"] == 1


def test_the_same_entry_can_be_known_for_a_short_exit_and_unknown_for_fixed():
    tab = _table([("S0", VALID_T, "BRK_4H", "LONG", 0.5, 280, {})])
    as_of = datetime.fromtimestamp(VALID_T + 3 * 3600, UTC)  # 3 h later
    cells, _ = _cells(tab, as_of)
    assert ("BRK_4H", "LONG", "ALL", "ALL", "FIXED", "VALID") not in cells


def test_lookup_reads_only_the_newest_snapshot_at_or_before_the_decision_time(tmp_path):
    db = tmp_path / "ev.db"
    conn = sqlite3.connect(db)
    conn.executescript(store.SCHEMA)
    for month, mean in ((2, -0.1), (3, -0.2)):
        as_of = datetime(2026, month, 1, tzinfo=UTC)
        meta = {
            "snapshot_id": f"s{month}",
            "as_of": as_of.isoformat(),
            "built_at": "x",
            "code_hash": "c",
            "source": "t",
            "data_start": "d",
            "symbols": 1,
            "survivorship_note": "note",
            "selection_sha": "sha",
        }
        cell = (
            "BRK_4H",
            "LONG",
            "ALL",
            "ALL",
            "FIXED",
            "OOS",
            500,
            1.0,
            mean,
            0.01,
            mean - 0.02,
            mean + 0.02,
            0.9,
            0.4,
            1.0,
            -0.8,
            300.0,
            -5.0,
            None,
            None,
            None,
        )
        eb.write_snapshot(conn, meta, [cell], [])
    conn.close()
    reader = store.EvidenceReader(db)
    assert reader.lookup("BRK_4H", "LONG", datetime(2026, 1, 31, tzinfo=UTC)) is None
    feb = reader.lookup("BRK_4H", "LONG", datetime(2026, 2, 20, tzinfo=UTC))
    assert feb.snapshot["snapshot_id"] == "s2"
    assert feb.horizons["FIXED"]["mean_r"] == -0.1
    exact = reader.lookup("BRK_4H", "LONG", datetime(2026, 3, 1, tzinfo=UTC))
    assert exact.snapshot["snapshot_id"] == "s3"
    with pytest.raises(ValueError):
        reader.lookup("BRK_4H", "LONG", datetime(2026, 3, 2))  # naive time refused
    reader.close()


def test_the_reader_cannot_write(tmp_path):
    db = tmp_path / "ev.db"
    sqlite3.connect(db).executescript(store.SCHEMA)
    reader = store.EvidenceReader(db)
    with pytest.raises(sqlite3.OperationalError):
        reader._conn.execute("DELETE FROM evidence_cell")
    reader.close()
    with pytest.raises(FileNotFoundError):
        store.EvidenceReader(tmp_path / "missing.db")  # never creates an empty db


# ------------------------------------------------------------------ statistics / verdicts


def test_train_positive_never_makes_a_signal_profitable():
    rnd = random.Random(3)
    rows = []
    for i in range(400):  # TRAIN clearly positive
        rows.append(
            ("S0", rl._ts(2025, 9, 1) + i * 7200, "BRK_4H", "LONG", 0.4 + rnd.gauss(0, 0.2), 10, {})
        )
    for i in range(400):  # OOS clearly negative
        rows.append(("S0", VALID_T + i * 7200, "BRK_4H", "LONG", -0.3 + rnd.gauss(0, 0.2), 10, {}))
    _, verdicts = _cells(_table(rows), datetime(2026, 9, 30, tzinfo=UTC))
    status, _, _, train_only, accepted, head = verdicts[("BRK_4H", "LONG", "ALL", "ALL")]
    assert status == "NEGATIVE_OOS" and train_only == 1 and accepted == 0
    assert "NEGATIVE_OOS" in head


def test_positive_oos_is_unconfirmed_unless_the_protocol_accepted_it():
    rnd = random.Random(4)
    rows = [
        ("S0", VALID_T + i * 7200, "ACCEL", "SHORT", 0.3 + rnd.gauss(0, 0.2), 10, {})
        for i in range(400)
    ]
    tab = _table(rows)
    as_of = datetime(2026, 9, 30, tzinfo=UTC)
    _, v = _cells(tab, as_of)
    assert v[("ACCEL", "SHORT", "ALL", "ALL")][0] == "POSITIVE_UNCONFIRMED"
    _, v2 = _cells(tab, as_of, accepted={("ACCEL", "SHORT", "ALL", "ALL")})
    assert v2[("ACCEL", "SHORT", "ALL", "ALL")][0] == "VALIDATED_EDGE"


def test_small_samples_are_insufficient_not_labelled():
    assert verdict.oos_status({"n": 99, "ci_low": 0.1, "ci_high": 0.3}, True) == "INSUFFICIENT_DATA"
    assert verdict.oos_status(None, False) == "INSUFFICIENT_DATA"


def test_day_clustered_standard_error_matches_a_direct_computation():
    rnd = random.Random(5)
    rows = []
    for d in range(30):
        shock = rnd.gauss(0, 0.3)  # common daily shock -> clustering matters
        for h in range(8):
            rows.append(
                (
                    "S0",
                    VALID_T + d * 86400 + h * 3600,
                    "ACCEL",
                    "LONG",
                    shock + rnd.gauss(0, 0.1),
                    2,
                    {},
                )
            )
    cells, _ = _cells(_table(rows), datetime(2026, 9, 30, tzinfo=UTC))
    s = cells[("ACCEL", "LONG", "ALL", "ALL", "FIXED", "VALID")]
    rs = [round(r[4] * 1000) / 1000 for r in rows]  # stored at 0.001 R (exit_lab.QArr)
    m = sum(rs) / len(rs)
    sums = {}
    for r in rows:
        k = r[1] // 86400
        sums[k] = sums.get(k, 0.0) + (round(r[4] * 1000) / 1000 - m)
    g = len(sums)
    se = math.sqrt(sum(x * x for x in sums.values()) / len(rs) ** 2 * g / (g - 1))
    assert abs(s["se_r"] - se) < 1e-9
    naive = math.sqrt(sum((x - m) ** 2 for x in rs) / (len(rs) - 1) / len(rs))
    assert s["se_r"] > 2 * naive  # the clustered SE is honest about correlated days


def test_difference_to_the_random_baseline_uses_the_same_side_regime_exit_and_period():
    rows = [
        ("S0", VALID_T + i * 7200 + 86400 * (i % 20), "ACCEL", "LONG", 0.1, 2, {"sym_oi": "up"})
        for i in range(200)
    ]
    rows += [
        ("S1", VALID_T + i * 7200 + 86400 * (i % 20), "BASELINE", "LONG", -0.1, 2, {"sym_oi": "up"})
        for i in range(200)
    ]
    rows += [
        ("S2", VALID_T + i * 7200 + 86400 * (i % 20), "BASELINE", "SHORT", 5.0, 2, {"sym_oi": "up"})
        for i in range(200)
    ]  # other side: must be ignored
    cells, _ = _cells(_table(rows), datetime(2026, 9, 30, tzinfo=UTC))
    s = cells[("ACCEL", "LONG", "sym_oi", "up", "FIXED", "OOS")]
    assert abs(s["baseline_mean_r"] - (-0.1)) < 1e-9
    assert abs(s["diff_vs_baseline"] - 0.2) < 1e-9


# ------------------------------------------------------------------ what GODFATHER receives


def test_the_package_is_immutable_and_the_ai_context_carries_no_action(tmp_path):
    pkg = store.EvidencePackage(
        decision_time="t",
        snapshot={
            "snapshot_id": "s",
            "as_of": "a",
            "survivorship_note": "n",
            "selection_sha": "x",
            "symbols": 1,
        },
        signal_type="BRK_4H",
        side="LONG",
        overall={
            "verdict": {"oos_status": "NO_EDGE", "headline": "h", "train_only_positive": False},
            "periods": {},
        },
        horizons={"FIXED": {"mean_r": -0.1}},
        regimes={},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        pkg.side = "SHORT"
    ctx = store.evidence_context(pkg)
    forbidden = {"action", "veto", "close", "open", "size", "stop_loss", "take_profit"}
    assert not forbidden & set(ctx)
    assert store.evidence_context(None)["available"] is False


# ------------------------------------------------------------------ isolation


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    return names


def test_the_evidence_package_imports_nothing_that_can_trade():
    for p in (ROOT / "crypto_trading/evidence").glob("*.py"):
        for name in _imports(p):
            if name.startswith("crypto_trading"):
                assert name.startswith("crypto_trading.evidence"), (p.name, name)


def _imports_evidence(path: Path) -> bool:
    return any(
        n.startswith("crypto_trading.evidence") or n.endswith("evidence_builder")
        for n in _imports(path)
    )


def test_no_decision_path_reads_the_evidence_layer():
    """Guardian's deterministic state machine, its authority (TIGHTEN_SL /
    TAKE_PROFIT), the Safety Kernel, the Gate, execution and sizing must not
    import it: evidence can only ever reach an AI context (stage 2, flagged)."""
    decision_paths = [
        ROOT / "crypto_trading/safety_kernel.py",
        ROOT / "crypto_trading/guardian/deterministic.py",
        ROOT / "crypto_trading/guardian/authority.py",
        ROOT / "crypto_trading/guardian/authority_live.py",
        ROOT / "crypto_trading/orchestrator.py",
        *(ROOT / "crypto_trading/paper_trading").rglob("*.py"),
        *(ROOT / "crypto_trading/gate").rglob("*.py"),
    ]
    for p in decision_paths:
        assert p.exists(), p
        assert not _imports_evidence(p), p


def test_the_bot_never_imports_the_builder():
    for p in (ROOT / "crypto_trading").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith("crypto_trading/entry_research/"):
            continue
        assert not any(n.endswith("evidence_builder") for n in _imports(p)), rel
