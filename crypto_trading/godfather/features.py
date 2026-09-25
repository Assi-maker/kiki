"""The one shared pre-entry feature vocabulary.

This module exists so that the features Experience Memory LEARNS from
and the features the Entry Quality layer ASKS about are, by
construction, the same strings computed the same way. Two independent
extractors would drift within a week, and the failure would be silent:
`experience._matches` is fail-closed, so a renamed bucket does not raise,
it merely stops matching - and the system would quietly go back to
knowing nothing while still reporting patterns.

Everything here is derivable BEFORE entry. Nothing derived from the
outcome may ever be added: a feature that encodes the answer makes every
pattern look like a perfect edge, which is exactly how a learning system
poisons itself.

Bucketing rather than raw numbers is deliberate. With a few hundred
trades, a continuous feature yields one sample per value and therefore no
pattern at all; coarse, stable buckets are what allow a group to ever
reach the sample-size floor. The bucket boundaries below are taken from
the thresholds the screener itself already uses (RSI 70, volume z-score
2.5), not invented, so a bucket edge means something the system already
acts on.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from crypto_trading.schemas.candidate import Candidate


def _bucket(value: float, edges: list[float], labels: list[str]) -> str:
    for edge, label in zip(edges, labels, strict=False):
        if value < edge:
            return label
    return labels[-1]


def candidate_score_bucket(score: float) -> str:
    return _bucket(score, [0.2, 0.4, 0.6], ["<0.2", "0.2-0.4", "0.4-0.6", ">=0.6"])


def rsi_bucket(rsi: float) -> str:
    """Edges at 50/70/80: 50 is the screener's own RSI baseline, 70 its
    overbought trigger, and 80 the level the Bear role repeatedly flags
    as late-entry territory in the stored assessments."""
    return _bucket(rsi, [50.0, 70.0, 80.0], ["<50", "50-70", "70-80", ">=80"])


def volume_zscore_bucket(zscore: float) -> str:
    """0 separates below-average from above-average volume; 2.5 is the
    screener's own `volume_zscore_threshold`."""
    return _bucket(zscore, [0.0, 1.0, 2.5], ["<0", "0-1", "1-2.5", ">=2.5"])


def probability_bucket(probability: float) -> str:
    return _bucket(probability, [0.3, 0.4, 0.5], ["<0.3", "0.3-0.4", "0.4-0.5", ">=0.5"])


def opportunity_score_bucket(score: float) -> str:
    return _bucket(score, [4.0, 6.0, 8.0], ["<4", "4-6", "6-8", ">=8"])


def counterargument_bucket(count: int) -> str:
    return _bucket(float(count), [1.0, 3.0, 5.0], ["0", "1-2", "3-4", ">=5"])


def hour_bucket(moment: datetime) -> str:
    """UTC six-hour blocks. Crypto trades continuously, but liquidity and
    volatility do not - and four buckets is the most granularity a
    few-hundred-trade history can support without emptying every group."""
    return _bucket(
        float(moment.hour), [6.0, 12.0, 18.0], ["00-06", "06-12", "12-18", "18-24"]
    )


def btc_regime_bucket(market_regime_factor: float) -> str:
    """Guardian's own `market_regime` factor, bucketed.

    The factor is `(50 - btc_rsi) / 50` clipped to [0, 1], so 0 means BTC
    is at or above RSI 50 and 1 means BTC RSI is 0. This is a LONG-only
    system, so higher means worse.
    """
    return _bucket(
        market_regime_factor, [0.05, 0.2, 0.4], ["btc_strong", "btc_ok", "btc_weak", "btc_bad"]
    )


def build_candidate_features(
    candidate: Candidate | None,
    opportunity_screen: dict | None = None,
    opened_at: datetime | None = None,
    regime: str | None = None,
) -> dict[str, object]:
    """The complete pre-entry feature vector for one candidate.

    Any source that is missing simply contributes no key - never a
    placeholder. Because `experience._matches` is fail-closed, an absent
    key means "this pattern does not apply to this trade" rather than a
    silently wrong match.
    """
    features: dict[str, object] = {}
    if candidate is None:
        return features

    evidence = candidate.evidence_record
    triggers = sorted(evidence.trigger_reasons)
    features["instrument"] = candidate.instrument
    features["trigger_reasons_key"] = ",".join(triggers) if triggers else "none"
    features["trigger_count"] = str(min(len(triggers), 3))
    features["candidate_score_bucket"] = candidate_score_bucket(
        float(evidence.candidate_score)
    )
    features["data_quality_status"] = evidence.data_quality_status

    features["price_volatility_triggered"] = bool(evidence.price_volatility_evidence.triggered)
    features["momentum_triggered"] = bool(evidence.momentum_breakout_evidence.triggered)
    features["volume_triggered"] = bool(evidence.volume_evidence.triggered)
    features["funding_triggered"] = bool(evidence.funding_oi_evidence.triggered)
    features["entry_rsi_bucket"] = rsi_bucket(float(evidence.momentum_breakout_evidence.value))
    features["volume_zscore_bucket"] = volume_zscore_bucket(
        float(evidence.volume_evidence.value)
    )

    secondary = evidence.secondary_timeframe_evidence
    if secondary is None:
        features["secondary_confirmed"] = "absent"
    else:
        confirmed = any(
            item.triggered
            for item in (
                secondary.price_volatility_evidence,
                secondary.momentum_breakout_evidence,
                secondary.volume_evidence,
                secondary.funding_oi_evidence,
            )
        )
        features["secondary_confirmed"] = "yes" if confirmed else "no"

    forecast = getattr(candidate, "forecast", None)
    probabilities = dict(getattr(forecast, "scenario_probabilities", {}) or {})
    if probabilities:
        features["forecast_bullish_bucket"] = probability_bucket(
            float(probabilities.get("bullish", 0.0))
        )
        features["forecast_most_likely"] = max(probabilities.items(), key=lambda kv: kv[1])[0]

    bear = getattr(candidate, "bear_adversarial", None)
    if bear is not None:
        features["bear_counterargument_bucket"] = counterargument_bucket(
            len(getattr(bear, "counterarguments", []) or [])
        )

    if opportunity_screen is not None and opportunity_screen.get("opportunity_score") is not None:
        features["opportunity_score_bucket"] = opportunity_score_bucket(
            float(opportunity_screen["opportunity_score"])
        )

    if opened_at is not None:
        features["entry_hour_bucket"] = hour_bucket(opened_at)

    if regime is not None:
        features["btc_regime_bucket"] = regime

    return features


def risk_reward_ratio(
    entry: Decimal, stop_loss: Decimal, target: Decimal
) -> Decimal | None:
    risk = abs(entry - stop_loss)
    if risk == 0:
        return None
    return abs(target - entry) / risk
