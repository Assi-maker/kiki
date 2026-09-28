"""LIVE exit reconciliation against the exchange (2026-09-28).

When a LIVE position goes flat, `live_execution.reconcile_active_executions`
must close the row at once (capacity depends on it). If the exchange's order
history does not show the closing fill yet at that moment - it lags by
seconds - the row is priced from the ticker (`exit_fill_source='TICKER'`)
and its exit reason is guessed from the distance to SL/TP. In the 2026-09-26
..28 LIVE period that gave 4 of 34 UNVERIFIABLE exits: a Profit-Protection
stop recorded as 'target' and two external MARKET closes recorded as
'stop_loss'. Fees and funding were never stored.

This module re-derives every CLOSED LIVE exit afterwards from the exchange's
own records - the order history (fills, commissions, the exchange's realized
P/L) and the income ledger (funding) - and stores the result next to the
bot's original record:

- VERIFIED: the entry fill and closing fills were found and their quantities
  match; the exit price, fees and funding are the exchange's own numbers.
- UNVERIFIABLE: the evidence is missing or contradictory; nothing is guessed
  and the P/L is never learned from.

It only ever reads from the exchange (get_order_history, get_income) and only
writes to CLOSED live_executions rows. `exit_reason` - the bot's own record
of what it believed at close time - is never rewritten; the verified truth is
`exit_classification`.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

import httpx

from crypto_trading.connectors.exceptions import ConnectorUnavailableError
from crypto_trading.logging import log_event, new_run_id

# Exchange order history can lag the position going flat; until this long
# after the close an unprovable exit is retried rather than recorded.
VERIFY_GRACE = timedelta(hours=6)
# BingX refuses allOrders/income ranges over 7 days (109400); history that is
# still unreadable a week after the close is recorded UNVERIFIABLE instead of
# being retried forever.
MAX_QUERY_RANGE = timedelta(days=7) - timedelta(minutes=1)
HISTORY_GIVE_UP = timedelta(days=7)
_READ_ERRORS = (ConnectorUnavailableError, httpx.TransportError)


@dataclass(frozen=True)
class ExitReconciliation:
    verification: str  # "VERIFIED" | "UNVERIFIABLE"
    reason: str | None = None
    classification: str | None = None
    entry_filled_at: datetime | None = None
    exit_filled_at: datetime | None = None
    exit_price: Decimal | None = None
    exit_order_ids: str | None = None
    fees_usdt: Decimal | None = None  # cost: positive = paid
    funding_usdt: Decimal | None = None  # cost: positive = paid, negative = received
    exchange_realized_pnl_usdt: Decimal | None = None


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _ms(order: dict) -> int:
    return int(order.get("updateTime") or order.get("time") or 0)


def _at(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _client_prefix(position_id: str) -> str:
    # Same deterministic scheme as live_execution._client_order_id.
    return f"lv{position_id[:24]}"


def classify_exit_order(order: dict, position_id: str) -> str:
    """From the order's own type and whether it carries OUR client id -
    never from where the price happened to be."""
    kind = str(order.get("type"))
    client = str(order.get("clientOrderId") or "")
    ours = client.startswith(_client_prefix(position_id))
    if kind in ("STOP_MARKET", "STOP"):
        return "PROFIT_PROTECTION_STOP" if ours and client.endswith("pp") else "EXCHANGE_STOP_LOSS"
    if kind in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
        return "EXCHANGE_TAKE_PROFIT"
    if kind == "MARKET":
        if not ours:
            return "EXTERNAL_CLOSE"
        return {"x": "BOT_TIME_LIMIT_CLOSE", "g": "BOT_GUARDIAN_CLOSE"}.get(
            client[-1:], "BOT_MARKET_CLOSE"
        )
    return f"OTHER:{kind}"


def _filled_long(order: dict, side: str) -> bool:
    return (
        order.get("status") == "FILLED"
        and order.get("side") == side
        and order.get("positionSide", "LONG") == "LONG"
    )


def reconcile_exit(
    position_id: str, entry_quantity: Decimal, orders: list[dict], funding_income: list[dict]
) -> ExitReconciliation:
    """Pure. `orders` = the symbol's exchange order history since before the
    claim; `funding_income` = its FUNDING_FEE income rows."""
    entry_id = _client_prefix(position_id) + "e"
    entry = next(
        (o for o in orders if o.get("clientOrderId") == entry_id and _filled_long(o, "BUY")), None
    )
    if entry is None:
        return ExitReconciliation("UNVERIFIABLE", "ENTRY_FILL_NOT_FOUND")
    entry_ms = _ms(entry)
    prefix = _client_prefix(position_id)
    sells = sorted(
        (
            o
            for o in orders
            if _filled_long(o, "SELL")
            and _ms(o) >= entry_ms
            # a close carrying ANOTHER LIVE position's client id is never ours
            and not (
                str(o.get("clientOrderId") or "").startswith("lv")
                and not str(o.get("clientOrderId")).startswith(prefix)
            )
        ),
        key=_ms,
    )
    taken, filled = [], Decimal("0")
    for order in sells:
        if filled >= entry_quantity:
            break
        taken.append(order)
        filled += _dec(order.get("executedQty")) or Decimal("0")
    if not taken:
        return ExitReconciliation(
            "UNVERIFIABLE", "EXIT_FILL_NOT_FOUND", entry_filled_at=_at(entry_ms)
        )
    if filled != entry_quantity:
        reason = "EXIT_QTY_MISMATCH" if filled > entry_quantity else "EXIT_FILL_NOT_FOUND"
        return ExitReconciliation("UNVERIFIABLE", reason, entry_filled_at=_at(entry_ms))
    if len(taken) == 1:
        exit_price = _dec(taken[0].get("avgPrice"))
    else:
        notional = sum(
            (_dec(o.get("avgPrice")) or Decimal("0")) * _dec(o.get("executedQty")) for o in taken
        )
        exit_price = notional / filled
    classes = []
    for order in taken:
        label = classify_exit_order(order, position_id)
        if label not in classes:
            classes.append(label)
    classification = classes[0] if len(classes) == 1 else "MIXED:" + "+".join(classes)
    commissions = [_dec(o.get("commission")) for o in [entry, *taken]]
    fees = -sum(commissions) if all(c is not None for c in commissions) else None
    exit_ms = _ms(taken[-1])
    received = sum(
        (_dec(i.get("income")) or Decimal("0"))
        for i in funding_income
        if i.get("incomeType", "FUNDING_FEE") == "FUNDING_FEE"
        and entry_ms <= int(i.get("time", 0)) <= exit_ms
    )
    profits = [_dec(o.get("profit")) for o in taken]
    return ExitReconciliation(
        "VERIFIED",
        classification=classification,
        entry_filled_at=_at(entry_ms),
        exit_filled_at=_at(exit_ms),
        exit_price=exit_price,
        exit_order_ids=",".join(str(o.get("orderId")) for o in taken),
        fees_usdt=fees,
        funding_usdt=-received if received else Decimal("0"),
        exchange_realized_pnl_usdt=sum(profits) if all(p is not None for p in profits) else None,
    )


def query_window(claimed: datetime, closed: datetime) -> tuple[datetime, datetime]:
    """From just before the claim to a while after the close, never longer
    than BingX accepts."""
    start = claimed - timedelta(minutes=1)
    return start, min(closed + timedelta(hours=1), start + MAX_QUERY_RANGE)


def _read_exchange(connector, instrument: str, start: datetime, end: datetime):
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    orders = connector.get_order_history(instrument, start_ms, limit=100, end_time_ms=end_ms)
    funding = connector.get_income(instrument, "FUNDING_FEE", start_ms, end_ms)
    return orders, funding


def verify_closed_live_exits(
    repo, connector, run_id: str, now: datetime, limit: int = 5, grace: timedelta = VERIFY_GRACE
) -> int:
    """Verifies up to `limit` CLOSED LIVE rows not yet verified. Returns how
    many were recorded. A read error changes nothing (retried next tick); an
    unprovable exit is retried until `grace` after its close, then recorded
    UNVERIFIABLE."""
    recorded = 0
    for row in repo.find_live_executions_needing_exit_verification(limit):
        position = repo.get_position(row["position_id"])
        quantity = _dec(row.get("entry_quantity"))
        if position is None or quantity is None or quantity <= 0:
            continue
        claimed = datetime.fromisoformat(row["claimed_at"])
        closed = datetime.fromisoformat(row["closed_at"]) if row.get("closed_at") else now
        start, end = query_window(claimed, closed)
        if repo.has_overlapping_live_execution(row["position_id"]):
            # The exchange merges same-symbol LONGs; an exit cannot be attributed.
            result = ExitReconciliation("UNVERIFIABLE", "OVERLAPPING_SAME_SYMBOL_POSITION")
            repo.record_live_exit_verification(row["position_id"], result, now)
            recorded += 1
            log_event(
                run_id,
                event="live_exit_unverifiable",
                position_id=row["position_id"],
                instrument=position.instrument,
                reason=result.reason,
            )
            continue
        try:
            orders, funding = _read_exchange(connector, position.instrument, start, end)
        except _READ_ERRORS as exc:
            log_event(
                run_id,
                event="live_exit_verification_read_failed",
                position_id=row["position_id"],
                error_type=type(exc).__name__,
                error=str(exc),
            )
            if now - closed < HISTORY_GIVE_UP:
                continue
            result = ExitReconciliation("UNVERIFIABLE", "EXCHANGE_HISTORY_UNAVAILABLE")
        else:
            result = reconcile_exit(row["position_id"], quantity, orders, funding)
        if result.verification != "VERIFIED" and now - closed < grace:
            continue
        repo.record_live_exit_verification(row["position_id"], result, now)
        recorded += 1
        log_event(
            run_id,
            event="live_exit_verified"
            if result.verification == "VERIFIED"
            else "live_exit_unverifiable",
            position_id=row["position_id"],
            instrument=position.instrument,
            verification=result.verification,
            reason=result.reason,
            classification=result.classification,
            recorded_exit_reason=row.get("exit_reason"),
            recorded_fill_source=row.get("exit_fill_source"),
            recorded_exit_price=row.get("exchange_fill_exit"),
            verified_exit_price=str(result.exit_price) if result.exit_price is not None else None,
            fees_usdt=str(result.fees_usdt) if result.fees_usdt is not None else None,
            funding_usdt=str(result.funding_usdt) if result.funding_usdt is not None else None,
        )
    return recorded


def main() -> None:
    """Historical backfill: `python -m crypto_trading.paper_trading.live_exit_reconciliation`
    lists what would change; `--apply` records it. Read-only against the
    exchange either way."""
    from crypto_trading.config.loader import get_settings
    from crypto_trading.run import build_live_trading_connector_from_env
    from crypto_trading.storage.repository import SQLiteRepository

    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    connector = build_live_trading_connector_from_env()
    if connector is None:
        raise SystemExit(
            "LIVE exchange credentials missing (see run.build_live_trading_connector_from_env)"
        )
    now = datetime.now(UTC)
    run_id = new_run_id()
    if args.apply:
        total = 0
        while True:
            done = verify_closed_live_exits(repo, connector, run_id, now, limit=20)
            total += done
            if done == 0:
                break
        print(f"recorded {total} verifications")
        return
    for row in repo.find_live_executions_needing_exit_verification(1000):
        position = repo.get_position(row["position_id"])
        claimed = datetime.fromisoformat(row["claimed_at"])
        closed = datetime.fromisoformat(row["closed_at"]) if row.get("closed_at") else now
        try:
            orders, funding = _read_exchange(
                connector, position.instrument, *query_window(claimed, closed)
            )
        except _READ_ERRORS as exc:
            print(position.instrument, row["position_id"][:10], "READ FAILED", exc)
            continue
        r = reconcile_exit(row["position_id"], Decimal(row["entry_quantity"]), orders, funding)
        print(
            f"{position.instrument:14} {row['position_id'][:10]} recorded={row['exit_reason']}/"
            f"{row['exit_fill_source']}/{row['exchange_fill_exit']} -> {r.verification} "
            f"{r.reason or r.classification} exit={r.exit_price} fees={r.fees_usdt} "
            f"funding={r.funding_usdt}"
        )


if __name__ == "__main__":
    main()
