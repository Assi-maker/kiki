import json
from datetime import UTC, datetime
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
    decide_open_position,
    decide_pre_entry,
    evaluate_heuristics,
    maybe_open_position_for_candidate,
)
from crypto_trading.schemas.assessments import RiskAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
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
