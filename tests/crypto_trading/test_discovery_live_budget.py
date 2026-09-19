"""End-to-end tests for the discovery-side LIVE capacity + capital budget
(2026-09-19, AI-cost optimization): 4/4 or no capital => zero AI and zero
market fetch; otherwise the number of candidates sent to full 7-role analysis
is capped by the number of LIVE slots that can really be used. PAPER keeps
consuming the very same Gate-approved signal flow (no separate AI), stale
candidates never consume AI, and the final pre-order gate still has the last
word."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.discovery_loop import run_discovery_tick
from crypto_trading.guardian_loop import run_guardian_tick
from crypto_trading.monitoring_loop import run_monitoring_tick
from crypto_trading.paper_trading.live_discovery_gate import LiveDiscoveryGate
from crypto_trading.paper_trading.live_execution import process_pending_positions
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.screening.candidate_engine import _persist_new_candidate
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.paper_trading.test_live_execution import (
    _SpyConnector,
    _SpyMarketDataConnector,
)
from tests.crypto_trading.test_discovery_loop import (
    _seed_active_live_position,
    _stub_connector_that_triggers_a_candidate,
    _stub_connector_with_one_healthy_symbol,
)
from tests.crypto_trading.test_guardian_loop import _FakeRunner
from tests.crypto_trading.test_guardian_loop import _StubConnector as _GuardianStubConnector
from tests.crypto_trading.test_market_snapshot import (
    _ms,
    _raw_contract,
    _raw_funding,
    _raw_kline,
    _raw_open_interest,
    _raw_ticker,
    _settings,
    _StubConnector,
)
from tests.crypto_trading.test_monitoring_loop import _MonitoringStubConnector, _seed_open_position
from tests.crypto_trading.test_orchestrator import _happy_fixtures

_AI_CALLS_PER_FULL_ANALYSIS = 7


class _Live:
    """Confirms every seeded ACTIVE row is still open on the exchange and
    reports a controllable availableMargin; counts balance calls."""

    def __init__(self, available="500.00"):
        self.available = available
        self.balance_calls = 0

    def get_position(self, symbol):
        return {"symbol": symbol, "positionAmt": "0.002"}

    def get_balance(self):
        self.balance_calls += 1
        return {"availableMargin": self.available}


def _n_triggering_symbols_connector(n: int) -> _StubConnector:
    """n symbols that each independently trigger the screener (same spike
    pattern as _stub_connector_that_triggers_a_candidate), timestamps anchored
    to real wall-clock for the same staleness reason documented there."""
    now = datetime.now(UTC)
    symbols = [f"SYM{i}USDT" for i in range(n)]
    contracts = [_raw_contract(s) for s in symbols]
    tickers = {s: _raw_ticker(s, "55000", "10000000", _ms(now)) for s in symbols}
    klines = {
        s: [
            _raw_kline("50000", _ms(now - timedelta(hours=4))),
            _raw_kline("50000", _ms(now - timedelta(hours=3))),
            _raw_kline("50000", _ms(now - timedelta(hours=2))),
            _raw_kline("50000", _ms(now - timedelta(hours=1))),
            _raw_kline("55000", _ms(now), high="55100", low="54800"),
        ]
        for s in symbols
    }
    funding = {s: [_raw_funding(s, "0.0001", _ms(now))] for s in symbols}
    oi = {s: _raw_open_interest(s, "1000", _ms(now)) for s in symbols}
    return _StubConnector(contracts, tickers, klines, funding, oi)


def _ai_calls(repo) -> int:
    return repo.count_ai_calls_since(datetime(2000, 1, 1, tzinfo=UTC))


def _gate_events(repo) -> list[dict]:
    rows = repo._conn.execute(
        "SELECT payload FROM events WHERE event_type = 'DISCOVERY_LIVE_GATE' ORDER BY seq"
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]


def _tick(repo, connector, live, settings=None, gate=None, runner=None):
    return run_discovery_tick(
        connector,
        repo,
        runner or MockAgentRunner(_happy_fixtures()),
        settings or _settings(top_n=10),
        live_connector=live,
        live_market_data_connector=connector,
        live_discovery_gate=gate,
    )


def _fixtures_where_the_paper_position_stays_open() -> dict:
    """_happy_fixtures() suggests SL=1/TP=2, which the same cycle's monitoring
    immediately triggers (price 55000 >> 2). Realistic levels around the
    55000 entry keep the PAPER position OPEN so it can still be claimed by
    LIVE afterwards."""
    fixtures = _happy_fixtures()
    fixtures["crypto-risk-agent"] = fixtures["crypto-risk-agent"].model_copy(
        update={"suggested_stop_loss": "50000", "suggested_target": "60000"}
    )
    return fixtures


def _repo_with_open(tmp_path, n_open: int) -> SQLiteRepository:
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(n_open):
        _seed_active_live_position(repo, f"live-pos-{i}")
    return repo


# --- 4/4: zero expensive work, whole discovery flow --------------------------


def test_4_of_4_with_plenty_of_capital_does_zero_ai_and_zero_market_fetch(tmp_path):
    repo = _repo_with_open(tmp_path, 4)
    connector = _n_triggering_symbols_connector(5)

    positions = _tick(repo, connector, _Live(available="500"))

    assert positions == []
    assert _ai_calls(repo) == 0
    assert connector.klines_calls == []  # no snapshot fetch at all
    assert repo.find_candidates_by_status("CANDIDATE") == []
    assert _gate_events(repo)[-1]["outcome"] == "suppressed_capacity"


def test_4_of_4_with_no_capital_does_zero_ai(tmp_path):
    repo = _repo_with_open(tmp_path, 4)
    connector = _n_triggering_symbols_connector(5)

    _tick(repo, connector, _Live(available="0"))

    assert _ai_calls(repo) == 0
    assert connector.klines_calls == []


# --- free slots => analysis budget -------------------------------------------


def test_2_of_4_with_capital_for_2_sends_at_most_2_candidates_to_full_analysis(tmp_path):
    repo = _repo_with_open(tmp_path, 2)
    connector = _n_triggering_symbols_connector(5)

    _tick(repo, connector, _Live(available="21.00"))

    assert _ai_calls(repo) == 2 * _AI_CALLS_PER_FULL_ANALYSIS
    assert (
        len(repo.find_candidates_by_status("BUDGET_LIMITED")) == 3
    )  # the other 3 were never analysed
    event = _gate_events(repo)[-1]
    assert (event["outcome"], event["max_candidates"]) == ("capped", 2)


def test_2_of_4_with_capital_for_1_sends_at_most_1_candidate_to_full_analysis(tmp_path):
    repo = _repo_with_open(tmp_path, 2)
    connector = _n_triggering_symbols_connector(5)

    _tick(repo, connector, _Live(available="20.99"))

    assert _ai_calls(repo) == 1 * _AI_CALLS_PER_FULL_ANALYSIS


def test_2_of_4_with_capital_for_0_does_zero_ai_and_zero_market_fetch(tmp_path):
    repo = _repo_with_open(tmp_path, 2)
    connector = _n_triggering_symbols_connector(5)

    _tick(repo, connector, _Live(available="10.99"))

    assert _ai_calls(repo) == 0
    assert connector.klines_calls == []
    assert _gate_events(repo)[-1]["outcome"] == "suppressed_capital"


def test_1_of_4_with_capital_for_1_sends_exactly_the_usable_slot_count(tmp_path):
    repo = _repo_with_open(tmp_path, 1)
    connector = _n_triggering_symbols_connector(5)

    _tick(repo, connector, _Live(available="11.00"))

    assert _ai_calls(repo) == 1 * _AI_CALLS_PER_FULL_ANALYSIS


def test_0_of_4_with_capital_for_all_uses_the_normal_uncapped_budget(tmp_path):
    repo = _repo_with_open(tmp_path, 0)
    connector = _n_triggering_symbols_connector(5)

    _tick(repo, connector, _Live(available="500"))

    assert (
        _ai_calls(repo) == 5 * _AI_CALLS_PER_FULL_ANALYSIS
    )  # all 5, under max_candidates_per_discovery_run=10
    assert _gate_events(repo)[-1]["outcome"] == "normal"


def test_live_not_armed_is_completely_unchanged_no_cap_no_gate_event(tmp_path):
    repo = _repo_with_open(tmp_path, 4)  # would be "full" if live were armed
    connector = _n_triggering_symbols_connector(5)

    run_discovery_tick(connector, repo, MockAgentRunner(_happy_fixtures()), _settings(top_n=10))

    assert _ai_calls(repo) == 5 * _AI_CALLS_PER_FULL_ANALYSIS
    assert _gate_events(repo) == []


# --- recovery / debounce -----------------------------------------------------


def test_discovery_resumes_automatically_when_capital_returns(tmp_path):
    repo = _repo_with_open(tmp_path, 2)
    live = _Live(available="5")
    gate = LiveDiscoveryGate(cooldown_seconds=0)
    connector = _n_triggering_symbols_connector(3)

    _tick(repo, connector, live, gate=gate)
    assert _ai_calls(repo) == 0

    live.available = "100"
    _tick(repo, _n_triggering_symbols_connector(3), live, gate=gate)

    assert _ai_calls(repo) == 2 * _AI_CALLS_PER_FULL_ANALYSIS  # 2 usable slots


def test_repeated_suppressed_ticks_within_the_cooldown_do_not_refetch_the_balance(tmp_path):
    repo = _repo_with_open(tmp_path, 2)
    live = _Live(available="5")
    gate = LiveDiscoveryGate(cooldown_seconds=300)
    connector = _n_triggering_symbols_connector(3)

    _tick(repo, connector, live, gate=gate)
    _tick(repo, connector, live, gate=gate)

    assert live.balance_calls == 1
    assert [e["outcome"] for e in _gate_events(repo)] == [
        "suppressed_capital",
        "suppressed_capital",
    ]
    assert _gate_events(repo)[1]["from_cache"] is True


# --- stale signals -----------------------------------------------------------


def _stale_candidate(repo, age: timedelta, discovery_run_id: str = "old-run"):
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    created_at = datetime.now(UTC) - age
    evidence = CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=created_at,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=0.8,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )
    return _persist_new_candidate(repo, evidence, discovery_run_id, created_at, Decimal("50000"))


def test_a_stale_waiting_candidate_never_consumes_ai_when_a_live_slot_later_frees_up(tmp_path):
    repo = _repo_with_open(tmp_path, 2)
    stale = _stale_candidate(repo, timedelta(hours=2))  # far older than signal_ttl_seconds (1800)

    _tick(repo, _stub_connector_with_one_healthy_symbol(), _Live(available="500"))

    assert _ai_calls(repo) == 0
    assert repo.get_candidate(stale.candidate_id).status == "BUDGET_LIMITED"


def test_a_fresh_waiting_candidate_is_still_analysed(tmp_path):
    repo = _repo_with_open(tmp_path, 2)
    fresh = _stale_candidate(repo, timedelta(minutes=5))

    _tick(repo, _stub_connector_with_one_healthy_symbol(), _Live(available="500"))

    assert _ai_calls(repo) == _AI_CALLS_PER_FULL_ANALYSIS
    assert repo.get_candidate(fresh.candidate_id).status != "CANDIDATE"


def test_stale_rule_is_off_when_live_is_not_armed_paper_only_behaviour_unchanged(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    old = _stale_candidate(repo, timedelta(hours=2))

    run_discovery_tick(
        _stub_connector_with_one_healthy_symbol(),
        repo,
        MockAgentRunner(_happy_fixtures()),
        _settings(top_n=10),
    )

    assert _ai_calls(repo) == _AI_CALLS_PER_FULL_ANALYSIS
    assert repo.get_candidate(old.candidate_id).status != "CANDIDATE"


# --- PAPER shares the very same analysed, Gate-approved signal ----------------


def test_paper_position_comes_from_the_same_single_analysis_no_extra_paper_ai(tmp_path):
    repo = _repo_with_open(tmp_path, 2)
    connector = _stub_connector_that_triggers_a_candidate()

    positions = _tick(
        repo,
        connector,
        _Live(available="500"),
        runner=MockAgentRunner(_fixtures_where_the_paper_position_stays_open()),
    )

    assert len(positions) == 1
    candidate_ids = {
        r["aggregate_id"]
        for r in repo._conn.execute(
            "SELECT aggregate_id FROM events WHERE event_type = 'AI_CALL_MADE'"
        )
    }
    assert candidate_ids == {
        positions[0].candidate_id
    }  # one analysis chain, the one that became PAPER
    assert (
        _ai_calls(repo) == _AI_CALLS_PER_FULL_ANALYSIS
    )  # exactly the 7 standard roles, nothing extra
    assert repo.get_position(positions[0].position_id).status == "OPEN_POSITION"


# --- final execution gate still has the last word ----------------------------


def test_balance_dropping_between_discovery_and_execution_stops_the_order(tmp_path):
    repo = _repo_with_open(tmp_path, 2)
    positions = _tick(
        repo,
        _stub_connector_that_triggers_a_candidate(),
        _Live(available="500"),
        runner=MockAgentRunner(_fixtures_where_the_paper_position_stays_open()),
    )
    assert len(positions) == 1  # discovery saw ample capital and produced a signal

    exchange = _SpyConnector(balance="5")  # capital vanished before execution
    now = datetime.now(UTC)
    process_pending_positions(
        repo,
        exchange,
        _SpyMarketDataConnector(),
        {"BTCUSDT": 3},
        {},
        _settings(),
        "exec-run",
        now,
    )
    assert exchange.calls == []  # final pre-order gate refused: no order attempt

    exchange_with_capital = _SpyConnector(balance="500")
    process_pending_positions(
        repo,
        exchange_with_capital,
        _SpyMarketDataConnector(),
        {"BTCUSDT": 3},
        {},
        _settings(),
        "exec-run-2",
        now,
    )
    assert (
        len(exchange_with_capital.calls) == 1
    )  # control: same signal does go through when capital exists


# --- Guardian / monitoring keep working while discovery is suppressed --------


def test_monitoring_and_guardian_keep_working_while_discovery_is_suppressed(tmp_path):
    repo = _repo_with_open(tmp_path, 4)
    _seed_open_position(
        repo, instrument="BTCUSDT", stop_loss=Decimal("49000"), position_id="paper-1"
    )

    _tick(repo, _n_triggering_symbols_connector(3), _Live(available="0"))
    assert _ai_calls(repo) == 0  # discovery really was suppressed

    now = datetime.now(UTC)
    monitor = _MonitoringStubConnector(
        tickers={
            "BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now)),
            "BTC-USDT": _raw_ticker("BTC-USDT", "50000", "10000000", _ms(now)),  # seeded LIVE rows
        },
        klines={
            "BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")],
            "BTC-USDT": [_raw_kline("50000", _ms(now), high="50100", low="49900")],
        },
    )
    closed = run_monitoring_tick(monitor, repo, _settings(top_n=1))
    # (the back-dated seeded LIVE rows also time-limit out here - irrelevant; the point is that
    # the PAPER position is still stop-loss-closed while discovery is suppressed)
    assert "paper-1" in [p.position_id for p in closed]

    run_guardian_tick(repo, _GuardianStubConnector(), _FakeRunner(), _settings(), now)
    row = repo._conn.execute("SELECT status FROM runs WHERE run_type = 'guardian'").fetchone()
    assert row["status"] == "ok"  # guardian still ticks
