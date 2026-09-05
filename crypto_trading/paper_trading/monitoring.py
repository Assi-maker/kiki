from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from crypto_trading.schemas.guardian import GuardianState
from crypto_trading.schemas.trade import Position

_SECONDS_PER_HOUR = Decimal("3600")


def check_exit_trigger(
    position: Position,
    candle_low: Decimal,
    candle_high: Decimal,
    current_price: Decimal,
    now: datetime,
    max_position_hold_hours: int,
    guardian_state: GuardianState | None = None,
    guardian_assisted_exit_enabled: bool = False,
) -> tuple[str, Decimal] | None:
    """LONG-only i denna fas (se PLAN_CRYPTO_PHASE4.md beslut 1) - ingen
    direction-parameter, hårdkodat LONG-beteende.

    Prioritetsordning, alltid: stop_loss -> target -> time_limit ->
    guardian_exit. SL/TP kollas alltid före tidsgränsen, deterministiskt.
    time_limit kollas ALLTID före guardian_exit (2026-09-05, explicit
    användarkrav: "Guardian får aldrig förlänga en position förbi den hårda
    time limit") - guardian_exit kan därför strukturellt aldrig nås när
    hold_hours redan nått max_position_hold_hours, den absoluta gränsen
    vinner alltid.

    guardian_exit (Guardian-assisted exit) stänger ENDAST när
    guardian_assisted_exit_enabled=True OCH guardian_state=="EXIT" - den
    redan deterministiska klassificeringen från
    guardian/deterministic.py::classify_guardian_state() (som redan väger
    in time/momentum/volym/funding/sekundär bekräftelse/marknadsregim, och
    aldrig ger EXIT enbart av positiv PnL eller enbart av förfluten tid,
    se dess docstring/tester). WATCH/PROTECT/HOLD stänger ALDRIG, oavsett
    flaggan (explicit användarkrav punkt 6/8). Default
    guardian_assisted_exit_enabled=False bevarar exakt tidigare beteende -
    Guardian förblir rent shadow-only tills detta aktiveras separat.

    Konservativ gap-fill (SPEC §11, beslut 4): stop-fill = min(candle_low,
    stop_loss) - aldrig bättre än stop, sämre vid gap. target-fill =
    min(candle_high, target) - aldrig bättre än target, oavsett hur högt
    priset gappade. Båda formlerna degenererar korrekt till exakt
    SL/TP-nivån när candle:n bara nuddade nivån utan gap.
    """
    if candle_low <= position.stop_loss:
        return "stop_loss", min(candle_low, position.stop_loss)

    if candle_high >= position.target:
        return "target", min(candle_high, position.target)

    if compute_hold_hours(position, now) >= max_position_hold_hours:
        return "time_limit", current_price

    if guardian_assisted_exit_enabled and guardian_state == "EXIT":
        return "guardian_exit", current_price

    return None


def compute_hold_hours(position: Position, now: datetime) -> Decimal:
    """Shared with paper_trading/demo_execution.py's time-limit parity logic
    (2026-09-04 design) so PAPER and BingX Demo never drift on what counts
    as 'reached max_position_hold_hours' for the same position."""
    hold_seconds = Decimal(str((now - position.opened_at).total_seconds()))
    return hold_seconds / _SECONDS_PER_HOUR
