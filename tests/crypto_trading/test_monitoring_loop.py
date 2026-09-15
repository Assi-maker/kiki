import logging
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.monitoring_loop import run_monitoring_tick
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _ms, _raw_funding, _raw_kline, _raw_ticker
from tests.crypto_trading.test_market_snapshot import _settings as _market_settings


def _settings():
    return _market_settings(top_n=1)


def _seed_open_position(
    repo,
    instrument: str = "BTCUSDT",
    stop_loss: Decimal = Decimal("49000"),
    target: Decimal = Decimal("60000"),
    position_id: str = "pos-1",
) -> Position:
    opened_at = datetime.now(UTC)
    position = Position(
        position_id=position_id,
        candidate_id=position_id,
        instrument=instrument,
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"),
        stop_loss=stop_loss,
        target=target,
        size=Decimal("1000"),
        fill_model_version="v1",
        opened_at=opened_at,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id=position_id,
        occurred_at=opened_at,
        run_id="seed",
        schema_version=1,
        payload={},
    )
    repo.create_position_with_event(position, event)
    return position


class _MonitoringStubConnector:
    """Minimal connector-stub för monitoring_loop - anropar bara
    get_ticker/get_klines/get_funding_rate, aldrig get_contracts/
    get_open_interest (de hör bara till discovery/Task 6)."""

    def __init__(
        self, tickers=None, klines=None, funding_rates=None, raise_for=None, malformed_for=None
    ):
        self._tickers = tickers or {}
        self._klines = klines or {}
        self._funding_rates = funding_rates or {}
        self._raise_for = raise_for or {}
        self._malformed_for = malformed_for or set()

    def get_ticker(self, symbol):
        if symbol in self._raise_for:
            raise self._raise_for[symbol]
        raw = self._tickers[symbol]
        if symbol in self._malformed_for:
            raw = dict(raw)
            del raw["lastPrice"]  # genuint saknad nyckel, inte None
        return raw

    def get_klines(self, symbol, interval, limit=1):
        return self._klines[symbol][-limit:]

    def get_funding_rate(self, symbol, limit=1):
        return self._funding_rates.get(symbol, [])[-limit:]


def test_run_monitoring_tick_closes_a_triggered_position(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    closed = run_monitoring_tick(connector, repo, _settings())

    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"


def test_run_monitoring_tick_continues_when_daily_ai_budget_is_exhausted(tmp_path):
    """Krav 4 (budget enforcement): monitoring_loop tar ingen AgentRunner/
    Settings.budget_limits-parameter alls - denna test bevisar att en helt
    uttömd daglig AI-budget (både anropstak och dollartak) inte påverkar
    övervakningen av redan öppna PAPER-positioner, exakt som kravet säger."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    for i in range(500):
        repo.record_ai_call_event(
            Event(
                event_id=f"AI_CALL_MADE:exhaust:{i}",
                event_type="AI_CALL_MADE",
                aggregate_type="candidate",
                aggregate_id="exhaust",
                occurred_at=datetime.now(UTC),
                run_id="run-0",
                schema_version=1,
                payload={"role": "risk", "status": "ok", "cost_usd": "10.00"},
            )
        )
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    closed = run_monitoring_tick(connector, repo, _settings())

    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"


def test_run_monitoring_tick_skips_instrument_on_connector_failure_without_crashing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    connector = _MonitoringStubConnector(raise_for={"BTCUSDT": ConnectorUnavailableError("nere")})

    closed = run_monitoring_tick(connector, repo, _settings())

    assert closed == []
    # kvar öppen, aldrig gissad stängning
    assert repo.find_open_positions()[0].status == "OPEN_POSITION"


def test_run_monitoring_tick_persists_a_runs_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _MonitoringStubConnector()  # inga öppna positioner - inga anrop görs alls

    run_monitoring_tick(connector, repo, _settings())

    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'monitoring'").fetchone()
    assert row is not None


def test_run_monitoring_tick_does_not_crash_on_unexpected_malformed_payload(tmp_path):
    """Conflict-fix (2026-08-27): en genuint ofullständig rå-ticker (saknar
    lastPrice) ger Ticker.from_raw() ett KeyError - INTE ConnectorUnavailableError,
    så det inre except-blocket fångar det inte. Den nya yttre
    except Exception (samma mönster som discovery_loop.run_discovery_tick())
    ska fånga detta, markera runs.status='error', och ALDRIG låta undantaget
    nå anroparen (vilket annars skulle krascha run_forever())."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
        malformed_for={"BTCUSDT"},
    )

    closed = run_monitoring_tick(connector, repo, _settings())  # ska aldrig kasta

    assert closed == []
    assert repo.find_open_positions()[0].status == "OPEN_POSITION"
    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'monitoring'").fetchone()
    assert row["status"] == "error"
    assert "KeyError" in row["errors"]


def test_run_monitoring_tick_skips_instrument_on_empty_klines_without_blocking_others(tmp_path):
    """Live incident 2026-09-04: BingX returned an empty klines list for one
    instrument (`get_klines(...)[-1]` -> IndexError, not caught by the inner
    ConnectorUnavailableError handler) and aborted the WHOLE tick, silently
    skipping stop-loss/target checks for every OTHER open position too -
    not just the affected instrument. An empty response is exactly the same
    "no usable data available" case as a connector failure, so it's treated
    the same way: skip only that instrument, keep checking the rest."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"), position_id="pos-1")
    _seed_open_position(repo, instrument="ETHUSDT", stop_loss=Decimal("3000"), position_id="pos-2")
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={
            "BTCUSDT": _raw_ticker("BTCUSDT", "50000", "10000000", _ms(now)),
            "ETHUSDT": _raw_ticker("ETHUSDT", "2900", "10000000", _ms(now)),
        },
        klines={
            "BTCUSDT": [],  # empty response for this instrument only
            "ETHUSDT": [_raw_kline("2900", _ms(now), high="2950", low="2900")],
        },
        funding_rates={"ETHUSDT": [_raw_funding("ETHUSDT", "0.0001", _ms(now))]},
    )

    closed = run_monitoring_tick(connector, repo, _settings())

    assert len(closed) == 1
    assert closed[0].position_id == "pos-2"
    assert closed[0].exit_reason == "stop_loss"
    # BTCUSDT's own position is left open, never crashed the whole tick
    btc_position = repo.get_position("pos-1")
    assert btc_position.status == "OPEN_POSITION"
    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'monitoring'").fetchone()
    assert row["status"] == "partial_error"


def test_a_crash_in_the_profit_protection_experiment_never_affects_real_position_closing(
    tmp_path, monkeypatch, caplog
):
    """Spec G10 (explicit user requirement #8): forces the experiment tick
    to raise and proves (a) the real stop_loss close still happens and is
    still returned, (b) no exception propagates out of run_monitoring_tick,
    (c) the failure is logged."""
    import crypto_trading.monitoring_loop as monitoring_loop_module

    def _raiser(*args, **kwargs):
        raise RuntimeError("boom - simulated PP experiment failure")

    monkeypatch.setattr(
        monitoring_loop_module, "run_profit_protection_experiment_tick", _raiser
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        closed = run_monitoring_tick(connector, repo, _settings())  # must never raise

    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"
    assert closed[0].status == "CLOSED"
    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'monitoring'").fetchone()
    assert row["status"] == "ok"  # the OUTER try/except never even saw the failure
    assert "profit_protection_experiment_tick_failed" in caplog.text  # (c) failure is logged


class _CallRecorder:
    """Records every call's positional/keyword args, byte-for-byte, so
    tests can assert object identity (e.g. `is`) on the arguments a mock
    received - a plain `unittest.mock.Mock` would work too, but a bespoke
    recorder keeps the assertion below (same `open_positions` object
    reference) unambiguous and dependency-free."""

    def __init__(self):
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_run_monitoring_tick_never_calls_guardian_authority_shadow_when_flag_off(
    tmp_path, monkeypatch
):
    """Guardian Authority shadow mode defaults off (GuardianConfig.
    authority_shadow_enabled = False). This proves flag-off behavior is
    byte-identical to before this wiring existed: the function is never
    even called (proven via a call-recorder spy, not just by checking for
    absent rows afterwards - a spy catches the function being called and
    happening to no-op internally, which absence-of-rows alone cannot)."""
    import crypto_trading.monitoring_loop as monitoring_loop_module

    shadow_spy = _CallRecorder()
    monkeypatch.setattr(
        monitoring_loop_module, "run_guardian_authority_shadow_tick", shadow_spy
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    settings = _settings()
    assert settings.guardian.authority_shadow_enabled is False  # baseline assumption

    run_monitoring_tick(connector, repo, settings)

    assert shadow_spy.calls == []  # never called, not "called but no-op"
    rows = repo._conn.execute(
        "SELECT COUNT(*) FROM guardian_authority_shadow_observations"
    ).fetchone()[0]
    assert rows == 0  # no new DB writes


def test_run_monitoring_tick_calls_guardian_authority_shadow_once_with_pre_close_snapshot(
    tmp_path, monkeypatch
):
    """Flag-on: proves (1) run_guardian_authority_shadow_tick is called
    exactly once per tick, (2) with the same 7 positional arguments
    run_profit_protection_experiment_tick receives at the same call site,
    and (3) - the critical integration requirement confirmed by Task 4's
    reviewer - the `open_positions` object it receives is the EXACT SAME
    object (`is`, not just `==`) as the one the PP experiment call
    received: the PRE-close snapshot captured before
    close_triggered_positions runs, not a post-close-filtered list. If
    monitoring_loop ever passed a different/rebuilt list to one call than
    the other, this identity assertion (not just an equality check, which
    a coincidentally-equal-but-rebuilt list could also pass) would fail."""
    import crypto_trading.monitoring_loop as monitoring_loop_module

    pp_spy = _CallRecorder()
    shadow_spy = _CallRecorder()
    monkeypatch.setattr(
        monitoring_loop_module, "run_profit_protection_experiment_tick", pp_spy
    )
    monkeypatch.setattr(
        monitoring_loop_module, "run_guardian_authority_shadow_tick", shadow_spy
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    settings = _settings()
    settings.guardian.authority_shadow_enabled = True

    run_monitoring_tick(connector, repo, settings)

    assert len(pp_spy.calls) == 1
    assert len(shadow_spy.calls) == 1  # exactly once per tick

    pp_args, pp_kwargs = pp_spy.calls[0]
    shadow_args, shadow_kwargs = shadow_spy.calls[0]
    assert pp_kwargs == {}
    assert shadow_kwargs == {}
    assert len(pp_args) == 7
    assert len(shadow_args) == 7

    pp_repo, pp_open_positions, pp_closed, pp_price_lookup, pp_now, pp_settings, pp_run_id = (
        pp_args
    )
    (
        shadow_repo, shadow_open_positions, shadow_closed, shadow_price_lookup,
        shadow_now, shadow_settings, shadow_run_id,
    ) = shadow_args

    assert shadow_repo is pp_repo is repo
    # the critical pre-close-snapshot requirement: SAME object, not equal-by-value
    assert shadow_open_positions is pp_open_positions
    assert shadow_closed is pp_closed
    assert shadow_price_lookup is pp_price_lookup
    assert shadow_now == pp_now
    assert shadow_settings is pp_settings is settings
    assert shadow_run_id == pp_run_id


def test_a_crash_in_guardian_authority_shadow_never_affects_real_position_closing(
    tmp_path, monkeypatch, caplog
):
    """Mirrors test_a_crash_in_the_profit_protection_experiment_never_affects_
    real_position_closing exactly, but for the Guardian Authority shadow
    call - in its OWN try/except, so its failure can never be attributable
    to, or mask, the Profit Protection experiment's own failure handling."""
    import crypto_trading.monitoring_loop as monitoring_loop_module

    def _raiser(*args, **kwargs):
        raise RuntimeError("boom - simulated guardian authority shadow failure")

    monkeypatch.setattr(
        monitoring_loop_module, "run_guardian_authority_shadow_tick", _raiser
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    settings = _settings()
    settings.guardian.authority_shadow_enabled = True

    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        closed = run_monitoring_tick(connector, repo, settings)  # must never raise

    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"
    assert closed[0].status == "CLOSED"
    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'monitoring'").fetchone()
    assert row["status"] == "ok"  # the OUTER try/except never even saw the failure
    assert "guardian_authority_shadow_tick_failed" in caplog.text  # failure is logged
    # and the PP experiment's own failure-handling path was never touched
    assert "profit_protection_experiment_tick_failed" not in caplog.text


# ---------------------------------------------------------------------------
# resolve_pending_pre_entry_shadows wiring (Guardian Authority Shadow/
# Observation Mode, 2026-09-15, Task 7). Its own small step, in its own
# try/except, called right after Task 5's run_guardian_authority_shadow_tick
# call - kept separate (rather than folded into that same try/except block)
# so a crash in one can never be attributed to, or mask, a crash in the
# other, matching this module's own existing "each concern gets its own
# try/except" discipline (profit protection vs. shadow tick, above). Gated
# by the SAME settings.guardian.authority_shadow_enabled flag as the shadow
# tick call, at the same call site.
# ---------------------------------------------------------------------------


def test_run_monitoring_tick_never_resolves_pre_entry_shadows_when_flag_off(
    tmp_path, monkeypatch
):
    """Flag off (default): resolve_pending_pre_entry_shadows must never even
    be called - proven via a call-recorder spy, not just absent DB effects."""
    import crypto_trading.monitoring_loop as monitoring_loop_module

    resolve_spy = _CallRecorder()
    monkeypatch.setattr(
        monitoring_loop_module, "resolve_pending_pre_entry_shadows", resolve_spy
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    settings = _settings()
    assert settings.guardian.authority_shadow_enabled is False  # baseline assumption

    run_monitoring_tick(connector, repo, settings)

    assert resolve_spy.calls == []  # never called, not "called but no-op"


def test_run_monitoring_tick_resolves_pre_entry_shadows_every_tick_when_flag_on(
    tmp_path, monkeypatch
):
    """Flag on: resolve_pending_pre_entry_shadows must be called exactly
    once per tick - EVERY tick, not only when something closed this tick,
    since a shadow row can become resolvable on any later tick once its
    position happens to close (this fixture closes nothing this tick)."""
    import crypto_trading.monitoring_loop as monitoring_loop_module

    resolve_spy = _CallRecorder()
    monkeypatch.setattr(
        monitoring_loop_module, "resolve_pending_pre_entry_shadows", resolve_spy
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        # price stays well above the stop-loss - nothing closes this tick
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "50100", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("50100", _ms(now), high="50200", low="50050")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    settings = _settings()
    settings.guardian.authority_shadow_enabled = True

    closed = run_monitoring_tick(connector, repo, settings)

    assert closed == []  # sanity: nothing closed this tick
    assert len(resolve_spy.calls) == 1  # still called - resolution isn't gated on closes
    args, kwargs = resolve_spy.calls[0]
    assert kwargs == {}
    assert len(args) == 3
    resolve_repo, resolve_now, resolve_run_id = args
    assert resolve_repo is repo
    assert isinstance(resolve_run_id, str) and resolve_run_id


def test_a_crash_in_pre_entry_shadow_resolution_never_affects_real_position_closing(
    tmp_path, monkeypatch, caplog
):
    """Mirrors test_a_crash_in_guardian_authority_shadow_never_affects_real_
    position_closing exactly, but for the pre-entry shadow resolution call -
    in its OWN try/except, so its failure can never be attributable to, or
    mask, the shadow tick's own failure handling."""
    import crypto_trading.monitoring_loop as monitoring_loop_module

    def _raiser(*args, **kwargs):
        raise RuntimeError("boom - simulated pre-entry shadow resolution failure")

    monkeypatch.setattr(
        monitoring_loop_module, "resolve_pending_pre_entry_shadows", _raiser
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    now = datetime.now(UTC)
    connector = _MonitoringStubConnector(
        tickers={"BTCUSDT": _raw_ticker("BTCUSDT", "48000", "10000000", _ms(now))},
        klines={"BTCUSDT": [_raw_kline("48000", _ms(now), high="48500", low="48000")]},
        funding_rates={"BTCUSDT": [_raw_funding("BTCUSDT", "0.0001", _ms(now))]},
    )

    settings = _settings()
    settings.guardian.authority_shadow_enabled = True

    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        closed = run_monitoring_tick(connector, repo, settings)  # must never raise

    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"
    assert closed[0].status == "CLOSED"
    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'monitoring'").fetchone()
    assert row["status"] == "ok"  # the OUTER try/except never even saw the failure
    assert "guardian_authority_pre_entry_shadow_resolution_tick_failed" in caplog.text
    # and neither sibling failure-handling path was touched
    assert "guardian_authority_shadow_tick_failed" not in caplog.text
    assert "profit_protection_experiment_tick_failed" not in caplog.text
