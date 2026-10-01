"""Explicit log of LIVE closes the bot did not make (P1, 2026-10-01).

Every LIVE exit is attributed from the exchange's own order record (see
paper_trading/live_exit_reconciliation.py), never from where the price
happened to be:

- BOT                        a MARKET close carrying the bot's own client id
                             (time limit, Guardian close);
- EXCHANGE_PROTECTIVE_ORDER  the bot's own SL / TP / break-even stop,
                             triggered by the exchange;
- EXTERNAL_MANUAL            a close WITHOUT the bot's client id: the user in
                             the BingX app/web, or another process using the
                             same API key (the exchange record cannot tell the
                             two apart; nothing else is known to use the key);
- EXCHANGE_FORCED            any other exchange-generated close type
                             (liquidation / ADL / unknown order types);
- MIXED_INCLUDES_EXTERNAL    several closing fills, at least one external.

Only non-bot sources are written to `external_closes`, and the bot's own
exit performance excludes them (performance/live_track_report.py). A reason
can be attached afterwards:

    python -m crypto_trading.external_closes list
    python -m crypto_trading.external_closes note <position_id> "took profit early"
    python -m crypto_trading.external_closes backfill     # from verified history

Never touches an order or a position.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

BOT_SOURCES = frozenset({"BOT", "EXCHANGE_PROTECTIVE_ORDER"})
_BOT_MARKET = {"BOT_TIME_LIMIT_CLOSE", "BOT_GUARDIAN_CLOSE", "BOT_MARKET_CLOSE"}
_PROTECTIVE = {"EXCHANGE_STOP_LOSS", "EXCHANGE_TAKE_PROFIT", "PROFIT_PROTECTION_STOP"}


def exit_source(classification: str | None) -> str:
    """Who closed the position, from live_exit_reconciliation's classification."""
    if not classification:
        return "UNKNOWN"
    if classification.startswith("MIXED:"):
        parts = classification[len("MIXED:"):].split("+")
        if "EXTERNAL_CLOSE" in parts:
            return "MIXED_INCLUDES_EXTERNAL"
        if all(p in _BOT_MARKET | _PROTECTIVE for p in parts):
            return "BOT" if all(p in _BOT_MARKET for p in parts) else "EXCHANGE_PROTECTIVE_ORDER"
        return "EXCHANGE_FORCED"
    if classification in _BOT_MARKET:
        return "BOT"
    if classification in _PROTECTIVE:
        return "EXCHANGE_PROTECTIVE_ORDER"
    if classification == "EXTERNAL_CLOSE":
        return "EXTERNAL_MANUAL"
    return "EXCHANGE_FORCED"


def is_external(source: str) -> bool:
    return source not in BOT_SOURCES and source != "UNKNOWN"


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def external_close_row(live_row: dict, instrument: str, direction: str,
                       detected_at: datetime) -> dict | None:
    """The `external_closes` row for one VERIFIED CLOSED live_executions row,
    or None when the bot (or its own protective order) closed it."""
    source = exit_source(live_row.get("exit_classification"))
    if not is_external(source):
        return None
    realized = _dec(live_row.get("exchange_realized_pnl_usdt"))
    fees = _dec(live_row.get("realized_fees_usdt"))
    funding = _dec(live_row.get("realized_funding_usdt"))
    pnl = None
    if realized is not None:
        pnl = realized - (fees or Decimal("0")) - (funding or Decimal("0"))
    return {
        "position_id": live_row["position_id"],
        "symbol": instrument,
        "direction": direction,
        "entry_price": live_row.get("exchange_fill_entry"),
        "close_price": live_row.get("exchange_fill_exit"),
        "quantity": live_row.get("entry_quantity"),
        "pnl_usdt": str(pnl) if pnl is not None else None,
        "fees_usdt": live_row.get("realized_fees_usdt"),
        "classification": live_row.get("exit_classification"),
        "source": source,
        "order_ids": live_row.get("exit_exchange_order_ids"),
        "closed_at": live_row.get("exit_filled_at") or live_row.get("closed_at"),
        "detected_at": detected_at.isoformat(),
    }


def record_if_external(repo, position_id: str, detected_at: datetime) -> dict | None:
    """Called right after an exit was verified. Idempotent."""
    live_row = repo.get_live_execution(position_id)
    position = repo.get_position(position_id)
    if not live_row or position is None or live_row.get("exit_verification") != "VERIFIED":
        return None
    row = external_close_row(live_row, position.instrument, position.direction, detected_at)
    if row is not None:
        repo.record_external_close(row)
    return row


def backfill(repo, now: datetime) -> int:
    count = 0
    for live_row in repo.find_verified_closed_live_executions():
        if record_if_external(repo, live_row["position_id"], now) is not None:
            count += 1
    return count


def main() -> None:
    from crypto_trading.config.loader import get_settings
    from crypto_trading.storage.repository import SQLiteRepository

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("backfill")
    note = sub.add_parser("note")
    note.add_argument("position_id")
    note.add_argument("reason")
    args = parser.parse_args()
    settings = get_settings()
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    now = datetime.now(UTC)
    if args.cmd == "backfill":
        print(f"recorded {backfill(repo, now)} external closes")
    elif args.cmd == "note":
        if not repo.set_external_close_reason(args.position_id, args.reason, now):
            raise SystemExit(f"no external close recorded for {args.position_id}")
        print("reason saved")
    else:
        for row in repo.list_external_closes():
            print(f"{row['closed_at']}  {row['symbol']:<22} {row['source']:<24} "
                  f"entry {row['entry_price']} close {row['close_price']} "
                  f"pnl {row['pnl_usdt']}  reason: {row['reason'] or '-'}  "
                  f"({row['position_id'][:12]})")


if __name__ == "__main__":
    main()
