"""Cooldown after a Safety Kernel budget REJECT (2026-09-29 cost forensic).

A symbol that was CONFIRMED and then REJECTED by the Safety Kernel only on
PORTFOLIO_RISK_CAP / GROUP_RISK_CAP gets no new candidate - and therefore no
new 7-role AI analysis - until the cooldown has passed (overnight CRV was
analysed 5 times and cap-rejected each time). Structural rejections
(LIQUIDATION_TOO_CLOSE, ...) and other symbols are unaffected, and the symbol
is analysable again after the cooldown. No risk limit is involved here."""
from datetime import timedelta
from unittest.mock import patch

from crypto_trading.screening.candidate_engine import process_evidence
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.screening.test_candidate_engine import _NOW, _evidence

COOLDOWN = 120


def _confirmed_and_kernel_rejected(repo, instrument, reasons, at, cid="old"):
    repo._conn.execute(
        "INSERT INTO candidates (candidate_id, idempotency_key, instrument, discovery_run_id, evidence_hash,"
        " status, evidence_record, created_at, updated_at) VALUES (?, ?, ?, 'r0', 'h', 'CONFIRMED', ?, ?, ?)",
        (cid, cid, instrument, _evidence(instrument=instrument).model_dump_json(),
         (at - timedelta(minutes=20)).isoformat(), (at - timedelta(minutes=5)).isoformat()),
    )
    repo._conn.execute(
        "INSERT INTO positions (position_id, candidate_id, instrument, direction, status, theoretical_entry,"
        " simulated_fill_entry, stop_loss, target, size, fill_model_version, opened_at)"
        " VALUES (?, ?, ?, 'LONG', 'OPEN_POSITION', '1', '1', '0.95', '1.1', '500', 'v1', ?)",
        (f"pos-{cid}", cid, instrument, at.isoformat()),
    )
    repo._conn.commit()
    repo.record_safety_kernel_decision(f"pos-{cid}", at, "REJECT", {"action": "REJECT", "reasons": reasons})


def _new(repo, instrument, at, run_id="r1"):
    evidence = _evidence(instrument=instrument, trigger_reasons=["price_volatility"])
    return process_evidence(repo, evidence, discovery_run_id=run_id, created_at=at,
                            kernel_reject_cooldown_minutes=COOLDOWN)


def test_a_cap_rejected_symbol_gets_no_new_candidate_during_the_cooldown(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _confirmed_and_kernel_rejected(repo, "CRV-USDT", ["PORTFOLIO_RISK_CAP", "GROUP_RISK_CAP"], _NOW)
    with patch("crypto_trading.screening.candidate_engine.log_event") as log:
        assert _new(repo, "CRV-USDT", _NOW + timedelta(minutes=30)) is None
    skipped = [c.kwargs for c in log.call_args_list
               if c.kwargs.get("event") == "candidate_skipped_kernel_reject_cooldown"]
    assert len(skipped) == 1
    e = skipped[0]
    assert e["symbol"] == "CRV-USDT"
    assert e["previous_candidate_id"] == "old"
    assert e["previous_confirmed_at"] == (_NOW - timedelta(minutes=5)).isoformat()
    assert e["kernel_reasons"] == ["PORTFOLIO_RISK_CAP", "GROUP_RISK_CAP"]
    assert e["cooldown_start"] == _NOW.isoformat()
    assert e["cooldown_end"] == (_NOW + timedelta(minutes=COOLDOWN)).isoformat()


def test_the_group_cap_alone_also_starts_the_cooldown(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _confirmed_and_kernel_rejected(repo, "DOT-USDT", ["GROUP_RISK_CAP"], _NOW)
    assert _new(repo, "DOT-USDT", _NOW + timedelta(minutes=10)) is None


def test_the_symbol_is_analysable_again_after_the_cooldown_and_that_is_logged(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _confirmed_and_kernel_rejected(repo, "CRV-USDT", ["GROUP_RISK_CAP"], _NOW)
    later = _NOW + timedelta(minutes=COOLDOWN, seconds=1)
    with patch("crypto_trading.screening.candidate_engine.log_event") as log:
        created = _new(repo, "CRV-USDT", later)
    assert created is not None
    cleared = [c.kwargs for c in log.call_args_list if c.kwargs.get("event") == "kernel_reject_cooldown_cleared"]
    assert len(cleared) == 1
    assert cleared[0]["symbol"] == "CRV-USDT"
    assert cleared[0]["reanalysable_at"] == later.isoformat()
    # logged once: the next new candidate for the symbol does not repeat it
    with patch("crypto_trading.screening.candidate_engine.log_event") as log2:
        _new(repo, "CRV-USDT", later + timedelta(minutes=30), run_id="r2")
    assert not [c for c in log2.call_args_list if c.kwargs.get("event") == "kernel_reject_cooldown_cleared"]


def test_a_structural_kernel_reject_does_not_start_a_cooldown(tmp_path):
    """LIQUIDATION_TOO_CLOSE depends on that analysis' own stop - a new
    analysis may propose another one."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _confirmed_and_kernel_rejected(repo, "SOON-USDT", ["LIQUIDATION_TOO_CLOSE"], _NOW)
    assert _new(repo, "SOON-USDT", _NOW + timedelta(minutes=10)) is not None


def test_other_symbols_are_never_affected(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _confirmed_and_kernel_rejected(repo, "CRV-USDT", ["PORTFOLIO_RISK_CAP"], _NOW)
    assert _new(repo, "LINK-USDT", _NOW + timedelta(minutes=10)) is not None


def test_an_approved_kernel_decision_never_starts_a_cooldown(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _confirmed_and_kernel_rejected(repo, "PEOPLE-USDT", [], _NOW)
    repo.record_safety_kernel_decision("pos-old", _NOW, "APPROVE", {"action": "APPROVE", "reasons": []})
    assert _new(repo, "PEOPLE-USDT", _NOW + timedelta(minutes=10)) is not None


def test_without_the_setting_nothing_changes(tmp_path):
    """Replay/backtest callers that do not pass the setting are unchanged."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _confirmed_and_kernel_rejected(repo, "CRV-USDT", ["GROUP_RISK_CAP"], _NOW)
    evidence = _evidence(instrument="CRV-USDT", trigger_reasons=["price_volatility"])
    assert process_evidence(repo, evidence, discovery_run_id="r1", created_at=_NOW + timedelta(minutes=5)) is not None


def test_production_config_has_a_positive_cooldown():
    from crypto_trading.config.loader import get_settings

    assert get_settings().pipeline.kernel_reject_cooldown_minutes == COOLDOWN
