"""End-to-end tests for crypto_trading/godfather/pipeline.py.

These run the whole tick against a real SQLite database seeded with real
positions, real candidates and real Guardian observations - written
through the same repository methods the trading pipeline itself uses - so
the storage layer, the JSON round trips and the idempotency claims are
all exercised rather than mocked.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.godfather.pipeline import run_godfather_intelligence_tick
from crypto_trading.godfather.report import build_report, render_text
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.godfather.intelligence_fixtures import evidence_record
from tests.crypto_trading.test_market_snapshot import _settings

_NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)
_BASE = datetime(2026, 9, 1, tzinfo=UTC)
_ENTRY = Decimal("100")
_SIZE = Decimal("500")


def _event(kind: str, aggregate_id: str, at: datetime, aggregate_type: str) -> Event:
    return Event(
        event_id=f"{kind}:{aggregate_id}",
        event_type=kind,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        occurred_at=at,
        run_id="seed",
        schema_version=1,
        payload={},
    )


def _seed_trade(
    repo,
    index: int,
    *,
    prices: list[Decimal],
    exit_price: Decimal,
    exit_reason: str = "time_limit",
    status: str = "CLOSED",
    candidate_score: float = 0.5,
    volume_triggered: bool = False,
) -> str:
    position_id = f"pos-{index}"
    opened_at = _BASE + timedelta(hours=index * 6)
    closed_at = opened_at + timedelta(hours=4)

    candidate = Candidate(
        candidate_id=position_id,
        idempotency_key=f"key-{position_id}",
        instrument="BTC-USDT",
        discovery_run_id="seed",
        evidence_hash=f"hash-{position_id}",
        status="CONFIRMED",
        evidence_record=evidence_record(
            candidate_score=candidate_score,
            volume_triggered=volume_triggered,
            evaluated_at=opened_at,
        ),
        created_at=opened_at,
        updated_at=opened_at,
        reference_price=_ENTRY,
    )
    repo.create_candidate_with_event(
        candidate, _event("CANDIDATE_CREATED", position_id, opened_at, "candidate")
    )
    repo.save_gate_decision(position_id, "CONFIRMED", "[]", opened_at)

    repo.create_position_with_event(
        Position(
            position_id=position_id,
            candidate_id=position_id,
            instrument="BTC-USDT",
            direction="LONG",
            status="OPEN_POSITION",
            theoretical_entry=_ENTRY,
            simulated_fill_entry=_ENTRY,
            stop_loss=Decimal("95"),
            target=Decimal("110"),
            size=_SIZE,
            fill_model_version="v1",
            opened_at=opened_at,
        ),
        _event("POSITION_OPENED", position_id, opened_at, "position"),
    )

    for step, price in enumerate(prices):
        observed_at = opened_at + timedelta(minutes=30 * step)
        repo.save_guardian_observation(
            GuardianObservation(
                observation_id=f"{position_id}-{step}",
                position_id=position_id,
                observed_at=observed_at,
                state="HOLD",
                decay_score=Decimal("0.1"),
                progress_ratio=(price - _ENTRY) / Decimal("10"),
                unrealized_pnl=_SIZE * (price - _ENTRY) / _ENTRY,
                factors={"momentum_decay": 0.1, "market_regime": 0.02},
                run_id="seed",
            )
        )

    if status == "CLOSED":
        repo.close_position_with_event(
            position_id,
            theoretical_exit=exit_price,
            simulated_fill_exit=exit_price,
            exit_reason=exit_reason,
            fees=Decimal("0.2"),
            funding=Decimal("0"),
            closed_at=closed_at,
            event=_event("POSITION_CLOSED", position_id, closed_at, "position"),
        )
    return position_id


def _seed_book(repo, winners: int = 6, losers: int = 6) -> None:
    index = 0
    for _ in range(winners):
        _seed_trade(
            repo,
            index,
            prices=[_ENTRY, Decimal("104"), Decimal("108")],
            exit_price=Decimal("108"),
            exit_reason="target",
            volume_triggered=True,
        )
        index += 1
    for _ in range(losers):
        _seed_trade(
            repo,
            index,
            prices=[_ENTRY, Decimal("100.1"), Decimal("96")],
            exit_price=Decimal("96"),
            exit_reason="stop_loss",
            volume_triggered=False,
        )
        index += 1


def _repo(tmp_path):
    return SQLiteRepository(tmp_path / "godfather.db")


def test_the_tick_investigates_audits_and_simulates_every_closed_trade(tmp_path):
    repo = _repo(tmp_path)
    _seed_book(repo)

    summary = run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    assert summary["investigated"] == 12
    assert summary["audited"] == 12
    assert summary["counterfactuals_saved"] > 0
    assert summary["still_pending"] == 0


def test_the_tick_is_idempotent_and_reruns_produce_no_duplicates(tmp_path):
    repo = _repo(tmp_path)
    _seed_book(repo)

    run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")
    second = run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-2")

    assert second["investigated"] == 0
    assert len(repo.find_godfather_trade_investigations()) == 12
    assert len(repo.find_godfather_decision_audits()) == 12


def test_the_tick_does_nothing_when_the_layer_is_disabled(tmp_path):
    repo = _repo(tmp_path)
    _seed_book(repo)
    settings = _settings()
    settings.godfather.intelligence_enabled = False

    summary = run_godfather_intelligence_tick(repo, settings, _NOW, "run-1")

    assert "skipped" in summary
    assert repo.find_godfather_trade_investigations() == []


def test_the_tick_never_touches_the_positions_it_analyses(tmp_path):
    """The safety property, checked on real rows rather than argued: the
    analysed positions must be byte-identical afterwards."""
    repo = _repo(tmp_path)
    _seed_book(repo)
    before = [
        repo.get_position(f"pos-{i}").model_dump(mode="json") for i in range(12)
    ]

    run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    after = [repo.get_position(f"pos-{i}").model_dump(mode="json") for i in range(12)]
    assert before == after


def test_the_tick_writes_no_guardian_or_priority_heuristics(tmp_path):
    repo = _repo(tmp_path)
    _seed_book(repo)

    run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    assert repo.find_guardian_authority_heuristics() == []
    assert repo.find_godfather_priority_heuristics() == []
    assert repo.find_proposed_guardian_authority_heuristic_candidates() == []


def test_the_experience_sweep_reports_honest_sample_sizes(tmp_path):
    """Twelve trades cannot establish anything, and the sweep must say so
    rather than produce a confident-looking pattern."""
    repo = _repo(tmp_path)
    _seed_book(repo)

    summary = run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    by_class = summary["experience"]["by_edge_class"]
    assert set(by_class) <= {"INSUFFICIENT_DATA", "NOISE"}
    assert summary["experience"]["samples"] == 12


def test_prediction_errors_are_all_observation_only_without_a_proven_pattern(tmp_path):
    repo = _repo(tmp_path)
    _seed_book(repo)

    run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    rows = repo.find_godfather_prediction_errors()
    assert rows
    assert all(row["lesson"].startswith("OBSERVATION ONLY") for row in rows)


def test_open_positions_get_an_advisory_thesis_row_that_is_never_enforced(tmp_path):
    repo = _repo(tmp_path)
    _seed_book(repo)
    _seed_trade(
        repo,
        99,
        prices=[_ENTRY, Decimal("103")],
        exit_price=Decimal("0"),
        status="OPEN_POSITION",
    )

    summary = run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    rows = repo.find_godfather_position_thesis_for_position("pos-99")
    assert summary["thesis_rows"] == 1
    assert len(rows) == 1
    assert rows[0]["enforced"] == 0
    assert rows[0]["thesis_state"] in ("STRONG", "VALID", "WEAKENING", "INVALID", "EXIT")


def test_entry_quality_is_backfilled_advisory_only(tmp_path):
    repo = _repo(tmp_path)
    _seed_book(repo)

    run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    rows = repo.find_godfather_entry_quality_assessments()
    assert len(rows) == 12
    assert all(row["enforced"] == 0 for row in rows)
    assert all(row["expected_edge_class"] == "INSUFFICIENT_DATA" for row in rows)


def test_counterfactual_rows_carry_both_the_simulated_and_the_real_outcome(tmp_path):
    repo = _repo(tmp_path)
    _seed_book(repo)

    run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    rows = repo.find_godfather_counterfactuals_for_position("pos-0")
    assert rows
    for row in rows:
        assert row["actual_pnl_usdt"] is not None
        assert row["no_lookahead_verified"] == 1


def test_the_investigation_record_keeps_the_full_before_during_after_evidence(tmp_path):
    repo = _repo(tmp_path)
    _seed_book(repo)

    run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    detail = json.loads(repo.get_godfather_trade_investigation("pos-0")["detail_json"])
    assert "evidence_record" in detail["before"]
    assert detail["during"]["path_point_count"] == 3
    assert detail["after"]["exit_reason"] == "target"


def test_the_report_summarises_the_whole_layer_without_writing_anything(tmp_path):
    repo = _repo(tmp_path)
    _seed_book(repo)
    run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    report = build_report(repo)
    text = render_text(report)

    assert report["coverage"]["investigations"] == 12
    assert report["objective"]["trade_count"] == 12
    assert report["experience_memory"]["robust_conclusions"] == []
    assert "GODFATHER INTELLIGENCE REPORT" in text
    assert "component scoreboard" in text


def test_the_report_flags_a_policy_that_pays_for_itself_out_of_winning_trades(tmp_path):
    repo = _repo(tmp_path)
    _seed_book(repo)
    run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    policies = build_report(repo)["counterfactual_policies"]

    assert "REJECT_ENTRY" in policies
    # Rejecting every entry necessarily destroys the winners, so this row
    # must be flagged - if it is not, the winner/loser split is broken.
    assert policies["REJECT_ENTRY"]["damages_winners"] is True


def test_per_policy_results_survive_a_policy_that_is_never_scorable(tmp_path):
    """Regression: DELAY_ENTRY_60M cannot be scored on a book whose price
    paths are only an hour long, and intersecting every policy's scorable
    set against it emptied the entire report."""
    repo = _repo(tmp_path)
    _seed_book(repo)
    run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    report = build_report(repo)

    assert report["counterfactual_policies"] != {}
    assert report["counterfactual_comparison"]["comparable_positions"] == 0
    assert report["counterfactual_comparison"]["ranking"] == []


def test_a_position_with_no_observed_path_is_recorded_as_unknown_not_skipped(tmp_path):
    repo = _repo(tmp_path)
    _seed_trade(repo, 0, prices=[], exit_price=Decimal("97"), exit_reason="stop_loss")

    run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1")

    record = repo.get_godfather_trade_investigation("pos-0")
    assert record["classification"] == "UNKNOWN"
    assert record["path_point_count"] == 0


def test_the_batch_limit_is_respected_and_the_remainder_stays_pending(tmp_path):
    repo = _repo(tmp_path)
    _seed_book(repo)

    summary = run_godfather_intelligence_tick(repo, _settings(), _NOW, "run-1", batch_limit=5)

    assert summary["investigated"] == 5
    assert summary["still_pending"] == 7
