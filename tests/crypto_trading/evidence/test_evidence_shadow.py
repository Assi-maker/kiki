"""Evidence stage 2: live classifier, shadow service and the flagged AI-context
bridge - temporal isolation, read-only access, and proof that evidence cannot
reach Guardian authority, the deterministic Guardian state or the Safety
Kernel."""

import ast
import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from crypto_trading.entry_research import evidence_builder as eb
from crypto_trading.evidence import bridge, store
from crypto_trading.evidence_shadow import binance_data as bd
from crypto_trading.evidence_shadow import classifier as cl
from crypto_trading.evidence_shadow import service as sv
from crypto_trading.guardian.ai_context import build_ai_context

ROOT = Path(__file__).resolve().parents[3]
T = int(datetime(2026, 9, 20, 12, 0, tzinfo=UTC).timestamp())


# ------------------------------------------------------------------ data cut-off


def test_binance_fetchers_drop_everything_after_the_cutoff():
    def get(url):
        if "klines" in url:
            return [
                [(T - 600) * 1000, "1", "2", "0.5", "1.5", "10"],
                [(T - 300) * 1000, "1", "2", "0.5", "1.5", "10"],  # closes exactly at T
                [T * 1000, "9", "9", "9", "9", "9"],
            ]  # closes after T
        if "fundingRate" in url:
            return [
                {"fundingTime": (T - 10) * 1000, "fundingRate": "0.0001"},
                {"fundingTime": (T + 10) * 1000, "fundingRate": "0.9"},
            ]
        return [
            {"timestamp": (T - 300) * 1000, "sumOpenInterestValue": "5"},
            {"timestamp": (T + 300) * 1000, "sumOpenInterestValue": "999"},
        ]

    bars = bd.klines_5m("X", T - 3600, T, get)
    assert [b[0] for b in bars] == [T - 600, T - 300]
    assert bd.funding("X", T - 3600, T, get) == [(T - 10, 0.0001)]
    assert bd.open_interest("X", T - 3600, T, get) == [(T - 300, 5.0)]


def _bars(n_hours=40, end=T, trend=0.0, spike_last=False, wiggle=0.001):
    out, price = [], 100.0
    start = end - n_hours * 3600
    for i, t in enumerate(range(start, end, 300)):
        price *= 1 + trend + wiggle * math.sin(i / 7)
        c = price * (1.03 if spike_last and t == end - 300 else 1.0)
        out.append((t, price, max(price, c) * 1.001, min(price, c) * 0.999, c, 100 + (i % 5)))
    return out


def test_classification_is_identical_whether_or_not_future_data_is_present():
    past = _bars()
    future = [(t, 1e6, 1e6, 1e6, 1e6, 1e9) for t in range(T, T + 6 * 3600, 300)]
    fund = [(T - 3600, 0.0001), (T + 60, 0.5)]
    oi = [(t, 1e6 + t % 7) for t in range(T - 30 * 3600, T + 3600, 300)]
    a = cl.classify_symbol("X", "LONG", T, cl.SymbolData(past + future, fund, oi))
    b = cl.classify_symbol(
        "X",
        "LONG",
        T,
        cl.SymbolData(past, [f for f in fund if f[0] <= T], [o for o in oi if o[0] <= T]),
    )
    assert (a.signal_types, a.regimes, a.last_bar_close) == (
        b.signal_types,
        b.regimes,
        b.last_bar_close,
    )
    assert a.last_bar_close == T


def test_a_breakout_on_the_last_closed_bar_is_classified_and_quiet_data_is_no_event():
    brk = cl.classify_symbol("X", "LONG", T, cl.SymbolData(_bars(spike_last=True), [], []))
    assert "BRK_4H" in brk.signal_types
    flat = cl.SymbolData(_bars(wiggle=0.0), [], [])  # a flat market fires no event
    quiet = cl.classify_symbol("X", "SHORT", T, flat)
    assert quiet.signal_types == ["NO_EVENT"]


def test_market_regime_ignores_bars_after_the_hour():
    btc = _bars(n_hours=24 * 8, trend=0.0002)
    H = T
    later = [(t, 1.0, 1.0, 1.0, 1.0, 1.0) for t in range(H, H + 7200, 300)]
    a = cl.market_regime_at(H, cl.SymbolData(btc + later, [], []), {})
    b = cl.market_regime_at(H, cl.SymbolData(btc, [], []), {})
    assert a == b and a["mkt_trend"] == "bull"


# ------------------------------------------------------------------ bridge (flag)


def _settings(tmp_path, enabled=True):
    return SimpleNamespace(
        evidence=SimpleNamespace(
            context_enabled=enabled,
            evidence_db=str(tmp_path / "ev.db"),
            shadow_db=str(tmp_path / "sh.db"),
        )
    )


def _evidence_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(store.SCHEMA)
    meta = {
        "snapshot_id": "s",
        "as_of": datetime(2026, 9, 1, tzinfo=UTC).isoformat(),
        "built_at": "x",
        "code_hash": "c",
        "source": "t",
        "data_start": "d",
        "symbols": 1,
        "survivorship_note": "note",
        "selection_sha": "sha",
    }
    eb.write_snapshot(
        conn,
        meta,
        [],
        [
            (
                "BRK_4H",
                "LONG",
                "ALL",
                "ALL",
                "NEGATIVE_OOS",
                "HIGH",
                "NOT_DIFFERENT",
                0,
                0,
                "NEGATIVE_OOS: headline",
            )
        ],
    )
    conn.close()


def _classified(path, decision_time):
    sh = sv.open_shadow_db(str(path))
    sh.execute(
        "INSERT INTO candidate_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "c1",
            "XUSDT",
            "LONG",
            decision_time.isoformat(),
            "x",
            None,
            json.dumps(["BRK_4H"]),
            json.dumps({}),
            None,
            "{}",
            None,
            None,
        ),
    )
    sh.commit()
    sh.close()


def test_flag_off_returns_none_without_opening_anything(tmp_path):
    s = _settings(tmp_path, enabled=False)
    assert bridge.evidence_for_candidate(s, "c1", datetime.now(UTC)) is None
    assert bridge.evidence_overview(s, datetime.now(UTC)) is None
    assert not list(tmp_path.iterdir())  # no file was created or touched


def test_flag_on_gives_evidence_only_for_a_classification_made_before_the_decision(tmp_path):
    s = _settings(tmp_path)
    _evidence_db(tmp_path / "ev.db")
    cls_time = datetime(2026, 9, 20, 12, tzinfo=UTC)
    _classified(tmp_path / "sh.db", cls_time)
    early = bridge.evidence_for_candidate(s, "c1", cls_time - timedelta(minutes=1))
    assert early["available"] is False and early["role"] == "CONTEXT_NOT_RULE"
    ok = bridge.evidence_for_candidate(s, "c1", cls_time + timedelta(hours=1))
    assert ok["available"] and ok["role"] == "CONTEXT_NOT_RULE"
    assert ok["signals"][0]["signal_type"] == "BRK_4H"
    assert ok["signals"][0]["status"] == "NEGATIVE_OOS"
    assert (
        bridge.evidence_overview(s, cls_time)["signals"]["BRK_4H LONG"]["status"] == "NEGATIVE_OOS"
    )


def test_the_bridge_never_raises(tmp_path):
    s = _settings(tmp_path)
    (tmp_path / "sh.db").write_text("not a database")
    assert bridge.evidence_for_candidate(s, "c1", datetime.now(UTC)) is None
    assert bridge.evidence_overview(s, datetime.now(UTC)) is None


def test_guardian_ai_context_is_byte_identical_when_evidence_is_absent():
    args = (
        None,
        {"momentum": Decimal("0.5")},
        Decimal("0.3"),
        Decimal("0.1"),
        Decimal("1"),
        "WATCH",
    )
    before = build_ai_context(*args)
    assert "historical_evidence" not in before
    assert build_ai_context(*args, historical_evidence=None) == before
    with_ev = build_ai_context(*args, historical_evidence={"available": True})
    assert {k: v for k, v in with_ev.items() if k != "historical_evidence"} == before
    assert with_ev["new_state"] == "WATCH"  # the decided state is passed through untouched


# ------------------------------------------------------------------ shadow service


def test_the_shadow_process_cannot_write_to_the_bot_database(tmp_path):
    from crypto_trading.storage.repository import SQLiteRepository

    db = tmp_path / "bot.db"
    SQLiteRepository(db)  # creates the schema like the bot does
    repo = sv.read_only_repository(db)
    assert repo.get_position("nope") is None  # reads work
    with pytest.raises(sqlite3.OperationalError):
        repo._conn.execute("DELETE FROM positions")


def test_the_primary_status_is_the_most_cautionary_one():
    class R:
        def lookup(self, typ, side, when, regimes):
            status = {"BRK_4H": "POSITIVE_UNCONFIRMED", "ACCEL": "NEGATIVE_OOS"}[typ]
            return store.EvidencePackage(
                "t",
                {
                    "snapshot_id": "s",
                    "as_of": "a",
                    "survivorship_note": "n",
                    "selection_sha": "x",
                    "symbols": 1,
                },
                typ,
                side,
                {"verdict": {"oos_status": status}},
                {},
            )

    c = cl.Classification("X", "LONG", T, T, ["BRK_4H", "ACCEL"], {})
    _, ev, primary = sv.evidence_for(R(), c, datetime.now(UTC))
    assert primary == "NEGATIVE_OOS" and set(ev) == {"BRK_4H", "ACCEL"}


# ------------------------------------------------------------------ isolation


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    return names


def test_no_bot_module_imports_the_shadow_service():
    for p in (ROOT / "crypto_trading").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith("crypto_trading/evidence_shadow/"):
            continue
        assert not any(n.startswith("crypto_trading.evidence_shadow") for n in _imports(p)), rel


def test_only_the_guardian_ai_context_host_reads_the_evidence_bridge():
    allowed = {"crypto_trading/guardian/tick.py"}
    for p in (ROOT / "crypto_trading").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(
            (
                "crypto_trading/evidence/",
                "crypto_trading/evidence_shadow/",
                "crypto_trading/entry_research/",
            )
        ):
            continue
        if any(n.startswith("crypto_trading.evidence") for n in _imports(p)):
            assert rel in allowed, rel


def test_in_the_guardian_tick_evidence_flows_only_into_build_ai_context():
    """The bridge's return value is used exactly once: as the
    historical_evidence argument of build_ai_context. It never reaches
    classify_guardian_state, decide_open_position, decide_take_profit, the
    live SL tightening or any other call."""
    tree = ast.parse((ROOT / "crypto_trading/guardian/tick.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "evidence_for_candidate"
    ]
    assert len(calls) == 1
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    kw = parents[calls[0]]
    assert isinstance(kw, ast.keyword) and kw.arg == "historical_evidence"
    host = parents[kw]
    assert isinstance(host, ast.Call) and getattr(host.func, "id", None) == "build_ai_context"


def test_the_strategist_gets_no_evidence():
    """2026-09-30: strategist proposals can become PRE_ENTRY_VETO
    heuristics - evidence must never become a trade filter, even
    indirectly, so the strategist does not read it at all."""
    src = (ROOT / "crypto_trading/guardian/self_improvement.py").read_text(encoding="utf-8")
    assert "evidence_overview" not in src
    assert not any(n.startswith("crypto_trading.evidence") for n in _imports(
        ROOT / "crypto_trading/guardian/self_improvement.py"))


def test_decision_modules_still_do_not_import_evidence():
    for rel in (
        "crypto_trading/safety_kernel.py",
        "crypto_trading/guardian/deterministic.py",
        "crypto_trading/guardian/authority.py",
        "crypto_trading/guardian/authority_live.py",
        "crypto_trading/live_execution_loop.py",
        "crypto_trading/orchestrator.py",
    ):
        assert not any(n.startswith("crypto_trading.evidence") for n in _imports(ROOT / rel)), rel


def test_the_flag_is_off_in_the_production_config():
    from crypto_trading.config.loader import get_settings

    assert get_settings().evidence.context_enabled is False
