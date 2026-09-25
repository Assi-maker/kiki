"""Repository round trips for the seven GODFATHER intelligence tables.

Money values must survive storage as exact Decimals (the project-wide
rule: never through a float), the claim-style inserts must be idempotent,
and the append-only guarantee on the thesis table must be enforced by the
database rather than by caller discipline.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from crypto_trading.schemas.godfather import (
    AvoidableLossFinding,
    ComponentVerdict,
    CounterfactualResult,
    DecisionAudit,
    EntryQualityAssessment,
    ExperiencePattern,
    PredictionErrorRecord,
    ThesisObservation,
    TradeInvestigation,
)
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _repo(tmp_path):
    return SQLiteRepository(tmp_path / "t.db")


def _investigation(position_id="pos-1") -> TradeInvestigation:
    return TradeInvestigation(
        position_id=position_id,
        candidate_id="cand-1",
        instrument="BTC-USDT",
        created_at=_NOW,
        classification="EXIT_TOO_LATE",
        entry_verdict="GOOD",
        management_verdict="BAD",
        before={"candidate_score": 0.5},
        during={
            "mfe_pct": "6.5",
            "mae_pct": "-1.25",
            "giveback_ratio": "0.9",
            "minutes_to_mfe": 42.0,
            "minutes_to_target_touch": None,
            "minutes_to_sl_touch": 180.0,
            "first_questionable_minutes": 60.0,
            "first_invalid_minutes": None,
            "path_point_count": 200,
        },
        after={
            "exit_reason": "time_limit",
            "hold_minutes": 240.0,
            "realized_pnl_usdt": "-12.3456789012345678",
        },
        reason_codes=["a real favourable move was given back before exit"],
        avoidable_loss=AvoidableLossFinding(
            policy="EXIT_ON_THESIS_WEAKENING",
            decision_available_at_minutes=60.0,
            estimated_pnl_improvement_usdt=Decimal("18.5"),
            winner_damage_checked=True,
            explanation="checked",
        ),
        run_id="run-1",
    )


def test_an_investigation_round_trips_with_exact_decimals(tmp_path):
    repo = _repo(tmp_path)

    assert repo.save_godfather_trade_investigation(_investigation()) is True
    row = repo.get_godfather_trade_investigation("pos-1")

    assert row["classification"] == "EXIT_TOO_LATE"
    assert row["realized_pnl_usdt"] == "-12.3456789012345678"
    assert row["avoidable_loss_usdt"] == "18.5"
    assert row["best_alternative_policy"] == "EXIT_ON_THESIS_WEAKENING"
    assert row["path_point_count"] == 200


def test_investigating_the_same_trade_twice_is_an_idempotent_no_op(tmp_path):
    repo = _repo(tmp_path)
    repo.save_godfather_trade_investigation(_investigation())

    assert repo.save_godfather_trade_investigation(_investigation()) is False
    assert len(repo.find_godfather_trade_investigations()) == 1


def test_a_decision_audit_stores_components_and_conflicts_separately(tmp_path):
    repo = _repo(tmp_path)
    audit = DecisionAudit(
        position_id="pos-1",
        candidate_id="cand-1",
        created_at=_NOW,
        components=[
            ComponentVerdict(
                component="bull_thesis", stance="BULLISH", expectation="x", verdict="RIGHT"
            ),
            ComponentVerdict(
                component="forecast", stance="BEARISH", expectation="y", verdict="WRONG"
            ),
            ComponentVerdict(component="qa", stance="PASS", expectation="z", verdict="UNSCORABLE"),
        ],
        conflicts=["entry_rsi_at_or_above_80"],
        misleading_components=["forecast"],
        missing_information=["no_instrument_specific_news_facts"],
        fault_domain="POSITION_MANAGEMENT",
        run_id="run-1",
    )

    assert repo.save_godfather_decision_audit(audit) is True
    row = repo.get_godfather_decision_audit("pos-1")

    assert (row["right_count"], row["wrong_count"], row["unknown_count"]) == (1, 1, 1)
    assert row["conflict_count"] == 1
    assert "entry_rsi_at_or_above_80" in row["conflicts_json"]
    assert row["fault_domain"] == "POSITION_MANAGEMENT"


def test_a_counterfactual_keeps_the_simulated_and_real_outcome_side_by_side(tmp_path):
    repo = _repo(tmp_path)
    result = CounterfactualResult(
        counterfactual_id="cf-1",
        position_id="pos-1",
        policy="EXIT_ON_THESIS_INVALID",
        created_at=_NOW,
        triggered=True,
        trigger_minutes=90.0,
        simulated_exit_price=Decimal("101.5"),
        simulated_pnl_usdt=Decimal("7.25"),
        actual_pnl_usdt=Decimal("-12.75"),
        delta_pnl_usdt=Decimal("20"),
        no_lookahead_verified=True,
        detail={"trigger_index": 3},
        run_id="run-1",
    )

    assert repo.save_godfather_counterfactual(result) is True
    row = repo.find_godfather_counterfactuals_for_position("pos-1")[0]

    assert row["simulated_pnl_usdt"] == "7.25"
    assert row["actual_pnl_usdt"] == "-12.75"
    assert row["no_lookahead_verified"] == 1
    assert repo.save_godfather_counterfactual(result) is False


def test_an_experience_pattern_is_restated_rather_than_appended(tmp_path):
    repo = _repo(tmp_path)

    def _pattern(sample_size: int, edge_class: str) -> ExperiencePattern:
        return ExperiencePattern(
            pattern_id="trigger_reasons_key:momentum_breakout",
            pattern_family="trigger_reasons_key",
            pattern_key="momentum_breakout",
            condition={"trigger_reasons_key": "momentum_breakout"},
            computed_at=_NOW,
            sample_size=sample_size,
            win_count=sample_size // 2,
            expectancy_usdt=Decimal("1.5"),
            edge_class=edge_class,
            confidence=0.5,
            run_id="run-1",
        )

    repo.upsert_godfather_experience_pattern(_pattern(20, "INSUFFICIENT_DATA"))
    repo.upsert_godfather_experience_pattern(_pattern(40, "WEAK_EDGE"))

    rows = repo.find_godfather_experience_patterns()
    assert len(rows) == 1
    assert rows[0]["sample_size"] == 40
    assert rows[0]["edge_class"] == "WEAK_EDGE"


def test_a_prediction_error_round_trips_all_five_fields(tmp_path):
    repo = _repo(tmp_path)
    record = PredictionErrorRecord(
        prediction_error_id="pe-1",
        position_id="pos-1",
        source="forecast_agent",
        created_at=_NOW,
        expected="bullish at p=0.28",
        actual="bearish",
        error="wrong direction",
        cause="volume decay",
        lesson="OBSERVATION ONLY (insufficient evidence to act): track calibration",
        magnitude=0.62,
        run_id="run-1",
    )

    assert repo.save_godfather_prediction_error(record) is True
    row = repo.find_godfather_prediction_errors()[0]

    assert (row["expected"], row["actual"], row["error"]) == (
        "bullish at p=0.28",
        "bearish",
        "wrong direction",
    )
    assert row["magnitude"] == 0.62
    assert repo.save_godfather_prediction_error(record) is False


def _thesis(thesis_id="t-1") -> ThesisObservation:
    return ThesisObservation(
        thesis_id=thesis_id,
        position_id="pos-1",
        observed_at=_NOW,
        thesis_state="WEAKENING",
        recommended_action="TIGHTEN_SL",
        enforced=False,
        reason_codes=["gave_back_half_of_favourable_move"],
        features={"decay_score": "0.4"},
        run_id="run-1",
    )


def test_a_thesis_observation_round_trips_and_is_advisory(tmp_path):
    repo = _repo(tmp_path)

    assert repo.save_godfather_position_thesis(_thesis()) is True
    row = repo.find_latest_godfather_position_thesis("pos-1")

    assert row["thesis_state"] == "WEAKENING"
    assert row["recommended_action"] == "TIGHTEN_SL"
    assert row["enforced"] == 0


def test_the_thesis_table_refuses_an_update(tmp_path):
    """Append-only enforced by the database, not by caller discipline -
    the same trigger pattern `guardian_observations` already uses."""
    repo = _repo(tmp_path)
    repo.save_godfather_position_thesis(_thesis())

    with pytest.raises(Exception, match="append-only"):
        repo._conn.execute(
            "UPDATE godfather_position_thesis SET thesis_state = 'STRONG' WHERE thesis_id = ?",
            ("t-1",),
        )


def test_an_entry_quality_assessment_round_trips(tmp_path):
    repo = _repo(tmp_path)
    assessment = EntryQualityAssessment(
        candidate_id="cand-1",
        instrument="BTC-USDT",
        assessed_at=_NOW,
        verdict="WAIT",
        quality_score=0.42,
        expected_edge_class="INSUFFICIENT_DATA",
        expected_expectancy_usdt=None,
        risk_reward=Decimal("2"),
        regime_compatible=True,
        conflict_score=0.35,
        expected_cost_usdt=Decimal("1.2"),
        enforced=False,
        reason_codes=["wait:quality_below_trade_threshold"],
        run_id="run-1",
    )

    assert repo.save_godfather_entry_quality(assessment) is True
    row = repo.get_godfather_entry_quality("cand-1")

    assert row["verdict"] == "WAIT"
    assert row["risk_reward"] == "2"
    assert row["regime_compatible"] == 1
    assert row["enforced"] == 0
    assert repo.save_godfather_entry_quality(assessment) is False


def test_an_unknown_regime_is_stored_as_null_not_as_false(tmp_path):
    """A missing regime reading must never be recorded as "incompatible"
    - that would turn absence of data into a negative judgement."""
    repo = _repo(tmp_path)
    repo.save_godfather_entry_quality(
        EntryQualityAssessment(
            candidate_id="cand-2",
            instrument="BTC-USDT",
            assessed_at=_NOW,
            verdict="TRADE",
            quality_score=0.7,
            expected_edge_class="INSUFFICIENT_DATA",
            regime_compatible=None,
            run_id="run-1",
        )
    )

    assert repo.get_godfather_entry_quality("cand-2")["regime_compatible"] is None


def test_the_pending_investigation_queue_is_its_own_restart_safe_cursor(tmp_path):
    repo = _repo(tmp_path)
    assert repo.count_closed_positions_pending_godfather_investigation() == 0
    assert repo.find_closed_positions_pending_godfather_investigation(10) == []


def test_reading_an_assessment_payload_returns_none_for_a_missing_role(tmp_path):
    repo = _repo(tmp_path)

    assert repo.get_assessment_payload("nope", "opportunity_screen") is None
