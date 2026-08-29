from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from crypto_trading.schemas.market import Kline

DataQualityResult = Literal["ok", "invalid"]

_FUTURE_TIMESTAMP_GRACE_SECONDS = 5
"""AC3 2026-08-28: en riktig BingX-ticker vars closeTime landade ~3s efter
lokal `now` avslöjade att en skarp 0-gräns gjorde ALL live-data "invalid" -
en levande tickers closeTime speglar börsens klocka vid svarstillfället,
som normalt hinner passera klientens `now` (fångad före request-latensen)
även för det allra första anropet i en tick. En liten, explicit tolerans
för denna normala klock-/nätverksskew, inte en lucka i fail-closed-
principen: tydligt framtida tidsstämplar (bortom denna gräns) är
fortfarande lika otillförlitliga som för gamla."""


def check_completeness(raw: dict, required_fields: list[str]) -> DataQualityResult:
    for field in required_fields:
        if raw.get(field) is None:
            return "invalid"
    return "ok"


def check_staleness(
    observed_at: datetime, now: datetime, max_age_seconds: float
) -> DataQualityResult:
    age_seconds = (now - observed_at).total_seconds()
    if age_seconds < -_FUTURE_TIMESTAMP_GRACE_SECONDS:
        return "invalid"  # bortom grace-perioden: fortfarande lika orimligt som för gammal
    if age_seconds > max_age_seconds:
        return "invalid"
    return "ok"


def check_kline_consistency(klines: list[Kline], tolerance_pct: Decimal) -> DataQualityResult:
    """Strukturella invarianter (kräver ingen historik utöver den egna
    batchen) plus en median-avvikelsekontroll inom samma batch."""
    for kline in klines:
        if kline.high < kline.low:
            return "invalid"
        if kline.volume < 0:
            return "invalid"
        if kline.open <= 0 or kline.close <= 0 or kline.high <= 0 or kline.low <= 0:
            return "invalid"
    if len(klines) >= 3:
        closes = sorted(k.close for k in klines)
        median = closes[len(closes) // 2]
        if median > 0:
            for kline in klines:
                deviation = abs(kline.close - median) / median
                if deviation > tolerance_pct:
                    return "invalid"
    return "ok"


def classify(*results: DataQualityResult) -> DataQualityResult:
    """Kombinerar flera delresultat. All BingX-data är kritisk (SPEC §14) -
    Phase 1 kan därför bara producera 'ok' eller 'invalid', aldrig
    'degraded'. 'degraded' blir först möjligt i senare faser när icke-
    kritiska källor (nyheter) aggregeras tillsammans med BingX-data i en
    CandidateEvidenceRecord."""
    return "invalid" if "invalid" in results else "ok"
