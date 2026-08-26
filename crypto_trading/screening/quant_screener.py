from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.schemas.market import FundingRate, Kline


def _sorted_up_to(klines: list[Kline], evaluated_at: datetime) -> list[Kline]:
    """SPEC §8.4: filtrerar bort varje datapunkt daterad efter evaluated_at,
    sorterar sedan kronologiskt. Central, delad guard - alla evidence-
    byggare i denna modul går via denna funktion (eller motsvarande för
    funding rates), aldrig direkt på rå inputlista."""
    visible = [k for k in klines if k.observed_at <= evaluated_at]
    return sorted(visible, key=lambda k: k.observed_at)


def build_price_volatility_evidence(
    klines: list[Kline],
    threshold_pct: Decimal,
    lookback: int,
    evaluated_at: datetime,
) -> PriceVolatilityEvidence:
    ordered = _sorted_up_to(klines, evaluated_at)
    latest, previous = ordered[-1], ordered[-2]
    pct_change = abs((latest.close - previous.close) / previous.close) * 100

    window = ordered[-(lookback + 1) : -1]
    historical_changes = [
        abs((window[i].close - window[i - 1].close) / window[i - 1].close) * 100
        for i in range(1, len(window))
        if window[i - 1].close != 0
    ]
    baseline = (
        sum(historical_changes) / len(historical_changes) if historical_changes else Decimal("0")
    )

    return PriceVolatilityEvidence(
        triggered=pct_change > threshold_pct,
        metric="pct_change",
        value=float(pct_change),
        baseline=float(baseline),
        threshold=float(threshold_pct),
    )


def _compute_rsi(closes: list[Decimal], period: int) -> Decimal:
    window = closes[-(period + 1) :]
    gains, losses = [], []
    for i in range(1, len(window)):
        delta = window[i] - window[i - 1]
        gains.append(max(delta, Decimal("0")))
        losses.append(max(-delta, Decimal("0")))
    avg_gain = sum(gains) / len(gains)
    avg_loss = sum(losses) / len(losses)
    if avg_gain == 0 and avg_loss == 0:
        return Decimal("100") / 2  # helt platt fönster: neutralt RSI, inte "maximalt överköpt"
    if avg_loss == 0:
        return Decimal("100")
    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def build_momentum_breakout_evidence(
    klines: list[Kline],
    rsi_period: int,
    overbought_threshold: Decimal,
    evaluated_at: datetime,
) -> MomentumBreakoutEvidence:
    ordered = _sorted_up_to(klines, evaluated_at)
    rsi = _compute_rsi([k.close for k in ordered], rsi_period)
    return MomentumBreakoutEvidence(
        triggered=rsi > overbought_threshold,
        metric="rsi",
        value=float(rsi),
        baseline=50.0,  # RSI:s neutrala referenspunkt, inte ett historiskt medelvärde
        threshold=float(overbought_threshold),
    )


def _compute_zscore(latest: Decimal, history: list[Decimal]) -> Decimal:
    if not history:
        return Decimal("0")
    mean = sum(history) / len(history)
    variance = sum((x - mean) ** 2 for x in history) / len(history)
    if variance <= 0:
        # Nollvarians i historiken: en avvikelse här är matematiskt odefinierad
        # (division med noll), men i praktiken det MEST anomala fallet som
        # finns - en helt platt historik som plötsligt avviker, inte ett
        # "inget mätbart" fall. En identisk latest mot en platt historik är
        # fortsatt noll avvikelse.
        return Decimal("0") if latest == mean else Decimal("1000")
    stddev = variance.sqrt()
    return (latest - mean) / stddev


def build_volume_evidence(
    klines: list[Kline],
    zscore_threshold: Decimal,
    lookback: int,
    evaluated_at: datetime,
) -> VolumeEvidence:
    ordered = _sorted_up_to(klines, evaluated_at)
    latest_volume = ordered[-1].volume
    history = [k.volume for k in ordered[-(lookback + 1) : -1]]
    zscore = _compute_zscore(latest_volume, history)
    return VolumeEvidence(
        triggered=zscore > zscore_threshold,
        metric="volume_zscore",
        value=float(zscore),
        baseline=0.0,  # z-score är per definition centrerat på noll
        threshold=float(zscore_threshold),
    )


def _sorted_funding_up_to(
    funding_rates: list[FundingRate], evaluated_at: datetime
) -> list[FundingRate]:
    visible = [f for f in funding_rates if f.observed_at <= evaluated_at]
    return sorted(visible, key=lambda f: f.observed_at)


def build_funding_oi_evidence(
    funding_rates: list[FundingRate],
    threshold_pct: Decimal,
    evaluated_at: datetime,
) -> FundingOpenInterestEvidence:
    # Phase 1:s BingXMarketDataConnector.get_open_interest() returnerar bara
    # en aktuell engångssnapshot, ingen historik-endpoint - funding_oi_evidence
    # baseras därför på funding-rate-historik (get_funding_rate(limit=N)),
    # som redan stöds. Aktuell open_interest ingår i den kritiska data §8.1
    # kräver, men bidrar inte till detta måttets numeriska baseline i Phase 2.
    ordered = _sorted_funding_up_to(funding_rates, evaluated_at)
    latest = ordered[-1]
    history = ordered[:-1]
    value = abs(latest.funding_rate) * 100
    baseline = (
        sum(abs(f.funding_rate) for f in history) / len(history) * 100
        if history
        else Decimal("0")
    )
    return FundingOpenInterestEvidence(
        triggered=value > threshold_pct,
        metric="funding_rate_pct",
        value=float(value),
        baseline=float(baseline),
        threshold=float(threshold_pct),
    )


def _compute_candidate_score(evidences: list) -> float:
    """Transparent och reproducerbart (SPEC §4): för varje signal, hur
    mycket överstiger value sitt threshold (i förhållande till threshold),
    klippt till [0,1]. candidate_score = medelvärdet över de fyra
    signalerna. Ingen AI, inget dolt vägt medel."""
    ratios = []
    for ev in evidences:
        if ev.threshold == 0:
            ratios.append(1.0 if ev.triggered else 0.0)
            continue
        ratio = max(0.0, (ev.value - ev.threshold) / ev.threshold)
        ratios.append(min(ratio, 1.0))
    return sum(ratios) / len(ratios)


def _invalid_data_record(
    instrument: str, timeframes: list[str], evaluated_at: datetime
) -> CandidateEvidenceRecord:
    placeholder = dict(triggered=False, metric="n/a", value=0.0, baseline=0.0, threshold=0.0)
    return CandidateEvidenceRecord(
        instrument=instrument,
        timeframes=timeframes,
        evaluated_at=evaluated_at,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=0.0,
        trigger_reasons=[],
        data_quality_status="invalid",
        outcome="not_a_candidate",
    )


def evaluate_candidate(
    instrument: str,
    timeframes: list[str],
    klines: list[Kline],
    funding_rates: list[FundingRate],
    data_quality_status: Literal["ok", "invalid"],
    evaluated_at: datetime,
    price_volatility_threshold_pct: Decimal,
    lookback: int,
    rsi_period: int,
    rsi_overbought_threshold: Decimal,
    volume_zscore_threshold: Decimal,
    funding_rate_threshold_pct: Decimal,
) -> CandidateEvidenceRecord:
    """Ren funktion: samma indata -> alltid identisk output (AC2). Kräver
    att data_quality_status redan är beräknad av anroparen via Phase 1:s
    connectors.data_quality (check_completeness/check_staleness/
    check_kline_consistency/classify) - screenern gissar aldrig själv om
    datan är pålitlig. Uttalar sig aldrig om riktning (AC1) - schemat har
    strukturellt inget sådant fält."""
    if data_quality_status == "invalid":
        return _invalid_data_record(instrument, timeframes, evaluated_at)

    price_ev = build_price_volatility_evidence(
        klines, price_volatility_threshold_pct, lookback, evaluated_at
    )
    momentum_ev = build_momentum_breakout_evidence(
        klines, rsi_period, rsi_overbought_threshold, evaluated_at
    )
    volume_ev = build_volume_evidence(klines, volume_zscore_threshold, lookback, evaluated_at)
    funding_ev = build_funding_oi_evidence(funding_rates, funding_rate_threshold_pct, evaluated_at)

    named = [
        ("price_volatility", price_ev),
        ("momentum_breakout", momentum_ev),
        ("volume", volume_ev),
        ("funding_oi", funding_ev),
    ]
    trigger_reasons = [name for name, ev in named if ev.triggered]

    return CandidateEvidenceRecord(
        instrument=instrument,
        timeframes=timeframes,
        evaluated_at=evaluated_at,
        price_volatility_evidence=price_ev,
        momentum_breakout_evidence=momentum_ev,
        volume_evidence=volume_ev,
        funding_oi_evidence=funding_ev,
        candidate_score=_compute_candidate_score([price_ev, momentum_ev, volume_ev, funding_ev]),
        trigger_reasons=trigger_reasons,
        data_quality_status=data_quality_status,
        outcome="worth_deeper_analysis" if trigger_reasons else "not_a_candidate",
    )
