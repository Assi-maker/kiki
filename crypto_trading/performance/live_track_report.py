from __future__ import annotations

from decimal import Decimal

from crypto_trading.external_closes import exit_source, is_external
from crypto_trading.storage.repository import Repository


def build_live_report(repo: Repository) -> dict:
    """Read-only, pure reporting - same discipline as
    performance/paper_track_report.py. Never writes anything. Joins
    positions with live_executions (Task 4) and the latest Guardian
    observation (existing find_latest_guardian_observation) purely for
    display; this module has no opinion on strategy/risk and makes no
    trading decision."""
    live_positions = []
    total_pnl = Decimal("0")
    # P1 (2026-10-01): closes the bot did not make are reported apart and
    # never counted in the bot's own exit performance.
    bot_pnl = Decimal("0")
    external_pnl = Decimal("0")
    bot_closed = external_closed = 0
    for position in repo.find_all_positions(limit=10_000):
        live_row = repo.get_live_execution(position.position_id)
        if live_row is None:
            continue
        guardian = repo.find_latest_guardian_observation(position.position_id)
        row = {
            "position_id": position.position_id,
            "instrument": position.instrument,
            "direction": position.direction,
            "margin_usdt": live_row["margin_usdt"],
            "notional_usdt": live_row["notional_usdt"],
            "leverage": live_row["leverage"],
            "entry": live_row["exchange_fill_entry"],
            "stop_loss": str(position.stop_loss),
            "target": str(position.target),
            "guardian_state": guardian["state"] if guardian else None,
            "exit_reason": live_row["exit_reason"],
            "exchange_fill_exit": live_row["exchange_fill_exit"],
            "realized_fees_usdt": live_row["realized_fees_usdt"],
            "realized_funding_usdt": live_row["realized_funding_usdt"],
            "phase": live_row["phase"],
            "exit_source": exit_source(live_row.get("exit_classification"))
            if live_row["phase"] == "CLOSED" else None,
        }
        live_positions.append(row)
        if live_row["phase"] == "CLOSED" and live_row["exchange_fill_entry"] and live_row["exchange_fill_exit"]:
            entry_qty = Decimal(str(live_row["entry_quantity"] or "0"))
            gross = (
                Decimal(str(live_row["exchange_fill_exit"]))
                - Decimal(str(live_row["exchange_fill_entry"]))
            ) * entry_qty
            fees = Decimal(str(live_row["realized_fees_usdt"] or "0"))
            funding = Decimal(str(live_row["realized_funding_usdt"] or "0"))
            pnl = gross - fees + funding
            total_pnl += pnl
            if is_external(row["exit_source"] or "UNKNOWN"):
                external_pnl += pnl
                external_closed += 1
            else:
                bot_pnl += pnl
                bot_closed += 1
    return {
        "live_positions": live_positions,
        "total_live_pnl_usdt": str(total_pnl),
        "bot_managed_pnl_usdt": str(bot_pnl),
        "bot_managed_closed": bot_closed,
        "external_close_pnl_usdt": str(external_pnl),
        "external_closed": external_closed,
    }
