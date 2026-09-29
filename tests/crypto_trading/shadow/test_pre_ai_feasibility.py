"""Pre-AI risk feasibility - SHADOW ONLY (2026-09-29).

It answers "could a full 100 x 10 position possibly fit the current Safety
Kernel?" before the AI chain, logs the answer, and changes nothing: no
candidate is blocked, the AI chain, Gate, Safety Kernel, sizing and
execution are untouched, and UNKNOWN is never a reject."""
import ast
import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.config.loader import SafetyKernelConfig, get_settings
from crypto_trading.paper_trading.replay import run_single_cycle
from crypto_trading.safety_kernel import Exposure, size_entry
from crypto_trading.shadow import pre_ai_feasibility as paf
from crypto_trading.shadow.pre_ai_feasibility_hook import build_hook, closed_30m_atr
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.paper_trading.test_replay import (
    _build_snapshots,
    _happy_fixtures,
    _settings,
)

ROOT = pathlib.Path(__file__).resolve().parents[3]
LIMITS = SafetyKernelConfig(max_portfolio_risk_pct=Decimal("0.10"), max_group_risk_pct=Decimal("0.05"))
CAL = paf.Calibration("30m", 14, ratio_lower=0.45, ratio_median=2.72, ratio_upper=25.0, stop_floor_pct=0.47,
                      frozen_on="TRAIN")


def _assess(**kw):
    args = dict(symbol="X-USDT", price=100.0, atr30_pct=0.5, equity=Decimal("420"), open_exposures=[],
                open_symbols=set(), max_positions=4, margin_usdt=Decimal(100), leverage=10, limits=LIMITS,
                calib=CAL)
    args.update(kw)
    return paf.assess(**args)


# ---------------------------------------------------------------- verdicts

def test_structural_blocks_are_exact_and_infeasible():
    full = [Exposure(f"S{i}", "crypto_alt", Decimal(1), Decimal(1)) for i in range(4)]
    assert _assess(open_exposures=full)["reason"] == "MAX_POSITIONS"
    assert _assess(open_symbols={"X-USDT"})["pre_ai_feasible"] == paf.INFEASIBLE


def test_missing_inputs_are_unknown_never_infeasible():
    assert _assess(equity=None)["pre_ai_feasible"] == paf.UNKNOWN
    assert _assess(atr30_pct=None)["pre_ai_feasible"] == paf.UNKNOWN


def test_on_an_empty_portfolio_the_bound_cannot_prove_a_single_trade_infeasible():
    """The tightest stop the Risk Agent has ever set (0.47 %) fits 21 USDT,
    and the widest does not - the honest answer is UNKNOWN."""
    a = _assess()
    assert a["pre_ai_feasible"] == paf.UNKNOWN
    assert a["reason"] == "DEPENDS_ON_RISK_AGENT_STOP"
    assert a["estimated_stop_pct_interval"][0] == 0.47
    assert a["method"] == "bound"


def test_infeasible_only_when_even_the_tightest_stop_breaks_the_remaining_budget():
    used = [Exposure("A-USDT", "crypto_alt", Decimal("15"), Decimal("1000"))]   # 21 - 15 = 6 left
    a = _assess(open_exposures=used)
    assert a["pre_ai_feasible"] == paf.INFEASIBLE
    assert a["reason"] == "GROUP_BUDGET_EVEN_AT_TIGHTEST_STOP"
    assert a["estimated_worst_case_risk_interval"][0] > a["available_group_budget"]


def test_the_point_estimate_is_logged_but_never_decides():
    a = _assess(atr30_pct=2.0)
    assert a["shadow_estimate"]["method"] == "estimate"
    assert a["pre_ai_feasible"] == paf.UNKNOWN          # the estimate says "does not fit" ...
    assert a["shadow_estimate"]["fits"] is False        # ... but only the bound may say INFEASIBLE


def test_the_real_people_trade_is_never_called_infeasible():
    """PEOPLE 2026-09-29: the only trade the kernel approved all night
    (stop 0.79 %). The point estimate would have thrown it away."""
    a = _assess(symbol="PEOPLE-USDT", price=0.009324, atr30_pct=1.393, equity=Decimal("420.4388"))
    assert a["pre_ai_feasible"] != paf.INFEASIBLE


def test_the_safety_kernel_stays_authoritative():
    """A bound-FEASIBLE verdict gives nothing: the kernel still decides on the
    Risk Agent's real stop."""
    a = _assess(atr30_pct=0.02)                     # absurdly quiet market -> FEASIBLE by bound
    assert a["pre_ai_feasible"] == paf.FEASIBLE
    d = size_entry(symbol="X-USDT", equity=Decimal("420"), last_price=Decimal("100"), stop_loss=Decimal("95"),
                   target=Decimal("110"), leverage=10, base_quantity=Decimal("10"), quantity_precision=3,
                   min_notional=Decimal("2"), open_exposures=[], limits=LIMITS)
    assert d.action == "REJECT"


def test_the_production_calibration_is_frozen_on_train_and_ordered():
    c = paf.Calibration.load()
    assert c.timeframe == "30m" and c.frozen_on.startswith("TRAIN")
    assert 0 < c.ratio_lower <= c.ratio_median <= c.ratio_upper
    assert c.stop_floor_pct > 0


def test_atr_uses_only_closed_30m_bars():
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    k = lambda t, rng: SimpleNamespace(observed_at=t, high=Decimal(100 + rng), low=Decimal(100), close=Decimal(100))  # noqa: E731
    closed = [k(now - timedelta(minutes=30 * (i + 1)), 1) for i in range(14)]
    forming = [k(now - timedelta(minutes=10), 50)]            # still open: must be ignored
    assert closed_30m_atr(closed + forming, now, 14) == closed_30m_atr(closed, now, 14) == 1.0


# ---------------------------------------------------------------- shadow-only wiring

class _Live:
    def __init__(self, equity="420", fail=False):
        self.equity, self.fail, self.calls = equity, fail, []

    def get_balance(self):
        self.calls.append("get_balance")
        if self.fail:
            raise RuntimeError("exchange down")
        return {"equity": self.equity}


def test_an_all_infeasible_shadow_changes_nothing_in_the_cycle(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    seen = []

    def hook(candidates, snapshot, run_id):
        seen.extend(candidates)
        for c in candidates:
            repo.save_pre_ai_feasibility({"candidate_id": c.candidate_id, "pre_ai_feasible": paf.INFEASIBLE},
                                         snapshot.simulated_now)
        return "BLOCK EVERYTHING"   # a return value is ignored

    positions = run_single_cycle(_build_snapshots()[1], repo, MockAgentRunner(fixtures=_happy_fixtures()),
                                 _settings(), run_id="run-1", pre_ai_shadow=hook)
    assert len(seen) == 1
    assert len(positions) == 1                                   # AI, Gate and position unaffected
    assert repo.find_candidates_by_status("CONFIRMED")


def test_a_crashing_shadow_never_breaks_the_cycle(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    def hook(candidates, snapshot, run_id):
        raise RuntimeError("boom")

    positions = run_single_cycle(_build_snapshots()[1], repo, MockAgentRunner(fixtures=_happy_fixtures()),
                                 _settings(), run_id="run-1", pre_ai_shadow=hook)
    assert len(positions) == 1


def test_the_hook_records_a_verdict_per_candidate_and_unknown_equity_is_unknown(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    settings = get_settings()
    live = _Live(fail=True)
    run_single_cycle(_build_snapshots()[1], repo, MockAgentRunner(fixtures=_happy_fixtures()), _settings(),
                     run_id="run-1", pre_ai_shadow=build_hook(repo, live, settings))
    rows = repo.list_pre_ai_feasibility()
    assert len(rows) == 1
    assert rows[0]["pre_ai_feasible"] == paf.UNKNOWN
    assert rows[0]["reason"] == "EQUITY_OR_PRICE_UNKNOWN"
    assert live.calls == ["get_balance"]     # the only exchange call: read-only


# ---------------------------------------------------------------- isolation

def _imports(path):
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def _calls(path):
    return {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
            for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.Call)}


def test_the_assessment_module_is_pure():
    mods = _imports(ROOT / "crypto_trading/shadow/pre_ai_feasibility.py")
    crypto = {m for m in mods if m.startswith("crypto_trading")}
    assert crypto == {"crypto_trading.config.loader", "crypto_trading.safety_kernel"}


def test_the_shadow_cannot_send_orders_or_change_the_kernel_or_sizing():
    forbidden = {"place_entry_order_with_sl_tp", "place_stop_loss_order", "set_leverage", "cancel_order",
                 "close_position_market", "place_order", "_submit_entry_order", "process_pending_positions",
                 "record_safety_kernel_decision", "claim_live_execution_if_symbol_free", "setattr"}
    for f in ("pre_ai_feasibility.py", "pre_ai_feasibility_hook.py"):
        calls = _calls(ROOT / "crypto_trading/shadow" / f)
        assert not calls & forbidden, f"{f} calls {calls & forbidden}"
    kernel_src = (ROOT / "crypto_trading/safety_kernel.py").read_text(encoding="utf-8")
    assert "pre_ai" not in kernel_src


def test_no_decision_path_reads_the_shadow_verdict():
    """Only the shadow modules, their wiring (discovery_loop hands the hook to
    run_single_cycle, which ignores its result), storage and reports may
    mention it - never execution, the Gate, the kernel, Guardian or sizing."""
    allowed = {"crypto_trading/shadow/pre_ai_feasibility.py", "crypto_trading/shadow/pre_ai_feasibility_hook.py",
               "crypto_trading/discovery_loop.py", "crypto_trading/paper_trading/replay.py",
               "crypto_trading/storage/repository.py", "crypto_trading/storage/db.py",
               "crypto_trading/entry_research/pre_ai_calibration.py",
               "crypto_trading/entry_research/opportunity_v2.py",  # offline research only
               "crypto_trading/performance/pre_ai_feasibility_report.py"}
    offenders = []
    for p in (ROOT / "crypto_trading").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        text = p.read_text(encoding="utf-8")
        if ("pre_ai_feasib" in text or "pre_ai_shadow" in text) and rel not in allowed:
            offenders.append(rel)
    assert offenders == []


def test_run_single_cycle_discards_the_shadow_result():
    src = (ROOT / "crypto_trading/paper_trading/replay.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "pre_ai_shadow"]
    assert len(calls) == 1
    parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
    assert isinstance(parents[id(calls[0])], ast.Expr)      # a bare statement: the result is never used


def test_the_forward_report_joins_verdicts_with_what_really_happened(tmp_path):
    from crypto_trading.performance import pre_ai_feasibility_report as report

    repo = SQLiteRepository(tmp_path / "t.db")
    positions = run_single_cycle(
        _build_snapshots()[1], repo, MockAgentRunner(fixtures=_happy_fixtures()), _settings(), run_id="run-1",
        pre_ai_shadow=lambda cs, snap, rid: [repo.save_pre_ai_feasibility(
            {"candidate_id": c.candidate_id, "pre_ai_feasible": paf.INFEASIBLE, "reason": "TEST"},
            snap.simulated_now) for c in cs])
    repo.record_safety_kernel_decision(positions[0].position_id, positions[0].opened_at, "APPROVE",
                                       {"action": "APPROVE", "reasons": []})
    s = report.summarize(repo._conn, datetime(2000, 1, 1, tzinfo=UTC))
    assert s["n"] == 1 and s["confirmed"] == 1
    assert s["kernel_decisions"] == {"false->APPROVE": 1}
    assert s["false_rejections_kernel_approved"] == 1       # a wrong INFEASIBLE is surfaced, not hidden
    assert s["infeasible_precision_vs_kernel"] == 0.0


# ---------------------------------------------------------------- NO_4H_MOMENTUM + OI capture

def test_no_momentum_flag_is_the_exact_edge_lab_definition():
    from crypto_trading.entry_research import edge_lab as el

    closes = [100 + i * 0.01 for i in range(30)]
    bars = [(c, c * 1.001, c * 0.999, c, 1.0) for c in closes]
    f = el.features_from_bars(bars)
    fh = paf.no_momentum_4h(closes)
    assert fh["ret_4h"] == round(f["ret_4h"], 4) and fh["accel"] == round(f["accel"], 4)
    assert fh["flag"] is True                                  # flat drift: no 4 h momentum
    rising = closes[:-9] + [closes[-10] * (1 + 0.004 * k) for k in range(1, 10)]
    assert paf.no_momentum_4h(rising)["flag"] is False
    assert paf.no_momentum_4h(closes[:5])["flag"] is None      # unknown, never a verdict


def test_the_failure_hypothesis_is_logged_and_filters_nothing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    positions = run_single_cycle(_build_snapshots()[1], repo, MockAgentRunner(fixtures=_happy_fixtures()),
                                 _settings(), run_id="run-1",
                                 pre_ai_shadow=build_hook(repo, _Live(), get_settings()))
    rec = repo.list_pre_ai_feasibility()[0]
    assert rec["failure_hypotheses"][0]["name"] == "NO_4H_MOMENTUM"
    assert rec["failure_hypotheses"][0]["status"] == "FAILURE_HYPOTHESIS_SHADOW"
    assert len(positions) == 1                                 # candidate still analysed and traded


def test_open_interest_already_fetched_is_stored_without_extra_calls(tmp_path):
    from crypto_trading.paper_trading.replay import MarketSnapshot
    from crypto_trading.schemas.market import OpenInterest

    repo = SQLiteRepository(tmp_path / "t.db")
    snap = _build_snapshots()[1]
    oi = OpenInterest(instrument="BTCUSDT", open_interest=Decimal("12345.6"), observed_at=snap.simulated_now)
    snap = MarketSnapshot(**{**snap.__dict__, "open_interest": {"BTCUSDT": oi}})
    run_single_cycle(snap, repo, MockAgentRunner(fixtures=_happy_fixtures()), _settings(), run_id="run-oi")
    rows = repo._conn.execute("SELECT symbol, open_interest FROM market_observations").fetchall()
    assert [tuple(r) for r in rows] == [("BTCUSDT", "12345.6")]


def test_replays_without_open_interest_store_nothing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    run_single_cycle(_build_snapshots()[1], repo, MockAgentRunner(fixtures=_happy_fixtures()), _settings(),
                     run_id="run-1")
    assert repo._conn.execute("SELECT count(*) FROM market_observations").fetchone()[0] == 0
