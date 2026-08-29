from datetime import UTC, datetime, timedelta

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.discovery_loop import run_discovery_tick
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
