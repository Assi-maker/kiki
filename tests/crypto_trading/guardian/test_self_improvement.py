"""Tests for Task 3 of docs/superpowers/sdd/2026-09-15-guardian-authority-
live-autonomy/ - the PROPOSE step of the propose -> validate -> promote ->
track/demote pipeline (crypto_trading/guardian/self_improvement.py::
propose_candidate_heuristics).

Every test here runs the LLM call through MockAgentRunner (or a thin
MockAgentRunner subclass that only counts/records calls) - there is no code
path in this file that can construct RealClaudeRunner or reach the Anthropic
SDK, and no test asserts on anything that would require a network call. The
one test that must prove "no AI call happened at all" (budget exhausted)
does so with a call-counting runner rather than a raising one on purpose: a
raising runner would be swallowed by propose_candidate_heuristics' own
never-raise contract and the test would then pass even if the call HAD been
made.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.guardian.authority import heuristic_condition_matches
from crypto_trading.guardian.self_improvement import (
    _MAX_EVIDENCE_ROWS,
    _STRATEGIST_AGENT_FILE,
    propose_candidate_heuristics,
)
from crypto_trading.schemas.assessments import (
    GodfatherStrategistAssessment,
    ProposedHeuristic,
)
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.guardian.test_self_improvement_pre_entry_pool import (
    _BREAKEVEN_EXIT,
    _seed_closed_position,
)
from tests.crypto_trading.guardian.test_tick import _seed_candidate_and_position
from tests.crypto_trading.test_market_snapshot import _settings

_NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
_AGENT_NAME = "crypto-godfather-strategist"


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------
def _heuristic(
    description="TIGHTEN_SL underperforms while momentum_decay is already high",
    condition=None,
    adjustment=-0.2,
    rationale="18 of 24 resolved TIGHTEN_SL rows with momentum_decay >= 0.8 resolved wrong.",
    target_decision_type="TIGHTEN_SL",
) -> ProposedHeuristic:
    return ProposedHeuristic(
        description=description,
        condition={"guardian_state": "PROTECT", "momentum_decay_min": 0.8}
        if condition is None
        else condition,
        adjustment=adjustment,
        rationale=rationale,
        target_decision_type=target_decision_type,
    )


def _assessment(*heuristics, status="ok", run_id="run-1") -> GodfatherStrategistAssessment:
    return GodfatherStrategistAssessment(
        agent_name=_AGENT_NAME,
        run_id=run_id,
        created_at=_NOW,
        status=status,
        proposed_heuristics=list(heuristics),
    )


class _CountingRunner(MockAgentRunner):
    """MockAgentRunner plus a call log. Used instead of a raising stub for
    the "no AI call at all" assertions: propose_candidate_heuristics never
    raises by contract, so a raising stub would be swallowed and the test
    would pass vacuously."""

    def __init__(self, fixtures, **kwargs):
        super().__init__(fixtures, **kwargs)
        self.calls: list[tuple[str, dict, type]] = []

    def run(self, agent_def, context, output_schema):
        self.calls.append((agent_def.name, context, output_schema))
        return super().run(agent_def, context, output_schema)


class _RaisingRunner(MockAgentRunner):
    """An AgentRunner whose run() blows up - proves the never-raise
    contract, and that the watermark still advances on a hard failure."""

    def __init__(self):
        super().__init__(fixtures={})
        self.call_count = 0

    def run(self, agent_def, context, output_schema):
        self.call_count += 1
        raise RuntimeError("simulated transport explosion")


class _BilledRunner(_CountingRunner):
    """MockAgentRunner that reports a real billed cost, the way
    RealClaudeRunner does, so the AI-call/cost accounting can be asserted."""

    last_call_billed = True
    last_call_cost_usd = Decimal("0.07")


def _exhaust_budget(repo, now=_NOW, count=600):
    for i in range(count):
        repo.record_ai_call_event(
            Event(
                event_id=f"AI_CALL_MADE:exhaust:{i}",
                event_type="AI_CALL_MADE",
                aggregate_type="candidate",
                aggregate_id="exhaust",
                occurred_at=now,
                run_id="run-0",
                schema_version=1,
                payload={"role": "risk", "status": "ok", "cost_usd": "10.00"},
            )
        )


def _seed_resolved_shadow(repo, shadow_id="pos-1", decided_at=_NOW, expectation_correct=False):
    repo.seed_guardian_authority_shadow(
        shadow_id=shadow_id,
        position_id=shadow_id,
        candidate_id=shadow_id,
        instrument="BTCUSDT",
        opened_at=decided_at - timedelta(hours=4),
        created_at=decided_at - timedelta(hours=4),
        run_id="run-0",
    )
    repo.decide_guardian_authority_shadow(
        shadow_id=shadow_id,
        decision="TIGHTEN_SL",
        decided_at=decided_at,
        expected_outcome="TIGHTEN_SL: driven by 1 matched heuristic(s)",
        expected_direction="favorable",
        confidence=0.7,
        factors_json=json.dumps(
            {
                "time_decay": 0.4,
                "momentum_decay": 0.9,
                "volume_decay": 0.2,
                "funding_decay": 0.1,
                "secondary_confirmation_lost": 0.0,
                "market_regime": 0.3,
                "guardian_state": "PROTECT",
            }
        ),
        proposed_new_sl=Decimal("95"),
        updated_at=decided_at,
    )
    repo.resolve_guardian_authority_shadow_decided(
        shadow_id=shadow_id,
        actual_exit_reason="stop_loss",
        actual_pnl_usdt=Decimal("-12.5"),
        actual_closed_at=decided_at + timedelta(hours=2),
        expectation_correct=expectation_correct,
        prediction_error=0.8,
        updated_at=decided_at + timedelta(hours=2),
    )


def _seed_resolved_real_decision(
    repo, position_id="pos-1", decided_at=_NOW, expectation_correct=False
):
    """A resolved real TIGHTEN_SL decision PLUS the guardian_observations row
    whose observed_at is exactly the decision's decided_at - that exact-join
    is how authority.py::_reconstruct_tighten_sl_factors recovers the factors
    a real decision was made from, and this module reuses that unmodified
    function to build its context."""
    repo.save_guardian_observation(
        GuardianObservation(
            observation_id=f"{position_id}:{decided_at.isoformat()}",
            position_id=position_id,
            observed_at=decided_at,
            state="PROTECT",
            decay_score=Decimal("0.6"),
            progress_ratio=Decimal("0.2"),
            unrealized_pnl=Decimal("-3"),
            factors={
                "time_decay": 0.5,
                "momentum_decay": 0.85,
                "volume_decay": 0.3,
                "funding_decay": 0.1,
                "secondary_confirmation_lost": 0.0,
                "market_regime": 0.4,
            },
            run_id="run-0",
        )
    )
    decision_id = f"ga:tighten:{position_id}:{decided_at.isoformat()}"
    repo.save_guardian_authority_decision(
        decision_id=decision_id,
        position_id=position_id,
        candidate_id=position_id,
        decision_type="TIGHTEN_SL",
        decided_at=decided_at,
        reasoning="matched ga-hc:state:PROTECT",
        expected_outcome="TIGHTEN_SL: driven by 1 matched heuristic(s)",
        expected_direction="favorable",
        confidence=0.66,
        run_id="run-0",
        old_sl="90",
        new_sl="95",
        intervention_applied=True,
        matched_heuristic_ids_json=json.dumps(["ga-hc:state:PROTECT"]),
    )
    repo.resolve_guardian_authority_decision(
        decision_id,
        "stop_loss",
        "-12.5",
        expectation_correct,
        decided_at + timedelta(hours=2),
    )
    return decision_id


def _close_position(repo, position_id, closed_at, exit_reason="guardian_exit"):
    repo.close_position_with_event(
        position_id,
        Decimal("95"),
        Decimal("95"),
        exit_reason,
        Decimal("0.1"),
        Decimal("0"),
        closed_at,
        Event(
            event_id=f"POSITION_CLOSED:{position_id}",
            event_type="POSITION_CLOSED",
            aggregate_type="position",
            aggregate_id=position_id,
            occurred_at=closed_at,
            run_id="run-0",
            schema_version=1,
            payload={},
        ),
    )


def _seed_history(repo):
    """One closed position (so Detective's read-only stats have real
    content), one resolved shadow row, one resolved real decision row."""
    _seed_candidate_and_position(repo, position_id="pos-1", opened_at=_NOW - timedelta(hours=6))
    _close_position(repo, "pos-1", _NOW - timedelta(hours=1))
    _seed_resolved_shadow(repo)
    _seed_resolved_real_decision(repo)


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------
def test_propose_candidate_heuristics_saves_every_proposed_heuristic_with_its_own_fields(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    runner = _CountingRunner(
        fixtures={
            _AGENT_NAME: _assessment(
                _heuristic(),
                _heuristic(
                    description="TIGHTEN_SL works well while volume_decay is low",
                    condition={"guardian_state": "WATCH", "volume_decay_max": 0.2},
                    adjustment=0.18,
                    rationale="21 of 26 such rows resolved correct.",
                ),
            )
        }
    )

    saved = propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    assert saved == 2
    assert len(runner.calls) == 1
    assert runner.calls[0][0] == _AGENT_NAME
    assert runner.calls[0][2] is GodfatherStrategistAssessment

    first = repo.get_guardian_authority_heuristic_candidate("llm:run-1:0")
    second = repo.get_guardian_authority_heuristic_candidate("llm:run-1:1")
    assert first is not None and second is not None
    assert first["status"] == "PROPOSED"
    assert first["run_id"] == "run-1"
    assert first["proposed_at"] == _NOW.isoformat()
    assert first["description"] == "TIGHTEN_SL underperforms while momentum_decay is already high"
    assert json.loads(first["condition_json"]) == {
        "guardian_state": "PROTECT",
        "momentum_decay_min": 0.8,
    }
    assert first["proposed_adjustment"] == -0.2
    assert first["rationale"].startswith("18 of 24 resolved TIGHTEN_SL rows")
    assert second["description"] == "TIGHTEN_SL works well while volume_decay is low"
    assert json.loads(second["condition_json"]) == {
        "guardian_state": "WATCH",
        "volume_decay_max": 0.2,
    }
    assert second["proposed_adjustment"] == 0.18

    # Both rows are PROPOSED and therefore visible to the later validation
    # step, and nothing was written to the LIVE heuristics table.
    assert {
        row["candidate_id"] for row in repo.find_proposed_guardian_authority_heuristic_candidates()
    } == {"llm:run-1:0", "llm:run-1:1"}
    assert repo.find_guardian_authority_heuristics() == []


def test_propose_candidate_heuristics_persists_each_proposals_declared_target_decision_type(
    tmp_path,
):
    """Task 4B: the LLM declares which decision type each proposal is FOR,
    and that declaration is persisted verbatim - it is what routes the
    candidate to its own evidence pool at validation time (an explicit,
    auditable declaration rather than inferring intent from the condition's
    shape)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    runner = _CountingRunner(
        fixtures={
            _AGENT_NAME: _assessment(
                _heuristic(target_decision_type="TIGHTEN_SL"),
                _heuristic(
                    description="Entries on this trigger mix have mostly lost money",
                    condition={
                        "trigger_reasons": ["momentum_breakout"],
                        "candidate_score_max": 0.4,
                    },
                    adjustment=-0.25,
                    rationale="31 of 40 such entries closed at or below breakeven.",
                    target_decision_type="PRE_ENTRY_VETO",
                ),
            )
        }
    )

    assert propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW) == 2

    first = repo.get_guardian_authority_heuristic_candidate("llm:run-1:0")
    second = repo.get_guardian_authority_heuristic_candidate("llm:run-1:1")
    assert first["target_decision_type"] == "TIGHTEN_SL"
    assert second["target_decision_type"] == "PRE_ENTRY_VETO"


def test_saved_condition_json_round_trips_through_the_unmodified_condition_matcher(tmp_path):
    """The whole point of copying authority.py's condition-matching semantics
    into the agent's system prompt: what gets persisted must be directly
    consumable by the real, unmodified heuristic_condition_matches."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})

    propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    condition = json.loads(
        repo.get_guardian_authority_heuristic_candidate("llm:run-1:0")["condition_json"]
    )
    assert heuristic_condition_matches(
        condition, {"guardian_state": "PROTECT", "momentum_decay": 0.9}
    )
    assert not heuristic_condition_matches(
        condition, {"guardian_state": "PROTECT", "momentum_decay": 0.4}
    )
    assert not heuristic_condition_matches(condition, {"guardian_state": "WATCH"})


def test_propose_candidate_heuristics_records_the_ai_call_against_the_shared_budget(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    day_start = _NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    runner = _BilledRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})

    propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    assert repo.count_ai_calls_since(day_start) == 1
    assert repo.sum_ai_cost_since(day_start) == Decimal("0.07")


def test_propose_candidate_heuristics_context_carries_the_real_history_and_detective_stats(
    tmp_path,
):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment()})

    propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    context = runner.calls[0][1]
    assert context["run_id"] == "run-1"
    # (a) resolved shadow decisions, with their own factors parsed out.
    assert len(context["resolved_shadow_decisions"]) == 1
    shadow = context["resolved_shadow_decisions"][0]
    assert shadow["shadow_decision"] == "TIGHTEN_SL"
    assert shadow["expectation_correct"] is False
    assert shadow["factors"]["momentum_decay"] == 0.9
    assert shadow["factors"]["guardian_state"] == "PROTECT"
    # (b) resolved real decisions, with factors recovered via the unmodified
    # authority.py::_reconstruct_tighten_sl_factors exact-join.
    assert len(context["resolved_real_decisions"]) == 1
    real = context["resolved_real_decisions"][0]
    assert real["decision_type"] == "TIGHTEN_SL"
    assert real["expectation_correct"] is False
    assert real["factors"]["momentum_decay"] == 0.85
    assert real["factors"]["guardian_state"] == "PROTECT"
    # (c) Detective's read-only post-trade stats.
    assert context["historical_guardian_exit_effectiveness"]["guardian_exit"]["trade_count"] == 1
    assert context["historical_signal_type_breakdown"]
    # Anti-duplication + vocabulary guidance.
    assert context["existing_live_heuristics"] == []
    assert context["already_proposed_candidates"] == []
    assert "momentum_decay" in context["observed_factor_names"]
    assert "guardian_state" in context["observed_factor_names"]


def test_propose_candidate_heuristics_context_carries_real_pre_entry_evidence_and_outcomes(
    tmp_path,
):
    """Task 4B: a PRE_ENTRY_VETO proposal may only condition on
    `_pre_entry_factors`' own fields, so the context has to evidence those
    fields against real outcomes - otherwise the role's own "never invent a
    factor name / never cite evidence you were not given" discipline would
    make the whole second decision type unusable. The vocabulary lists stay
    SEPARATE: `guardian_state` is a TIGHTEN_SL-only factor and must not
    appear among the pre-entry names."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)  # one closed position, with its own real candidate
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment()})

    propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    context = runner.calls[0][1]
    assert len(context["closed_position_entry_outcomes"]) == 1
    entry = context["closed_position_entry_outcomes"][0]
    assert entry["factors"] == {
        "instrument": "BTCUSDT",
        "candidate_score": 0.5,
        "trigger_reasons": ["momentum_breakout"],
    }
    assert entry["exit_reason"] == "guardian_exit"
    # Entry 100 -> exit 95 on a size-1000 position, minus 0.1 in fees.
    assert entry["pnl_usdt"] == "-50.10"
    assert entry["closed_at"] == (_NOW - timedelta(hours=1)).isoformat()
    assert context["pre_entry_factor_names"] == [
        "candidate_score",
        "instrument",
        "trigger_reasons",
    ]
    assert "guardian_state" not in context["pre_entry_factor_names"]


def test_propose_candidate_heuristics_context_excludes_exposure_blocked_positions(tmp_path):
    """Review fix (round 1): a size-0, exposure-blocked position has no real
    realized outcome and is already excluded from
    `historical_signal_type_breakdown`/`historical_guardian_exit_
    effectiveness` (detective/stats.py, 2026-09-03 user ruling). Including
    it in the raw entry-outcome list would hand the model two mutually
    inconsistent views of the same trade history - and a "pnl_usdt" of
    roughly zero that no real trade ever produced."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)  # one REAL closed position
    _seed_closed_position(
        repo,
        "blocked-by-exposure",
        _NOW - timedelta(hours=2),
        _BREAKEVEN_EXIT,
        size=Decimal("0"),
    )
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment()})

    propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    outcomes = runner.calls[0][1]["closed_position_entry_outcomes"]
    assert len(outcomes) == 1
    assert outcomes[0]["closed_at"] == (_NOW - timedelta(hours=1)).isoformat()


# --------------------------------------------------------------------------
# Gate 1: budget
# --------------------------------------------------------------------------
def test_propose_candidate_heuristics_does_nothing_at_all_when_budget_is_exhausted(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    _exhaust_budget(repo)
    day_start = _NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    cost_before = repo.sum_ai_cost_since(day_start)
    calls_before = repo.count_ai_calls_since(day_start)
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})

    saved = propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    assert saved == 0
    assert runner.calls == []  # no AI call was even attempted
    assert repo.find_proposed_guardian_authority_heuristic_candidates() == []
    # Nothing happened at all: no cost, no extra AI_CALL_MADE row, and the
    # once-per-day slot is explicitly NOT consumed - a later tick today, once
    # budget frees up, must still be allowed to propose.
    assert repo.sum_ai_cost_since(day_start) == cost_before
    assert repo.count_ai_calls_since(day_start) == calls_before
    assert repo.get_guardian_authority_strategist_last_proposed_date() is None


# --------------------------------------------------------------------------
# Gate 2: once-per-UTC-day watermark
# --------------------------------------------------------------------------
def test_propose_candidate_heuristics_blocks_a_second_call_on_the_same_utc_day(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    settings = _settings()
    first_runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})
    assert propose_candidate_heuristics(repo, first_runner, settings, "run-1", _NOW) == 1
    assert repo.get_guardian_authority_strategist_last_proposed_date() == "2026-09-15"

    later_same_day = _NOW + timedelta(hours=9)
    second_runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})

    saved = propose_candidate_heuristics(repo, second_runner, settings, "run-2", later_same_day)

    assert saved == 0
    assert second_runner.calls == []  # budget was fine; the watermark alone blocked it
    assert [
        row["candidate_id"] for row in repo.find_proposed_guardian_authority_heuristic_candidates()
    ] == ["llm:run-1:0"]
    assert repo.get_guardian_authority_strategist_last_proposed_date() == "2026-09-15"


def test_propose_candidate_heuristics_allows_a_fresh_call_on_the_next_utc_day(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    settings = _settings()
    assert (
        propose_candidate_heuristics(
            repo,
            _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())}),
            settings,
            "run-1",
            _NOW,
        )
        == 1
    )

    next_day = _NOW + timedelta(days=1)
    next_runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})

    saved = propose_candidate_heuristics(repo, next_runner, settings, "run-2", next_day)

    assert saved == 1
    assert len(next_runner.calls) == 1
    assert {
        row["candidate_id"] for row in repo.find_proposed_guardian_authority_heuristic_candidates()
    } == {"llm:run-1:0", "llm:run-2:0"}
    assert repo.get_guardian_authority_strategist_last_proposed_date() == "2026-09-16"


def test_propose_candidate_heuristics_treats_the_utc_day_boundary_as_the_day_key(tmp_path):
    """23:59:59Z and 00:00:00Z the next day are different proposal days, one
    second apart - the watermark key is the UTC calendar date, exactly the
    same day boundary guardian/tick.py::_utc_day_start already uses for the
    AI budget window."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    settings = _settings()
    late = datetime(2026, 9, 15, 23, 59, 59, tzinfo=UTC)
    just_after = datetime(2026, 9, 16, 0, 0, 0, tzinfo=UTC)

    assert (
        propose_candidate_heuristics(
            repo,
            _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())}),
            settings,
            "run-1",
            late,
        )
        == 1
    )
    assert repo.get_guardian_authority_strategist_last_proposed_date() == "2026-09-15"

    assert (
        propose_candidate_heuristics(
            repo,
            _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())}),
            settings,
            "run-2",
            just_after,
        )
        == 1
    )
    assert repo.get_guardian_authority_strategist_last_proposed_date() == "2026-09-16"


# --------------------------------------------------------------------------
# Zero-heuristic response is a SUCCESS, not a failure
# --------------------------------------------------------------------------
def test_propose_candidate_heuristics_treats_an_empty_proposal_list_as_a_successful_call(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment()})  # zero heuristics

    saved = propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    assert saved == 0
    assert len(runner.calls) == 1  # the call really happened
    assert repo.find_proposed_guardian_authority_heuristic_candidates() == []
    # "the data does not support a confident pattern" is a legitimate answer
    # and still uses up today's proposal slot.
    assert repo.get_guardian_authority_strategist_last_proposed_date() == "2026-09-15"


# --------------------------------------------------------------------------
# Failure paths: zero candidates, and a failed AI CALL releases the day's slot
# (2026-09-19: an unusable call - failed/timed out/raised - produced no
# proposal, so it must not burn the once-per-day slot; only a call that
# actually returned an answer, even an empty one, consumes it).
# --------------------------------------------------------------------------
def test_propose_candidate_heuristics_releases_the_slot_on_a_failed_agent_response(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})
    runner._fail_agents = {_AGENT_NAME}

    saved = propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    assert saved == 0
    assert len(runner.calls) == 1
    assert repo.find_proposed_guardian_authority_heuristic_candidates() == []
    assert repo.get_guardian_authority_strategist_last_proposed_date() is None
    # a later tick the same day really does try again
    propose_candidate_heuristics(repo, runner, _settings(), "run-2", _NOW + timedelta(minutes=10))
    assert len(runner.calls) == 2


def test_propose_candidate_heuristics_releases_the_slot_on_a_timed_out_agent_response(
    tmp_path,
):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})
    runner._timeout_agents = {_AGENT_NAME}

    saved = propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    assert saved == 0
    assert len(runner.calls) == 1
    assert repo.find_proposed_guardian_authority_heuristic_candidates() == []
    assert repo.get_guardian_authority_strategist_last_proposed_date() is None


def test_propose_candidate_heuristics_never_raises_and_releases_the_slot_when_the_runner_explodes(
    tmp_path,
):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    runner = _RaisingRunner()

    saved = propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    assert saved == 0
    assert runner.call_count == 1
    assert repo.find_proposed_guardian_authority_heuristic_candidates() == []
    assert repo.get_guardian_authority_strategist_last_proposed_date() is None


def test_propose_candidate_heuristics_failed_call_restores_the_previous_days_watermark(tmp_path):
    """A release must put back what was there before the claim (yesterday's
    watermark), not blank it."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    repo.set_guardian_authority_strategist_last_proposed_date("2026-09-14", _NOW - timedelta(days=1))
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})
    runner._fail_agents = {_AGENT_NAME}

    propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    assert repo.get_guardian_authority_strategist_last_proposed_date() == "2026-09-14"


def test_propose_candidate_heuristics_never_raises_when_the_repository_read_explodes(tmp_path):
    """Context building is I/O and can fail for reasons that have nothing to
    do with the AI call - it must still be contained, and still burn the
    day's slot rather than retry a broken read every tick."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})

    def _boom():
        raise RuntimeError("simulated database failure")

    repo.find_resolved_guardian_authority_shadows = _boom

    saved = propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    assert saved == 0
    assert runner.calls == []
    assert repo.find_proposed_guardian_authority_heuristic_candidates() == []
    assert repo.get_guardian_authority_strategist_last_proposed_date() == "2026-09-15"


def test_propose_candidate_heuristics_never_raises_when_the_watermark_write_explodes(tmp_path):
    """Review fix, round 1, Finding 1. The watermark claim is a SQLite
    WRITE, and both gates before it are SQLite READS - under concurrent
    tick/write contention any of them can raise
    `sqlite3.OperationalError: database is locked`. Before the fix these
    four calls sat OUTSIDE the try/except, so such an error propagated
    straight out of propose_candidate_heuristics into the hosting trading
    tick. The pre-existing "repository read explodes" test could not catch
    this: it patches a method that was already inside the try."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})

    def _boom(date_iso, updated_at):
        raise sqlite3.OperationalError("database is locked")

    repo.set_guardian_authority_strategist_last_proposed_date = _boom

    saved = propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    assert saved == 0
    assert runner.calls == []  # it failed before the AI call
    assert repo.find_proposed_guardian_authority_heuristic_candidates() == []


def test_propose_candidate_heuristics_never_raises_when_the_budget_gate_explodes(tmp_path):
    """The other half of Finding 1: the budget gate is itself two SQLite
    reads, and it runs before anything else in the function."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())})

    def _boom(cutoff):
        raise sqlite3.OperationalError("database is locked")

    repo.count_ai_calls_since = _boom

    saved = propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    assert saved == 0
    assert runner.calls == []
    # The gate never passed, so the day's slot must remain unclaimed.
    assert repo.get_guardian_authority_strategist_last_proposed_date() is None


# --------------------------------------------------------------------------
# Evidence window (review fix, round 1, Finding 2)
# --------------------------------------------------------------------------
def test_propose_candidate_heuristics_bounds_the_evidence_to_the_newest_rows(tmp_path):
    """Unbounded, the three history reads grow forever: every resolved
    shadow, resolved decision and closed position ever recorded would be
    serialized into a PAID call, until one day the prompt crosses the
    model's context limit and the call fails - permanently, one wasted paid
    attempt per day, since the day's slot is claimed up front. The window
    keeps the NEWEST rows, which are also the relevant ones."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    overflow = _MAX_EVIDENCE_ROWS + 5
    # Seeded oldest-first, so the rows that must survive the window are the
    # LAST ones written, not simply "the ones that happened to be seeded".
    for i in range(overflow):
        decided_at = _NOW + timedelta(minutes=i)
        _seed_resolved_shadow(repo, shadow_id=f"shadow-{i:04d}", decided_at=decided_at)
        _seed_resolved_real_decision(repo, position_id=f"real-{i:04d}", decided_at=decided_at)
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment()})

    propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    context = runner.calls[0][1]
    assert len(context["resolved_shadow_decisions"]) == _MAX_EVIDENCE_ROWS
    assert len(context["resolved_real_decisions"]) == _MAX_EVIDENCE_ROWS
    # Newest-first, and the oldest rows really were dropped rather than the
    # newest ones truncated away.
    newest = _NOW + timedelta(minutes=overflow - 1)
    assert context["resolved_shadow_decisions"][0]["decided_at"] == newest.isoformat()
    assert context["resolved_real_decisions"][0]["decided_at"] == newest.isoformat()
    kept_shadow_ids = {row["shadow_id"] for row in context["resolved_shadow_decisions"]}
    assert f"shadow-{overflow - 1:04d}" in kept_shadow_ids
    assert "shadow-0000" not in kept_shadow_ids
    assert "pos-1" not in kept_shadow_ids  # the oldest row of all, from _seed_history


def test_propose_candidate_heuristics_bounds_the_closed_position_read_and_its_n_plus_1(tmp_path):
    """Closed positions feed Detective's aggregates rather than the prompt
    directly, but the read was also an unbounded N+1 (one get_candidate per
    closed position). The window is applied BEFORE those lookups."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    # All strictly newer than _seed_history's own pos-1 (closed at _NOW-1h),
    # so the surviving window is exactly this batch and the assertion below
    # cannot be satisfied by an accidental mix of the two.
    for i in range(_MAX_EVIDENCE_ROWS + 5):
        closed_at = _NOW - timedelta(seconds=i + 1)
        _seed_candidate_and_position(
            repo, position_id=f"closed-{i:04d}", opened_at=closed_at - timedelta(hours=2)
        )
        _close_position(repo, f"closed-{i:04d}", closed_at)
    looked_up: list[str] = []
    real_get_candidate = repo.get_candidate

    def _counting_get_candidate(candidate_id):
        looked_up.append(candidate_id)
        return real_get_candidate(candidate_id)

    repo.get_candidate = _counting_get_candidate
    runner = _CountingRunner(fixtures={_AGENT_NAME: _assessment()})

    propose_candidate_heuristics(repo, runner, _settings(), "run-1", _NOW)

    assert len(looked_up) == _MAX_EVIDENCE_ROWS
    effectiveness = runner.calls[0][1]["historical_guardian_exit_effectiveness"]
    assert effectiveness["guardian_exit"]["trade_count"] == _MAX_EVIDENCE_ROWS


def test_propose_candidate_heuristics_is_idempotent_within_one_run_id(tmp_path):
    """Two calls on different days with the SAME run_id produce the same
    deterministic candidate_ids; save_guardian_authority_heuristic_candidate's
    INSERT OR IGNORE keeps that a no-op instead of a duplicate row, and the
    count returned reflects rows actually written."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_history(repo)
    settings = _settings()
    assert (
        propose_candidate_heuristics(
            repo,
            _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())}),
            settings,
            "run-1",
            _NOW,
        )
        == 1
    )

    saved = propose_candidate_heuristics(
        repo,
        _CountingRunner(fixtures={_AGENT_NAME: _assessment(_heuristic())}),
        settings,
        "run-1",
        _NOW + timedelta(days=1),
    )

    assert saved == 0
    assert len(repo.find_proposed_guardian_authority_heuristic_candidates()) == 1


# --------------------------------------------------------------------------
# The agent role file itself
# --------------------------------------------------------------------------
def test_strategist_agent_definition_loads_with_the_expected_frontmatter():
    definition = load_agent_definition(_STRATEGIST_AGENT_FILE)
    assert definition.name == _AGENT_NAME
    assert definition.tools == ["Read"]
    assert definition.description


def test_strategist_system_prompt_reproduces_the_condition_matching_semantics():
    """Not a vague reference - the actual rules, verbatim enough that the
    model can produce `condition` dicts the unmodified
    heuristic_condition_matches will really evaluate. A prompt that got this
    wrong would make every candidate this agent ever proposes silently
    unusable (fail-closed matching means an unknown key never matches)."""
    prompt = load_agent_definition(_STRATEGIST_AGENT_FILE).system_prompt

    # The three requirement kinds, by name.
    assert "_max" in prompt
    assert "_min" in prompt
    assert "numeric upper bound" in prompt
    assert "numeric lower bound" in prompt
    assert "list-membership" in prompt
    assert "equality" in prompt
    # AND across keys, and the two opposite empty-collection conventions.
    assert "logical AND across keys" in prompt
    assert "vacuously satisfied by everything" in prompt
    assert "An EMPTY condition list never matches" in prompt


def test_strategist_system_prompt_forbids_an_empty_condition_outright():
    """C1 (final whole-branch review, 2026-09-17). The prompt used to
    characterize an empty `condition` ({}) as "a legitimate 'always-on'
    base-rate heuristic" in its semantics section while forbidding it a few
    lines later - a self-contradiction, and on the wrong side of a guard that
    now REJECTS such a candidate at validation time. The prompt must now say
    only the latter, in both places."""
    prompt = load_agent_definition(_STRATEGIST_AGENT_FILE).system_prompt
    assert "always-on" not in prompt
    assert "ALDRIG får föreslå ett tomt `condition`" in prompt
    assert "AVVISAR (`REJECTED`)" in prompt
    # Fail-closed on missing factors.
    assert "A missing key in `factors` never satisfies any requirement" in prompt
    assert "fail-closed" in prompt
    # The exact function whose semantics these are.
    assert "heuristic_condition_matches" in prompt


def test_strategist_system_prompt_states_it_only_proposes_candidates_for_validation():
    prompt = load_agent_definition(_STRATEGIST_AGENT_FILE).system_prompt
    assert "KANDIDATER" in prompt
    assert "out-of-sample" in prompt
    assert "noll effekt" in prompt


def test_strategist_system_prompt_explicitly_allows_proposing_zero_heuristics():
    prompt = load_agent_definition(_STRATEGIST_AGENT_FILE).system_prompt
    assert "NOLL" in prompt
    assert "0-N" in prompt
