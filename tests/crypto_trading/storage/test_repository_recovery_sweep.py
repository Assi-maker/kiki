from datetime import UTC, datetime

from crypto_trading.storage.repository import SQLiteRepository


def test_get_recovery_sweep_activated_at_returns_none_before_first_set(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    assert repo.get_recovery_sweep_activated_at() is None


def test_set_recovery_sweep_activated_at_if_missing_persists_and_returns_true_on_first_call(
    tmp_path,
):
    repo = SQLiteRepository(tmp_path / "t.db")
    activated_at = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    result = repo.set_recovery_sweep_activated_at_if_missing(activated_at)

    assert result is True
    assert repo.get_recovery_sweep_activated_at() == activated_at


def test_set_recovery_sweep_activated_at_if_missing_is_a_no_op_on_second_call(tmp_path):
    """Idempotent first-writer-wins, same INSERT OR IGNORE pattern as
    schema_version: whichever timestamp was set FIRST stays authoritative
    forever, never overwritten by a later call (e.g. on a later restart)."""
    repo = SQLiteRepository(tmp_path / "t.db")
    first = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    later = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    repo.set_recovery_sweep_activated_at_if_missing(first)

    result = repo.set_recovery_sweep_activated_at_if_missing(later)

    assert result is False
    assert repo.get_recovery_sweep_activated_at() == first  # unchanged
