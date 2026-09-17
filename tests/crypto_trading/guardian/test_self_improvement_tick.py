"""Tests for Task 7 of docs/superpowers/sdd/2026-09-15-guardian-authority-
live-autonomy/ - the orchestrator that wires the propose -> validate ->
promote -> track/demote pipeline (Tasks 3-6) into a single periodic tick
call: crypto_trading.guardian.self_improvement.run_godfather_self_improvement_
tick.

This file tests ONLY the orchestration contract (call order, per-step
failure isolation, per-step log_event names) via monkeypatched spies on the
four already-tested step functions - it does not re-test any step's own
internal behavior (that's test_self_improvement.py /
test_self_improvement_promotion.py / test_self_improvement_demotion.py's
job). The one exception is
test_a_candidate_proposed_this_tick_is_validated_in_the_same_call, which
runs the real validate/promote/track functions end-to-end (only propose is
stubbed, to avoid needing a full AI fixture) to pin down and prove the
deliberate "no re-query gate; validate simply runs against the live
repository right after propose, so it sees the same tick's write" behavior
this task chose.

Flag-off "complete no-op" coverage lives in test_discovery_loop.py, next to
where settings.guardian.authority_enabled is actually consulted -
run_godfather_self_improvement_tick ITSELF never reads that flag; gating the
whole pipeline is the wiring's job, not the orchestrator's own (Task 7
brief: "Wire this WHOLE function into the chosen loop... gated by
settings.guardian.authority_enabled" describes the CALL SITE, not the
function body)."""

import json
import logging
from datetime import UTC, datetime

import pytest

import crypto_trading.guardian.self_improvement as self_improvement_module
from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.guardian.self_improvement import run_godfather_self_improvement_tick
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_market_snapshot import _settings

_NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _enabled_settings():
    """`_settings()` with `guardian.authority_enabled` flipped ON, in memory
    only (never guardian.yaml, which must stay false).

    Needed since the 2026-09-17 fix wave added a defense-in-depth internal
    gate to `run_godfather_self_improvement_tick` itself: every test below is
    about what the orchestrator does when it is ALLOWED to run, so each one
    now has to say so explicitly. The flag-off behavior has its own test at
    the end of this file (and its call-site coverage in
    test_discovery_loop.py)."""
    settings = _settings()
    return settings.model_copy(
        update={"guardian": settings.guardian.model_copy(update={"authority_enabled": True})}
    )


_STEP_NAMES = [
    "propose_candidate_heuristics",
    "validate_pending_heuristic_candidates",
    "promote_validated_heuristic_candidates",
    "track_and_demote_underperforming_heuristics",
]


# ---------------------------------------------------------------------------
# Spies
# ---------------------------------------------------------------------------
class _CallRecorder:
    """Records every call's positional/keyword args - same idiom
    test_monitoring_loop.py's own _CallRecorder uses for spying on module-
    level functions via monkeypatch."""

    def __init__(self, return_value=0):
        self.calls: list[tuple[tuple, dict]] = []
        self._return_value = return_value

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self._return_value


class _OrderRecorder(_CallRecorder):
    """Like _CallRecorder, but also appends its own label to a list shared
    across all four spies, so a single assertion on that list proves
    relative call ORDER, not just each spy's own independent call count."""

    def __init__(self, label, order, return_value=0):
        super().__init__(return_value=return_value)
        self.label = label
        self.order = order

    def __call__(self, *args, **kwargs):
        self.order.append(self.label)
        return super().__call__(*args, **kwargs)


class _RaisingRecorder:
    """Raises unconditionally - proves a step's own failure is caught and
    does not propagate out of run_godfather_self_improvement_tick."""

    def __init__(self, exc):
        self._exc = exc
        self.call_count = 0

    def __call__(self, *args, **kwargs):
        self.call_count += 1
        raise self._exc


def _propose_stub_writes_one_candidate(repo, runner, settings, run_id, now):
    """Stands in for propose_candidate_heuristics: writes exactly one real
    PROPOSED candidate row via the existing, unmodified repository method,
    without needing a full AI fixture."""
    repo.save_guardian_authority_heuristic_candidate(
        candidate_id="same-tick-cand",
        description="same-tick description",
        condition_json=json.dumps({"guardian_state": "PROTECT"}),
        proposed_adjustment=0.5,
        rationale="same-tick rationale",
        run_id=run_id,
        proposed_at=now,
        target_decision_type="TIGHTEN_SL",
    )
    return 1


def _monkeypatch_all_steps(monkeypatch, overrides: dict):
    """Monkeypatches all four step names on the self_improvement module,
    using overrides[name] where given and a fresh _CallRecorder() (a no-op
    spy, returning 0) otherwise."""
    for name in _STEP_NAMES:
        monkeypatch.setattr(self_improvement_module, name, overrides.get(name, _CallRecorder()))


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
def test_run_godfather_self_improvement_tick_calls_all_four_steps_in_order(tmp_path, monkeypatch):
    order: list[str] = []
    propose_spy = _OrderRecorder("propose", order)
    validate_spy = _OrderRecorder("validate", order)
    promote_spy = _OrderRecorder("promote", order)
    track_spy = _OrderRecorder("track", order)
    _monkeypatch_all_steps(
        monkeypatch,
        {
            "propose_candidate_heuristics": propose_spy,
            "validate_pending_heuristic_candidates": validate_spy,
            "promote_validated_heuristic_candidates": promote_spy,
            "track_and_demote_underperforming_heuristics": track_spy,
        },
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner({})
    settings = _enabled_settings()

    run_godfather_self_improvement_tick(repo, runner, settings, "run-1", _NOW)

    assert order == ["propose", "validate", "promote", "track"]
    assert len(propose_spy.calls) == 1
    assert len(validate_spy.calls) == 1
    assert len(promote_spy.calls) == 1
    assert len(track_spy.calls) == 1

    propose_args, propose_kwargs = propose_spy.calls[0]
    assert propose_kwargs == {}
    assert propose_args == (repo, runner, settings, "run-1", _NOW)

    validate_args, validate_kwargs = validate_spy.calls[0]
    assert validate_kwargs == {}
    assert validate_args == (repo, _NOW)

    promote_args, promote_kwargs = promote_spy.calls[0]
    assert promote_kwargs == {}
    assert promote_args == (repo, _NOW)

    track_args, track_kwargs = track_spy.calls[0]
    assert track_kwargs == {}
    assert track_args == (repo, _NOW)


# ---------------------------------------------------------------------------
# Per-step failure isolation
# ---------------------------------------------------------------------------
def test_a_propose_failure_does_not_block_validate_promote_track(tmp_path, monkeypatch):
    validate_spy = _CallRecorder()
    promote_spy = _CallRecorder()
    track_spy = _CallRecorder()
    _monkeypatch_all_steps(
        monkeypatch,
        {
            "propose_candidate_heuristics": _RaisingRecorder(RuntimeError("boom")),
            "validate_pending_heuristic_candidates": validate_spy,
            "promote_validated_heuristic_candidates": promote_spy,
            "track_and_demote_underperforming_heuristics": track_spy,
        },
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner({})
    settings = _enabled_settings()

    run_godfather_self_improvement_tick(repo, runner, settings, "run-1", _NOW)  # must not raise

    assert len(validate_spy.calls) == 1
    assert len(promote_spy.calls) == 1
    assert len(track_spy.calls) == 1


def test_a_validate_failure_does_not_block_promote_track(tmp_path, monkeypatch):
    propose_spy = _CallRecorder()
    promote_spy = _CallRecorder()
    track_spy = _CallRecorder()
    _monkeypatch_all_steps(
        monkeypatch,
        {
            "propose_candidate_heuristics": propose_spy,
            "validate_pending_heuristic_candidates": _RaisingRecorder(RuntimeError("boom")),
            "promote_validated_heuristic_candidates": promote_spy,
            "track_and_demote_underperforming_heuristics": track_spy,
        },
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner({})
    settings = _enabled_settings()

    run_godfather_self_improvement_tick(repo, runner, settings, "run-1", _NOW)

    assert len(propose_spy.calls) == 1
    assert len(promote_spy.calls) == 1
    assert len(track_spy.calls) == 1


def test_a_promote_failure_does_not_block_track(tmp_path, monkeypatch):
    propose_spy = _CallRecorder()
    validate_spy = _CallRecorder()
    track_spy = _CallRecorder()
    _monkeypatch_all_steps(
        monkeypatch,
        {
            "propose_candidate_heuristics": propose_spy,
            "validate_pending_heuristic_candidates": validate_spy,
            "promote_validated_heuristic_candidates": _RaisingRecorder(RuntimeError("boom")),
            "track_and_demote_underperforming_heuristics": track_spy,
        },
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner({})
    settings = _enabled_settings()

    run_godfather_self_improvement_tick(repo, runner, settings, "run-1", _NOW)

    assert len(propose_spy.calls) == 1
    assert len(validate_spy.calls) == 1
    assert len(track_spy.calls) == 1


def test_a_track_failure_never_propagates(tmp_path, monkeypatch):
    propose_spy = _CallRecorder()
    validate_spy = _CallRecorder()
    promote_spy = _CallRecorder()
    _monkeypatch_all_steps(
        monkeypatch,
        {
            "propose_candidate_heuristics": propose_spy,
            "validate_pending_heuristic_candidates": validate_spy,
            "promote_validated_heuristic_candidates": promote_spy,
            "track_and_demote_underperforming_heuristics": _RaisingRecorder(RuntimeError("boom")),
        },
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner({})
    settings = _enabled_settings()

    run_godfather_self_improvement_tick(repo, runner, settings, "run-1", _NOW)  # must not raise

    assert len(propose_spy.calls) == 1
    assert len(validate_spy.calls) == 1
    assert len(promote_spy.calls) == 1


# ---------------------------------------------------------------------------
# Per-step log_event names (each step gets its OWN event name - never a
# shared/generic one, so a failure's step of origin is always identifiable
# from logs alone)
# ---------------------------------------------------------------------------
_EXPECTED_FAILURE_EVENTS = {
    "propose_candidate_heuristics": "godfather_self_improvement_propose_failed",
    "validate_pending_heuristic_candidates": "godfather_self_improvement_validate_failed",
    "promote_validated_heuristic_candidates": "godfather_self_improvement_promote_failed",
    "track_and_demote_underperforming_heuristics": "godfather_self_improvement_track_failed",
}


@pytest.mark.parametrize("failing_step", _STEP_NAMES)
def test_each_step_failure_logs_its_own_distinct_event_name(
    tmp_path, monkeypatch, caplog, failing_step
):
    overrides = {failing_step: _RaisingRecorder(RuntimeError("boom"))}
    _monkeypatch_all_steps(monkeypatch, overrides)

    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner({})
    settings = _enabled_settings()

    with caplog.at_level(logging.INFO, logger="crypto_trading"):
        run_godfather_self_improvement_tick(repo, runner, settings, "run-1", _NOW)

    expected_event = _EXPECTED_FAILURE_EVENTS[failing_step]
    assert expected_event in caplog.text
    for step_name, event in _EXPECTED_FAILURE_EVENTS.items():
        if step_name != failing_step:
            assert event not in caplog.text


# ---------------------------------------------------------------------------
# Same-tick propose -> validate behavior (deliberate design choice, Task 7
# brief: "decide and test whichever behavior is chosen")
# ---------------------------------------------------------------------------
def test_a_candidate_proposed_this_tick_is_validated_in_the_same_call(tmp_path, monkeypatch):
    """Chosen behavior: validate_pending_heuristic_candidates is called
    unconditionally right after propose_candidate_heuristics, against the
    SAME live repository, with no re-query gate suppressing rows written
    this same tick. propose_candidate_heuristics commits its candidate row
    to SQLite before returning, so by the time validate runs (moments
    later, same call), its own find_proposed_guardian_authority_heuristic_
    candidates() query already sees it.

    Proven here with zero evidence seeded anywhere: an out-of-sample
    validation over empty train/test pools cannot clear the sample-size
    bar (_validation_outcome: train_n=0 < _MIN_SAMPLE_SIZE), so the
    candidate is deterministically transitioned PROPOSED -> REJECTED.
    Nothing else in this test touches this row, so any transition away
    from PROPOSED is proof validate examined it within this same call."""
    monkeypatch.setattr(
        self_improvement_module,
        "propose_candidate_heuristics",
        _propose_stub_writes_one_candidate,
    )

    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner({})
    settings = _enabled_settings()

    run_godfather_self_improvement_tick(repo, runner, settings, "run-1", _NOW)

    row = repo.get_guardian_authority_heuristic_candidate("same-tick-cand")
    assert row is not None
    assert row["status"] == "REJECTED"


# ---------------------------------------------------------------------------
# The orchestrator's OWN authority_enabled gate (Task 7 gap, found by the
# 2026-09-17 final whole-branch review).
#
# Until this fix the flag was consulted at exactly one place - the call site
# in discovery_loop.py. That is correct but thin for a function with this
# blast radius: it is a WRITE path into the very table the live decision core
# reads on every real decision, and anything that calls it directly (a future
# loop, a maintenance script, a test, a REPL) would run the whole pipeline
# with the feature switched off. The call-site gate STAYS (see
# test_discovery_loop.py and the isolation suite's own exclusivity check);
# this is defense in depth, not a replacement.
# ---------------------------------------------------------------------------
def test_the_orchestrator_is_a_complete_no_op_when_authority_is_disabled(tmp_path, monkeypatch):
    """Called DIRECTLY - no discovery_loop.py anywhere in this test - with
    authority_enabled False: zero calls to all four pipeline steps."""
    spies = {name: _CallRecorder() for name in _STEP_NAMES}
    _monkeypatch_all_steps(monkeypatch, spies)

    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner({})
    settings = _settings()  # guardian.authority_enabled is False by default
    assert settings.guardian.authority_enabled is False

    run_godfather_self_improvement_tick(repo, runner, settings, "run-1", _NOW)

    for name, spy in spies.items():
        assert spy.calls == [], f"{name} was called while authority_enabled is False"
    # And nothing whatsoever was written.
    assert repo.find_proposed_guardian_authority_heuristic_candidates() == []
    assert repo.find_guardian_authority_heuristics() == []


def test_the_orchestrator_gate_fails_closed_on_an_unreadable_flag(tmp_path, monkeypatch):
    """The gate must never be the thing that raises inside a trading tick:
    a settings object without a readable guardian.authority_enabled is
    treated as OFF, not as an error and not as ON."""
    spies = {name: _CallRecorder() for name in _STEP_NAMES}
    _monkeypatch_all_steps(monkeypatch, spies)

    class _BrokenSettings:
        @property
        def guardian(self):
            raise RuntimeError("settings exploded")

    repo = SQLiteRepository(tmp_path / "t.db")
    runner = MockAgentRunner({})

    run_godfather_self_improvement_tick(repo, runner, _BrokenSettings(), "run-1", _NOW)

    for name, spy in spies.items():
        assert spy.calls == [], f"{name} was called despite an unreadable flag"
