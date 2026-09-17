import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.discovery_loop import run_discovery_tick
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
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
from tests.crypto_trading.test_orchestrator import _happy_fixtures


class _RaisingConnector:
    """Simulerar en helt otillgänglig BingX - kraschar redan på första
    anropet, precis som ett verkligt anslutningsfel skulle göra."""

    def __init__(self, exc: Exception):
        self._exc = exc

    def get_contracts(self):
        raise self._exc


class _CrashingRunner:
    """Simulerar en oväntad, ohanterad krasch mitt i en candidates rollkedja
    (skiljer sig från MockAgentRunners fail_agents/timeout_agents, som bara
    ÄNDRAR utfallet av ett lyckat anrop - detta kraschar anropet självt, som
    en riktig processkrasch/bugg skulle göra)."""

    def __init__(self, fixtures: dict, crash_on: str):
        self._fixtures = fixtures
        self._crash_on = crash_on

    def run(self, agent_def, context, output_schema):
        if agent_def.name == self._crash_on:
            raise RuntimeError("simulerad krasch mitt i analysen")
        return self._fixtures[agent_def.name]


def _stub_connector_with_one_healthy_symbol() -> _StubConnector:
    """Flat data, inget triggar screenern - bara för att bevisa att en tick
    kan slutföras och skriva en 'ok'-runs-rad utan att en candidate behöver
    skapas.

    Tidsstämplarna ankras mot RIKTIG väggklocketid (datetime.now(UTC)),
    beräknad här och nu vid anropstillfället - inte mot en frusen konstant.
    run_discovery_tick() (till skillnad från Task 6:s build_live_snapshot(),
    som tar emot `now` som parameter) sätter alltid `now = datetime.now(UTC)`
    internt, med skarpa max_data_age_seconds-trösklar (ticker: 30s) - en
    frusen historisk tidsstämpel skulle göra all fixturdata "stale" så fort
    riktig tid hunnit gå om den."""
    now = datetime.now(UTC)
    contracts = [_raw_contract("BTCUSDT")]
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(now))}
    klines = {
        "BTCUSDT": [
            _raw_kline("50000", _ms(now - timedelta(hours=2))),
            _raw_kline("50000", _ms(now - timedelta(hours=1))),
            _raw_kline("50000", _ms(now)),
        ]
    }
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(now))}
    return _StubConnector(contracts, tickers, klines, funding_rates, open_interest)


def _stub_connector_that_triggers_a_candidate() -> _StubConnector:
    """Samma spik-mönster som test_replay.py: fyra platta klines följt av en
    10%-spik (> screener_price_volatility_threshold_pct=2.0) -> triggar
    worth_deeper_analysis. Tidsstämplar ankrade mot riktig väggklocketid,
    se docstring i _stub_connector_with_one_healthy_symbol()."""
    now = datetime.now(UTC)
    contracts = [_raw_contract("BTCUSDT")]
    tickers = {"BTCUSDT": _raw_ticker("BTCUSDT", "55000", "10000000", _ms(now))}
    klines = {
        "BTCUSDT": [
            _raw_kline("50000", _ms(now - timedelta(hours=4))),
            _raw_kline("50000", _ms(now - timedelta(hours=3))),
            _raw_kline("50000", _ms(now - timedelta(hours=2))),
            _raw_kline("50000", _ms(now - timedelta(hours=1))),
            _raw_kline("55000", _ms(now), high="55100", low="54800"),
        ]
    }
    funding_rates = {"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]}
    open_interest = {"BTCUSDT": _raw_open_interest("BTCUSDT", "1000", _ms(now))}
    return _StubConnector(contracts, tickers, klines, funding_rates, open_interest)


def test_run_discovery_tick_persists_a_runs_row_on_success(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _stub_connector_with_one_healthy_symbol()

    run_discovery_tick(connector, repo, MockAgentRunner(_happy_fixtures()), _settings(top_n=1))

    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'discovery'").fetchone()
    assert row["status"] == "ok"
    assert row["completed_at"] is not None


def test_run_discovery_tick_forwards_screener_runner_to_run_single_cycle(tmp_path):
    """Ren ledningskontroll (kostnadsoptimering 2026-09-02): run_discovery_
    tick() ska vidarebefordra screener_runner oförändrat till run_single_
    cycle() - annars skulle produktionens Haiku-förscreening tyst aldrig
    köras trots att den är korrekt konfigurerad i run.py."""
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _stub_connector_with_one_healthy_symbol()
    runner = MockAgentRunner(_happy_fixtures())
    screener_runner = MockAgentRunner({})

    with patch(
        "crypto_trading.discovery_loop.run_single_cycle", return_value=[]
    ) as mock_run_single_cycle:
        run_discovery_tick(
            connector, repo, runner, _settings(top_n=1), screener_runner=screener_runner
        )

    assert mock_run_single_cycle.call_args.kwargs["screener_runner"] is screener_runner


def test_run_discovery_tick_persists_instruments_scanned_count_on_success(tmp_path):
    """Fas 6 daily report (2026-08-29): antalet instrument i BingX-
    universumet (len(snapshot.instruments), inte bara top_n) persisteras
    på run-recordet - härlett direkt från redan hämtad data, ingen
    separat räkning."""
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _stub_connector_with_one_healthy_symbol()  # exakt 1 kontrakt

    run_discovery_tick(connector, repo, MockAgentRunner(_happy_fixtures()), _settings(top_n=1))

    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'discovery'").fetchone()
    assert row["instruments_scanned"] == 1


def test_run_discovery_tick_marks_run_as_error_and_does_not_raise_on_connector_failure(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _RaisingConnector(ConnectorUnavailableError("BingX nere"))

    result = run_discovery_tick(
        connector, repo, MockAgentRunner(_happy_fixtures()), _settings(top_n=1)
    )

    assert result == []  # fail-closed, inget kraschar
    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'discovery'").fetchone()
    assert row["status"] == "error"
    assert "ConnectorUnavailableError" in row["errors"]


def test_run_discovery_tick_returns_confirmed_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _stub_connector_that_triggers_a_candidate()

    positions = run_discovery_tick(
        connector, repo, MockAgentRunner(_happy_fixtures()), _settings(top_n=1)
    )

    assert len(positions) == 1


def test_run_discovery_tick_recovers_a_mid_analysis_crash_on_the_next_tick(tmp_path):
    """Verifierar den specificerade recovery-policyn (SPEC §8.5, Fas 5 Beslut
    2) end-to-end genom två på varandra följande run_discovery_tick-anrop:
    tick 1 kraschar oväntat mitt i en candidates rollkedja (en riktig bugg/
    krasch, inte ett modellerat 'failed'-utfall) - discovery_tick fångar
    detta, skriver runs.status='error', och candraten blir kvar i
    UNDER_AI_ANALYSIS. Tick 2 (ny anropare, ingen krasch denna gång) ska via
    sweep_interrupted_analyses + Fas 5:s återupptagningspolicy (Task 4)
    hitta den föräldralösa candidaten, sätta den till ANALYSIS_INTERRUPTED,
    och sedan köra klart hela rollkedjan till ett terminalt state - aldrig
    lämna den i UNDER_AI_ANALYSIS/ANALYSIS_INTERRUPTED permanent."""
    repo = SQLiteRepository(tmp_path / "t.db")

    tick1 = run_discovery_tick(
        _stub_connector_that_triggers_a_candidate(),
        repo,
        _CrashingRunner(_happy_fixtures(), crash_on="crypto-risk-agent"),
        _settings(top_n=1),
    )
    assert tick1 == []
    stuck_status = repo._conn.execute("SELECT status FROM candidates").fetchone()["status"]
    assert stuck_status == "UNDER_AI_ANALYSIS"

    run_discovery_tick(
        _stub_connector_that_triggers_a_candidate(),
        repo,
        MockAgentRunner(_happy_fixtures()),
        _settings(top_n=1),
    )

    final_statuses = {
        row["status"] for row in repo._conn.execute("SELECT status FROM candidates").fetchall()
    }
    assert "UNDER_AI_ANALYSIS" not in final_statuses
    assert "ANALYSIS_INTERRUPTED" not in final_statuses
    assert final_statuses & {"CONFIRMED", "NO_TRADE", "REJECTED"}


_LIVE_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _seed_active_live_position(repo, position_id: str) -> None:
    """Seeds a real OPEN_POSITION + a matching ACTIVE live_executions row -
    the reconciled-capacity check (has_sufficient_live_capacity) counts
    THESE rows, never a mocked connector method alone, so a test proving
    the capacity gate must actually populate them."""
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTC-USDT",
        direction="LONG", status="OPEN_POSITION", theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50000"), stop_loss=Decimal("49000"),
        target=Decimal("52000"), size=Decimal("1000"), fill_model_version="v1",
        opened_at=_LIVE_NOW,
    )
    repo.create_position_with_event(
        position,
        Event(event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
              aggregate_type="position", aggregate_id=position_id, occurred_at=_LIVE_NOW,
              run_id="seed", schema_version=1, payload={}),
    )
    repo.claim_live_execution(position_id, _LIVE_NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        position_id, f"cid-{position_id}", f"ex-{position_id}", "0.002", "50000",
        None, None, _LIVE_NOW,
    )


class _LiveConnectorStub:
    """Confirms every seeded ACTIVE row is still genuinely open on the
    exchange (reconciliation finds nothing stale to close), and reports
    ample balance - so the ONLY thing that can make capacity read "full"
    is the number of seeded rows the test itself set up, not the mock."""

    def get_position(self, symbol):
        return {"symbol": symbol, "positionAmt": "0.002"}

    def get_balance(self):
        return {"availableMargin": "100.00"}


def test_run_discovery_tick_skips_entirely_when_live_capacity_full(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(4):  # settings' default live_execution.max_concurrent_positions == 4
        _seed_active_live_position(repo, f"live-pos-{i}")
    settings = _settings()
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    connector = _stub_connector_with_one_healthy_symbol()

    positions = run_discovery_tick(
        connector, repo, runner, settings,
        live_connector=_LiveConnectorStub(), live_market_data_connector=connector,
    )

    assert positions == []
    assert repo.find_candidates_by_status("CANDIDATE") == []  # never even discovered
    run_rows = repo._conn.execute("SELECT status FROM runs ORDER BY started_at DESC LIMIT 1").fetchall()
    assert run_rows[0]["status"] == "ok"  # a clean, logged no-op, not an error


def test_run_discovery_tick_proceeds_normally_when_live_disabled(tmp_path):
    """live_connector=None (the default) - today's exact behavior,
    unaffected by anything in this plan. Seeds the same 4 active LIVE rows
    as the "full" test above to prove it's live_connector=None, not an
    empty DB, that short-circuits the gate."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(4):
        _seed_active_live_position(repo, f"live-pos-{i}")
    settings = _settings()
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    connector = _stub_connector_with_one_healthy_symbol()

    positions = run_discovery_tick(connector, repo, runner, settings)

    assert isinstance(positions, list)  # completes normally, gate never even runs


def test_run_discovery_tick_proceeds_when_live_capacity_available(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(3):  # one slot free under the default cap of 4
        _seed_active_live_position(repo, f"live-pos-{i}")
    settings = _settings()
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    connector = _stub_connector_with_one_healthy_symbol()

    positions = run_discovery_tick(
        connector, repo, runner, settings,
        live_connector=_LiveConnectorStub(), live_market_data_connector=connector,
    )

    assert isinstance(positions, list)


def test_run_discovery_tick_recovers_an_orphaned_confirmed_candidate_at_tick_start(tmp_path):
    """P2 remediation (2026-09-11): a CONFIRMED candidate with no position
    row (simulating a prior crash between confirmation and open) gets a
    PAPER position opened at the very start of the next discovery tick -
    before the tick's own snapshot/candidate-search logic runs at all."""
    from crypto_trading.schemas.assessments import RiskAssessment
    from crypto_trading.schemas.candidate import Candidate
    from crypto_trading.schemas.evidence import (
        CandidateEvidenceRecord,
        FundingOpenInterestEvidence,
        MomentumBreakoutEvidence,
        PriceVolatilityEvidence,
        VolumeEvidence,
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _stub_connector_with_one_healthy_symbol()  # BTCUSDT ticker @ 50000
    now = datetime.now(UTC)
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    evidence = CandidateEvidenceRecord(
        instrument="BTCUSDT", timeframes=["1h"], evaluated_at=now,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=0.8, trigger_reasons=["price_volatility"],
        data_quality_status="ok", outcome="worth_deeper_analysis",
    )
    risk = RiskAssessment(
        agent_name="crypto-risk-agent", run_id="run-0", created_at=now, status="ok",
        suggested_stop_loss="49000", suggested_target="52000",
        downside="d", liquidity_risk="l", model_risk="m", timing_risk="t",
    )
    candidate = Candidate(
        candidate_id="orphan-1", idempotency_key="key-orphan-1", instrument="BTCUSDT",
        discovery_run_id="run-0", evidence_hash="hash-1", status="CANDIDATE",
        evidence_record=evidence, created_at=now, updated_at=now, risk=risk,
    )
    repo.create_candidate_with_event(
        candidate,
        Event(
            event_id="CANDIDATE_CREATED:orphan-1", event_type="CANDIDATE_CREATED",
            aggregate_type="candidate", aggregate_id="orphan-1", occurred_at=now,
            run_id="run-0", schema_version=1, payload={},
        ),
    )
    repo.save_assessment("orphan-1", "risk", risk)
    repo.transition_candidate_with_event(
        "orphan-1", "CONFIRMED", now,
        Event(
            event_id="CANDIDATE_TRANSITIONED:orphan-1:CONFIRMED",
            event_type="CANDIDATE_TRANSITIONED", aggregate_type="candidate",
            aggregate_id="orphan-1", occurred_at=now, run_id="run-0",
            schema_version=1, payload={"from": "UNDER_AI_ANALYSIS", "to": "CONFIRMED"},
        ),
    )
    # Activate the watermark BEFORE the candidate's confirmed_at, so this
    # tick's sweep treats it as a genuine forward-recovery target.
    repo.set_recovery_sweep_activated_at_if_missing(now - timedelta(seconds=1))

    run_discovery_tick(connector, repo, MockAgentRunner(_happy_fixtures()), _settings(top_n=1))

    position = repo.get_position("orphan-1")
    assert position is not None
    assert position.status == "OPEN_POSITION"


# ---------------------------------------------------------------------------
# Guardian Authority self-improvement pipeline wiring (Task 7,
# docs/superpowers/plans/2026-09-15-guardian-authority-live-autonomy.md).
# run_godfather_self_improvement_tick (crypto_trading/guardian/
# self_improvement.py) is wired in HERE (discovery_loop.py), not
# monitoring_loop.py, because propose_candidate_heuristics (Task 3) makes an
# LLM call and needs an AgentRunner - run_monitoring_tick has no `runner`
# parameter at all, while run_discovery_tick already has one in scope for
# exactly this reason. Its own try/except (never shared with the recovery
# sweep or live-capacity gate above), gated by settings.guardian.
# authority_enabled (NOT authority_shadow_enabled - a completely separate,
# already-shipped concern) - same "each concern gets its own try/except,
# gated by its own flag" discipline monitoring_loop.py already established
# for the shadow tick / pre-entry shadow resolution / shadow self-critique
# calls.
# ---------------------------------------------------------------------------
class _CallRecorder:
    """Records every call's positional/keyword args - same idiom
    test_monitoring_loop.py's own _CallRecorder uses for spying on
    module-level functions via monkeypatch."""

    def __init__(self):
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_run_discovery_tick_never_calls_self_improvement_tick_when_flag_off(tmp_path, monkeypatch):
    """authority_enabled defaults False. Proven via a call-recorder spy, not
    just by checking for absent DB effects afterwards - a spy catches the
    function being called and happening to no-op internally, which
    absence-of-writes alone cannot."""
    import crypto_trading.discovery_loop as discovery_loop_module

    spy = _CallRecorder()
    monkeypatch.setattr(discovery_loop_module, "run_godfather_self_improvement_tick", spy)

    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _stub_connector_with_one_healthy_symbol()
    settings = _settings(top_n=1)
    assert settings.guardian.authority_enabled is False  # baseline assumption

    run_discovery_tick(connector, repo, MockAgentRunner(_happy_fixtures()), settings)

    assert spy.calls == []  # never called, not "called but no-op"


def test_run_discovery_tick_calls_self_improvement_tick_once_with_correct_args_when_flag_on(
    tmp_path, monkeypatch
):
    import crypto_trading.discovery_loop as discovery_loop_module

    spy = _CallRecorder()
    monkeypatch.setattr(discovery_loop_module, "run_godfather_self_improvement_tick", spy)

    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _stub_connector_with_one_healthy_symbol()
    runner = MockAgentRunner(_happy_fixtures())
    settings = _settings(top_n=1)
    settings.guardian.authority_enabled = True

    run_discovery_tick(connector, repo, runner, settings)

    assert len(spy.calls) == 1  # exactly once per tick
    args, kwargs = spy.calls[0]
    assert kwargs == {}
    assert len(args) == 5
    call_repo, call_runner, call_settings, call_run_id, call_now = args
    assert call_repo is repo
    assert call_runner is runner
    assert call_settings is settings
    assert isinstance(call_run_id, str) and call_run_id
    assert call_now is not None


def test_a_crash_in_self_improvement_tick_never_affects_the_discovery_pipeline(
    tmp_path, monkeypatch, caplog
):
    """Mirrors test_monitoring_loop.py's own
    test_a_crash_in_guardian_authority_shadow_never_affects_real_position_
    closing: an unexpected crash in the self-improvement tick must never
    propagate out of run_discovery_tick, must never mark the run as
    'error', and must be logged under its OWN failed-event name."""
    import crypto_trading.discovery_loop as discovery_loop_module

    def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(discovery_loop_module, "run_godfather_self_improvement_tick", _raise)

    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _stub_connector_with_one_healthy_symbol()
    settings = _settings(top_n=1)
    settings.guardian.authority_enabled = True

    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        positions = run_discovery_tick(connector, repo, MockAgentRunner(_happy_fixtures()), settings)

    assert positions == []  # this fixture triggers no candidate either way
    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'discovery'").fetchone()
    assert row["status"] == "ok"  # the crash never reached the outer try/except
    assert "godfather_self_improvement_tick_failed" in caplog.text
    assert "discovery_tick_failed" not in caplog.text
