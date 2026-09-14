import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from crypto_trading.config.loader import (
    BudgetLimitsConfig,
    DashboardConfig,
    GuardianConfig,
    NotifyConfig,
    PipelineConfig,
    RiskLimitsConfig,
    Settings,
)
from crypto_trading.guardian.authority import (
    _reconstruct_tighten_sl_factors,
    decide_open_position,
    decide_pre_entry,
    evaluate_heuristics,
    maybe_open_position_for_candidate,
    resolve_pending_decisions,
    update_heuristics_from_resolved_decisions,
)
from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.schemas.assessments import RiskAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository


def _heuristic(
    heuristic_id="h-1",
    condition=None,
    adjustment=0.15,
    confidence=0.75,
    description="test heuristic",
    sample_size=10,
):
    return {
        "heuristic_id": heuristic_id,
        "description": description,
        "condition_json": json.dumps(condition if condition is not None else {}),
        "adjustment": adjustment,
        "confidence": confidence,
        "sample_size": sample_size,
        "updated_at": "2026-09-14T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# evaluate_heuristics / condition matching
# ---------------------------------------------------------------------------


def test_list_membership_condition_matches_on_overlap():
    h = _heuristic(condition={"trigger_reasons": ["momentum_breakout", "volume_spike"]})
    factors = {"trigger_reasons": ["momentum_breakout"]}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == ["h-1"]
    assert score == pytest.approx(0.15)


def test_list_membership_condition_does_not_match_without_overlap():
    h = _heuristic(condition={"trigger_reasons": ["momentum_breakout"]})
    factors = {"trigger_reasons": ["volume_spike"]}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []
    assert score == pytest.approx(0.0)


def test_list_membership_condition_matches_scalar_factor_value():
    h = _heuristic(condition={"instrument_class": ["majors", "midcaps"]})
    factors = {"instrument_class": "majors"}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == ["h-1"]


def test_empty_condition_list_never_matches():
    h = _heuristic(condition={"trigger_reasons": []})
    factors = {"trigger_reasons": ["momentum_breakout"]}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []


def test_numeric_max_condition_matches_at_or_below_bound():
    h = _heuristic(condition={"candidate_score_max": 0.1})
    factors = {"candidate_score": 0.1}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == ["h-1"]


def test_numeric_max_condition_does_not_match_above_bound():
    h = _heuristic(condition={"candidate_score_max": 0.1})
    factors = {"candidate_score": 0.2}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []


def test_numeric_min_condition_matches_at_or_above_bound():
    h = _heuristic(condition={"decay_score_min": 0.5})
    factors = {"decay_score": 0.5}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == ["h-1"]


def test_numeric_min_condition_does_not_match_below_bound():
    h = _heuristic(condition={"decay_score_min": 0.5})
    factors = {"decay_score": 0.4}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []


def test_scalar_equality_condition_matches():
    h = _heuristic(condition={"guardian_state": "PROTECT"})
    factors = {"guardian_state": "PROTECT"}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == ["h-1"]


def test_scalar_equality_condition_does_not_match():
    h = _heuristic(condition={"guardian_state": "PROTECT"})
    factors = {"guardian_state": "HOLD"}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []


def test_missing_factor_key_fails_closed():
    h = _heuristic(condition={"candidate_score_max": 0.1})
    factors = {}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []


def test_multi_key_condition_requires_all_keys_satisfied():
    h = _heuristic(
        condition={"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.1}
    )
    # trigger_reasons matches, candidate_score_max does not
    factors = {"trigger_reasons": ["momentum_breakout"], "candidate_score": 0.5}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == []


def test_multi_key_condition_matches_when_all_keys_satisfied():
    h = _heuristic(
        condition={"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.1}
    )
    factors = {"trigger_reasons": ["momentum_breakout"], "candidate_score": 0.05}

    score, matched = evaluate_heuristics(factors, [h])

    assert matched == ["h-1"]


def test_multiple_matching_heuristics_sum_adjustments():
    h1 = _heuristic(heuristic_id="h-1", condition={}, adjustment=0.15)
    h2 = _heuristic(heuristic_id="h-2", condition={}, adjustment=0.30)
    factors = {}

    score, matched = evaluate_heuristics(factors, [h1, h2])

    assert score == pytest.approx(0.45)
    assert matched == ["h-1", "h-2"]


def test_non_matching_heuristics_are_excluded_from_score():
    h1 = _heuristic(heuristic_id="h-1", condition={}, adjustment=0.15)
    h2 = _heuristic(heuristic_id="h-2", condition={"guardian_state": "EXIT"}, adjustment=0.9)
    factors = {"guardian_state": "HOLD"}

    score, matched = evaluate_heuristics(factors, [h1, h2])

    assert score == pytest.approx(0.15)
    assert matched == ["h-1"]


def test_empty_condition_matches_universally():
    h = _heuristic(condition={})

    score, matched = evaluate_heuristics({"anything": "goes"}, [h])

    assert matched == ["h-1"]


def test_syntactically_invalid_condition_json_raises_json_decode_error():
    h = _heuristic()
    h["condition_json"] = "{not valid json"

    with pytest.raises(json.JSONDecodeError):
        evaluate_heuristics({}, [h])


@pytest.mark.parametrize("non_object_json", ["[]", "null", "3", '"x"'])
def test_valid_non_object_condition_json_raises_attribute_error(non_object_json):
    h = _heuristic()
    h["condition_json"] = non_object_json

    with pytest.raises(AttributeError):
        evaluate_heuristics({}, [h])


# ---------------------------------------------------------------------------
# decide_pre_entry
# ---------------------------------------------------------------------------


def test_decide_pre_entry_approves_when_no_heuristics_match():
    decision, text, direction, confidence = decide_pre_entry(
        candidate_evidence={"trigger_reasons": ["momentum_breakout"]},
        heuristics=[],
        veto_threshold=0.5,
    )

    assert decision == "APPROVE"
    assert direction == "neutral"
    assert confidence == pytest.approx(1.0)
    assert isinstance(text, str) and text


def test_decide_pre_entry_approves_at_exact_threshold():
    h = _heuristic(condition={}, adjustment=0.5, confidence=0.6)

    decision, text, direction, confidence = decide_pre_entry(
        candidate_evidence={}, heuristics=[h], veto_threshold=0.5
    )

    assert decision == "APPROVE"


def test_decide_pre_entry_vetoes_when_score_exceeds_threshold():
    h = _heuristic(
        condition={"candidate_score_max": 0.1},
        adjustment=0.6,
        confidence=0.8,
        description="poor historical outcomes for low-score candidates",
    )

    decision, text, direction, confidence = decide_pre_entry(
        candidate_evidence={"candidate_score": 0.05},
        heuristics=[h],
        veto_threshold=0.5,
    )

    assert decision == "PRE_ENTRY_VETO"
    assert direction == "unfavorable"
    assert confidence == pytest.approx(0.8)
    assert "poor historical outcomes" in text


def test_decide_pre_entry_confidence_is_weighted_by_adjustment_magnitude():
    h1 = _heuristic(heuristic_id="h-1", condition={}, adjustment=0.9, confidence=0.9)
    h2 = _heuristic(heuristic_id="h-2", condition={}, adjustment=0.1, confidence=0.1)
    # score = 1.0, exceeds threshold -> VETO
    decision, text, direction, confidence = decide_pre_entry(
        candidate_evidence={}, heuristics=[h1, h2], veto_threshold=0.5
    )

    assert decision == "PRE_ENTRY_VETO"
    # weighted average: (0.9*0.9 + 0.1*0.1) / (0.9+0.1) = 0.82
    assert confidence == pytest.approx(0.82)


# ---------------------------------------------------------------------------
# decide_open_position - NO_ACTION / TIGHTEN_SL / CLOSE_EARLY
# ---------------------------------------------------------------------------


def test_decide_open_position_no_action_when_score_below_all_thresholds():
    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={"decay_score": 0.1},
        guardian_state="HOLD",
        current_sl=Decimal("90"),
        entry=Decimal("100"),
        heuristics=[],
        tighten_threshold=0.3,
        close_threshold=0.6,
    )

    assert decision == "NO_ACTION"
    assert direction == "neutral"
    assert proposed_sl is None
    assert confidence == pytest.approx(1.0)


def test_decide_open_position_tightens_sl_when_score_between_thresholds():
    h = _heuristic(
        condition={"guardian_state": "WATCH"},
        adjustment=0.4,
        confidence=0.7,
        description="watch-state momentum loss",
    )

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="WATCH",
        current_sl=Decimal("90"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=0.6,
    )

    assert decision == "TIGHTEN_SL"
    assert direction == "favorable"
    assert proposed_sl is not None
    assert proposed_sl > Decimal("90")
    assert proposed_sl <= Decimal("100")
    assert confidence == pytest.approx(0.7)
    assert "watch-state momentum loss" in text


def test_decide_open_position_closes_early_when_score_exceeds_close_threshold():
    h = _heuristic(
        condition={"guardian_state": "EXIT"},
        adjustment=0.9,
        confidence=0.85,
        description="exit-state deep decay",
    )

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="EXIT",
        current_sl=Decimal("90"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=0.6,
    )

    assert decision == "CLOSE_EARLY"
    assert direction == "unfavorable"
    assert proposed_sl is None
    assert confidence == pytest.approx(0.85)


def test_decide_open_position_close_takes_precedence_over_tighten():
    # score exceeds BOTH thresholds -> must resolve to CLOSE_EARLY, not TIGHTEN_SL
    h = _heuristic(condition={}, adjustment=0.95, confidence=0.9)

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="EXIT",
        current_sl=Decimal("90"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=0.6,
    )

    assert decision == "CLOSE_EARLY"
    assert proposed_sl is None


# ---------------------------------------------------------------------------
# THE CRITICAL SAFETY TEST
# ---------------------------------------------------------------------------


def test_decide_open_position_never_returns_invalid_tighten_sl():
    """Adversarial fixture: heuristics score comfortably above tighten_threshold
    (so the decision logic WOULD tighten), but current_sl is already at/above
    entry (e.g. Profit Protection already moved it to break-even or beyond),
    so this module's own internal proposed-SL computation has no room to
    move current_sl any closer to entry. The function must catch this
    itself and downgrade to NO_ACTION - it must NEVER return TIGHTEN_SL
    paired with a proposed_new_sl that is not strictly greater than
    current_sl.
    """
    h = _heuristic(
        condition={"guardian_state": "WATCH"},
        adjustment=0.5,
        confidence=0.7,
        description="would normally trigger a tighten",
    )

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="WATCH",
        current_sl=Decimal("105"),  # already AT/ABOVE entry - no room to tighten toward entry
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=0.9,
    )

    assert decision == "NO_ACTION"
    assert proposed_sl is None
    # Never, under any circumstance, TIGHTEN_SL with a non-strictly-greater SL.
    assert not (decision == "TIGHTEN_SL" and (proposed_sl is None or proposed_sl <= Decimal("105")))


def test_decide_open_position_never_returns_invalid_tighten_sl_when_current_sl_equals_entry():
    """Same adversarial class, boundary case: current_sl == entry exactly
    (zero gap) rather than current_sl > entry."""
    h = _heuristic(condition={}, adjustment=0.5, confidence=0.7)

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="WATCH",
        current_sl=Decimal("100"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=0.9,
    )

    assert decision == "NO_ACTION"
    assert proposed_sl is None


def test_decide_open_position_invalid_tighten_downgrade_is_observable_not_identical_to_no_signal():
    """The downgrade path should still be observable (text mentions why),
    not silently identical to a genuine no-signal NO_ACTION - useful for
    debugging/monitoring, and proves the downgrade branch actually ran
    rather than the score simply never having crossed the threshold."""
    h = _heuristic(
        condition={},
        adjustment=0.5,
        confidence=0.7,
        description="would normally trigger a tighten",
    )

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="WATCH",
        current_sl=Decimal("105"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=0.9,
    )

    assert decision == "NO_ACTION"
    assert "would normally trigger a tighten" in text
    assert confidence == pytest.approx(0.7)


def test_decide_open_position_valid_tighten_never_exceeds_entry():
    h = _heuristic(condition={}, adjustment=100.0, confidence=0.5)  # extreme score

    decision, text, direction, confidence, proposed_sl = decide_open_position(
        position_factors={},
        guardian_state="WATCH",
        current_sl=Decimal("90"),
        entry=Decimal("100"),
        heuristics=[h],
        tighten_threshold=0.3,
        close_threshold=1000.0,  # keep it below close threshold
    )

    assert decision == "TIGHTEN_SL"
    assert proposed_sl > Decimal("90")
    assert proposed_sl <= Decimal("100")


# ---------------------------------------------------------------------------
# maybe_open_position_for_candidate (Task 6: pre-entry veto wiring)
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def _settings(guardian: GuardianConfig | None = None) -> Settings:
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
        risk_limits=_risk_limits(),
        budget_limits=BudgetLimitsConfig(
            max_candidates_per_discovery_run=10,
            max_ai_calls_per_discovery_run=70,
            max_ai_calls_per_day=500,
            warning_threshold_pct=Decimal("0.8"),
        ),
        notify=NotifyConfig(notification_level="important", notify_interval_seconds=60),
        dashboard=DashboardConfig(host="127.0.0.1", port=8000),
        guardian=guardian if guardian is not None else GuardianConfig(),
    )


def _risk_limits(**overrides) -> RiskLimitsConfig:
    defaults = dict(
        starting_capital_usdt=Decimal("10000"), risk_per_trade_pct=Decimal("0.01"),
        max_concurrent_positions=5, max_total_exposure_pct=Decimal("1.0"),
        max_position_notional_usdt=Decimal("1000000"), spread_pct=Decimal("0.0005"),
        slippage_pct=Decimal("0.0005"), fee_pct=Decimal("0.0004"), max_position_hold_hours=24,
    )
    defaults.update(overrides)
    return RiskLimitsConfig(**defaults)


def _evidence(candidate_score=0.05, trigger_reasons=None) -> CandidateEvidenceRecord:
    placeholder = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    return CandidateEvidenceRecord(
        instrument="BTCUSDT", timeframes=["1h"], evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=candidate_score,
        trigger_reasons=trigger_reasons if trigger_reasons is not None else ["momentum_breakout"],
        data_quality_status="ok", outcome="worth_deeper_analysis",
    )


def _confirmed_candidate(
    candidate_id="cand-1", candidate_score=0.05, trigger_reasons=None
) -> Candidate:
    return Candidate(
        candidate_id=candidate_id, idempotency_key=f"key-{candidate_id}", instrument="BTCUSDT",
        discovery_run_id="run-1", evidence_hash="hash-1", status="CONFIRMED",
        evidence_record=_evidence(candidate_score, trigger_reasons),
        created_at=_NOW, updated_at=_NOW,
        risk=RiskAssessment(
            agent_name="crypto-risk-agent", run_id="run-1", created_at=_NOW, status="ok",
            suggested_stop_loss="49000", suggested_target="52000",
            downside="d", liquidity_risk="l", model_risk="m", timing_risk="t",
        ),
    )


def _decision_count(repo: SQLiteRepository) -> int:
    row = repo._conn.execute("SELECT COUNT(*) AS n FROM guardian_authority_decisions").fetchone()
    return row["n"]


def test_maybe_open_position_flag_off_is_byte_identical_to_direct_call(tmp_path):
    """Default OFF (settings.guardian.authority_enabled is False): the
    wrapper must be a pure passthrough - same Position, no decision row,
    heuristics never even read (no I/O beyond open_position_for_candidate
    itself)."""
    from crypto_trading.paper_trading.position_opening import open_position_for_candidate

    repo_direct = SQLiteRepository(tmp_path / "direct.db")
    repo_wrapped = SQLiteRepository(tmp_path / "wrapped.db")
    candidate = _confirmed_candidate()
    settings = _settings(GuardianConfig(authority_enabled=False))

    direct = open_position_for_candidate(
        candidate, repo_direct, settings.risk_limits, Decimal("50000"), _NOW, "run-1"
    )
    wrapped = maybe_open_position_for_candidate(
        repo_wrapped, candidate, settings.risk_limits, Decimal("50000"), _NOW, "run-1", settings
    )

    assert wrapped is not None
    assert wrapped.model_dump() == direct.model_dump()
    assert _decision_count(repo_wrapped) == 0


def test_maybe_open_position_flag_on_approve_opens_position_with_no_decision_row(tmp_path):
    """Flag True, zero heuristics match -> decide_pre_entry defaults to
    APPROVE: position opens exactly as before, and - per the plan's own
    'only actual interventions get a row' ruling - no decision row is
    saved for an APPROVE."""
    repo = SQLiteRepository(tmp_path / "t.db")
    candidate = _confirmed_candidate()
    settings = _settings(GuardianConfig(authority_enabled=True, authority_veto_threshold=0.3))

    position = maybe_open_position_for_candidate(
        repo, candidate, settings.risk_limits, Decimal("50000"), _NOW, "run-1", settings
    )

    assert position is not None
    assert position.position_id == "cand-1"
    assert repo.get_position("cand-1") is not None
    assert _decision_count(repo) == 0


def test_maybe_open_position_flag_on_veto_never_opens_a_position(tmp_path):
    """Flag True, a matched heuristic pushes the score past veto_threshold
    -> PRE_ENTRY_VETO: open_position_for_candidate must never be called at
    all (asserted via spy), no positions row is created, and a
    PRE_ENTRY_VETO decision row exists with position_id=None and a
    non-null expected_outcome."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.upsert_guardian_authority_heuristic(
        heuristic_id="h-veto-1",
        description="momentum breakout at a very low candidate_score has historically lost",
        condition_json=json.dumps(
            {"trigger_reasons": ["momentum_breakout"], "candidate_score_max": 0.1}
        ),
        adjustment=0.5,
        confidence=0.8,
        sample_size=20,
        updated_at=_NOW,
    )
    candidate = _confirmed_candidate(candidate_score=0.05, trigger_reasons=["momentum_breakout"])
    settings = _settings(GuardianConfig(authority_enabled=True, authority_veto_threshold=0.3))

    with patch(
        "crypto_trading.guardian.authority.open_position_for_candidate"
    ) as mock_open:
        position = maybe_open_position_for_candidate(
            repo, candidate, settings.risk_limits, Decimal("50000"), _NOW, "run-1", settings
        )

    assert position is None
    mock_open.assert_not_called()
    assert repo.get_position("cand-1") is None
    row = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM positions"
    ).fetchone()
    assert row["n"] == 0

    decisions = repo._conn.execute("SELECT * FROM guardian_authority_decisions").fetchall()
    assert len(decisions) == 1
    decision = dict(decisions[0])
    assert decision["decision_type"] == "PRE_ENTRY_VETO"
    assert decision["position_id"] is None
    assert decision["candidate_id"] == "cand-1"
    assert decision["expected_outcome"]  # non-null/non-empty
    assert decision["expected_direction"] == "unfavorable"


# --------------------------------------------------------------------------
# Task 8: resolve_pending_decisions
#
# Controller ruling (see task-8-brief.md, "A controller ruling you must
# follow exactly"): the brief's literal instruction - sign-compare
# expected_direction against actual P/L for every pending decision - is
# only valid for TIGHTEN_SL. CLOSE_EARLY's expected_direction predicts a
# counterfactual of inaction that the realized P/L (of the close itself)
# doesn't measure, so CLOSE_EARLY rows get expectation_correct=None
# instead. PRE_ENTRY_VETO rows have position_id=None (get_position(None)
# returns None the same as a real missing position) and are skipped
# forever - a deliberate, permanent scope limit, not a bug.
# --------------------------------------------------------------------------


def _open_position(
    repo: SQLiteRepository,
    position_id: str,
    fill_entry="50000",
    stop_loss="49000",
    target="52000",
    size="5000",
) -> Position:
    position = Position(
        position_id=position_id,
        candidate_id=position_id,
        instrument="BTCUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry=fill_entry,
        simulated_fill_entry=fill_entry,
        stop_loss=stop_loss,
        target=target,
        size=size,
        fill_model_version="v1",
        opened_at=_NOW,
    )
    event = Event(
        event_id=f"POS_OPENED:{position_id}",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id=position_id,
        occurred_at=_NOW,
        run_id="run-1",
        schema_version=1,
        payload={"instrument": position.instrument},
    )
    repo.create_position_with_event(position, event)
    return position


def _close_position(
    repo: SQLiteRepository,
    position_id: str,
    fill_exit: str,
    exit_reason="target",
    fees="1",
    funding="0",
    closed_at=None,
) -> None:
    closed_at = closed_at or (_NOW + timedelta(hours=1))
    event = Event(
        event_id=f"POS_CLOSED:{position_id}",
        event_type="POSITION_CLOSED",
        aggregate_type="position",
        aggregate_id=position_id,
        occurred_at=closed_at,
        run_id="run-1",
        schema_version=1,
        payload={"exit_reason": exit_reason},
    )
    ok = repo.close_position_with_event(
        position_id,
        Decimal(fill_exit),
        Decimal(fill_exit),
        exit_reason,
        Decimal(fees),
        Decimal(funding),
        closed_at,
        event,
    )
    assert ok is True


def test_resolve_pending_decisions_skips_still_open_position(tmp_path):
    """AC1: a PENDING TIGHTEN_SL decision whose position is still open is
    left completely untouched - stays PENDING, not resolved."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-1")
    repo.save_guardian_authority_decision(
        "ga-1", "pos-1", "cand-1", "TIGHTEN_SL", _NOW,
        "reasoning", "expect small favorable move", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500",
    )

    count = resolve_pending_decisions(repo, _NOW + timedelta(hours=1))

    assert count == 0
    row = repo.get_guardian_authority_decision("ga-1")
    assert row["outcome_status"] == "PENDING"
    assert row["actual_exit_reason"] is None
    assert row["actual_pnl_usdt"] is None
    assert row["expectation_correct"] is None
    assert row["resolved_at"] is None


def test_resolve_pending_decisions_tighten_sl_profitable_close_is_correct(tmp_path):
    """AC2: TIGHTEN_SL, expected_direction='favorable', position closed
    profitably -> resolved, expectation_correct=True."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-2")
    _close_position(repo, "pos-2", fill_exit="51000", exit_reason="target", fees="2", funding="1")
    closed_position = repo.get_position("pos-2")
    expected_pnl = compute_pnl(closed_position)
    assert expected_pnl > 0  # sanity: this scenario really is profitable
    repo.save_guardian_authority_decision(
        "ga-2", "pos-2", "cand-2", "TIGHTEN_SL", _NOW,
        "reasoning", "expect small favorable move", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500",
    )
    resolved_at = _NOW + timedelta(hours=2)

    count = resolve_pending_decisions(repo, resolved_at)

    assert count == 1
    row = repo.get_guardian_authority_decision("ga-2")
    assert row["outcome_status"] == "RESOLVED"
    assert row["actual_exit_reason"] == "target"
    assert row["actual_pnl_usdt"] == str(expected_pnl)
    assert row["expectation_correct"] == 1
    assert row["resolved_at"] == resolved_at.isoformat()


def test_resolve_pending_decisions_tighten_sl_losing_close_is_incorrect(tmp_path):
    """AC3 (adapted per the controller ruling - see module comment above):
    same TIGHTEN_SL/expected_direction='favorable' setup, but the position
    closes at a LOSS -> resolved, expectation_correct=False. Proves the
    False branch of the sign comparison without inventing an
    'unfavorable'+TIGHTEN_SL combination Task 3 never actually produces."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-3")
    _close_position(
        repo, "pos-3", fill_exit="49000", exit_reason="stop_loss", fees="2", funding="1"
    )
    closed_position = repo.get_position("pos-3")
    expected_pnl = compute_pnl(closed_position)
    assert expected_pnl < 0  # sanity: this scenario really is a loss
    repo.save_guardian_authority_decision(
        "ga-3", "pos-3", "cand-3", "TIGHTEN_SL", _NOW,
        "reasoning", "expect small favorable move", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500",
    )
    resolved_at = _NOW + timedelta(hours=2)

    count = resolve_pending_decisions(repo, resolved_at)

    assert count == 1
    row = repo.get_guardian_authority_decision("ga-3")
    assert row["outcome_status"] == "RESOLVED"
    assert row["actual_exit_reason"] == "stop_loss"
    assert row["actual_pnl_usdt"] == str(expected_pnl)
    assert row["expectation_correct"] == 0
    assert row["resolved_at"] == resolved_at.isoformat()


def test_resolve_pending_decisions_never_mutates_the_pre_decision_expectation(tmp_path):
    """Re-run of Task 1's own immutability test, at this integration level:
    resolve_pending_decisions must never change expected_outcome/
    expected_direction/confidence/decided_at - assert byte-identical
    before and after."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-4")
    _close_position(repo, "pos-4", fill_exit="51000", exit_reason="target", fees="2", funding="1")
    repo.save_guardian_authority_decision(
        "ga-4", "pos-4", "cand-4", "TIGHTEN_SL", _NOW,
        "decay accelerating on weak volume", "expect small favorable move",
        "favorable", 0.65, "run-1", old_sl="49000", new_sl="49500",
    )
    before = repo.get_guardian_authority_decision("ga-4")

    count = resolve_pending_decisions(repo, _NOW + timedelta(hours=2))

    assert count == 1
    after = repo.get_guardian_authority_decision("ga-4")
    assert after["expected_outcome"] == before["expected_outcome"] == "expect small favorable move"
    assert after["expected_direction"] == before["expected_direction"] == "favorable"
    assert after["confidence"] == before["confidence"] == 0.65
    assert after["decided_at"] == before["decided_at"] == _NOW.isoformat()
    assert after["outcome_status"] == "RESOLVED"


def test_resolve_pending_decisions_close_early_fills_actuals_but_leaves_expectation_unknown(
    tmp_path,
):
    """AC5: a CLOSE_EARLY decision whose position has closed is resolved
    (real actual_exit_reason/actual_pnl_usdt filled in) but
    expectation_correct is explicitly None - per the controller ruling,
    the realized P/L of the early close itself doesn't validly measure the
    counterfactual expected_direction='unfavorable' actually predicts."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-5")
    _close_position(
        repo, "pos-5", fill_exit="50100", exit_reason="GUARDIAN_EXIT", fees="2", funding="1"
    )
    closed_position = repo.get_position("pos-5")
    expected_pnl = compute_pnl(closed_position)
    repo.save_guardian_authority_decision(
        "ga-5", "pos-5", "cand-5", "CLOSE_EARLY", _NOW,
        "reasoning", "expect unfavorable if left open", "unfavorable", 0.9, "run-1",
    )

    count = resolve_pending_decisions(repo, _NOW + timedelta(hours=2))

    assert count == 1
    row = repo.get_guardian_authority_decision("ga-5")
    assert row["outcome_status"] == "RESOLVED"
    assert row["actual_exit_reason"] == "GUARDIAN_EXIT"
    assert row["actual_pnl_usdt"] == str(expected_pnl)
    assert row["expectation_correct"] is None


def test_resolve_pending_decisions_skips_pre_entry_veto_forever(tmp_path):
    """AC6: a PRE_ENTRY_VETO decision has position_id=None (no position was
    ever opened). resolve_pending_decisions must never crash on this and
    must never resolve it - it stays PENDING forever, every time this
    function runs."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_decision(
        "ga-veto", None, "cand-6", "PRE_ENTRY_VETO", _NOW,
        "poor historical pattern match", "expect unfavorable if opened", "unfavorable",
        0.8, "run-1",
    )

    count_1 = resolve_pending_decisions(repo, _NOW + timedelta(hours=1))
    count_2 = resolve_pending_decisions(repo, _NOW + timedelta(hours=2))

    assert count_1 == 0
    assert count_2 == 0
    row = repo.get_guardian_authority_decision("ga-veto")
    assert row["outcome_status"] == "PENDING"
    assert row["resolved_at"] is None


def test_resolve_pending_decisions_returns_count_of_actually_resolved_rows_only(tmp_path):
    """AC7: the returned count reflects only rows actually resolved in this
    call - not still-open-position skips, not PRE_ENTRY_VETO skips."""
    repo = SQLiteRepository(tmp_path / "t.db")

    # Still open - skip.
    _open_position(repo, "pos-open")
    repo.save_guardian_authority_decision(
        "ga-open", "pos-open", "cand-open", "TIGHTEN_SL", _NOW,
        "r", "expect favorable", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500",
    )

    # PRE_ENTRY_VETO - skip forever.
    repo.save_guardian_authority_decision(
        "ga-veto", None, "cand-veto", "PRE_ENTRY_VETO", _NOW,
        "r", "expect unfavorable if opened", "unfavorable", 0.8, "run-1",
    )

    # Two genuinely resolvable rows.
    _open_position(repo, "pos-r1")
    _close_position(repo, "pos-r1", fill_exit="51000", fees="2", funding="1")
    repo.save_guardian_authority_decision(
        "ga-r1", "pos-r1", "cand-r1", "TIGHTEN_SL", _NOW,
        "r", "expect favorable", "favorable", 0.7, "run-1",
        old_sl="49000", new_sl="49500",
    )

    _open_position(repo, "pos-r2")
    _close_position(repo, "pos-r2", fill_exit="50100", exit_reason="GUARDIAN_EXIT")
    repo.save_guardian_authority_decision(
        "ga-r2", "pos-r2", "cand-r2", "CLOSE_EARLY", _NOW,
        "r", "expect unfavorable if left open", "unfavorable", 0.9, "run-1",
    )

    count = resolve_pending_decisions(repo, _NOW + timedelta(hours=3))

    assert count == 2
    assert repo.get_guardian_authority_decision("ga-open")["outcome_status"] == "PENDING"
    assert repo.get_guardian_authority_decision("ga-veto")["outcome_status"] == "PENDING"
    assert repo.get_guardian_authority_decision("ga-r1")["outcome_status"] == "RESOLVED"
    assert repo.get_guardian_authority_decision("ga-r2")["outcome_status"] == "RESOLVED"


# ---------------------------------------------------------------------------
# Task 9: update_heuristics_from_resolved_decisions
#
# Controller ruling (see task-9-brief.md, "Two controller rulings you must
# follow"): TIGHTEN_SL is the ONLY decision_type that ever produces a
# resolved row with a real, non-null expectation_correct (Task 8's own
# ruling means CLOSE_EARLY always resolves with expectation_correct=None
# and PRE_ENTRY_VETO never resolves at all). So every fixture below builds
# resolved TIGHTEN_SL rows directly (position rows are NOT needed - only
# the decision row itself plus a guardian_observations row whose
# observed_at exactly matches decided_at, per ruling (a)'s reconstruction
# join), and grouping is by guardian_state alone (the simplest group shape)
# for deterministic, hand-verifiable test assertions.
# ---------------------------------------------------------------------------

_MID_FACTORS = {
    "time_decay": 0.5,
    "momentum_decay": 0.5,
    "volume_decay": 0.5,
    "funding_decay": 0.5,
    "secondary_confirmation_lost": 0.5,
    "market_regime": 0.5,
}


def _resolved_tighten_sl(
    repo: SQLiteRepository,
    idx: int,
    guardian_state: str,
    factors: dict,
    correct: bool,
    position_id: str = "pos-hc",
) -> None:
    """Builds ONE resolved TIGHTEN_SL decision + its exactly-matching
    guardian_observations row (same decided_at/observed_at ISO string, per
    ruling (a)) - no real Position/Candidate row is needed, since
    update_heuristics_from_resolved_decisions never reads either."""
    decided_at = _NOW + timedelta(minutes=idx)
    decision_id = f"ga-hc-{position_id}-{idx}"
    repo.save_guardian_authority_decision(
        decision_id, position_id, f"cand-{position_id}-{idx}", "TIGHTEN_SL", decided_at,
        "reasoning", "expect small favorable move", "favorable", 0.7, "run-1",
        old_sl="100", new_sl="105",
    )
    repo.resolve_guardian_authority_decision(
        decision_id,
        "target" if correct else "stop_loss",
        "10.0" if correct else "-10.0",
        correct,
        decided_at + timedelta(hours=1),
    )
    repo.save_guardian_observation(
        GuardianObservation(
            observation_id=f"obs-{decision_id}",
            position_id=position_id,
            observed_at=decided_at,
            state=guardian_state,
            decay_score=Decimal("0.5"),
            progress_ratio=Decimal("0.1"),
            unrealized_pnl=Decimal("10"),
            factors=factors,
            run_id="run-1",
        )
    )


def test_reconstruct_tighten_sl_factors_joins_on_matching_observed_at(tmp_path):
    """Ruling (a): decided_at == observed_at (exact ISO string equality)
    reconstructs the factors dict Guardian Authority actually decided
    against, including the merged guardian_state key - mirrors
    decide_open_position's own `{**position_factors, "guardian_state":
    guardian_state}` merge (see that function's docstring)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    decided_at = _NOW
    repo.save_guardian_authority_decision(
        "ga-join-1", "pos-join", "cand-join", "TIGHTEN_SL", decided_at,
        "reasoning", "expect small favorable move", "favorable", 0.7, "run-1",
        old_sl="100", new_sl="105",
    )
    factors = {
        "time_decay": 0.8, "momentum_decay": 0.2, "volume_decay": 0.1,
        "funding_decay": 0.05, "secondary_confirmation_lost": 0.0, "market_regime": 0.6,
    }
    repo.save_guardian_observation(
        GuardianObservation(
            observation_id="obs-join-1", position_id="pos-join", observed_at=decided_at,
            state="PROTECT", decay_score=Decimal("0.5"), progress_ratio=Decimal("0.1"),
            unrealized_pnl=Decimal("10"), factors=factors, run_id="run-1",
        )
    )
    decision = repo.get_guardian_authority_decision("ga-join-1")

    reconstructed = _reconstruct_tighten_sl_factors(repo, decision)

    assert reconstructed == {**factors, "guardian_state": "PROTECT"}


def test_reconstruct_tighten_sl_factors_returns_none_when_no_matching_observation(tmp_path):
    """No crash, no spurious match: a decision with zero (or only
    non-matching) guardian_observations rows for its position reconstructs
    to None."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_decision(
        "ga-join-2", "pos-join2", "cand-join2", "TIGHTEN_SL", _NOW,
        "reasoning", "expect small favorable move", "favorable", 0.7, "run-1",
        old_sl="100", new_sl="105",
    )
    decision = repo.get_guardian_authority_decision("ga-join-2")

    assert _reconstruct_tighten_sl_factors(repo, decision) is None


def test_update_heuristics_miscalibrated_group_gets_negative_adjustment(tmp_path):
    """AC1 (brief): all TIGHTEN_SL decisions matching one condition
    (guardian_state=PROTECT) were wrong -> a heuristic row for that
    condition with a NEGATIVE adjustment."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(30):
        _resolved_tighten_sl(repo, i, "PROTECT", _MID_FACTORS, correct=False, position_id="pos-hc1")

    updated = update_heuristics_from_resolved_decisions(repo, _NOW + timedelta(days=1))

    assert updated >= 1
    heuristics = {h["heuristic_id"]: h for h in repo.find_guardian_authority_heuristics()}
    assert "ga-hc:state:PROTECT" in heuristics
    state_heuristic = heuristics["ga-hc:state:PROTECT"]
    assert state_heuristic["adjustment"] < 0
    assert state_heuristic["sample_size"] == 30
    assert json.loads(state_heuristic["condition_json"]) == {"guardian_state": "PROTECT"}


def test_update_heuristics_well_calibrated_group_gets_positive_adjustment(tmp_path):
    """AC2 (brief): a well-calibrated group (high correct rate) produces a
    heuristic with a POSITIVE adjustment - per the symmetric mapping this
    implementation chose (correct_rate - 0.5, see module docstring/report)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(30):
        _resolved_tighten_sl(repo, i, "WATCH", _MID_FACTORS, correct=True, position_id="pos-hc2")

    update_heuristics_from_resolved_decisions(repo, _NOW + timedelta(days=1))

    heuristics = {h["heuristic_id"]: h for h in repo.find_guardian_authority_heuristics()}
    assert "ga-hc:state:WATCH" in heuristics
    assert heuristics["ga-hc:state:WATCH"]["adjustment"] > 0


def test_update_heuristics_respects_minimum_sample_size_threshold(tmp_path):
    """AC3 (brief): a group just below the minimum sample size threshold
    does NOT produce a heuristic; a group at/above it does - tested
    explicitly at the exact boundary (29 vs 30)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(29):
        _resolved_tighten_sl(repo, i, "HOLD", _MID_FACTORS, correct=False, position_id="pos-hc3a")
    for i in range(30):
        _resolved_tighten_sl(repo, i, "EXIT", _MID_FACTORS, correct=False, position_id="pos-hc3b")

    update_heuristics_from_resolved_decisions(repo, _NOW + timedelta(days=1))

    heuristic_ids = {h["heuristic_id"] for h in repo.find_guardian_authority_heuristics()}
    assert "ga-hc:state:HOLD" not in heuristic_ids  # 29 < threshold
    assert "ga-hc:state:EXIT" in heuristic_ids  # 30 >= threshold


def test_update_heuristics_is_idempotent_on_rerun(tmp_path):
    """AC4 (brief): running twice with the same underlying data upserts the
    SAME heuristic_id (REPLACE semantics, per Task 2) - same row count, not
    doubled, and the row reflects the latest computed values (updated_at
    from the second run)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(30):
        _resolved_tighten_sl(repo, i, "PROTECT", _MID_FACTORS, correct=False, position_id="pos-hc4")

    update_heuristics_from_resolved_decisions(repo, _NOW + timedelta(days=1))
    first_count = len(repo.find_guardian_authority_heuristics())

    second_updated_at = _NOW + timedelta(days=2)
    update_heuristics_from_resolved_decisions(repo, second_updated_at)
    second_count = len(repo.find_guardian_authority_heuristics())

    assert first_count == second_count
    heuristics = {h["heuristic_id"]: h for h in repo.find_guardian_authority_heuristics()}
    assert heuristics["ga-hc:state:PROTECT"]["updated_at"] == second_updated_at.isoformat()


def test_update_heuristics_excludes_close_early_and_pre_entry_veto(tmp_path):
    """AC5 (brief): a resolved CLOSE_EARLY row (expectation_correct=None per
    Task 8's own ruling) and a PENDING PRE_ENTRY_VETO row (position_id=None,
    never resolves) must not crash the grouping logic and must not produce
    any spurious heuristic from None values."""
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.save_guardian_authority_decision(
        "ga-close-1", "pos-close", "cand-close", "CLOSE_EARLY", _NOW,
        "reasoning", "expect unfavorable if left open", "unfavorable", 0.9, "run-1",
    )
    repo.resolve_guardian_authority_decision(
        "ga-close-1", "GUARDIAN_EXIT", "5.0", None, _NOW + timedelta(hours=1)
    )
    repo.save_guardian_authority_decision(
        "ga-veto-1", None, "cand-veto", "PRE_ENTRY_VETO", _NOW,
        "reasoning", "expect unfavorable if opened", "unfavorable", 0.8, "run-1",
    )

    updated = update_heuristics_from_resolved_decisions(repo, _NOW + timedelta(days=1))

    assert updated == 0
    assert repo.find_guardian_authority_heuristics() == []


# =========================================================================
# Task 10: Production isolation / Global Constraints (req-9 checklist)
# =========================================================================
# Same discipline as Task 5's own authority_live.py isolation tests (see
# test_authority_live.py's "18. Production isolation / Global Constraints"
# section, in particular test_module_never_imports_forbidden_production_
# modules, which this section reuses/extends rather than reinvents): AST-
# based import/call scanning where it is practical, plain source-text
# scanning for forbidden symbol/config-field names where it isn't. Every
# item below is traced 1:1 to the plan's own Global Constraints list / the
# spec's "Safety architecture (structural, not policy - traces to
# requirement 9)" table (docs/superpowers/specs/2026-09-14-guardian-
# authority-design.md), covering BOTH crypto_trading/guardian/authority.py
# AND crypto_trading/guardian/authority_live.py - the only two modules in
# this extension capable of doing anything consequential. (decide_pre_entry/
# decide_open_position/evaluate_heuristics/etc. are Zero I/O - see
# authority.py's own module docstring - and so cannot themselves violate
# any of these; the wrapper (maybe_open_position_for_candidate) and the
# tick-time/LIVE-SL functions in these same two files are what actually
# touch the database/exchange, so scanning the two full files covers
# everything reachable.)


def _authority_source() -> str:
    from pathlib import Path

    import crypto_trading.guardian.authority as module_under_test

    return Path(module_under_test.__file__).read_text(encoding="utf-8")


def _authority_live_source() -> str:
    from pathlib import Path

    import crypto_trading.guardian.authority_live as module_under_test

    return Path(module_under_test.__file__).read_text(encoding="utf-8")


def test_neither_module_imports_or_calls_position_sizing():
    """Guardrail (spec table: 'Never martingales / changes sizing'; plan's
    own Global Constraints): no function in either module ever imports or
    calls position_sizing.py. AST-based import scan on both files (authority_
    live.py already has its own standalone version of this check -
    test_module_never_imports_forbidden_production_modules below - this is
    the paired assertion that also covers authority.py), plus a textual
    scan on both for the module name, the one function this codebase's
    sizing logic exposes (compute_position_size), and a dynamic-import
    escape hatch (importlib) that an AST import scan alone would miss."""
    import ast

    forbidden_module = "crypto_trading.paper_trading.position_sizing"
    for source in (_authority_source(), _authority_live_source()):
        tree = ast.parse(source)
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
        offenders = [
            m for m in imported_modules
            if m == forbidden_module or m.startswith(forbidden_module + ".")
        ]
        assert offenders == [], f"imports position_sizing: {offenders}"
        assert "position_sizing" not in source
        assert "compute_position_size" not in source
        assert "importlib" not in source


def test_neither_module_calls_set_leverage_or_references_a_leverage_config_field():
    """Guardrail (spec table: 'Never increases leverage'): no function in
    either module ever calls BingXLiveTradingConnector.set_leverage or
    reads/writes any leverage config field (LiveExecutionConfig.leverage,
    config/loader.py). `set_leverage` is checked via plain substring - the
    function name is not ordinary English prose and cannot appear
    incidentally (same precedent as test_module_never_references_
    set_leverage below, which already establishes this for authority_
    live.py alone). The `leverage` CONFIG FIELD is checked via AST
    attribute/subscript access instead of a substring, deliberately: this
    module's own docstring legitimately uses the bare English word
    "leverage" once, in prose describing this very guarantee ("It also
    never changes leverage..."), which a naive substring scan would
    incorrectly flag as a violation of the property it is documenting."""
    import ast

    for source in (_authority_source(), _authority_live_source()):
        assert "set_leverage" not in source

        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "leverage":
                offenders.append(f"attribute:.{node.attr}")
            elif isinstance(node, ast.Subscript):
                key_node = node.slice
                if isinstance(key_node, ast.Constant) and key_node.value == "leverage":
                    offenders.append("subscript:['leverage']")
        assert offenders == [], f"found leverage config field reference(s): {offenders}"


def test_neither_module_reads_or_writes_any_capital_defining_config_field():
    """Guardrail (spec table: 'Never changes the capital limit'; plan's own
    Global Constraints): no function in either module reads or writes
    starting_capital_usdt, max_total_exposure_pct, margin_per_trade_usdt,
    max_concurrent_positions, or any other capital-defining config field -
    RiskLimitsConfig's own PAPER-side exposure/sizing fields and
    LiveExecutionConfig's own LIVE-side sizing fields (config/loader.py).
    Plain source-text scan: every one of these is a long, specific
    snake_case identifier, not ordinary English prose, so a substring match
    cannot false-positive the way the bare word "leverage" would (handled
    separately, via AST, in the test above)."""
    capital_fields = (
        "starting_capital_usdt",
        "max_total_exposure_pct",
        "margin_per_trade_usdt",
        "max_concurrent_positions",
        "max_position_notional_usdt",
        "risk_per_trade_pct",
        "margin_safety_buffer_usdt",
    )
    for source in (_authority_source(), _authority_live_source()):
        offenders = [field for field in capital_fields if field in source]
        assert offenders == [], f"found capital-defining config field reference(s): {offenders}"


def test_neither_module_ever_calls_a_paper_live_demo_entry_claim_method_directly():
    """Guardrail (spec table: 'Never opens unlimited/additional positions';
    plan's own Global Constraints): 'no code path that calls
    open_position_for_candidate, create_position_with_event, or any LIVE/
    PAPER/Demo entry-claim method' - EXCEPT the one sanctioned exception,
    Task 6's wrapper (maybe_open_position_for_candidate), which may call
    open_position_for_candidate, and only ever on the APPROVE branch
    (already covered at the behavioral level by
    test_maybe_open_position_flag_on_approve_opens_position_with_no_decision_row
    and test_maybe_open_position_flag_on_veto_never_opens_a_position above -
    this test adds the STRUCTURAL guarantee those behavioral tests don't
    cover: that no OTHER function, in either file, could ever reach that
    call).

    create_position_with_event and the LIVE/Demo entry-claim repository
    methods are checked via plain source-text scan (none of these names
    collides with ordinary prose, and neither module has any legitimate
    reason to reference them at all - unlike open_position_for_candidate,
    there is no sanctioned exception for these). open_position_for_candidate
    itself is checked via AST: every Call to it, anywhere in authority.py,
    must be lexically nested inside maybe_open_position_for_candidate's own
    function body; authority_live.py must not reference it at all."""
    import ast

    forbidden_entry_claim_calls = (
        "create_position_with_event",
        "claim_demo_execution",
        "claim_live_execution",
    )
    for source in (_authority_source(), _authority_live_source()):
        offenders = [name for name in forbidden_entry_claim_calls if name in source]
        assert offenders == [], f"found forbidden entry-claim reference(s): {offenders}"

    sanctioned_function = "maybe_open_position_for_candidate"
    tree = ast.parse(_authority_source())

    class _OpenPositionCallVisitor(ast.NodeVisitor):
        def __init__(self):
            self.function_stack: list[str] = []
            self.violations: list[str] = []

        def visit_FunctionDef(self, node):
            self.function_stack.append(node.name)
            self.generic_visit(node)
            self.function_stack.pop()

        def visit_Call(self, node):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "open_position_for_candidate":
                enclosing = self.function_stack[-1] if self.function_stack else "<module level>"
                if enclosing != sanctioned_function:
                    self.violations.append(enclosing)
            self.generic_visit(node)

    visitor = _OpenPositionCallVisitor()
    visitor.visit(tree)
    assert visitor.violations == [], (
        f"open_position_for_candidate called outside the sanctioned wrapper: {visitor.violations}"
    )

    # authority_live.py has no legitimate reason to ever reference
    # open_position_for_candidate at all - it only ever tightens an
    # already-open LIVE position's stop-loss.
    assert "open_position_for_candidate" not in _authority_live_source()


def test_neither_module_contains_a_stop_loss_removal_or_loosening_path():
    """Guardrail (spec table: 'Never removes/loosens a stop-loss'). The
    core invariant - a SL write is only ever valid when the new value is
    strictly closer to entry than the current one - is already deeply
    tested at the unit level: decide_open_position's own downgrade-to-
    NO_ACTION safety net (test_decide_open_position_never_returns_invalid_
    tighten_sl and its _when_current_sl_equals_entry variant above) for
    authority.py, and _verify_tightening_invariant's independent re-
    verification (test_authority_live.py's own dedicated tests) for
    authority_live.py. This test does not re-derive that logic - it is the
    broad reference-scan companion the brief asks for: confirming neither
    module contains a textual path shaped like removing or loosening a
    stop-loss outside those two already-reviewed, invariant-checked
    mechanisms (e.g. a second, un-gated way to clear or widen a stop)."""
    forbidden_substrings = (
        "remove_stop_loss",
        "clear_stop_loss",
        "delete_stop_loss",
        "widen_stop_loss",
        "loosen_stop_loss",
        "stop_loss=None",
        "stop_loss = None",
        "stop_loss=0",
    )
    for source in (_authority_source(), _authority_live_source()):
        offenders = [s for s in forbidden_substrings if s in source]
        assert offenders == [], f"found forbidden SL-loosening reference(s): {offenders}"


def test_neither_module_ever_writes_to_a_py_or_yaml_file_at_runtime():
    """Guardrail (spec table: 'Never silently modifies production
    strategy' - 'this module never writes to any .py or .yaml file under
    crypto_trading/'). This is more a design property than something a
    runtime unit test can directly exercise: there is no filesystem call
    site in either module to instrument/mock an assertion against in the
    first place (unlike the other guardrails above, which are all "this
    symbol/call must never appear" checks against real call sites). Per the
    brief's own guidance for exactly this situation, what IS tested here:
    an AST scan confirming neither module contains ANY call to the builtin
    open() or to pathlib.Path's write_text/write_bytes - the only two ways
    Python code anywhere in this codebase ever writes a file. If neither
    call form exists in the source at all, the module is structurally
    incapable of writing to any file (.py/.yaml or otherwise) at runtime;
    its only writes are the already-audited repo.* calls (SQLite, via the
    Repository protocol) and, on the LIVE tightening path, the reused,
    already-reviewed BingXLiveTradingConnector HTTP calls - neither of
    which is a filesystem write under crypto_trading/."""
    import ast

    forbidden_attr_calls = {"write_text", "write_bytes"}
    for source in (_authority_source(), _authority_live_source()):
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "open":
                    offenders.append("open()")
                elif isinstance(func, ast.Attribute) and func.attr in forbidden_attr_calls:
                    offenders.append(func.attr)
        assert offenders == [], f"found file-write call(s): {offenders}"
