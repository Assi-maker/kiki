from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig
from crypto_trading.schemas.evidence import SecondaryTimeframeEvidence
from crypto_trading.schemas.guardian import GuardianState
from crypto_trading.schemas.trade import Position

_ONE = Decimal("1")
_ZERO = Decimal("0")
_FIFTY = Decimal("50")


def _clip01(value: Decimal) -> Decimal:
    return max(_ZERO, min(value, _ONE))


def compute_time_decay_factor(opened_at: datetime, now: datetime, max_position_hold_hours: int) -> Decimal:
    elapsed_hours = Decimal(str((now - opened_at).total_seconds())) / Decimal("3600")
    return _clip01(elapsed_hours / Decimal(max_position_hold_hours))


def compute_momentum_decay_factor(rsi_entry: Decimal, rsi_now: Decimal) -> Decimal:
    """Reference-point-relative, not a fixed RSI threshold (design doc §4):
    only measurable when entry itself was an overbought/breakout RSI - no
    reversion-from-strength signal exists otherwise."""
    if rsi_entry <= _FIFTY:
        return _ZERO
    return _clip01((rsi_entry - rsi_now) / (rsi_entry - _FIFTY))


def compute_volume_decay_factor(zscore_entry: Decimal, zscore_now: Decimal) -> Decimal:
    if zscore_entry <= _ZERO:
        return _ZERO
    return _clip01((zscore_entry - zscore_now) / zscore_entry)


def compute_funding_decay_factor(funding_mag_entry: Decimal, funding_mag_now: Decimal) -> Decimal:
    """Magnitude-only (design doc §4.1) - quant_screener.py never stores a
    signed funding rate, so this measures "has funding pressure calmed
    down", not "has it flipped against the position"."""
    if funding_mag_entry <= _ZERO:
        return _ZERO
    return _clip01((funding_mag_entry - funding_mag_now) / funding_mag_entry)


def compute_secondary_confirmation_lost_factor(
    entry_secondary: SecondaryTimeframeEvidence | None,
    fresh_secondary: SecondaryTimeframeEvidence | None,
) -> Decimal:
    if entry_secondary is None:
        return _ZERO
    entry_evidences = [
        entry_secondary.price_volatility_evidence, entry_secondary.momentum_breakout_evidence,
        entry_secondary.volume_evidence, entry_secondary.funding_oi_evidence,
    ]
    if not any(ev.triggered for ev in entry_evidences):
        return _ZERO
    if fresh_secondary is None:
        return _ONE
    fresh_evidences = [
        fresh_secondary.price_volatility_evidence, fresh_secondary.momentum_breakout_evidence,
        fresh_secondary.volume_evidence, fresh_secondary.funding_oi_evidence,
    ]
    return _ZERO if any(ev.triggered for ev in fresh_evidences) else _ONE


def compute_market_regime_factor(btc_rsi_now: Decimal) -> Decimal:
    """LONG-only system (design doc §4): a bearish/neutral BTC-USDT RSI is a
    headwind. No entry-time reference needed - a pure current-regime read."""
    return _clip01((_FIFTY - btc_rsi_now) / _FIFTY)


def compute_decay_score(factors: dict[str, Decimal], weights: dict[str, Decimal]) -> Decimal:
    """Weight-NORMALIZED average - weights need not sum to 1.0 (design doc
    §4), so re-tuning one weight later never requires touching the others."""
    total_weight = sum(weights.get(name, _ZERO) for name in factors)
    if total_weight <= _ZERO:
        return _ZERO
    weighted_sum = sum(factors[name] * weights.get(name, _ZERO) for name in factors)
    return weighted_sum / total_weight


def compute_progress_ratio(entry_price: Decimal, target_price: Decimal, current_price: Decimal) -> Decimal:
    denominator = target_price - entry_price
    if denominator == _ZERO:
        return _ZERO
    return (current_price - entry_price) / denominator


def compute_unrealized_pnl(position: Position, current_price: Decimal) -> Decimal:
    """Same gross formula as performance/paper_track_report.py's
    _unrealized_pnl() (LONG-only), reimplemented here as a tiny pure
    function so this live loop never imports the reporting module."""
    price_return = (current_price - position.simulated_fill_entry) / position.simulated_fill_entry
    return position.size * price_return


def classify_guardian_state(
    decay_score: Decimal, unrealized_pnl_positive: bool, config: GuardianConfig
) -> GuardianState:
    if decay_score < config.watch_decay_threshold:
        return "HOLD"
    if decay_score < config.protect_decay_threshold:
        return "WATCH"
    if decay_score < config.exit_decay_threshold:
        return "PROTECT" if unrealized_pnl_positive else "WATCH"
    return "EXIT"
