from datetime import UTC, datetime

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.discovery_loop import run_discovery_tick
from crypto_trading.monitoring_loop import run_monitoring_tick
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.paper_trading.test_replay import _happy_fixtures as _realistic_fixtures
from tests.crypto_trading.test_discovery_loop import _stub_connector_that_triggers_a_candidate
from tests.crypto_trading.test_market_snapshot import _ms, _raw_funding, _raw_kline, _raw_ticker
from tests.crypto_trading.test_market_snapshot import _settings as _market_settings
from tests.crypto_trading.test_monitoring_loop import _MonitoringStubConnector
from tests.crypto_trading.test_orchestrator import (
    _happy_fixtures,
    _SpyRunner,
    _StubExternalDataConnector,
    _StubNewsConnector,
)

_OTHER_ROLE_AGENT_NAMES = (
    "crypto-technical-analyst",
    "crypto-bull-thesis",
    "crypto-forecast-agent",
    "crypto-risk-agent",
    "crypto-bear-adversarial",
    "crypto-qa-gate",
)


def test_news_sentiment_role_actually_receives_news_and_fear_greed_context(tmp_path):
    """AC1 (Fas 5.5): bevisar end-to-end, genom den RIKTIGA live entry point
    (run_discovery_tick -> build_live_snapshot -> run_single_cycle ->
    run_discovery_cycle -> Orchestrator), att News RSS-/Fear&Greed-
    connectorerna faktiskt når news_sentiment-rollens context - inte bara
    att run_discovery_cycle (Task 3:s test) vidarebefordrar parametern."""
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _stub_connector_that_triggers_a_candidate()
    spy = _SpyRunner(_happy_fixtures())

    positions = run_discovery_tick(
        connector,
        repo,
        spy,
        _market_settings(top_n=1),
        news_connector=_StubNewsConnector(),
        external_data_connector=_StubExternalDataConnector(),
    )

    news_context = spy.captured_contexts["crypto-news-sentiment"]
    assert news_context["news_headlines"] == [
        {"title": "UNIK_TESTRUBRIK", "link": "l", "pub_date": "p", "description": "d"}
    ]
    assert news_context["fear_greed_index"] == {"value": "50", "value_classification": "Neutral"}
    assert "evidence_record" in news_context  # befintligt fält kvar oförändrat

    for agent_name in _OTHER_ROLE_AGENT_NAMES:
        other_context = spy.captured_contexts[agent_name]
        assert "news_headlines" not in other_context
        assert "fear_greed_index" not in other_context

    # Bevisar att den befintliga discovery-pipelinen fortfarande fungerar
    # normalt genom hela den riktiga kedjan - inte bara att inget kraschar.
    assert len(positions) == 1


def test_forecast_record_survives_restart_and_links_to_actual_trade_result(tmp_path):
    """AC3-AC6 (Fas 5.5): driver en candidate genom den RIKTIGA pipelinen
    (run_discovery_tick -> CONFIRMED, sedan run_monitoring_tick -> CLOSED)
    och bevisar att ForecastRecord:en (a) faktiskt skapades av en lyckad
    Forecast-roll, (b) överlever en simulerad processomstart, (c) förblir
    länkad till den faktiska stängda positionens resultat via delat
    candidate_id, och (d) aldrig får actual_outcome/outcome_timestamp
    ifyllda i denna fas (Fas 8:s jobb, inte denna). Inget slutresultat
    skapas manuellt - allt drivs genom run_discovery_tick/run_monitoring_tick."""
    db_path = tmp_path / "t.db"
    repo = SQLiteRepository(db_path)
    settings = _market_settings(top_n=1)
    fixtures = _realistic_fixtures()  # stop_loss=53000, target=57000 - överlever samma-cykel-close

    run_discovery_tick(
        _stub_connector_that_triggers_a_candidate(),
        repo,
        MockAgentRunner(fixtures),
        settings,
    )

    seeded = repo._conn.execute(
        "SELECT candidate_id, status FROM candidates WHERE instrument = 'BTCUSDT'"
    ).fetchone()
    assert seeded is not None
    candidate_id = seeded["candidate_id"]
    assert seeded["status"] == "CONFIRMED"

    forecast_before = repo.get_forecast_record(candidate_id)
    assert forecast_before is not None  # AC3: skapad av en lyckad Forecast-roll
    assert forecast_before.candidate_id == candidate_id

    position_before = repo._conn.execute(
        "SELECT status FROM positions WHERE candidate_id = ?", (candidate_id,)
    ).fetchone()
    assert position_before is not None
    assert position_before["status"] == "OPEN_POSITION"

    # Simulera en processomstart: ny SQLiteRepository-instans mot samma DB-fil.
    repo_after_restart = SQLiteRepository(db_path)
    forecast_after_restart = repo_after_restart.get_forecast_record(candidate_id)
    assert forecast_after_restart is not None
    assert forecast_after_restart == forecast_before  # identiskt innehåll (AC4)

    # Stäng positionen via den RIKTIGA monitoring-loopen, pris under stop (53000).
    now = datetime.now(UTC)
    monitoring_connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "52000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("52000", _ms(now), high="52100", low="51900")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )
    closed = run_monitoring_tick(monitoring_connector, repo_after_restart, settings)

    assert len(closed) == 1
    assert closed[0].status == "CLOSED"
    assert closed[0].exit_reason == "stop_loss"

    # Bevisa länkbarheten (AC5) via delat candidate_id, efter stängningen.
    forecast_after_close = repo_after_restart.get_forecast_record(candidate_id)
    # position_id = candidate_id (Fas 4)
    position_after_close = repo_after_restart.get_position(candidate_id)

    assert forecast_after_close is not None  # fortfarande kvar efter stängningen
    assert position_after_close is not None
    assert forecast_after_close.candidate_id == position_after_close.candidate_id == candidate_id
    assert position_after_close.position_id == candidate_id
    assert position_after_close.status == "CLOSED"
    assert position_after_close.exit_reason == "stop_loss"
    assert position_after_close.simulated_fill_exit is not None
    assert forecast_after_close.scenario_probabilities == forecast_before.scenario_probabilities

    # AC5:s uttalade gräns: actual_outcome/outcome_timestamp sätts INTE här,
    # varken av discovery, monitoring eller denna task - Fas 8:s jobb.
    assert forecast_after_close.actual_outcome is None
    assert forecast_after_close.outcome_timestamp is None
