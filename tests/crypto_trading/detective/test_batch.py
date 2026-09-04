from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.config.loader import DetectiveConfig
from crypto_trading.detective.batch import run_detective_batch
from crypto_trading.schemas.assessments import RiskAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.detective import DetectiveBatchAnalysis
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _settings

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _settings_with(**detective_overrides):
    settings = _settings()
    defaults = dict(
        batch_size=3, check_interval_seconds=300, min_history_for_win_loss_comparison=20
    )
    defaults.update(detective_overrides)
    return settings.model_copy(update={"detective": DetectiveConfig(**defaults)})


def _evidence() -> CandidateEvidenceRecord:
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    return CandidateEvidenceRecord(
        instrument="BTCUSDT",
        timeframes=["1h"],
        evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=0.8,
        trigger_reasons=["price_volatility"],
        data_quality_status="ok",
        outcome="worth_deeper_analysis",
    )


def _seed_closed_trade(repo, i: int, win: bool) -> None:
    candidate_id = f"cand-{i}"
    candidate = Candidate(
        candidate_id=candidate_id,
        idempotency_key=f"key-{i}",
        instrument="BTCUSDT",
        discovery_run_id="run-0",
        evidence_hash=f"hash-{i}",
        status="CONFIRMED",
        evidence_record=_evidence(),
        created_at=_NOW,
        updated_at=_NOW,
        risk=RiskAssessment(
            agent_name="crypto-risk-agent",
            run_id="run-0",
            created_at=_NOW,
            status="ok",
            suggested_stop_loss="49000",
            suggested_target="52000",
            downside="d",
            liquidity_risk="l",
            model_risk="m",
            timing_risk="t",
        ),
    )
    repo.create_candidate_with_event(
        candidate,
        Event(
            event_id=f"CANDIDATE_CREATED:{candidate_id}",
            event_type="CANDIDATE_CREATED",
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            occurred_at=_NOW,
            run_id="run-0",
            schema_version=1,
            payload={},
        ),
    )
    position = Position(
        position_id=candidate_id,
        candidate_id=candidate_id,
        instrument="BTCUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"),
        stop_loss=Decimal("49000"),
        target=Decimal("52000"),
        size=Decimal("1000"),
        fill_model_version="v1",
        opened_at=_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(
            event_id=f"POSITION_OPENED:{candidate_id}",
            event_type="POSITION_OPENED",
            aggregate_type="position",
            aggregate_id=candidate_id,
            occurred_at=_NOW,
            run_id="run-0",
            schema_version=1,
            payload={},
        ),
    )
    exit_price = Decimal("52000") if win else Decimal("49000")
    repo.close_position_with_event(
        position_id=candidate_id,
        theoretical_exit=exit_price,
        simulated_fill_exit=exit_price,
        exit_reason="target" if win else "stop_loss",
        fees=Decimal("0.4"),
        funding=Decimal("0"),
        closed_at=_NOW,
        event=Event(
            event_id=f"POSITION_CLOSED:{candidate_id}",
            event_type="POSITION_CLOSED",
            aggregate_type="position",
            aggregate_id=candidate_id,
            occurred_at=_NOW,
            run_id="run-0",
            schema_version=1,
            payload={},
        ),
    )


def _mock_runner(status="ok") -> MockAgentRunner:
    fixture = DetectiveBatchAnalysis(
        agent_name="crypto-detective",
        run_id="run-1",
        created_at=_NOW,
        status="ok",
        observations=["several losses shared late entries"],
        winning_patterns=["funding/OI setups outperformed"],
        losing_patterns=["late momentum entries"],
    )
    if status == "failed":
        return MockAgentRunner(
            fixtures={"crypto-detective": fixture}, fail_agents={"crypto-detective"}
        )
    return MockAgentRunner(fixtures={"crypto-detective": fixture})


def test_below_batch_threshold_makes_no_ai_call_and_returns_none(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(2):  # batch_size=3
        _seed_closed_trade(repo, i, win=True)
    runner = _mock_runner()

    result = run_detective_batch(repo, runner, _settings_with(), "run-1", _NOW)

    assert result is None
    assert repo.count_ai_calls_since(_NOW.replace(hour=0, minute=0, second=0, microsecond=0)) == 0
    assert repo.count_closed_positions_pending_detective_analysis() == 2


def test_winning_trade_is_analyzed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):
        _seed_closed_trade(repo, i, win=True)
    runner = _mock_runner()

    result = run_detective_batch(repo, runner, _settings_with(), "run-1", _NOW)

    assert result is not None
    assert result.win_count == 3
    assert result.loss_count == 0
    assert result.status == "ok"


def test_losing_trade_is_analyzed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):
        _seed_closed_trade(repo, i, win=False)
    runner = _mock_runner()

    result = run_detective_batch(repo, runner, _settings_with(), "run-1", _NOW)

    assert result is not None
    assert result.win_count == 0
    assert result.loss_count == 3


def test_batch_with_both_wins_and_losses_analyzes_both(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_closed_trade(repo, 0, win=True)
    _seed_closed_trade(repo, 1, win=False)
    _seed_closed_trade(repo, 2, win=True)
    runner = _mock_runner()

    result = run_detective_batch(repo, runner, _settings_with(), "run-1", _NOW)

    assert result.win_count == 2
    assert result.loss_count == 1
    assert result.winning_patterns != []
    assert result.losing_patterns != []


def test_detective_batch_never_touches_gate_decisions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):
        _seed_closed_trade(repo, i, win=True)
    before = repo.get_gate_decision("cand-0")

    run_detective_batch(repo, _mock_runner(), _settings_with(), "run-1", _NOW)

    after = repo.get_gate_decision("cand-0")
    assert before == after == None  # noqa: E711 - explicit None check for clarity


def test_detective_batch_never_opens_closes_or_reopens_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):
        _seed_closed_trade(repo, i, win=True)
    before_open = repo.count_open_positions()
    before_statuses = {p.position_id: p.status for p in repo.find_all_positions(limit=100)}

    run_detective_batch(repo, _mock_runner(), _settings_with(), "run-1", _NOW)

    after_open = repo.count_open_positions()
    after_statuses = {p.position_id: p.status for p in repo.find_all_positions(limit=100)}
    assert before_open == after_open == 0
    assert before_statuses == after_statuses


def test_detective_batch_never_mutates_candidate_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):
        _seed_closed_trade(repo, i, win=True)
    before = repo.get_candidate("cand-0").status

    run_detective_batch(repo, _mock_runner(), _settings_with(), "run-1", _NOW)

    after = repo.get_candidate("cand-0").status
    assert before == after == "CONFIRMED"


def test_result_is_persisted(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):
        _seed_closed_trade(repo, i, win=True)

    result = run_detective_batch(repo, _mock_runner(), _settings_with(), "run-1", _NOW)

    stored = repo.find_detective_analyses(limit=10)
    assert len(stored) == 1
    assert stored[0].analysis_id == result.analysis_id
    assert set(stored[0].position_ids) == {"cand-0", "cand-1", "cand-2"}


def test_batch_size_of_exactly_threshold_triggers_exactly_one_batch_of_that_size(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(7):  # more than batch_size=3
        _seed_closed_trade(repo, i, win=True)

    result = run_detective_batch(repo, _mock_runner(), _settings_with(), "run-1", _NOW)

    assert len(result.position_ids) == 3
    assert repo.count_closed_positions_pending_detective_analysis() == 4


def test_daily_ai_cost_cap_is_respected_batch_is_deferred(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):
        _seed_closed_trade(repo, i, win=True)
    settings = _settings_with()
    # Töm dagens $-budget helt innan Detective ens får chansen.
    repo.record_ai_call_event(
        Event(
            event_id="AI_CALL_MADE:exhaust:0",
            event_type="AI_CALL_MADE",
            aggregate_type="candidate",
            aggregate_id="exhaust",
            occurred_at=_NOW,
            run_id="run-0",
            schema_version=1,
            payload={"role": "risk", "status": "ok", "cost_usd": "10.00"},
        )
    )

    result = run_detective_batch(repo, _mock_runner(), settings, "run-1", _NOW)

    assert result is None
    # Ingen batch kördes alls - positionerna förblir ej-analyserade, ingen
    # förlorad historik.
    assert repo.count_closed_positions_pending_detective_analysis() == 3
    assert repo.find_detective_analyses(limit=10) == []


def test_failed_ai_call_is_handled_safely_and_still_marks_positions_analyzed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):
        _seed_closed_trade(repo, i, win=True)

    result = run_detective_batch(
        repo, _mock_runner(status="failed"), _settings_with(), "run-1", _NOW
    )

    assert result is not None
    assert result.status == "failed"
    assert result.observations == []
    # Misslyckandet fastnar aldrig i en oändlig omanalys-loop vid omstart -
    # positionerna markeras analyserade även för ett misslyckat AI-anrop.
    assert repo.count_closed_positions_pending_detective_analysis() == 0


class _UnbilledFailingRunner:
    """Simulerar ett anrop som ALDRIG nådde modellen (t.ex. kreditstopp) -
    last_call_billed=False, exakt samma kontrakt som RealClaudeRunner
    sätter (agents/runner.py::AgentRunner.last_call_billed) när inget
    försök i run() någonsin fakturerades."""

    last_call_billed = False
    last_call_cost_usd = Decimal("0")

    def run(self, agent_def, context, output_schema):
        return output_schema.model_construct(
            agent_name=agent_def.name,
            run_id=context.get("run_id", "unknown"),
            created_at=_NOW,
            status="failed",
            observations=[],
            winning_patterns=[],
            losing_patterns=[],
        )


def test_unbilled_failed_ai_call_is_never_recorded_as_a_billable_ai_call(tmp_path):
    """Ett anrop som ALDRIG nådde modellen (last_call_billed=False, samma
    kontrakt som agents/runner.py::AgentRunner) får aldrig konsumera dagens
    AI-anropsbudget - identiskt med orchestrator.py::process_candidate()s
    redan etablerade last_call_billed-mönster, återanvänt oförändrat här."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):
        _seed_closed_trade(repo, i, win=True)

    run_detective_batch(repo, _UnbilledFailingRunner(), _settings_with(), "run-1", _NOW)

    day_start = _NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    assert repo.count_ai_calls_since(day_start) == 0


def test_restart_does_not_reanalyze_already_analyzed_positions(tmp_path):
    db_path = tmp_path / "t.db"
    repo1 = SQLiteRepository(db_path)
    for i in range(3):
        _seed_closed_trade(repo1, i, win=True)
    first_result = run_detective_batch(repo1, _mock_runner(), _settings_with(), "run-1", _NOW)
    assert first_result is not None

    # Simulerar en omstart: ny Repository-instans mot samma DB-fil, tre nya
    # stängda trades utöver de tre redan analyserade.
    repo2 = SQLiteRepository(db_path)
    for i in range(3, 6):
        _seed_closed_trade(repo2, i, win=False)

    second_result = run_detective_batch(repo2, _mock_runner(), _settings_with(), "run-2", _NOW)

    assert second_result is not None
    assert set(second_result.position_ids) == {"cand-3", "cand-4", "cand-5"}
    assert repo2.count_closed_positions_pending_detective_analysis() == 0


def test_small_history_produces_observations_but_never_touches_config(tmp_path):
    """Liten historik (under min_history_for_win_loss_comparison) ger
    fortfarande observationer - bara utan den historiska WIN-vs-LOSS-
    jämförelsen - och rör aldrig config/strategi (explicit användarkrav:
    "En eller några misslyckade trades får aldrig vara tillräckligt för
    att ändra en signaltyp")."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):
        _seed_closed_trade(repo, i, win=(i != 1))
    settings = _settings_with(min_history_for_win_loss_comparison=20)
    max_concurrent_positions_before = settings.risk_limits.max_concurrent_positions

    result = run_detective_batch(repo, _mock_runner(), settings, "run-1", _NOW)

    assert result is not None
    assert result.observations != []
    # Ingen config-mutation möjlig via denna funktions gränssnitt överhuvud-
    # taget - settings skickas aldrig vidare till något skrivande anrop.
    assert settings.risk_limits.max_concurrent_positions == max_concurrent_positions_before
    assert settings.detective.min_history_for_win_loss_comparison == 20
