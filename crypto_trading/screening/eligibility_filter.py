from __future__ import annotations

from decimal import Decimal
from typing import Literal

from crypto_trading.schemas.market import InstrumentMetadata, Ticker

_TRADING_STATUS = 1  # BingX contracts-svar, verifierat live Phase 1 (SPEC_CRYPTO.md §14)


def compute_spread_pct(ticker: Ticker) -> Decimal:
    mid = (ticker.ask_price + ticker.bid_price) / 2
    if mid <= 0:
        return Decimal("1")  # fail-closed: garanterat ineligibelt, aldrig division med noll
    return (ticker.ask_price - ticker.bid_price) / mid


def check_eligibility(
    instrument: InstrumentMetadata,
    ticker: Ticker,
    data_quality_status: Literal["ok", "invalid"],
    min_quote_volume_24h_usdt: Decimal,
    max_spread_pct: Decimal,
) -> tuple[bool, str]:
    if instrument.status != _TRADING_STATUS:
        return False, "not_trading"
    if data_quality_status != "ok":
        return False, "data_quality_invalid"
    if ticker.quote_volume < min_quote_volume_24h_usdt:
        return False, "insufficient_liquidity"
    if compute_spread_pct(ticker) > max_spread_pct:
        return False, "spread_too_wide"
    return True, "eligible"


def select_top_n(eligible: list[Ticker], n: int) -> list[str]:
    """Rent, deterministiskt urval - tar INGET beslut om att skapa en
    Candidate-rad (SPEC: Top N-medlemskap är ingen tradingsignal, se AC4)."""
    ranked = sorted(eligible, key=lambda t: t.quote_volume, reverse=True)
    return [t.instrument for t in ranked[:n]]
