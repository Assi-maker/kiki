from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig, RiskLimitsConfig, Settings
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.execution import (
    FILL_MODEL_VERSION,
    compute_fees,
    compute_fill_price,
    compute_funding,
    compute_pnl,
)
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

# Pre-registered hypotheses under test (2026-09-11, spec G9). Frozen for
# the duration of this experiment - not a tuning parameter, not read from
# config/YAML/env. Changing this set is a source-code change requiring the
# same review as any other logic change, never a deploy-time toggle.
FROZEN_THRESHOLDS_PCT: tuple[Decimal, ...] = (Decimal("0.010"), Decimal("0.015"))

_DIRECTION = "LONG"


def _guardian_state_for(
    repo: Repository, position_id: str, now: datetime, guardian_config: GuardianConfig
) -> str | None:
    """Read-only DUPLICATE of position_closing.py::close_triggered_positions's
    own staleness-guarded Guardian read (spec G4) - deliberately duplicated,
    not shared, so position_closing.py (baseline exit logic) stays
    completely untouched. See
    test_guardian_state_lookup_matches_close_triggered_positions_accept_reject_parity
    in test_profit_protection_experiment.py for the parity proof against the
    real function."""
    if not guardian_config.assisted_exit_enabled:
        return None
    latest_observation = repo.find_latest_guardian_observation(position_id)
    if latest_observation is None:
        return None
    observed_at = datetime.fromisoformat(latest_observation["observed_at"])
    staleness_limit = timedelta(seconds=2 * guardian_config.check_interval_seconds)
    if now - observed_at <= staleness_limit:
        return latest_observation["state"]
    return None


def _shadow_id(position_id: str, threshold_pct: Decimal) -> str:
    return f"{position_id}:{threshold_pct}"


def seed_shadows_for_position(
    repo: Repository, position: Position, activated_at: datetime, now: datetime
) -> None:
    """Spec §5.1, G6: only ever seeds a position opened at-or-after the
    activation watermark. Idempotent per (position_id, threshold) via
    Repository.seed_profit_protection_shadow's own INSERT OR IGNORE."""
    if position.opened_at < activated_at:
        return
    for threshold_pct in FROZEN_THRESHOLDS_PCT:
        shadow_id = _shadow_id(position.position_id, threshold_pct)
        threshold_price = position.theoretical_entry * (1 + threshold_pct)
        repo.seed_profit_protection_shadow(
            shadow_id=shadow_id,
            position_id=position.position_id,
            instrument=position.instrument,
            threshold_pct=str(threshold_pct),
            entry_price=position.theoretical_entry,
            original_stop_loss=position.stop_loss,
            target=position.target,
            threshold_price=threshold_price,
            opened_at=position.opened_at,
            created_at=now,
        )


def advance_shadow(
    shadow: dict,
    candle_low: Decimal,
    candle_high: Decimal,
    current_price: Decimal,
    funding_rate: Decimal,
    now: datetime,
    max_position_hold_hours: int,
    guardian_state: str | None,
    guardian_assisted_exit_enabled: bool,
    risk_limits: RiskLimitsConfig,
    repo: Repository,
) -> None:
    """One shadow row, one tick (spec §5.2). Conservative ordering (G7/G8):
    stop -> target -> time_limit -> guardian_exit, always checked against
    the ACTIVE sl as of the START of this tick - a new threshold-touch is
    only ever detected AFTER all four checks, and only takes effect
    starting the tick after this one (activate_profit_protection_breakeven
    is a separate call the NEXT tick will see via `shadow["breakeven_stop_loss"]`
    being non-None, never within this same call)."""
    shadow_id = shadow["shadow_id"]
    entry_price = Decimal(shadow["entry_price"])
    original_stop_loss = Decimal(shadow["original_stop_loss"])
    target = Decimal(shadow["target"])
    threshold_price = Decimal(shadow["threshold_price"])
    breakeven_stop_loss = (
        Decimal(shadow["breakeven_stop_loss"])
        if shadow["breakeven_stop_loss"] is not None
        else None
    )
    active_sl = breakeven_stop_loss if breakeven_stop_loss is not None else original_stop_loss

    mfe = max(Decimal(shadow["mfe"]), candle_high - entry_price)
    mae = min(Decimal(shadow["mae"]), candle_low - entry_price)
    repo.record_profit_protection_tick(shadow_id, mfe, mae, now)

    opened_at = datetime.fromisoformat(shadow["opened_at"])
    hold_hours = Decimal(str((now - opened_at).total_seconds())) / Decimal("3600")

    exit_reason: str | None = None
    theoretical_exit: Decimal | None = None
    if candle_low <= active_sl:
        exit_reason, theoretical_exit = "stop_loss", min(candle_low, active_sl)
    elif candle_high >= target:
        exit_reason, theoretical_exit = "target", min(candle_high, target)
    elif hold_hours >= max_position_hold_hours:
        exit_reason, theoretical_exit = "time_limit", current_price
    elif guardian_assisted_exit_enabled and guardian_state == "EXIT":
        exit_reason, theoretical_exit = "guardian_exit", current_price

    if exit_reason is not None:
        _close_shadow(repo, shadow, exit_reason, theoretical_exit, funding_rate, risk_limits, now)
        return

    if not shadow["threshold_reached"] and candle_high >= threshold_price:
        repo.activate_profit_protection_breakeven(shadow_id, entry_price, now, now)


def _close_shadow(
    repo: Repository,
    shadow: dict,
    exit_reason: str,
    theoretical_exit: Decimal,
    funding_rate: Decimal,
    risk_limits: RiskLimitsConfig,
    closed_at: datetime,
) -> None:
    """Spec §5.4: size/simulated_fill_entry are read from the real
    position (plan correction, PnL-parity data sourcing) - they never
    change before the real position closes, so this read is always safe
    whether or not the real position has closed yet. Never writes to
    `positions` - only reads via repo.get_position() and writes via
    repo.close_profit_protection_shadow()."""
    real_position = repo.get_position(shadow["position_id"])
    if real_position is None:
        # Defensive only: in today's codebase positions are never deleted, so
        # the real position backing a shadow always exists in practice. But
        # per G1 ("no production impact"), this experiment's own code must
        # never let an unexpected AttributeError escape into the shared
        # monitoring tick - that would abort every OTHER shadow's processing
        # this same tick, not just this one. Leave the shadow row untouched
        # (no partial writes) rather than risk that.
        return
    simulated_fill_exit = compute_fill_price(
        theoretical_exit, _DIRECTION, risk_limits.spread_pct, risk_limits.slippage_pct, "exit"
    )
    fees = compute_fees(real_position.size, risk_limits.fee_pct)
    opened_at = datetime.fromisoformat(shadow["opened_at"])
    hold_hours = Decimal(str((closed_at - opened_at).total_seconds())) / Decimal("3600")
    funding = compute_funding(real_position.size, funding_rate, hold_hours)

    ephemeral = Position(
        position_id=shadow["shadow_id"],
        candidate_id="profit_protection_experiment",
        instrument=shadow["instrument"],
        direction=_DIRECTION,
        status="CLOSED",
        theoretical_entry=Decimal(shadow["entry_price"]),
        simulated_fill_entry=real_position.simulated_fill_entry,
        stop_loss=Decimal(shadow["original_stop_loss"]),
        target=Decimal(shadow["target"]),
        size=real_position.size,
        fill_model_version=FILL_MODEL_VERSION,
        opened_at=opened_at,
        theoretical_exit=theoretical_exit,
        simulated_fill_exit=simulated_fill_exit,
        exit_reason=exit_reason,
        fees=fees,
        funding=funding,
        closed_at=closed_at,
    )
    shadow_realized_pnl = compute_pnl(ephemeral)
    repo.close_profit_protection_shadow(
        shadow_id=shadow["shadow_id"],
        exit_reason=exit_reason,
        theoretical_exit=theoretical_exit,
        simulated_fill_exit=simulated_fill_exit,
        fees=fees,
        funding=funding,
        closed_at=closed_at,
        shadow_realized_pnl=shadow_realized_pnl,
        updated_at=closed_at,
    )


def run_profit_protection_experiment_tick(
    repo: Repository,
    open_positions: list[Position],
    closed_positions: list[Position],
    price_lookup: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]],
    now: datetime,
    settings: Settings,
    run_id: str,
) -> None:
    """Spec §3.2/§5, plan correction C2: seed -> advance -> backfill, in
    that strict order, every tick. Called from monitoring_loop.py AFTER
    close_triggered_positions, wrapped in the caller's own try/except -
    this function itself never needs to guard against crashing the caller,
    only against writing anything outside profit_protection_shadow_positions."""
    if not settings.profit_protection_experiment.enabled:
        return

    repo.set_profit_protection_activated_at_if_missing(now)
    activated_at = repo.get_profit_protection_activated_at()

    # 1) Seed - only for positions whose instrument's candle is actually
    # available this tick (plan correction C2: never seed on absent data,
    # mirrors close_triggered_positions's own price_lookup-presence skip).
    for position in open_positions:
        if position.instrument not in price_lookup:
            continue
        seed_shadows_for_position(repo, position, activated_at, now)

    # 2) Advance every currently-open shadow (includes any just seeded
    # above, since find_open_profit_protection_shadows() re-queries after
    # the seed loop's commits) against this same tick's price_lookup.
    guardian_assisted_exit_enabled = settings.guardian.assisted_exit_enabled
    for shadow in repo.find_open_profit_protection_shadows():
        if shadow["instrument"] not in price_lookup:
            continue
        try:
            candle_low, candle_high, current_price, funding_rate = price_lookup[
                shadow["instrument"]
            ]
            guardian_state = (
                _guardian_state_for(repo, shadow["position_id"], now, settings.guardian)
                if guardian_assisted_exit_enabled
                else None
            )
            advance_shadow(
                shadow, candle_low, candle_high, current_price, funding_rate, now,
                settings.risk_limits.max_position_hold_hours, guardian_state,
                guardian_assisted_exit_enabled, settings.risk_limits, repo,
            )
        except Exception as exc:
            # Review finding (round 1): a single malformed/unexpected shadow row
            # must never abort every OTHER shadow's advance this tick, nor skip
            # step 3's backfill loop entirely - same "isolate one item's
            # failure, keep processing the batch" pattern as
            # recovery_sweep.py::recovery_sweep_ticker_unavailable and
            # position_opening.py::position_open_skipped_non_numeric_risk_values.
            log_event(
                run_id,
                event="profit_protection_shadow_advance_failed",
                shadow_id=shadow["shadow_id"],
                error_type=type(exc).__name__,
                error=str(exc),
            )
            continue

    # 3) Backfill baseline outcome for whatever close_triggered_positions
    # closed this same tick (spec §5.5) - read-only against `positions`.
    for position in closed_positions:
        if position.exit_reason is None or position.fees is None or position.funding is None:
            continue  # defensive - close_triggered_positions always sets these
        baseline_pnl = compute_pnl(position)
        repo.backfill_profit_protection_baseline_outcome(
            position.position_id, position.exit_reason, baseline_pnl, now
        )
