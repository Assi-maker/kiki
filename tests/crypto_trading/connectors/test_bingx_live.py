from datetime import UTC, datetime

import pytest

from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
from crypto_trading.schemas.market import (
    FundingRate,
    InstrumentMetadata,
    Kline,
    OpenInterest,
    Ticker,
)


@pytest.mark.live
def test_all_five_endpoints_work_against_real_bingx_api():
    """Manuell engångskörning (AC5) - kräver riktig nätverksåtkomst, körs
    INTE i default pytest. Kör explicit: pytest -m live
    tests/crypto_trading/connectors/test_bingx_live.py -v"""
    connector = BingXMarketDataConnector(
        base_url="https://open-api.bingx.com",
        timeout_seconds=10,
        max_retries=3,
        requests_per_second=5,
        cache_ttl_seconds=0,
    )

    contracts_raw = connector.get_contracts()
    assert len(contracts_raw) > 0
    btc_contract = next(c for c in contracts_raw if c["symbol"] == "BTC-USDT")
    instrument = InstrumentMetadata.from_raw(btc_contract, fetched_at=datetime.now(UTC))
    assert instrument.symbol == "BTC-USDT"

    ticker_raw = connector.get_ticker("BTC-USDT")
    ticker = Ticker.from_raw(ticker_raw)
    assert ticker.last_price > 0

    klines_raw = connector.get_klines("BTC-USDT", interval="1h", limit=5)
    klines = [Kline.from_raw(k, instrument="BTC-USDT", interval="1h") for k in klines_raw]
    assert len(klines) == 5
    assert all(k.high >= k.low for k in klines)

    funding_raw = connector.get_funding_rate("BTC-USDT")
    funding = FundingRate.from_raw(funding_raw[0])
    assert funding.mark_price > 0

    oi_raw = connector.get_open_interest("BTC-USDT")
    oi = OpenInterest.from_raw(oi_raw)
    assert oi.open_interest > 0
