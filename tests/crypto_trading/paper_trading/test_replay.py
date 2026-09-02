from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.config.loader import (
    BudgetLimitsConfig,
    DashboardConfig,
    NotifyConfig,
    PipelineConfig,
    RiskLimitsConfig,
    Settings,
)
from crypto_trading.paper_trading.replay import MarketSnapshot, run_replay, run_single_cycle
from crypto_trading.schemas.assessments import (
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)
from crypto_trading.schemas.market import FundingRate, InstrumentMetadata, Kline, Ticker
from crypto_trading.storage.repository import SQLiteRepository

_T0 = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(
        db_path="unused",
        pipeline=PipelineConfig(
            discovery_interval_minutes=60,
            monitoring_interval_seconds=30,
            top_n=5,
            cooldown_minutes=60,
            max_data_age_seconds={
                "ticker": 3600,
                "kline": 3600,
                "funding_rate": 36000,
                "open_interest": 3600,
                "contracts": 86400,
            },
            min_sample_size_for_calibration=30,
            calibration_preliminary_sample_size=10,
            sqlite_busy_timeout_ms=5000,
            required_fields={
                "ticker": ["lastPrice"],
                "kline": ["open"],
                "funding_rate": ["fundingRate"],
                "open_interest": ["openInterest"],
                "contracts": ["symbol"],
            },
            screener_timeframes=["1h"],
            bingx_base_url="https://open-api.bingx.com",
            bingx_requests_per_second=10,
            bingx_cache_ttl_seconds=5,
            bingx_max_retries=3,
            kline_consistency_tolerance_pct=Decimal("0.5"),
            eligibility_min_quote_volume_24h_usdt=Decimal("1000000"),
            eligibility_max_spread_pct=Decimal("0.01"),
            screener_lookback_periods=3,
            screener_price_volatility_threshold_pct=Decimal("2.0"),
            screener_rsi_period=3,
            screener_rsi_overbought_threshold=Decimal("70"),
            screener_volume_zscore_threshold=Decimal("2.5"),
            screener_funding_rate_threshold_pct=Decimal("0.05"),
            screener_funding_history_limit=10,
            evidence_change_threshold_for_reanalysis=Decimal("0.15"),
        ),
        risk_limits=RiskLimitsConfig(
            starting_capital_usdt=Decimal("10000"),
            risk_per_trade_pct=Decimal("0.01"),
            max_concurrent_positions=5,
            max_total_exposure_pct=Decimal("1.0"),
            spread_pct=Decimal("0.0005"),
            slippage_pct=Decimal("0.0005"),
            fee_pct=Decimal("0.0004"),
            max_position_hold_hours=24,
        ),
        budget_limits=BudgetLimitsConfig(
            max_candidates_per_discovery_run=10,
            max_ai_calls_per_discovery_run=70,
            max_ai_calls_per_day=500,
            warning_threshold_pct=Decimal("0.8"),
        ),
        notify=NotifyConfig(notification_level="important", notify_interval_seconds=60),
        dashboard=DashboardConfig(host="127.0.0.1", port=8000),
    )


def _instrument() -> InstrumentMetadata:
    return InstrumentMetadata(
        symbol="BTCUSDT",
        status=1,
        price_precision=2,
        quantity_precision=3,
        trade_min_usdt=Decimal("2"),
        fetched_at=_T0,
    )


def _ticker(last_price: str, quote_volume: str, at: datetime) -> Ticker:
    price = Decimal(last_price)
    return Ticker(
        instrument="BTCUSDT",
        last_price=price,
        price_change=Decimal("0"),
        price_change_percent=Decimal("0"),
        high_price=price,
        low_price=price,
        volume=Decimal("500"),
        quote_volume=Decimal(quote_volume),
        open_price=price,
        ask_price=price * Decimal("1.0002"),
        ask_qty=Decimal("1"),
        bid_price=price * Decimal("0.9998"),
        bid_qty=Decimal("1"),
        observed_at=at,
    )


def _kline(close: str, offset_hours: int, high=None, low=None, volume="100") -> Kline:
    close_dec = Decimal(close)
    return Kline(
        instrument="BTCUSDT",
        interval="1h",
        open=close_dec,
        high=Decimal(high) if high else close_dec,
        low=Decimal(low) if low else close_dec,
        close=close_dec,
        volume=Decimal(volume),
        observed_at=_T0 + timedelta(hours=offset_hours),
    )


def _funding(rate: str, offset_hours: int) -> FundingRate:
    return FundingRate(
        instrument="BTCUSDT",
        funding_rate=Decimal(rate),
        mark_price=Decimal("50000"),
        observed_at=_T0 + timedelta(hours=offset_hours),
    )


def _happy_fixtures(stop_loss="53000", target="57000") -> dict:
    return {
        "crypto-news-sentiment": NewsSentimentAssessment(
            agent_name="crypto-news-sentiment",
            run_id="run-1",
            created_at=_T0,
            status="ok",
            verified_facts=["f"],
            source_claims=["c"],
            interpretation="i",
        ),
        "crypto-technical-analyst": TechnicalAssessment(
            agent_name="crypto-technical-analyst",
            run_id="run-1",
            created_at=_T0,
            status="ok",
            market_data={},
            interpretation="i",
        ),
        "crypto-bull-thesis": BullThesisAssessment(
            agent_name="crypto-bull-thesis",
            run_id="run-1",
            created_at=_T0,
            status="ok",
            hypothesis="h",
            catalyst="c",
            setup="s",
        ),
        "crypto-forecast-agent": ForecastAssessment(
            agent_name="crypto-forecast-agent",
            run_id="run-1",
            created_at=_T0,
            status="ok",
            scenario_probabilities={"bullish": 0.6, "neutral": 0.3, "bearish": 0.1},
            horizon="4h",
            forecast_version="v1",
        ),
        "crypto-risk-agent": RiskAssessment(
            agent_name="crypto-risk-agent",
            run_id="run-1",
            created_at=_T0,
            status="ok",
            suggested_stop_loss=stop_loss,
            suggested_target=target,
            downside="d",
            liquidity_risk="l",
            model_risk="m",
            timing_risk="t",
        ),
        "crypto-bear-adversarial": BearAdversarialAssessment(
            agent_name="crypto-bear-adversarial",
            run_id="run-1",
            created_at=_T0,
            status="ok",
            counterarguments=["c"],
            alternative_explanations=["a"],
            falsification_conditions="f",
        ),
        "crypto-qa-gate": QAAssessment(
            agent_name="crypto-qa-gate",
            run_id="run-1",
            created_at=_T0,
            status="ok",
            passed=True,
            violations=[],
        ),
    }


def _build_snapshots() -> list[MarketSnapshot]:
    """Steg 1: flat, inget triggar. Steg 2: prisspik (10%) -> worth_deeper_analysis
    -> CONFIRMED -> öppen position (entry~55000, stop=53000, target=57000).
    Steg 3: en wick upp till 57500 (high) men close nära 55200 (ingen ny
    screener-trigger) -> target-träff -> stängd position."""
    flat_klines = [_kline("50000", offset_hours=-(3 - i)) for i in range(3)]
    spike_kline = _kline("55000", offset_hours=0, high="55100", low="54800", volume="9000")

    step1_klines = flat_klines
    step2_klines = [*flat_klines, spike_kline]
    step3_kline = Kline(
        instrument="BTCUSDT",
        interval="1h",
        open=Decimal("55200"),
        high=Decimal("57500"),
        low=Decimal("55000"),
        close=Decimal("55200"),
        volume=Decimal("120"),
        observed_at=_T0 + timedelta(hours=2),
    )
    step3_klines = [*step2_klines, step3_kline]

    funding_step1 = [_funding("0.0001", offset_hours=0)]
    funding_step2 = [*funding_step1, _funding("0.0001", offset_hours=1)]
    funding_step3 = [*funding_step2, _funding("0.0001", offset_hours=2)]

    instrument = _instrument()

    snapshot1 = MarketSnapshot(
        simulated_now=_T0,
        instruments={"BTCUSDT": instrument},
        tickers={"BTCUSDT": _ticker("50000", "5000000", _T0)},
        klines={"BTCUSDT": step1_klines},
        funding_rates={"BTCUSDT": funding_step1},
        data_quality_status={"BTCUSDT": "ok"},
    )
    snapshot2 = MarketSnapshot(
        simulated_now=_T0 + timedelta(hours=1),
        instruments={"BTCUSDT": instrument},
        tickers={"BTCUSDT": _ticker("55000", "5500000", _T0 + timedelta(hours=1))},
        klines={"BTCUSDT": step2_klines},
        funding_rates={"BTCUSDT": funding_step2},
        data_quality_status={"BTCUSDT": "ok"},
    )
    snapshot3 = MarketSnapshot(
        simulated_now=_T0 + timedelta(hours=2),
        instruments={"BTCUSDT": instrument},
        # quote_volume under eligibility_min_quote_volume_24h_usdt (1000000):
        # BTCUSDT faller ur Top N vid detta steg, så ingen ny discovery-
        # utvärdering sker (annars skulle det ihållande momentumet från
        # spiken kunna trigga en ANDRA candidate här). Övervakning/stängning
        # av den redan öppna positionen är oberoende av eligibility/Top N
        # (_build_price_lookup itererar snapshot.klines direkt) och påverkas
        # alltså inte.
        tickers={"BTCUSDT": _ticker("55200", "100", _T0 + timedelta(hours=2))},
        klines={"BTCUSDT": step3_klines},
        funding_rates={"BTCUSDT": funding_step3},
        data_quality_status={"BTCUSDT": "ok"},
    )
    return [snapshot1, snapshot2, snapshot3]


def test_replay_produces_a_confirmed_position_and_closes_it_at_target(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures=_happy_fixtures())

    positions = run_replay(_build_snapshots(), repo, runner, _settings(), run_id="run-1")

    assert len(positions) == 1
    position = positions[0]
    assert position.instrument == "BTCUSDT"
    assert position.direction == "LONG"
    assert position.status == "CLOSED"
    assert position.exit_reason == "target"
    assert position.theoretical_exit == Decimal("57000")
    assert position.simulated_fill_exit != position.theoretical_exit


def test_replay_decision_at_time_t_is_unaffected_by_injected_future_data(tmp_path):
    """AC2: en kraftigt avvikande, framtida datapunkt injicerad i en tidigare
    snapshots klines-lista har bevisligen noll effekt på resultatet."""
    repo_clean = SQLiteRepository(tmp_path / "clean.db")
    repo_tampered = SQLiteRepository(tmp_path / "tampered.db")

    clean_snapshots = _build_snapshots()
    tampered_snapshots = _build_snapshots()

    # Injicera en extrem, framtida datapunkt (daterad EFTER steg 1:s
    # simulated_now) i steg 1:s klines-lista - simulerar att en framtida
    # candle av misstag hamnat i en tidigare hämtning.
    step1 = tampered_snapshots[0]
    future_kline = Kline(
        instrument="BTCUSDT",
        interval="1h",
        open=Decimal("999999"),
        high=Decimal("999999"),
        low=Decimal("999999"),
        close=Decimal("999999"),
        volume=Decimal("999999"),
        observed_at=step1.simulated_now + timedelta(hours=1),
    )
    step1.klines["BTCUSDT"] = [*step1.klines["BTCUSDT"], future_kline]

    runner_clean = MockAgentRunner(fixtures=_happy_fixtures())
    runner_tampered = MockAgentRunner(fixtures=_happy_fixtures())

    positions_clean = run_replay(
        clean_snapshots, repo_clean, runner_clean, _settings(), run_id="run-1"
    )
    positions_tampered = run_replay(
        tampered_snapshots, repo_tampered, runner_tampered, _settings(), run_id="run-1"
    )

    assert len(positions_clean) == len(positions_tampered) == 1
    clean, tampered = positions_clean[0], positions_tampered[0]
    assert clean.theoretical_entry == tampered.theoretical_entry
    assert clean.simulated_fill_entry == tampered.simulated_fill_entry
    assert clean.exit_reason == tampered.exit_reason
    assert clean.theoretical_exit == tampered.theoretical_exit


def test_run_single_cycle_wires_secondary_timeframe_evidence_into_candidate(tmp_path):
    """Beslut 2026-08-29 'primary triggers, secondary confirms': när
    screener_timeframes har en andra timeframe konfigurerad och snapshoten
    har secondary_klines/secondary_funding_rates, ska den skapade
    candidate:ns evidence_record.secondary_timeframe_evidence vara ifylld -
    stänger wiring-halvan av Fas 5:s multi-timeframe-lucka (market_snapshot.py
    hämtar redan sekundärdatan sedan föregående commit; detta bevisar att
    run_single_cycle -> evaluate_candidate faktiskt konsumerar den)."""
    base_settings = _settings()
    settings = base_settings.model_copy(
        update={
            "pipeline": base_settings.pipeline.model_copy(
                update={"screener_timeframes": ["1h", "4h"]}
            )
        }
    )
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    spike_snapshot = _build_snapshots()[1]
    # screener_lookback_periods=3, screener_rsi_period=3 -> minst 5 klines krävs.
    secondary_klines = [_kline("100", offset_hours=-(5 - i)) for i in range(5)]
    secondary_funding = [_funding("0.0001", offset_hours=-(5 - i)) for i in range(5)]
    snapshot_with_secondary = spike_snapshot.model_copy(
        update={
            "secondary_klines": {"BTCUSDT": secondary_klines},
            "secondary_funding_rates": {"BTCUSDT": secondary_funding},
        }
    )

    run_single_cycle(snapshot_with_secondary, repo, runner, settings, run_id="run-1")

    candidates = repo.find_candidates_by_status("CONFIRMED")
    assert len(candidates) == 1
    secondary_evidence = candidates[0].evidence_record.secondary_timeframe_evidence
    assert secondary_evidence is not None
    assert secondary_evidence.timeframe == "4h"


def test_run_single_cycle_can_be_called_directly_with_one_snapshot(tmp_path):
    """Låser run_single_cycle()s fristående kontrakt (Task 5): discovery_loop.py
    (Fas 5) ska kunna anropa den en gång per tick, utan run_replay()s loop."""
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    spike_snapshot = _build_snapshots()[1]

    positions = run_single_cycle(spike_snapshot, repo, runner, _settings(), run_id="run-1")

    assert len(positions) == 1
    assert positions[0].instrument == "BTCUSDT"
    assert positions[0].status == "OPEN_POSITION"


def test_run_single_cycle_without_screener_runner_is_unaffected(tmp_path):
    """Bakåtkompatibilitet: screener_runner=None (default) -> ingen
    opportunity-screening alls, exakt samma beteende som innan
    kostnadsoptimeringen lades till (2026-09-02)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    spike_snapshot = _build_snapshots()[1]

    positions = run_single_cycle(spike_snapshot, repo, runner, _settings(), run_id="run-1")

    assert len(positions) == 1
    candidates = repo.find_candidates_by_status("CONFIRMED")
    row = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM assessments WHERE field_name = 'opportunity_screen'"
    ).fetchone()
    assert row["n"] == 0
    assert len(candidates) == 1


def test_run_single_cycle_shadow_mode_screens_but_does_not_change_outcome(tmp_path):
    """opportunity_screening_enforce=False (default): screeningen körs och
    persisteras för utvärdering, men candidaten går fortfarande hela vägen
    till CONFIRMED/OPEN_POSITION precis som innan - noll beteendeändring."""
    from crypto_trading.schemas.assessments import OpportunityScreenAssessment

    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    screener_runner = MockAgentRunner(
        fixtures={
            "crypto-opportunity-screener": OpportunityScreenAssessment(
                agent_name="crypto-opportunity-screener",
                run_id="run-1",
                created_at=_T0,
                status="ok",
                opportunity_score=8.0,
                reasoning="stark signal",
            )
        }
    )
    spike_snapshot = _build_snapshots()[1]

    positions = run_single_cycle(
        spike_snapshot, repo, runner, _settings(), run_id="run-1", screener_runner=screener_runner
    )

    assert len(positions) == 1
    candidates = repo.find_candidates_by_status("CONFIRMED")
    assert len(candidates) == 1
    row = repo._conn.execute(
        "SELECT payload FROM assessments WHERE field_name = 'opportunity_screen'"
    ).fetchone()
    assert row is not None
    import json

    assert json.loads(row["payload"])["opportunity_score"] == 8.0


def test_run_single_cycle_enforce_mode_blocks_full_analysis_when_screen_scores_low(tmp_path):
    """opportunity_screening_enforce=True + en låg opportunity_score ->
    kandidaten ska INTE nå full analys/CONFIRMED, utan hamna i
    BUDGET_LIMITED (Gate/QA/PAPER-öppning rörs aldrig - candidaten blockeras
    innan den fulla 7-rollskedjan ens startar)."""
    from crypto_trading.schemas.assessments import OpportunityScreenAssessment

    settings = _settings().model_copy(
        update={
            "budget_limits": _settings().budget_limits.model_copy(
                update={
                    "opportunity_screening_enforce": True,
                    "max_candidates_for_ai_prescreen": 5,
                    "max_candidates_for_full_analysis": 1,
                }
            )
        }
    )
    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner(fixtures=_happy_fixtures())
    screener_runner = MockAgentRunner(
        fixtures={
            "crypto-opportunity-screener": OpportunityScreenAssessment(
                agent_name="crypto-opportunity-screener",
                run_id="run-1",
                created_at=_T0,
                status="failed",
                opportunity_score=0.0,
                reasoning="",
            )
        },
        fail_agents={"crypto-opportunity-screener"},
    )
    spike_snapshot = _build_snapshots()[1]

    positions = run_single_cycle(
        spike_snapshot, repo, runner, settings, run_id="run-1", screener_runner=screener_runner
    )

    assert positions == []
    candidates = repo.find_candidates_by_status("BUDGET_LIMITED")
    assert len(candidates) == 1
    assert repo.find_candidates_by_status("CONFIRMED") == []


def test_replay_is_deterministic_on_repeated_runs(tmp_path):
    """AC1: samma indata -> samma trades, vid upprepad körning mot separata,
    färska repo-instanser."""
    repo_a = SQLiteRepository(tmp_path / "a.db")
    repo_b = SQLiteRepository(tmp_path / "b.db")
    runner_a = MockAgentRunner(fixtures=_happy_fixtures())
    runner_b = MockAgentRunner(fixtures=_happy_fixtures())

    positions_a = run_replay(_build_snapshots(), repo_a, runner_a, _settings(), run_id="run-1")
    positions_b = run_replay(_build_snapshots(), repo_b, runner_b, _settings(), run_id="run-1")

    assert len(positions_a) == len(positions_b) == 1
    a, b = positions_a[0], positions_b[0]
    assert a.position_id == b.position_id
    assert a.theoretical_entry == b.theoretical_entry
    assert a.simulated_fill_entry == b.simulated_fill_entry
    assert a.theoretical_exit == b.theoretical_exit
    assert a.simulated_fill_exit == b.simulated_fill_exit
    assert a.exit_reason == b.exit_reason
    assert a.fees == b.fees
    assert a.funding == b.funding
