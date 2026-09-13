from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


def _open_position(
    repo: SQLiteRepository, position_id: str = "pos-1", opened_at: datetime = _NOW
) -> Position:
    position = Position(
        position_id=position_id,
        candidate_id=position_id,
        instrument="BTC-USDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry=Decimal("50000"),
        simulated_fill_entry=Decimal("50025"),
        stop_loss=Decimal("49000"),
        target=Decimal("52000"),
        size=Decimal("1000"),
        fill_model_version="v1",
        opened_at=opened_at,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position_id}",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id=position_id,
        occurred_at=opened_at,
        run_id="seed",
        schema_version=1,
        payload={},
    )
    repo.create_position_with_event(position, event)
    return position


def test_claim_live_execution_is_idempotent_and_records_sizing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)

    first = repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    second = repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    assert first is True
    assert second is False
    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLAIMED"
    assert row["margin_usdt"] == "10"
    assert row["notional_usdt"] == "100"
    assert row["leverage"] == "10"


def test_find_positions_pending_live_execution_excludes_claimed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-1")
    _open_position(repo, "pos-2")
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    pending = repo.find_positions_pending_live_execution(limit=10)

    assert [p.position_id for p in pending] == ["pos-2"]


def test_find_positions_pending_live_execution_returns_newest_first(tmp_path):
    """2026-09-13 pipeline-queue bugfix: a large, permanently-stale backlog
    of never-claimed old positions must never crowd a fresh signal out of
    the LIMIT window. `opened_at DESC` (not ASC) guarantees any genuinely
    fresh position is always at/near the front of the result set, so a
    small `limit` (matching real per-tick capacity, never dozens) still
    sees it - freshness itself is still enforced downstream by
    _signal_is_fresh()/TTL, this ordering change only controls which
    candidates are even LOOKED AT within a bounded-size query."""
    repo = SQLiteRepository(tmp_path / "t.db")
    old = _open_position(repo, "pos-old", opened_at=_NOW - timedelta(hours=6))
    new = _open_position(repo, "pos-new", opened_at=_NOW)

    pending = repo.find_positions_pending_live_execution(limit=1)

    assert [p.position_id for p in pending] == ["pos-new"]


def test_find_positions_pending_live_execution_still_returns_all_within_limit(tmp_path):
    """The ordering change must not drop or hide any pending position that
    fits within `limit` - only its ORDER within that window changes."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-old", opened_at=_NOW - timedelta(hours=6))
    _open_position(repo, "pos-new", opened_at=_NOW)

    pending = repo.find_positions_pending_live_execution(limit=10)

    assert {p.position_id for p in pending} == {"pos-old", "pos-new"}


def test_find_positions_pending_live_execution_never_deletes_or_mutates_old_rows(tmp_path):
    """Requirement: old pending rows must never be deleted or have their
    history fabricated/altered by this query - it is a pure, read-only
    SELECT. A position's own row (status, exit_reason, closed_at) must be
    byte-identical before and after the call."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo, "pos-old", opened_at=_NOW - timedelta(hours=6))

    repo.find_positions_pending_live_execution(limit=1)

    row = repo.get_position("pos-old")
    assert row.status == "OPEN_POSITION"
    assert row.exit_reason is None
    assert row.closed_at is None


def test_mark_live_execution_entry_submitted_transitions_from_claimed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    repo.mark_live_execution_entry_submitted("pos-1", "cid-1", _NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "ENTRY_SUBMITTED"
    assert row["entry_client_order_id"] == "cid-1"
    # still unresolved - no fill data recorded yet:
    assert row["entry_exchange_order_id"] is None
    assert row["exchange_fill_entry"] is None
    active = repo.find_active_live_executions()
    assert len(active) == 1 and active[0]["phase"] == "ENTRY_SUBMITTED"


def test_update_live_execution_submitted_then_close(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    repo.update_live_execution_submitted(
        "pos-1",
        entry_client_order_id="cid-1",
        entry_exchange_order_id="ex-1",
        entry_quantity="0.002",
        exchange_fill_entry="50030",
        sl_exchange_order_id=None,
        tp_exchange_order_id=None,
        updated_at=_NOW,
    )
    active = repo.find_active_live_executions()
    assert len(active) == 1
    assert active[0]["phase"] == "ACTIVE"

    repo.close_live_execution(
        "pos-1", "target", "52100", _NOW + timedelta(hours=1),
        realized_fees_usdt="0.08", realized_funding_usdt="-0.01",
    )
    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "CLOSED"
    assert row["exit_reason"] == "target"
    assert row["realized_fees_usdt"] == "0.08"
    assert row["realized_funding_usdt"] == "-0.01"
    assert repo.find_active_live_executions() == []


def test_mark_live_execution_failed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    repo.mark_live_execution_failed("pos-1", "ConnectorUnavailableError: boom", _NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "FAILED"
    assert "boom" in row["last_error"]


def test_mark_live_execution_skipped(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    repo.mark_live_execution_skipped("pos-1", "below_exchange_minimum", _NOW)

    row = repo.get_live_execution("pos-1")
    assert row["phase"] == "SKIPPED"
    assert row["last_error"] == "below_exchange_minimum"
    # SKIPPED is terminal and must never be retried:
    assert repo.find_positions_pending_live_execution(limit=10) == []


def test_find_stale_claimed_live_executions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    not_yet_stale = repo.find_stale_claimed_live_executions(_NOW - timedelta(seconds=1))
    stale = repo.find_stale_claimed_live_executions(_NOW + timedelta(seconds=31))

    assert not_yet_stale == []
    assert len(stale) == 1
    assert stale[0]["position_id"] == "pos-1"


def test_live_execution_never_writes_to_positions_table(tmp_path):
    """Isolation guarantee (spec §3): every repository method touching
    live_executions must leave the positions row exactly as it was."""
    repo = SQLiteRepository(tmp_path / "t.db")
    before = _open_position(repo)

    repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")
    repo.update_live_execution_submitted(
        "pos-1", "cid-1", "ex-1", "0.002", "50030", None, None, _NOW
    )
    repo.close_live_execution("pos-1", "target", "52100", _NOW)

    after = repo.get_position("pos-1")
    assert after == before
    assert after.status == "OPEN_POSITION"  # untouched by live_execution close


def _confirm_signal(repo: SQLiteRepository, candidate_id: str, confirmed_at: datetime) -> None:
    """Spec §17.3's authoritative signal timestamp: a CANDIDATE_TRANSITIONED
    event with payload.to == 'CONFIRMED'. transition_candidate_with_event()
    inserts the event regardless of whether a matching `candidates` row
    exists (its UPDATE simply affects zero rows if not) - fine for these
    tests, which only need the event to exist for get_candidate_confirmed_at()."""
    event = Event(
        event_id=f"CANDIDATE_TRANSITIONED:{candidate_id}:CONFIRMED:{confirmed_at.isoformat()}",
        event_type="CANDIDATE_TRANSITIONED",
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        occurred_at=confirmed_at,
        run_id="seed",
        schema_version=1,
        payload={"from": "UNDER_AI_ANALYSIS", "to": "CONFIRMED"},
    )
    repo.transition_candidate_with_event(candidate_id, "CONFIRMED", confirmed_at, event)


def test_get_candidate_confirmed_at_returns_the_confirmed_transition_timestamp(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    confirmed_at = _NOW - timedelta(minutes=10)
    _confirm_signal(repo, "cand-1", confirmed_at)

    assert repo.get_candidate_confirmed_at("cand-1") == confirmed_at


def test_get_candidate_confirmed_at_returns_none_when_never_confirmed(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    assert repo.get_candidate_confirmed_at("cand-never-confirmed") is None


def test_get_candidate_confirmed_at_ignores_non_confirmed_transitions(tmp_path):
    """A CANDIDATE_TRANSITIONED event that isn't the CONFIRMED one (e.g.
    CANDIDATE -> UNDER_AI_ANALYSIS) must never be mistaken for the signal
    timestamp."""
    repo = SQLiteRepository(tmp_path / "t.db")
    event = Event(
        event_id="CANDIDATE_TRANSITIONED:cand-1:UNDER_AI_ANALYSIS",
        event_type="CANDIDATE_TRANSITIONED",
        aggregate_type="candidate",
        aggregate_id="cand-1",
        occurred_at=_NOW - timedelta(minutes=5),
        run_id="seed",
        schema_version=1,
        payload={"from": "CANDIDATE", "to": "UNDER_AI_ANALYSIS"},
    )
    not_confirmed_at = _NOW - timedelta(minutes=5)
    repo.transition_candidate_with_event("cand-1", "UNDER_AI_ANALYSIS", not_confirmed_at, event)

    assert repo.get_candidate_confirmed_at("cand-1") is None


def test_claim_live_execution_fails_closed_when_position_no_longer_open(tmp_path):
    """Race defense (spec §17.6): the observed live incident had PAPER's
    own time-limit closer flip positions.status to CLOSED roughly one
    second before LIVE's independent thread claimed the same row as still
    OPEN_POSITION. The atomic claim itself must re-verify status - not rely
    on a Position object read earlier - so a position closed by ANY
    concurrent PAPER decision (time-limit, SL, TP, manual) can never be
    claimed by LIVE, even if an earlier read of it still said OPEN_POSITION."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _open_position(repo)
    close_event = Event(
        event_id="POSITION_CLOSED:pos-1", event_type="POSITION_CLOSED",
        aggregate_type="position", aggregate_id="pos-1", occurred_at=_NOW,
        run_id="seed", schema_version=1, payload={"exit_reason": "time_limit"},
    )
    repo.close_position_with_event(
        "pos-1", Decimal("50100"), Decimal("50090"), "time_limit",
        Decimal("1"), Decimal("0"), _NOW, close_event,
    )

    claimed = repo.claim_live_execution("pos-1", _NOW, "10", "100", "10")

    assert claimed is False
    assert repo.get_live_execution("pos-1") is None
