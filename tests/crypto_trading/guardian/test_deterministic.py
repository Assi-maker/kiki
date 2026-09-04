from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig
from crypto_trading.guardian.deterministic import (
    classify_guardian_state,
    compute_decay_score,
    compute_funding_decay_factor,
    compute_market_regime_factor,
    compute_momentum_decay_factor,
    compute_progress_ratio,
    compute_secondary_confirmation_lost_factor,
    compute_time_decay_factor,
    compute_unrealized_pnl,
    compute_volume_decay_factor,
)
from crypto_trading.schemas.evidence import (
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    SecondaryTimeframeEvidence,
    VolumeEvidence,
)
from crypto_trading.schemas.trade import Position


def _placeholder_ev(**overrides):
    base = dict(triggered=True, metric="m", value=1.0, baseline=0.0, threshold=0.5)
    base.update(overrides)
    return base


def test_compute_time_decay_factor_scales_with_elapsed_vs_max_hold():
    opened_at = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    now = opened_at + timedelta(hours=12)

    factor = compute_time_decay_factor(opened_at, now, max_position_hold_hours=24)

    assert factor == Decimal("0.5")


def test_compute_time_decay_factor_clips_at_one():
    opened_at = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)
    now = opened_at + timedelta(hours=48)

    factor = compute_time_decay_factor(opened_at, now, max_position_hold_hours=24)

    assert factor == Decimal("1")


def test_compute_momentum_decay_factor_zero_when_rsi_unchanged():
    assert compute_momentum_decay_factor(rsi_entry=Decimal("75"), rsi_now=Decimal("75")) == Decimal("0")


def test_compute_momentum_decay_factor_full_decay_at_neutral_rsi():
    assert compute_momentum_decay_factor(rsi_entry=Decimal("75"), rsi_now=Decimal("50")) == Decimal("1")


def test_compute_momentum_decay_factor_partial_decay():
    # entry 80, now 65: (80-65)/(80-50) = 0.5
    assert compute_momentum_decay_factor(rsi_entry=Decimal("80"), rsi_now=Decimal("65")) == Decimal("0.5")


def test_compute_momentum_decay_factor_zero_when_entry_not_overbought():
    assert compute_momentum_decay_factor(rsi_entry=Decimal("40"), rsi_now=Decimal("20")) == Decimal("0")


def test_compute_volume_decay_factor_partial_decay():
    # entry zscore 4, now 2: (4-2)/4 = 0.5
    assert compute_volume_decay_factor(zscore_entry=Decimal("4"), zscore_now=Decimal("2")) == Decimal("0.5")


def test_compute_volume_decay_factor_zero_when_entry_not_elevated():
    assert compute_volume_decay_factor(zscore_entry=Decimal("-1"), zscore_now=Decimal("-5")) == Decimal("0")


def test_compute_funding_decay_factor_partial_decay():
    result = compute_funding_decay_factor(funding_mag_entry=Decimal("0.08"), funding_mag_now=Decimal("0.04"))
    assert result == Decimal("0.5")


def test_compute_secondary_confirmation_lost_when_entry_confirmed_but_now_does_not():
    entry_secondary = SecondaryTimeframeEvidence(
        timeframe="1h",
        price_volatility_evidence=PriceVolatilityEvidence(**_placeholder_ev(triggered=False)),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**_placeholder_ev(triggered=True)),
        volume_evidence=VolumeEvidence(**_placeholder_ev(triggered=False)),
        funding_oi_evidence=FundingOpenInterestEvidence(**_placeholder_ev(triggered=False)),
    )
    fresh_secondary = SecondaryTimeframeEvidence(
        timeframe="1h",
        price_volatility_evidence=PriceVolatilityEvidence(**_placeholder_ev(triggered=False)),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**_placeholder_ev(triggered=False)),
        volume_evidence=VolumeEvidence(**_placeholder_ev(triggered=False)),
        funding_oi_evidence=FundingOpenInterestEvidence(**_placeholder_ev(triggered=False)),
    )

    result = compute_secondary_confirmation_lost_factor(entry_secondary, fresh_secondary)

    assert result == Decimal("1")


def test_compute_secondary_confirmation_lost_zero_when_no_entry_evidence():
    assert compute_secondary_confirmation_lost_factor(None, None) == Decimal("0")


def test_compute_market_regime_factor_zero_when_btc_bullish():
    assert compute_market_regime_factor(btc_rsi_now=Decimal("60")) == Decimal("0")


def test_compute_market_regime_factor_full_when_btc_fully_bearish():
    assert compute_market_regime_factor(btc_rsi_now=Decimal("0")) == Decimal("1")


def test_compute_decay_score_is_weight_normalized_average():
    factors = {"a": Decimal("1.0"), "b": Decimal("0.0")}
    weights = {"a": Decimal("1"), "b": Decimal("3")}

    score = compute_decay_score(factors, weights)

    # (1.0*1 + 0.0*3) / (1+3) = 0.25
    assert score == Decimal("0.25")


def test_compute_progress_ratio_near_entry_is_near_zero():
    ratio = compute_progress_ratio(entry_price=Decimal("100"), target_price=Decimal("110"), current_price=Decimal("100"))
    assert ratio == Decimal("0")


def test_compute_progress_ratio_at_target_is_one():
    ratio = compute_progress_ratio(entry_price=Decimal("100"), target_price=Decimal("110"), current_price=Decimal("110"))
    assert ratio == Decimal("1")


def test_compute_unrealized_pnl_matches_gross_formula():
    position = Position(
        position_id="p1", candidate_id="p1", instrument="BTCUSDT", direction="LONG",
        status="OPEN_POSITION", theoretical_entry=Decimal("100"), simulated_fill_entry=Decimal("100"),
        stop_loss=Decimal("90"), target=Decimal("120"), size=Decimal("1000"),
        fill_model_version="v1", opened_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    pnl = compute_unrealized_pnl(position, current_price=Decimal("110"))
    assert pnl == Decimal("100")  # 1000 * (110-100)/100


def test_classify_guardian_state_hold_below_watch_threshold():
    config = GuardianConfig()
    assert classify_guardian_state(Decimal("0.1"), unrealized_pnl_positive=True, config=config) == "HOLD"


def test_classify_guardian_state_watch_between_thresholds():
    config = GuardianConfig()
    assert classify_guardian_state(Decimal("0.45"), unrealized_pnl_positive=True, config=config) == "WATCH"


def test_classify_guardian_state_protect_requires_profit():
    config = GuardianConfig()
    assert classify_guardian_state(Decimal("0.6"), unrealized_pnl_positive=True, config=config) == "PROTECT"
    assert classify_guardian_state(Decimal("0.6"), unrealized_pnl_positive=False, config=config) == "WATCH"


def test_classify_guardian_state_exit_regardless_of_pnl():
    config = GuardianConfig()
    assert classify_guardian_state(Decimal("0.9"), unrealized_pnl_positive=True, config=config) == "EXIT"
    assert classify_guardian_state(Decimal("0.9"), unrealized_pnl_positive=False, config=config) == "EXIT"
