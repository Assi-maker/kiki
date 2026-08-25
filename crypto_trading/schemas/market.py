from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel


def _ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


class InstrumentMetadata(BaseModel):
    symbol: str
    status: int
    price_precision: int
    quantity_precision: int
    trade_min_usdt: Decimal
    fetched_at: datetime  # BingX contracts-svaret saknar en egen "senast uppdaterad"-
    # tidsstämpel - fetched_at fångas av anroparen vid hämtningstillfället istället.

    @classmethod
    def from_raw(cls, raw: dict, fetched_at: datetime) -> InstrumentMetadata:
        return cls(
            symbol=raw["symbol"],
            status=raw["status"],
            price_precision=raw["pricePrecision"],
            quantity_precision=raw["quantityPrecision"],
            trade_min_usdt=Decimal(str(raw["tradeMinUSDT"])),
            fetched_at=fetched_at,
        )


class Kline(BaseModel):
    instrument: str
    interval: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    observed_at: datetime

    @classmethod
    def from_raw(cls, raw: dict, instrument: str, interval: str) -> Kline:
        return cls(
            instrument=instrument,
            interval=interval,
            open=Decimal(str(raw["open"])),
            high=Decimal(str(raw["high"])),
            low=Decimal(str(raw["low"])),
            close=Decimal(str(raw["close"])),
            volume=Decimal(str(raw["volume"])),
            observed_at=_ms_to_datetime(raw["time"]),
        )


class Ticker(BaseModel):
    instrument: str
    last_price: Decimal
    price_change: Decimal
    price_change_percent: Decimal
    high_price: Decimal
    low_price: Decimal
    volume: Decimal
    quote_volume: Decimal
    open_price: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    bid_price: Decimal
    bid_qty: Decimal
    observed_at: datetime

    @classmethod
    def from_raw(cls, raw: dict) -> Ticker:
        return cls(
            instrument=raw["symbol"],
            last_price=Decimal(str(raw["lastPrice"])),
            price_change=Decimal(str(raw["priceChange"])),
            price_change_percent=Decimal(str(raw["priceChangePercent"])),
            high_price=Decimal(str(raw["highPrice"])),
            low_price=Decimal(str(raw["lowPrice"])),
            volume=Decimal(str(raw["volume"])),
            quote_volume=Decimal(str(raw["quoteVolume"])),
            open_price=Decimal(str(raw["openPrice"])),
            ask_price=Decimal(str(raw["askPrice"])),
            ask_qty=Decimal(str(raw["askQty"])),
            bid_price=Decimal(str(raw["bidPrice"])),
            bid_qty=Decimal(str(raw["bidQty"])),
            observed_at=_ms_to_datetime(raw["closeTime"]),
        )


class FundingRate(BaseModel):
    instrument: str
    funding_rate: Decimal
    mark_price: Decimal
    observed_at: datetime

    @classmethod
    def from_raw(cls, raw: dict) -> FundingRate:
        return cls(
            instrument=raw["symbol"],
            funding_rate=Decimal(str(raw["fundingRate"])),
            mark_price=Decimal(str(raw["markPrice"])),
            observed_at=_ms_to_datetime(raw["fundingTime"]),
        )


class OpenInterest(BaseModel):
    instrument: str
    open_interest: Decimal
    observed_at: datetime

    @classmethod
    def from_raw(cls, raw: dict) -> OpenInterest:
        return cls(
            instrument=raw["symbol"],
            open_interest=Decimal(str(raw["openInterest"])),
            observed_at=_ms_to_datetime(raw["time"]),
        )
