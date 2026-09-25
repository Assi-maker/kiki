"""Tests for GODFATHER Fas 2: experience backfill and its consumption.

What is pinned: profiles are reconstructed from the real path only;
entry quality is decided before any exit; features after entry are never
used; unobservable is never scored; small samples carry no weight; and
experience can change an advisory decision ONLY through a classified
pattern - never through NOISE or INSUFFICIENT_DATA.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.godfather.book import TradeContext, load_book
from crypto_trading.godfather.entry_quality import assess_entry_quality
from crypto_trading.godfather.experience import (
    ExperienceConfig,
    ExperienceSample,
    build_experience_memory,
    experience_evidence,
)
from crypto_trading.godfather.experience_builder import (
    build_samples,
    run_experience_backfill,
    trade_profile,
)
from crypto_trading.godfather.experience_impact import measure_entry_impact, run_fas2
from crypto_trading.godfather.pipeline import run_experience_sweep
from crypto_trading.godfather.supervisor import build_entry_signals
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.godfather.intelligence_fixtures import (
    OPENED,
    evidence_record,
    make_position,
    path_point,
)
from tests.crypto_trading.godfather.test_intelligence_pipeline import _event
from tests.crypto_trading.godfather.test_supervisor_sweep import _book, _seed
from tests.crypto_trading.test_market_snapshot import _settings

_NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)


def _trade(prices, exit_price, exit_reason, *, size=Decimal("1000"), close_after=None):
    close_minutes = close_after if close_after is not None else len(prices)
    position = make_position(
        exit_price=Decimal(str(exit_price)), exit_reason=exit_reason, size=size,
        closed_at=OPENED + timedelta(minutes=close_minutes),
    )
    points = [path_point(i, Decimal(str(p)), position=position) for i, p in enumerate(prices)]
    return TradeContext(
        position=position, candidate=None, opportunity_screen=None, gate_decision=None,
        observations=[], points=points, pnl=None if size == 0 else Decimal("1"),
        regime="btc_ok", features={"x": "1"},
    )


# ---------------------------------------------------------------------
# Price-path profile (MFE/MAE reconstruction, time-to-event)
# ---------------------------------------------------------------------


def test_the_profile_reconstructs_time_to_event_levels_and_giveback():
    trade = _trade([100, 100.2, 100.6, 101.1, 103, 102], 101, "time_limit")
    profile = trade_profile(trade, [], [])

    assert profile["minutes_to_first_favorable"] == 2
    assert profile["levels_reached"] == ["0.5", "1.0", "2.0", "3.0"]
    assert profile["minutes_to_target"] is None
    assert profile["minutes_to_sl"] is None
    # MFE +3%, exit +1% -> two thirds given back, one third captured.
    assert abs(float(profile["giveback_ratio"]) - 2 / 3) < 1e-9
    assert abs(float(profile["management_capture"]) - 1 / 3) < 1e-9
    assert profile["exit_reason"] == "time_limit"


def test_entry_success_is_decided_by_whichever_one_percent_came_first():
    good = trade_profile(_trade([100, 100.5, 101.2, 98, 95], 95, "stop_loss"), [], [])
    bad = trade_profile(_trade([100, 99.5, 98.9, 104, 110], 110, "target"), [], [])

    # A good entry that then lost, and a bad entry an exit rescued: entry
    # quality is judged before any exit rule acts.
    assert good["entry_success"] is True
    assert bad["entry_success"] is False
    assert bad["minutes_to_target"] == 4
    assert good["minutes_to_sl"] == 4


def test_entry_success_is_unobservable_across_a_hole_not_guessed():
    position = make_position(exit_price=Decimal("102"), exit_reason="time_limit",
                             closed_at=OPENED + timedelta(minutes=120))
    points = [
        path_point(0, Decimal("100"), position=position),
        path_point(90, Decimal("102"), position=position),
    ]
    trade = TradeContext(position, None, None, None, [], points, Decimal("1"), "btc_ok")
    assert trade_profile(trade, [], [])["entry_success"] is None


def test_the_profile_keeps_only_observed_triggered_counterfactuals():
    trade = _trade([100, 101, 102], 102, "time_limit")
    rows = [
        {"policy": "BASELINE", "delta_pnl_usdt": "0", "triggered": 1, "detail_json": "{}"},
        {"policy": "THESIS_TIGHTEN", "delta_pnl_usdt": "-3", "triggered": 1,
         "detail_json": json.dumps({"observation_status": "OBSERVED"})},
        {"policy": "PROFIT_LOCK_HALF_MFE", "delta_pnl_usdt": None, "triggered": 1,
         "detail_json": json.dumps({"observation_status": "UNOBSERVABLE"})},
        {"policy": "EXIT_ON_THESIS_INVALID", "delta_pnl_usdt": "0", "triggered": 0,
         "detail_json": "{}"},
    ]
    assert trade_profile(trade, rows, [])["counterfactual_delta"] == {"THESIS_TIGHTEN": "-3"}


def test_zero_size_and_unknown_pnl_trades_are_not_experience():
    zero = _trade([100, 101], 101, "time_limit", size=Decimal("0"))
    unknown = _trade([100, 101], 101, "time_limit")
    unknown.pnl = None
    assert build_samples([zero, unknown], {}, {}, _settings()) == []


# ---------------------------------------------------------------------
# Feature timestamps
# ---------------------------------------------------------------------


def test_features_evaluated_after_entry_are_never_used(tmp_path):
    repo = SQLiteRepository(tmp_path / "f2.db")
    pid = _seed(repo, 0, [100, 101, 102], 102, "time_limit")
    late = datetime(2026, 9, 2, tzinfo=UTC)  # after the seeded entry on 2026-09-01
    candidate = repo.get_candidate(pid)
    repo.create_candidate_with_event(
        Candidate(**{**candidate.model_dump(), "candidate_id": "late",
                     "idempotency_key": "late", "evidence_hash": "late",
                     "evidence_record": evidence_record(evaluated_at=late)}),
        _event("CANDIDATE_CREATED", "late", late, "candidate"),
    )
    repo._conn.execute("UPDATE positions SET candidate_id = 'late' WHERE position_id = ?", (pid,))
    repo._conn.commit()

    trade = load_book(repo)[0]
    assert trade.features_valid is False
    assert trade.features == {}


# ---------------------------------------------------------------------
# Train / OOS separation, sample size, confidence
# ---------------------------------------------------------------------


def _sample(index, pnl, features, entry_success=None, live=False):
    return ExperienceSample(
        position_id=f"s{index}", closed_at=OPENED + timedelta(hours=index), pnl=Decimal(str(pnl)),
        mfe_pct=Decimal("1.5"), mae_pct=Decimal("-0.5"), minutes_to_mfe=30.0, regime="btc_ok",
        features=features,
        profile={"has_path": True, "entry_success": entry_success, "levels_reached": ["0.5"],
                 "counterfactual_delta": {"THESIS_TIGHTEN": "1"}},
        live=live,
    )


def test_every_pattern_says_which_evidence_is_calibration_oos_and_live():
    samples = [_sample(i, 1, {"k": "a"}, live=i % 2 == 0) for i in range(40)]
    pattern = build_experience_memory(samples, _NOW, "r", ExperienceConfig())[0]
    evidence = pattern.detail["evidence"]

    assert evidence["calibration_n"] == 28
    assert evidence["oos_n"] == 12
    assert evidence["live_n"] == 20
    assert pattern.detail["profile"]["mfe_pct"]["n"] == 40


def test_small_samples_are_insufficient_data_with_zero_confidence_everywhere():
    samples = [_sample(i, 5, {"k": "a"}, entry_success=True) for i in range(12)]
    samples += [_sample(100 + i, -5, {"k": "b"}, entry_success=False) for i in range(12)]
    config = ExperienceConfig(min_support=8)
    patterns = build_experience_memory(samples, _NOW, "r", config)

    for pattern in patterns:
        assert pattern.edge_class == "INSUFFICIENT_DATA"
        assert pattern.confidence == 0.0
        assert pattern.detail["entry_quality"]["class"] == "INSUFFICIENT_DATA"
        summary = pattern.detail["profile"]["counterfactual_mean_delta_usdt"]["THESIS_TIGHTEN"]
        assert summary["n"] == 12 and summary["mean"] is not None


def test_a_counterfactual_summary_below_min_support_is_insufficient():
    samples = [_sample(i, 1, {"k": "a"}) for i in range(20)]
    for sample in samples[5:]:
        sample.profile["counterfactual_delta"] = {}
    pattern = build_experience_memory(samples, _NOW, "r", ExperienceConfig(min_support=8))[0]
    summary = pattern.detail["profile"]["counterfactual_mean_delta_usdt"]["THESIS_TIGHTEN"]

    assert summary == {"n": 5, "mean": None, "status": "INSUFFICIENT_DATA"}


# ---------------------------------------------------------------------
# Experience lookup and consumption
# ---------------------------------------------------------------------


def _row(condition, edge_class, confidence, n=40, entry_class="NOISE"):
    return {
        "pattern_id": f"p:{condition}", "condition": condition, "edge_class": edge_class,
        "confidence": confidence, "sample_size": n, "expectancy_usdt": "-5",
        "detail": {"entry_quality": {"class": entry_class},
                   "profile": {"mfe_pct": {"p50": 1.0}, "mae_pct": {"p50": -2.0}}},
    }


def test_noise_and_insufficient_patterns_are_recognition_not_evidence():
    patterns = [_row({"a": 1}, "NOISE", 0.0), _row({"b": 2}, "INSUFFICIENT_DATA", 0.0, n=10)]
    result = experience_evidence(patterns, {"a": 1, "b": 2})

    assert result["verdict"] == "NO_EVIDENCE"
    assert result["signed_weight"] == 0
    assert len(result["matched"]) == 2
    assert result["similar_cases"]["sample_size"] == 40


def test_a_failure_pattern_opposes_and_an_unseen_situation_is_unknown():
    failure = experience_evidence([_row({"a": 1}, "FAILURE_PATTERN", 0.8)], {"a": 1})
    unseen = experience_evidence([_row({"a": 1}, "FAILURE_PATTERN", 0.8)], {"a": 2})
    missing_key = experience_evidence([_row({"z": 1}, "EDGE", 0.8)], {"a": 1})

    assert failure["verdict"] == "OPPOSES" and failure["signed_weight"] < 0
    assert unseen["verdict"] == "UNKNOWN_SITUATION"
    assert missing_key["verdict"] == "UNKNOWN_SITUATION"


def test_similar_cases_come_from_the_most_specific_reliable_pattern_only():
    patterns = [_row({"a": 1}, "NOISE", 0.0, n=80), _row({"b": 1}, "NOISE", 0.0, n=35),
                _row({"c": 1}, "NOISE", 0.0, n=12)]
    result = experience_evidence(patterns, {"a": 1, "b": 1, "c": 1}, min_sample_size=30)
    assert result["similar_cases"]["sample_size"] == 35
    none = experience_evidence([_row({"c": 1}, "NOISE", 0.0, n=12)], {"c": 1})
    assert none["similar_cases"] == {"status": "INSUFFICIENT_DATA"}


def _quality(evidence):
    from tests.crypto_trading.godfather.test_intelligence_entry_quality import _candidate
    return assess_entry_quality(
        candidate=_candidate(), features={"a": 1}, conflicts=[], experience_patterns=[],
        planned_entry=Decimal("100"), stop_loss=Decimal("95"), target=Decimal("110"),
        size=Decimal("1000"), risk_limits=_settings().risk_limits, now=_NOW, run_id="r",
        experience_evidence=evidence,
    )


def test_entry_quality_moves_only_on_classified_experience():
    baseline = _quality(None)
    noise = _quality(experience_evidence([_row({"a": 1}, "NOISE", 0.0)], {"a": 1}))
    failure = _quality(experience_evidence([_row({"a": 1}, "FAILURE_PATTERN", 0.9)], {"a": 1}))
    edge = _quality(experience_evidence([_row({"a": 1}, "EDGE", 0.9)], {"a": 1}))

    assert noise.quality_score == baseline.quality_score
    assert failure.quality_score < baseline.quality_score < edge.quality_score
    assert failure.detail["experience"]["verdict"] == "OPPOSES"


# ---------------------------------------------------------------------
# Backfill, pipeline and GODFATHER consumption against a real database
# ---------------------------------------------------------------------


def test_the_backfill_restates_memory_with_profiles_and_drops_zero_size_errors(tmp_path):
    repo = SQLiteRepository(tmp_path / "f2.db")
    _book(repo)
    book, samples, patterns, cleanup = run_experience_backfill(repo, _settings(), _NOW, "r")

    stored = repo.find_godfather_experience_patterns()
    assert len(stored) == len(patterns)
    assert all('"profile"' in row["detail_json"] for row in stored)
    assert all(s.position_id != "pos-12" for s in samples)  # zero-size seeded trade
    assert cleanup["zero_size_prediction_errors_removed"] == 0


def test_the_pipeline_sweep_uses_the_same_enriched_samples(tmp_path):
    repo = SQLiteRepository(tmp_path / "f2.db")
    _book(repo)
    run_experience_sweep(repo, _settings(), _NOW, "r")
    assert all('"entry_quality"' in row["detail_json"]
               for row in repo.find_godfather_experience_patterns())


def test_godfather_entry_decisions_change_only_when_experience_is_classified(tmp_path):
    repo = SQLiteRepository(tmp_path / "f2.db")
    _book(repo)
    settings = _settings()
    book, samples, _patterns, _ = run_experience_backfill(repo, settings, _NOW, "r")

    impact = measure_entry_impact(repo, settings, book, samples, _NOW, "r")
    assert impact["decisions_changed"] == 0  # 12 trades: nothing classified

    failure = [{"pattern_id": "f", "condition": {"trigger_reasons_key": "momentum_breakout"},
                "edge_class": "FAILURE_PATTERN", "confidence": 0.9, "sample_size": 40,
                "expectancy_usdt": "-20", "detail": {}}]
    rejected = build_entry_signals(repo, settings, book, lambda _m: failure, _NOW, "r", False)
    assert all(s.selection_verdict == "REJECT" for s in rejected)


def test_the_fas2_report_makes_no_ai_call(tmp_path):
    repo = SQLiteRepository(tmp_path / "f2.db")
    _book(repo)
    report = run_fas2(repo, _settings(), _NOW, "r")
    assert report["ai_calls"] == 0
    assert report["coverage"]["usable_trades"] == 12
