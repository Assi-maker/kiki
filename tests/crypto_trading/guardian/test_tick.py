from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from crypto_trading.config.loader import GuardianConfig
from crypto_trading.guardian.tick import run_guardian_tick_body
from crypto_trading.schemas.assessments import RiskAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord, FundingOpenInterestEvidence, MomentumBreakoutEvidence,
    PriceVolatilityEvidence, VolumeEvidence,
)
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _raw_funding, _raw_kline, _raw_ticker, _settings

_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _placeholder_ev(**overrides):
    base = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    base.update(overrides)
    return base


# These three values are chosen to EXACTLY match what evaluate_candidate()
# itself computes from _StubConnector's flat klines (all closes "100", all
# volumes identical -> RSI=50 exactly per quant_screener.py's "helt platt
# fönster: neutralt RSI" rule, volume zscore=0 exactly per its "zero
# variance" rule) and its single funding entry (abs(0.0001)*100 = 0.01) -
# so a genuinely UNCHANGED market produces decay_score=0.0 exactly, not an
# arbitrary/mismatched placeholder that would silently produce some other
# state than the test's own name claims.
_MATCHING_ENTRY_RSI = 50.0
_MATCHING_ENTRY_VOLUME_ZSCORE = 0.0
_MATCHING_ENTRY_FUNDING_MAGNITUDE = 0.01


def _seed_candidate_and_position(repo, position_id="pos-1", opened_at=_NOW):
    evidence = CandidateEvidenceRecord(
        instrument="BTCUSDT", timeframes=["30m"], evaluated_at=opened_at,
        price_volatility_evidence=PriceVolatilityEvidence(**_placeholder_ev(value=3.0, threshold=2.0)),
        momentum_breakout_evidence=MomentumBreakoutEvidence(
            **_placeholder_ev(value=_MATCHING_ENTRY_RSI, threshold=70.0)
        ),
        volume_evidence=VolumeEvidence(
            **_placeholder_ev(value=_MATCHING_ENTRY_VOLUME_ZSCORE, threshold=2.5)
        ),
        funding_oi_evidence=FundingOpenInterestEvidence(
            **_placeholder_ev(value=_MATCHING_ENTRY_FUNDING_MAGNITUDE, threshold=0.05)
        ),
        candidate_score=0.5, trigger_reasons=["momentum_breakout"],
        data_quality_status="ok", outcome="worth_deeper_analysis",
    )
    candidate = Candidate(
        candidate_id=position_id, idempotency_key=f"key-{position_id}", instrument="BTCUSDT",
        discovery_run_id="run-0", evidence_hash="hash-1", status="CONFIRMED",
        evidence_record=evidence, created_at=opened_at, updated_at=opened_at,
        risk=RiskAssessment(
            agent_name="crypto-risk-agent", run_id="run-0", created_at=opened_at, status="ok",
            suggested_stop_loss="90", suggested_target="120", downside="d", liquidity_risk="l",
            model_risk="m", timing_risk="t",
        ),
    )
    repo.create_candidate_with_event(
        candidate,
        Event(event_id=f"CANDIDATE_CREATED:{position_id}", event_type="CANDIDATE_CREATED",
              aggregate_type="candidate", aggregate_id=position_id, occurred_at=opened_at,
              run_id="seed", schema_version=1, payload={}),
    )
    position = Position(
        position_id=position_id, candidate_id=position_id, instrument="BTCUSDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("100"), simulated_fill_entry=Decimal("100"),
        stop_loss=Decimal("90"), target=Decimal("120"), size=Decimal("1000"),
        fill_model_version="v1", opened_at=opened_at,
    )
    repo.create_position_with_event(
        position,
        Event(event_id=f"POSITION_OPENED:{position_id}", event_type="POSITION_OPENED",
              aggregate_type="position", aggregate_id=position_id, occurred_at=opened_at,
              run_id="seed", schema_version=1, payload={}),
    )
    return candidate, position


class _StubConnector:
    """Flat klines (all closes "100", identical volumes) dated backward
    from _NOW ending exactly at _NOW - the evidence builders reject any
    kline dated after evaluated_at (SPEC §8.4 no-future-data guard)."""

    def __init__(self, price="100"):
        self._price = price

    def get_klines(self, symbol, interval, limit=100):
        return [
            _raw_kline("100", int(_NOW.timestamp() * 1000) - (29 - i) * 60000) for i in range(30)
        ]

    def get_funding_rate(self, symbol, limit=1):
        return [_raw_funding(symbol, "0.0001", int(_NOW.timestamp() * 1000))]

    def get_ticker(self, symbol):
        return _raw_ticker(symbol, self._price, "1000000", int(_NOW.timestamp() * 1000))


class _FakeRunner:
    last_call_billed = True
    last_call_cost_usd = Decimal("0.01")

    def run(self, agent_def, context, response_model):
        return response_model(
            agent_name="crypto-guardian", run_id="run-1", created_at=_NOW, status="ok",
            reasoning="Momentum has faded materially since entry.",
        )


def test_run_guardian_tick_body_persists_a_hold_observation_without_ai(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)
    connector = _StubConnector(price="100")  # unchanged since entry -> HOLD

    observations = run_guardian_tick_body(repo, connector, _FakeRunner(), _settings(), "run-1", _NOW)

    assert len(observations) == 1
    assert observations[0].state == "HOLD"
    assert observations[0].ai_reasoning is None
    row = repo.find_latest_guardian_observation("pos-1")
    assert row["state"] == "HOLD"


def test_run_guardian_tick_body_skips_position_on_insufficient_data(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)

    class _EmptyConnector:
        def get_klines(self, symbol, interval, limit=100):
            return []

        def get_funding_rate(self, symbol, limit=1):
            return []

        def get_ticker(self, symbol):
            return _raw_ticker(symbol, "100", "1000000", int(_NOW.timestamp() * 1000))

    observations = run_guardian_tick_body(repo, _EmptyConnector(), _FakeRunner(), _settings(), "run-1", _NOW)

    assert observations == []
    assert repo.find_latest_guardian_observation("pos-1") is None


def test_run_guardian_tick_body_never_touches_positions_table(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _candidate, position = _seed_candidate_and_position(repo)
    before = repo.get_position("pos-1")
    connector = _StubConnector(price="100")

    run_guardian_tick_body(repo, connector, _FakeRunner(), _settings(), "run-1", _NOW)

    after = repo.get_position("pos-1")
    assert after == before


def test_run_guardian_tick_body_still_persists_observation_when_budget_exhausted(tmp_path):
    """Forces a non-HOLD state WITHOUT touching the stub's flat kline data
    (which would also perturb the momentum/volume/funding factors in ways
    that are hard to hand-verify) - instead uses two independently
    controllable, exactly-computable levers: opened_at far enough in the
    past to drive time_decay_factor to exactly 1.0 (elapsed 30h vs.
    risk_limits.max_position_hold_hours=24 -> clipped to 1.0), and a
    lowered watch_decay_threshold so that decay_score's contribution from
    time_decay ALONE (1.0 / 6 equally-weighted factors = 0.1667) is enough
    to cross into WATCH. Every other factor stays at 0 (matching entry
    evidence, per _seed_candidate_and_position's docstring above)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    opened_at = _NOW - timedelta(hours=30)  # exceeds max_position_hold_hours=24 -> time_decay=1.0
    _seed_candidate_and_position(repo, opened_at=opened_at)
    for i in range(600):
        repo.record_ai_call_event(
            Event(event_id=f"AI_CALL_MADE:exhaust:{i}", event_type="AI_CALL_MADE",
                  aggregate_type="candidate", aggregate_id="exhaust", occurred_at=_NOW,
                  run_id="run-0", schema_version=1, payload={"role": "risk", "status": "ok", "cost_usd": "10.00"}),
        )
    connector = _StubConnector(price="100")  # matches entry - only time_decay drives the state here
    settings = _settings().model_copy(
        update={
            "guardian": GuardianConfig(
                watch_decay_threshold=Decimal("0.05"),
                protect_decay_threshold=Decimal("0.5"),
                exit_decay_threshold=Decimal("0.9"),
            )
        }
    )

    observations = run_guardian_tick_body(repo, connector, _FakeRunner(), settings, "run-1", _NOW)

    assert len(observations) == 1
    assert observations[0].state == "WATCH"  # first observation, non-HOLD -> should_invoke_ai() would be True
    assert observations[0].ai_reasoning is None  # ...but budget exhaustion still blocked the call


def test_run_guardian_tick_body_reaches_exit_state_even_when_budget_exhausted(tmp_path):
    """Guardian-assisted exit (2026-09-05, explicit användarkrav: 'ingen
    budget-bypass'). Den deterministiska EXIT-klassificeringen (guardian/
    deterministic.py::classify_guardian_state()) är fri/kostar ingenting -
    bara den TOLKANDE AI-förklaringen (ai_reasoning) är budget-gated (se
    should_invoke_ai()/_budget_allows_one_more_call() ovan). Detta bevisar
    att en tom AI-budget INTE kan blockera/förvränga EXIT-beslutet självt -
    det finns alltså ingen väg att "kringgå" budgeten genom att fler
    positioner stängs: stängning är helt oberoende av om AI-anropet
    lyckas. Samma mönster/lekvärden som test_..._when_budget_exhausted
    ovan, bara med tröskeln satt så lågt att ENBART time_decay (0.1667)
    redan klassificeras som EXIT."""
    repo = SQLiteRepository(tmp_path / "t.db")
    opened_at = _NOW - timedelta(hours=30)  # time_decay klipps till 1.0 (>24h max hold)
    _seed_candidate_and_position(repo, opened_at=opened_at)
    for i in range(600):
        repo.record_ai_call_event(
            Event(event_id=f"AI_CALL_MADE:exhaust:{i}", event_type="AI_CALL_MADE",
                  aggregate_type="candidate", aggregate_id="exhaust", occurred_at=_NOW,
                  run_id="run-0", schema_version=1, payload={"role": "risk", "status": "ok", "cost_usd": "10.00"}),
        )
    connector = _StubConnector(price="100")  # matches entry - only time_decay drives the state
    settings = _settings().model_copy(
        update={
            "guardian": GuardianConfig(
                watch_decay_threshold=Decimal("0.01"),
                protect_decay_threshold=Decimal("0.02"),
                exit_decay_threshold=Decimal("0.1"),  # time_decay alone (0.1667) clears this
            )
        }
    )

    observations = run_guardian_tick_body(repo, connector, _FakeRunner(), settings, "run-1", _NOW)

    assert len(observations) == 1
    assert observations[0].state == "EXIT"  # deterministic classification, unaffected by budget
    assert observations[0].ai_reasoning is None  # budget exhaustion still blocked the AI narration
    assert observations[0].ai_cost_usd is None


# --------------------------------------------------------------------------
# Task 7 (2026-09-14): Guardian Authority tick-time decision wiring
# (TIGHTEN_SL / CLOSE_EARLY), gated by settings.guardian.authority_enabled.
# --------------------------------------------------------------------------


def _authority_settings(tighten_threshold=0.15, close_threshold=0.45):
    """settings.guardian.authority_enabled=True with the two Task-7
    pulled-forward thresholds set explicitly (rather than relying on the
    config module's own defaults), so these tests keep working unchanged if
    Task 10 later retunes the real defaults."""
    return _settings().model_copy(
        update={
            "guardian": GuardianConfig(
                authority_enabled=True,
                authority_tighten_threshold=tighten_threshold,
                authority_close_threshold=close_threshold,
            )
        }
    )


def _seed_always_on_heuristic(repo, heuristic_id="h-1", adjustment=0.2):
    """An "always-on" heuristic (empty condition_json matches every factors
    dict, per guardian/authority.py's own documented semantics) - the
    simplest possible deterministic lever to drive evaluate_heuristics'
    summed score above/below a chosen threshold in these wiring tests,
    without needing to hand-compute specific factor values."""
    repo.upsert_guardian_authority_heuristic(
        heuristic_id=heuristic_id,
        description="always-on test heuristic",
        condition_json="{}",
        adjustment=adjustment,
        confidence=0.8,
        sample_size=10,
        updated_at=_NOW,
    )


def _make_active_live_execution(repo, position_id, entry_quantity="10", avg_entry="100"):
    """Seeds a live_executions row already in phase ACTIVE for an
    already-seeded position - same claim/update_submitted sequence
    test_authority_live.py's own _open_active_live_position helper uses,
    so `repo.get_live_execution(position_id)["phase"] == "ACTIVE"` (the
    exact check process_one_position uses to route LIVE vs PAPER)."""
    repo.claim_live_execution(position_id, _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        position_id, "cid-1", "ex-1", entry_quantity, avg_entry, None, None, _NOW,
    )


def _decision_rows(repo):
    rows = repo._conn.execute("SELECT * FROM guardian_authority_decisions").fetchall()
    return [dict(r) for r in rows]


def _observation_count(repo, position_id):
    row = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM guardian_observations WHERE position_id = ?", (position_id,)
    ).fetchone()
    return row["n"]


def test_authority_flag_off_all_existing_tick_tests_pass_unmodified():
    """Documentation test: the four tests above this section already ARE
    the flag-off byte-identical proof (none of them touch
    settings.guardian.authority_enabled, which defaults to False) - this
    assertion just makes that explicit and machine-checked so a future
    change to the default can't silently flip it without a test noticing."""
    assert _settings().guardian.authority_enabled is False


def test_authority_flag_on_no_heuristics_matched_is_no_action(tmp_path):
    """Flag on, zero heuristics seeded -> evaluate_heuristics' score is 0.0,
    below both thresholds -> NO_ACTION. Behavior must be unchanged from the
    flag-off case: the existing guardian_observations write happens exactly
    as before (same HOLD state, same single row), and no
    guardian_authority_decisions row is created (per the plan's own "only
    actual interventions get a row" ruling)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)
    connector = _StubConnector(price="100")  # unchanged since entry -> HOLD, matches test 1 above

    observations = run_guardian_tick_body(
        repo, connector, _FakeRunner(), _authority_settings(), "run-1", _NOW
    )

    assert len(observations) == 1
    assert observations[0].state == "HOLD"
    row = repo.find_latest_guardian_observation("pos-1")
    assert row["state"] == "HOLD"
    assert _decision_rows(repo) == []


def test_authority_tighten_sl_on_paper_position_calls_tighten_position_stop_loss(tmp_path):
    """Flag on, a matched heuristic pushes the score (0.2) above
    authority_tighten_threshold (0.15) but not above
    authority_close_threshold (0.45) -> TIGHTEN_SL. No ACTIVE
    live_executions row exists for this position (pure PAPER), so the
    PAPER-only path (repo.tighten_position_stop_loss) must be used, never
    apply_live_sl_tightening - proven here by observing the actual effect
    (positions.stop_loss increases) rather than by mocking, plus a decision
    row recorded."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)  # stop_loss=90, simulated_fill_entry=100
    _seed_always_on_heuristic(repo, adjustment=0.2)
    connector = _StubConnector(price="100")

    with patch("crypto_trading.guardian.tick.apply_live_sl_tightening") as mock_live_tighten:
        run_guardian_tick_body(repo, connector, _FakeRunner(), _authority_settings(), "run-1", _NOW)

    mock_live_tighten.assert_not_called()
    position = repo.get_position("pos-1")
    assert position.stop_loss > Decimal("90")  # tightened

    decisions = _decision_rows(repo)
    assert len(decisions) == 1
    assert decisions[0]["decision_type"] == "TIGHTEN_SL"
    assert decisions[0]["position_id"] == "pos-1"


def test_authority_tighten_sl_refused_on_paper_position_logs_event(tmp_path):
    """Final-review fix C1/M1: repo.tighten_position_stop_loss's returned
    bool was previously discarded at this call site, so a refused
    tightening (the DB-level "only ever tighten" guard rejecting the
    proposed value) produced zero visible trace anywhere. Same
    heuristic/score as the successful-PAPER-tighten test above, but
    repo.tighten_position_stop_loss itself is mocked to return False
    (simulating the guard refusing, the same way the review found it could
    silently happen) - proving the call site now captures the return value
    and logs a distinct event instead of silently discarding it, matching
    the existing log_event(...) pattern already used elsewhere in this
    file (e.g. ga_tick_live_sl_tightening_skipped_no_connector)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)  # stop_loss=90, simulated_fill_entry=100
    _seed_always_on_heuristic(repo, adjustment=0.2)
    connector = _StubConnector(price="100")

    with (
        patch.object(repo, "tighten_position_stop_loss", return_value=False) as mock_tighten,
        patch("crypto_trading.guardian.tick.log_event") as mock_log_event,
    ):
        run_guardian_tick_body(repo, connector, _FakeRunner(), _authority_settings(), "run-1", _NOW)

    mock_tighten.assert_called_once()
    refusal_events = [
        call for call in mock_log_event.call_args_list
        if call.kwargs.get("event") == "ga_tick_paper_sl_tighten_refused"
    ]
    assert len(refusal_events) == 1
    assert refusal_events[0].kwargs["position_id"] == "pos-1"

    # The decision row is still recorded - only the visibility of the
    # refused DB write itself was the gap being fixed.
    decisions = _decision_rows(repo)
    assert len(decisions) == 1
    assert decisions[0]["decision_type"] == "TIGHTEN_SL"


def test_authority_tighten_sl_on_active_live_position_calls_apply_live_sl_tightening(tmp_path):
    """Same heuristic/score as the PAPER test above, but this position DOES
    have an ACTIVE live_executions row (repo.get_live_execution(...)["phase"]
    == "ACTIVE") - the exact same check live_profit_protection.py itself
    uses to distinguish a real LIVE position. apply_live_sl_tightening must
    be called instead of the PAPER path (repo.tighten_position_stop_loss),
    and recover_claimed_live_sl_tightenings must have been called first,
    exactly once for the whole tick (not once per position - only one
    position exists in this test, so this also covers the "once per tick"
    shape; the two-position variant is covered separately below)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)
    _make_active_live_execution(repo, "pos-1")
    _seed_always_on_heuristic(repo, adjustment=0.2)
    connector = _StubConnector(price="100")
    live_connector = object()  # never actually touched: apply_live_sl_tightening is mocked below

    with (
        patch("crypto_trading.guardian.tick.apply_live_sl_tightening") as mock_live_tighten,
        patch("crypto_trading.guardian.tick.recover_claimed_live_sl_tightenings") as mock_recover,
    ):
        run_guardian_tick_body(
            repo, connector, _FakeRunner(), _authority_settings(), "run-1", _NOW, live_connector,
        )

    mock_recover.assert_called_once_with(repo, live_connector, "run-1", _NOW)
    mock_live_tighten.assert_called_once()
    call_args = mock_live_tighten.call_args.args
    assert call_args[0] is repo
    assert call_args[1] is live_connector
    assert call_args[2] == "pos-1"
    assert call_args[3] == "BTCUSDT"
    assert call_args[4] > Decimal("90")  # proposed_sl, strictly greater than current stop_loss

    # PAPER path must NOT have run: positions.stop_loss is untouched (LIVE
    # tightening is applied to the real exchange order, never this column).
    assert repo.get_position("pos-1").stop_loss == Decimal("90")


def test_authority_tighten_sl_on_active_live_position_with_no_connector_skips_with_diagnostic(
    tmp_path,
):
    """Task 10 diagnostic fix: an ACTIVE live_executions row exists (a real
    LIVE position) but live_connector is None (a real misconfiguration -
    e.g. LIVE execution disabled at startup after some positions were
    already opened LIVE). Before this fix, this fell through to
    apply_live_sl_tightening(repo, None, ...) and failed via a generic
    AttributeError; now it must be skipped entirely - apply_live_sl_tightening
    is never even called - with a distinct diagnostic log event instead.
    The fail-safe OUTCOME is unchanged either way: no order is placed, and
    the PAPER-only path (repo.tighten_position_stop_loss) must NOT be used
    as a fallback (this is a LIVE position; silently tightening the local
    positions.stop_loss column would desync it from the real exchange
    order, which nothing then re-tightens)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)
    _make_active_live_execution(repo, "pos-1")
    _seed_always_on_heuristic(repo, adjustment=0.2)
    connector = _StubConnector(price="100")

    with (
        patch("crypto_trading.guardian.tick.apply_live_sl_tightening") as mock_live_tighten,
        patch("crypto_trading.guardian.tick.log_event") as mock_log_event,
    ):
        run_guardian_tick_body(
            repo, connector, _FakeRunner(), _authority_settings(), "run-1", _NOW,
            None,  # live_connector explicitly None
        )

    mock_live_tighten.assert_not_called()
    skip_events = [
        call for call in mock_log_event.call_args_list
        if call.kwargs.get("event") == "ga_tick_live_sl_tightening_skipped_no_connector"
    ]
    assert len(skip_events) == 1
    assert skip_events[0].kwargs["position_id"] == "pos-1"

    # Neither write path ran: real exchange order untouched (nothing to
    # assert there directly - connector is never contacted), and the local
    # positions.stop_loss column is also untouched (no silent PAPER-path
    # fallback for what is really a LIVE position).
    assert repo.get_position("pos-1").stop_loss == Decimal("90")

    # The decision row itself is still recorded - only the LIVE application
    # step is skipped, exactly like the existing exception-handling branch.
    decisions = _decision_rows(repo)
    assert len(decisions) == 1
    assert decisions[0]["decision_type"] == "TIGHTEN_SL"


def test_authority_recover_claimed_live_sl_tightenings_once_per_tick(tmp_path):
    """Two open positions, both ACTIVE LIVE, both driven to TIGHTEN_SL by
    the same always-on heuristic - recover_claimed_live_sl_tightenings must
    still be invoked exactly ONCE for the whole tick (before the
    per-position loop), never once per position."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo, position_id="pos-1")
    _seed_candidate_and_position(repo, position_id="pos-2")
    _make_active_live_execution(repo, "pos-1")
    _make_active_live_execution(repo, "pos-2")
    _seed_always_on_heuristic(repo, adjustment=0.2)
    connector = _StubConnector(price="100")
    live_connector = object()

    with (
        patch("crypto_trading.guardian.tick.apply_live_sl_tightening"),
        patch("crypto_trading.guardian.tick.recover_claimed_live_sl_tightenings") as mock_recover,
    ):
        run_guardian_tick_body(
            repo, connector, _FakeRunner(), _authority_settings(), "run-1", _NOW, live_connector,
        )

    mock_recover.assert_called_once_with(repo, live_connector, "run-1", _NOW)


def test_authority_close_early_saves_single_exit_observation_and_decision_row(tmp_path):
    """Flag on, a matched heuristic pushes the score (0.5) above
    authority_close_threshold (0.45) -> CLOSE_EARLY, evaluated before/
    instead of TIGHTEN_SL (decide_open_position's own documented
    precedence). Must write via the exact same mechanism the deterministic
    EXIT state already uses (repo.save_guardian_observation with
    state="EXIT") - no new closing code - and must save EXACTLY ONE
    observation for this position/tick (not the normal end-of-function
    observation AND the EXIT one)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)
    _seed_always_on_heuristic(repo, adjustment=0.5)
    connector = _StubConnector(price="100")

    observations = run_guardian_tick_body(
        repo, connector, _FakeRunner(), _authority_settings(), "run-1", _NOW
    )

    assert len(observations) == 1
    assert observations[0].state == "EXIT"
    assert observations[0].observation_id.startswith("ga-exit:")

    assert _observation_count(repo, "pos-1") == 1  # no duplicate save
    row = repo.find_latest_guardian_observation("pos-1")
    assert row["state"] == "EXIT"

    decisions = _decision_rows(repo)
    assert len(decisions) == 1
    assert decisions[0]["decision_type"] == "CLOSE_EARLY"


def test_authority_live_tightening_exception_does_not_abort_the_rest_of_the_batch(tmp_path):
    """A LIVE tightening attempt that raises must not abort the rest of the
    tick: pos-1 is ACTIVE LIVE and its apply_live_sl_tightening call raises;
    pos-2 is a plain PAPER position driven to TIGHTEN_SL by the same
    heuristic and must still be processed normally (its stop_loss still
    gets tightened via the PAPER path) despite pos-1's failure."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo, position_id="pos-1")
    _seed_candidate_and_position(repo, position_id="pos-2")
    _make_active_live_execution(repo, "pos-1")  # pos-2 stays pure PAPER
    _seed_always_on_heuristic(repo, adjustment=0.2)
    connector = _StubConnector(price="100")
    live_connector = object()

    with (
        patch(
            "crypto_trading.guardian.tick.apply_live_sl_tightening",
            side_effect=RuntimeError("simulated exchange failure"),
        ) as mock_live_tighten,
        patch("crypto_trading.guardian.tick.recover_claimed_live_sl_tightenings"),
    ):
        observations = run_guardian_tick_body(
            repo, connector, _FakeRunner(), _authority_settings(), "run-1", _NOW, live_connector,
        )

    mock_live_tighten.assert_called_once()  # only pos-1 is LIVE - raised, but did not propagate
    # pos-2 (PAPER) was still processed normally despite pos-1's failure.
    assert repo.get_position("pos-2").stop_loss > Decimal("90")
    # pos-1's own decision row was still saved (the exception is only in the
    # LIVE application step, which comes after the decision is recorded).
    decision_position_ids = {d["position_id"] for d in _decision_rows(repo)}
    assert decision_position_ids == {"pos-1", "pos-2"}
    assert len(observations) == 2  # both positions still produced/returned an observation


# --------------------------------------------------------------------------
# Task 9 (2026-09-14): self-critique wiring - resolve_pending_decisions and
# update_heuristics_from_resolved_decisions, gated by
# settings.guardian.authority_enabled, called once per tick (above the
# per-position loop, same shape/placement as
# recover_claimed_live_sl_tightenings above), with
# update_heuristics_from_resolved_decisions only called when
# resolve_pending_decisions actually resolved >= 1 decision this tick.
# --------------------------------------------------------------------------


def test_authority_wiring_calls_resolve_and_update_heuristics_when_something_resolves(tmp_path):
    """Flag on, a tick that resolves >= 1 decision also calls
    update_heuristics_from_resolved_decisions in the SAME tick, with the
    SAME `now`."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)
    connector = _StubConnector(price="100")

    with (
        patch(
            "crypto_trading.guardian.tick.resolve_pending_decisions", return_value=2
        ) as mock_resolve,
        patch(
            "crypto_trading.guardian.tick.update_heuristics_from_resolved_decisions"
        ) as mock_update,
    ):
        run_guardian_tick_body(repo, connector, _FakeRunner(), _authority_settings(), "run-1", _NOW)

    mock_resolve.assert_called_once_with(repo, _NOW)
    mock_update.assert_called_once_with(repo, _NOW)


def test_authority_wiring_skips_update_heuristics_when_nothing_resolved(tmp_path):
    """Flag on, a tick that resolves 0 decisions does NOT call
    update_heuristics_from_resolved_decisions - re-deriving heuristics from
    unchanged data would be wasted effort."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)
    connector = _StubConnector(price="100")

    with (
        patch(
            "crypto_trading.guardian.tick.resolve_pending_decisions", return_value=0
        ) as mock_resolve,
        patch(
            "crypto_trading.guardian.tick.update_heuristics_from_resolved_decisions"
        ) as mock_update,
    ):
        run_guardian_tick_body(repo, connector, _FakeRunner(), _authority_settings(), "run-1", _NOW)

    mock_resolve.assert_called_once_with(repo, _NOW)
    mock_update.assert_not_called()


def test_authority_wiring_flag_off_never_calls_resolve_or_update_heuristics(tmp_path):
    """Flag off (default): neither resolve_pending_decisions nor
    update_heuristics_from_resolved_decisions is ever called - flag-off must
    remain byte-identical to before this task existed."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_candidate_and_position(repo)
    connector = _StubConnector(price="100")

    with (
        patch("crypto_trading.guardian.tick.resolve_pending_decisions") as mock_resolve,
        patch(
            "crypto_trading.guardian.tick.update_heuristics_from_resolved_decisions"
        ) as mock_update,
    ):
        run_guardian_tick_body(repo, connector, _FakeRunner(), _settings(), "run-1", _NOW)

    mock_resolve.assert_not_called()
    mock_update.assert_not_called()
