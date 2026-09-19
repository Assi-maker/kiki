"""Discovery-side LIVE capacity + capital gate (2026-09-19, AI-cost
optimization): decides, BEFORE any market fetch / candidate search / AI call,
how many candidates a discovery tick may send to expensive full analysis,
derived from free LIVE slots AND real BingX capital. It never replaces the
final pre-order gate in live_execution.process_pending_positions (that one is
covered in test_live_execution.py and again in the combination tests in
test_discovery_loop.py)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import LiveExecutionConfig
from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.paper_trading.live_discovery_gate import (
    LiveDiscoveryGate,
    affordable_live_slots,
)
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_discovery_loop import _seed_active_live_position

_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
_CFG = LiveExecutionConfig()  # max 4 concurrent, margin 10, buffer 1.00


class _Live:
    """Reports every seeded ACTIVE row as still genuinely open on the
    exchange, and a controllable balance - counts get_balance() calls so the
    cooldown/debounce tests can prove no network call happened."""

    def __init__(self, available="100.00", balance_raises=None):
        self.available = available
        self.balance_raises = balance_raises
        self.balance_calls = 0

    def get_position(self, symbol):
        return {"symbol": symbol, "positionAmt": "0.002"}

    def get_balance(self):
        self.balance_calls += 1
        if self.balance_raises is not None:
            raise self.balance_raises
        return {"availableMargin": self.available}


class _Market:
    def get_ticker(self, symbol):
        return {"lastPrice": "50000"}


def _repo_with_open(tmp_path, n_open):
    repo = SQLiteRepository(tmp_path / "t.db")
    for i in range(n_open):
        _seed_active_live_position(repo, f"live-pos-{i}")
    return repo


def _evaluate(repo, live, gate=None, cfg=_CFG, now=_NOW):
    gate = gate or LiveDiscoveryGate(cooldown_seconds=0)
    return gate.evaluate(repo, live, _Market(), cfg, "run-1", now)


# --- affordable_live_slots: the pure capital arithmetic ---------------------


def test_affordable_slots_uses_the_same_margin_plus_buffer_rule_as_the_execution_gate():
    # Execution gate: one position needs availableMargin >= margin + buffer.
    # n positions therefore need n * margin + buffer.
    cfg = _CFG
    assert affordable_live_slots(Decimal("10.99"), cfg) == 0
    assert affordable_live_slots(Decimal("11.00"), cfg) == 1
    assert affordable_live_slots(Decimal("20.99"), cfg) == 1
    assert affordable_live_slots(Decimal("21.00"), cfg) == 2
    assert affordable_live_slots(Decimal("0"), cfg) == 0
    assert affordable_live_slots(Decimal("-5"), cfg) == 0


# --- the requested combination matrix ---------------------------------------


def test_4_of_4_with_plenty_of_capital_suppresses_discovery_for_capacity(tmp_path):
    decision = _evaluate(_repo_with_open(tmp_path, 4), _Live(available="500"))

    assert decision.suppressed_reason == "capacity"
    assert decision.max_candidates == 0


def test_4_of_4_with_no_capital_suppresses_discovery(tmp_path):
    decision = _evaluate(_repo_with_open(tmp_path, 4), _Live(available="0"))

    assert decision.suppressed_reason == "capacity"  # capacity is checked first: cheapest, decisive
    assert decision.max_candidates == 0


def test_2_of_4_with_capital_for_2_allows_at_most_2_candidates(tmp_path):
    decision = _evaluate(_repo_with_open(tmp_path, 2), _Live(available="21.00"))

    assert decision.suppressed_reason is None
    assert (decision.free_slots, decision.affordable_slots, decision.usable_slots) == (2, 2, 2)
    assert decision.max_candidates == 2


def test_2_of_4_with_capital_for_only_1_allows_at_most_1_candidate(tmp_path):
    decision = _evaluate(_repo_with_open(tmp_path, 2), _Live(available="20.99"))

    assert decision.suppressed_reason is None
    assert (decision.free_slots, decision.affordable_slots, decision.usable_slots) == (2, 1, 1)
    assert decision.max_candidates == 1


def test_2_of_4_with_capital_for_0_suppresses_discovery_for_capital(tmp_path):
    decision = _evaluate(_repo_with_open(tmp_path, 2), _Live(available="10.99"))

    assert decision.suppressed_reason == "capital"
    assert decision.max_candidates == 0


def test_3_of_4_allows_at_most_1_candidate(tmp_path):
    decision = _evaluate(_repo_with_open(tmp_path, 3), _Live(available="500"))

    assert decision.max_candidates == 1


def test_1_of_4_with_capital_for_1_allows_exactly_the_one_usable_slot_by_default(tmp_path):
    decision = _evaluate(_repo_with_open(tmp_path, 1), _Live(available="11.00"))

    assert decision.max_candidates == 1  # within the spec'd "1-2" - default buffer is 0


def test_a_configured_candidate_buffer_adds_headroom(tmp_path):
    cfg = LiveExecutionConfig(discovery_candidate_buffer=1)

    decision = _evaluate(_repo_with_open(tmp_path, 1), _Live(available="11.00"), cfg=cfg)

    assert decision.max_candidates == 2  # 1 usable slot + 1 buffer


def test_a_configured_candidate_buffer_never_lifts_a_zero_budget(tmp_path):
    cfg = LiveExecutionConfig(discovery_candidate_buffer=1)

    decision = _evaluate(_repo_with_open(tmp_path, 1), _Live(available="5"), cfg=cfg)

    assert decision.max_candidates == 0


def test_0_of_4_with_capital_for_all_4_is_the_normal_uncapped_budget(tmp_path):
    decision = _evaluate(_repo_with_open(tmp_path, 0), _Live(available="500"))

    assert decision.suppressed_reason is None
    assert decision.max_candidates is None  # None == the normal max_candidates_per_discovery_run


def test_0_of_4_but_capital_for_only_2_is_capped_at_2(tmp_path):
    decision = _evaluate(_repo_with_open(tmp_path, 0), _Live(available="21.00"))

    assert decision.max_candidates == 2


# --- fail-closed -------------------------------------------------------------


def test_balance_lookup_failure_suppresses_discovery_fail_closed(tmp_path):
    live = _Live(balance_raises=ConnectorUnavailableError("BingX nere"))

    decision = _evaluate(_repo_with_open(tmp_path, 1), live)

    assert decision.suppressed_reason == "check_failed"
    assert decision.max_candidates == 0


def test_garbage_balance_payload_suppresses_discovery_instead_of_raising(tmp_path):
    decision = _evaluate(_repo_with_open(tmp_path, 1), _Live(available="not-a-number"))

    assert decision.suppressed_reason == "check_failed"


def test_missing_available_margin_field_is_treated_as_no_capital_not_as_a_guess(tmp_path):
    class _NoField(_Live):
        def get_balance(self):
            self.balance_calls += 1
            return {}

    decision = _evaluate(_repo_with_open(tmp_path, 1), _NoField())

    assert decision.suppressed_reason == "capital"


# --- cooldown / debounce and automatic resumption ----------------------------


def test_a_suppressed_decision_is_reused_within_the_cooldown_without_new_exchange_calls(tmp_path):
    repo = _repo_with_open(tmp_path, 2)
    live = _Live(available="5")
    gate = LiveDiscoveryGate(cooldown_seconds=300)

    first = _evaluate(repo, live, gate=gate, now=_NOW)
    second = _evaluate(repo, live, gate=gate, now=_NOW + timedelta(seconds=120))

    assert first.suppressed_reason == second.suppressed_reason == "capital"
    assert first.from_cache is False and second.from_cache is True
    assert live.balance_calls == 1  # the balance was NOT fetched again


def test_discovery_resumes_automatically_once_capital_returns_after_the_cooldown(tmp_path):
    repo = _repo_with_open(tmp_path, 2)
    live = _Live(available="5")
    gate = LiveDiscoveryGate(cooldown_seconds=300)
    assert _evaluate(repo, live, gate=gate, now=_NOW).suppressed_reason == "capital"

    live.available = "100"  # capital is back
    resumed = _evaluate(repo, live, gate=gate, now=_NOW + timedelta(seconds=301))

    assert resumed.suppressed_reason is None
    assert resumed.max_candidates == 2
    assert resumed.from_cache is False


def test_a_permissive_decision_is_never_served_from_cache(tmp_path):
    """Only suppressions are debounced. A positive budget authorises spending
    money, so it must always rest on a fresh reconciliation + balance."""
    repo = _repo_with_open(tmp_path, 2)
    live = _Live(available="100")
    gate = LiveDiscoveryGate(cooldown_seconds=300)

    _evaluate(repo, live, gate=gate, now=_NOW)
    _evaluate(repo, live, gate=gate, now=_NOW + timedelta(seconds=10))

    assert live.balance_calls == 2
