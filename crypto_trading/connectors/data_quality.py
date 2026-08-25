from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from crypto_trading.schemas.market import Kline

DataQualityResult = Literal["ok", "invalid"]


def check_completeness(raw: dict, required_fields: list[str]) -> DataQualityResult:
    for field in required_fields:
        if raw.get(field) is None:
            return "invalid"
    return "ok"


def check_staleness(
    observed_at: datetime, now: datetime, max_age_seconds: float
) -> DataQualityResult:
    age_seconds = (now - observed_at).total_seconds()
    if age_seconds < 0:
        return "invalid"  # framtida tidsstämpel är lika orimligt som för gammal
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
