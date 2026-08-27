from datetime import UTC, datetime, timedelta

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.discovery_loop import run_discovery_tick
from crypto_trading.monitoring_loop import run_monitoring_tick
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.paper_trading.test_replay import _happy_fixtures as _realistic_fixtures
from tests.crypto_trading.test_discovery_loop import _stub_connector_that_triggers_a_candidate
from tests.crypto_trading.test_market_snapshot import (
    _ms,
    _raw_contract,
    _raw_funding,
    _raw_kline,
    _raw_open_interest,
    _raw_ticker,
    _StubConnector,
)
from tests.crypto_trading.test_market_snapshot import _settings as _market_settings
from tests.crypto_trading.test_monitoring_loop import _MonitoringStubConnector
from tests.crypto_trading.test_orchestrator import _happy_fixtures


def _settings(max_ai_calls_per_day: int = 500):
    return _market_settings(top_n=1, max_ai_calls_per_day=max_ai_calls_per_day)


def _flat_connector_near_entry(symbol: str = "BTCUSDT", price: str = "55000") -> _StubConnector:
    """Platt, icke-triggande prisserie NÄRA entry (53000-57000 stop/target)
    - varken price_volatility/momentum/volume triggar (0% förändring), och
    priset ligger mellan stop och target så en redan öppen position INTE
    stängs av discovery-tickens EGNA inbäddade close_triggered_positions-
    anrop (det skulle annars maskera det separata, explicita
    run_monitoring_tick()-anropet den här testfilen vill verifiera)."""
    now = datetime.now(UTC)
    contracts = [_raw_contract(symbol)]
    tickers = {symbol: _raw_ticker(symbol, price, "10000000", _ms(now))}
    klines = {
        symbol: [
            _raw_kline(price, _ms(now - timedelta(hours=2))),
            _raw_kline(price, _ms(now - timedelta(hours=1))),
            _raw_kline(price, _ms(now), high=price, low=price),
        ]
    }
    funding_rates = {symbol: [_raw_funding(symbol, "0.0001", _ms(now))]}
    open_interest = {symbol: _raw_open_interest(symbol, "1000", _ms(now))}
    return _StubConnector(contracts, tickers, klines, funding_rates, open_interest)


def test_multi_cycle_discovery_with_simulated_crash_and_restart_produces_no_duplicates(tmp_path):
    """AC1 (Fas 5): en process-krasch mitt i analysen (simulerad genom att
    direkt manipulera raden till UNDER_AI_ANALYSIS efter tick 1, eftersom
    process_candidate() inte har någon egen krasch-återhämtning internt)
    får aldrig lämna candidaten i ett okänt/permanent avbrutet läge, och
    får aldrig skapa en dubblettrad (§8.5/§8.6). Använder realistiska
    stop/target (53000/57000, inte test_orchestrator._happy_fixtures()s
    degenererade 1/2) så den öppnade positionen överlever fram till det
    separata, explicita run_monitoring_tick()-anropet i slutet."""
    repo = SQLiteRepository(tmp_path / "t.db")
    settings = _settings()
    fixtures = _realistic_fixtures()

    run_discovery_tick(
        _stub_connector_that_triggers_a_candidate(),
        repo,
        MockAgentRunner(fixtures, timeout_agents={"crypto-bull-thesis"}),
        settings,
    )

    seeded = repo._conn.execute(
        "SELECT candidate_id FROM candidates WHERE instrument = 'BTCUSDT'"
    ).fetchone()
    assert seeded is not None
    candidate_id = seeded["candidate_id"]

    # Simulerar en processkrasch: raden manipuleras direkt till
    # UNDER_AI_ANALYSIS, som om processen dog innan den hann skriva sitt
    # riktiga slututfall (process_candidate() har ingen egen recovery).
    repo._conn.execute(
        "UPDATE candidates SET status = 'UNDER_AI_ANALYSIS' WHERE candidate_id = ?",
        (candidate_id,),
    )
    repo._conn.commit()

    # "Efter omstart": en ny tick mot samma repo. Platt/icke-triggande
    # connector - discovery-steget ska inte skapa en NY BTCUSDT-candidate,
    # bara återupptagningspolicyn (Task 4) ska plocka upp den befintliga.
    run_discovery_tick(
        _flat_connector_near_entry(),
        repo,
        MockAgentRunner(fixtures),
        settings,
    )

    candidate_rows = repo._conn.execute(
        "SELECT candidate_id, status FROM candidates WHERE instrument = 'BTCUSDT'"
    ).fetchall()
    assert len(candidate_rows) == 1  # ingen dubblett skapades av tick 2
    assert candidate_rows[0]["candidate_id"] == candidate_id
    final_status = candidate_rows[0]["status"]
    assert final_status == "CONFIRMED"

    positions = repo._conn.execute("SELECT * FROM positions").fetchall()
    assert len(positions) == 1
    assert positions[0]["status"] == "OPEN_POSITION"  # inte redan stängd av tick 2 självt

    # Full livscykel, en gång till: en explicit övervaknings-tick med ett
    # pris under stop_loss (53000) ska stänga positionen normalt.
    monitoring_connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "52000", "10000000", _ms(datetime.now(UTC)))},
        klines={
            "BTCUSDT": [_raw_kline("52000", _ms(datetime.now(UTC)), high="52100", low="51900")]
        },
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(datetime.now(UTC)))]},
    )
    closed = run_monitoring_tick(monitoring_connector, repo, settings)

    assert len(closed) == 1
    assert closed[0].status == "CLOSED"
    assert closed[0].exit_reason == "stop_loss"


def _connector_triggering_one_symbol(symbol: str) -> _StubConnector:
    """Samma spik-mönster som _stub_connector_that_triggers_a_candidate()
    (test_discovery_loop.py), parameteriserad på symbol - Task 11 behöver
    tre oberoende candidates, ett per tick, för att undvika cooldown-/
    dedup-tvetydighet kring att återtriggra samma instrument."""
    now = datetime.now(UTC)
    contracts = [_raw_contract(symbol)]
    tickers = {symbol: _raw_ticker(symbol, "55000", "10000000", _ms(now))}
    klines = {
        symbol: [
            _raw_kline("50000", _ms(now - timedelta(hours=4))),
            _raw_kline("50000", _ms(now - timedelta(hours=3))),
            _raw_kline("50000", _ms(now - timedelta(hours=2))),
            _raw_kline("50000", _ms(now - timedelta(hours=1))),
            _raw_kline("55000", _ms(now), high="55100", low="54800"),
        ]
    }
    funding_rates = {symbol: [_raw_funding(symbol, "0.0001", _ms(now))]}
    open_interest = {symbol: _raw_open_interest(symbol, "1000", _ms(now))}
    return _StubConnector(contracts, tickers, klines, funding_rates, open_interest)


def _run_three_ticks_against_fresh_repo(db_path) -> dict[str, str]:
    repo = SQLiteRepository(db_path)
    settings = _settings(max_ai_calls_per_day=14)  # exakt två candidaters fulla analyser
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        run_discovery_tick(
            _connector_triggering_one_symbol(symbol),
            repo,
            MockAgentRunner(_happy_fixtures()),
            settings,
        )
    rows = repo._conn.execute("SELECT instrument, status FROM candidates").fetchall()
    return {row["instrument"]: row["status"] for row in rows}


def test_daily_cap_blocks_third_candidate_across_three_discovery_ticks_deterministically(
    tmp_path,
):
    """AC2 (Fas 5): taket är exakt två fulla 7-rollsanalyser (14) - de två
    första candidaterna (i separata discovery-ticks) ska fullt analyseras,
    den tredje ska bli BUDGET_LIMITED, aldrig REJECTED/NO_TRADE (§8.3).
    Verifierar även determinism: samma scenario mot ett färskt repo ger
    samma slutresultat."""
    statuses_a = _run_three_ticks_against_fresh_repo(tmp_path / "a.db")
    statuses_b = _run_three_ticks_against_fresh_repo(tmp_path / "b.db")

    assert statuses_a == statuses_b
    assert set(statuses_a) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

    budget_limited = [s for s in statuses_a.values() if s == "BUDGET_LIMITED"]
    fully_analyzed = [s for s in statuses_a.values() if s != "BUDGET_LIMITED"]
    assert len(budget_limited) == 1
    assert len(fully_analyzed) == 2
    assert all(s in {"CONFIRMED", "NO_TRADE", "REJECTED"} for s in fully_analyzed)
