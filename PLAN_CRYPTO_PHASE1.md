# Crypto Trading — Phase 1 (BingX Market Data) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bygga en BingX-connector för publik, nyckellös swap-marknadsdata (kontrakt, ticker, klines, funding rate, open interest) plus ett data-quality-lager som implementerar SPEC §8.1 exakt — helt testbart utan nätverk i default `pytest`, med en explicit, en gång manuellt körd verifiering mot riktig BingX-data.

**Architecture:** `schemas/market.py` (beroendefritt) ← `connectors/exceptions.py` ← `connectors/base.py` ← `connectors/bingx_market_data.py` + `connectors/data_quality.py`. Connectorn hämtar och returnerar **rådata** (dict) — mappning till typade scheman (`from_raw`) och data-quality-klassificering är separata steg, samma separation-of-concerns-princip som Fas 1:s `BaseConnector` (`fetch()` ⊥ `pipeline/normalize.py`).

**Tech Stack:** Python 3.13, `pydantic` v2, `httpx`, `tenacity`, `pytest` + `respx`. Alla redan i `pyproject.toml`.

**Spec:** `SPEC_CRYPTO.md` §14 (BingX kritisk/publik/nyckellös), §15 (connector-krav), §8.1 (data-quality). `PLAN_CRYPTO.md` Phase 1-avsnittet (Omfattning/Levererar/Acceptance criteria). Konsoliderad Phase 1-design godkänd i konversationshistoriken, inklusive skärpningarna: config-driven `required_fields` och `Literal["ok","invalid"]` som typnivå-garanti (degraded är omöjligt i Phase 1).

## Global Constraints

- Endast publika BingX swap (USDT-marginerade futures) market-data-endpoints. Ingen kod refererar ett konto, en order eller en broker-credential (SPEC §1/§19) — verifierat av befintlig `tests/crypto_trading/test_no_intelligence_coupling.py` (glob:ar hela `crypto_trading/`) plus en dedikerad endpoint-whitelist-test i denna plan.
- Inga endpoint-format antas i förväg — samtliga fem endpoints och deras exakta svarsformat är **verifierade live** mot `https://open-api.bingx.com` under brainstorming-sessionen (se kodkommentarer nedan för de faktiska svaren).
- Alla pris-/volym-/funding-/OI-fält är `Decimal`, konverterade via `Decimal(str(x))` — aldrig via `float`, oavsett om BingX råkar skicka värdet som JSON-sträng eller JSON-tal (verifierat: `tradeMinUSDT` kommer som tal, de flesta andra som strängar — inkonsekvent nog i sig att motivera `str()`-omvägen genomgående).
- `required_fields` och `max_data_age_seconds` (§8.1) läses uteslutande från `config/pipeline.yaml`, aldrig hårdkodat i Python — och valideras vid config-inläsning (fail-fast) att de täcker alla fem datatyper.
- Data-quality-klassificeringen i Phase 1 returnerar `Literal["ok", "invalid"]` — en snävare typ än hela `DataQualityStatus`. All BingX-data är kritisk (SPEC §14); `degraded` blir först möjligt i senare faser när icke-kritiska källor aggregeras.
- Ingen skrivning till SQLite i denna fas — Phase 1 producerar bara typade Python-objekt i minnet. Repository-integrationen (skriva `Candidate`/`evidence_record`) är Phase 2:s jobb.
- Default `pytest`-körning kräver noll nätverk. Ett separat, `@pytest.mark.live`-märkt test finns för den manuella engångsverifieringen (AC5) och exkluderas från default-körning.
- `intelligence/` rörs inte. `ruff` line-length 100, regler `E,F,I,UP,B`.

---

## Task 1: Utöka config för Phase 1 (`PipelineConfig` + `pipeline.yaml`)

**Files:**
- Modify: `crypto_trading/config/loader.py`
- Modify: `crypto_trading/config/pipeline.yaml`
- Modify: `tests/crypto_trading/config/test_loader.py`

**Interfaces:**
- Produces: `PipelineConfig` får sju nya fält: `required_fields`, `screener_timeframes`, `bingx_base_url`, `bingx_requests_per_second`, `bingx_cache_ttl_seconds`, `bingx_max_retries`, `kline_consistency_tolerance_pct`. `max_data_age_seconds` och `required_fields` valideras (fail-fast) att innehålla nycklarna `{ticker, kline, funding_rate, open_interest, contracts}`.

- [ ] **Step 1: Write the failing tests**

Lägg till i `tests/crypto_trading/config/test_loader.py` (efter befintliga tester):

```python
def test_get_settings_loads_phase1_fields():
    settings = get_settings()
    assert settings.pipeline.screener_timeframes == ["1h", "4h"]
    assert settings.pipeline.bingx_base_url == "https://open-api.bingx.com"
    assert settings.pipeline.bingx_requests_per_second > 0
    assert set(settings.pipeline.required_fields.keys()) >= {
        "ticker", "kline", "funding_rate", "open_interest", "contracts"
    }
    assert set(settings.pipeline.max_data_age_seconds.keys()) >= {
        "ticker", "kline", "funding_rate", "open_interest", "contracts"
    }


def test_pipeline_config_rejects_missing_max_data_age_seconds_key():
    with pytest.raises(ValidationError):
        PipelineConfig(
            discovery_interval_minutes=15,
            monitoring_interval_seconds=30,
            top_n=30,
            cooldown_minutes=60,
            max_data_age_seconds={"ticker": 30, "kline": 120, "funding_rate": 3600},
            min_sample_size_for_calibration=30,
            calibration_preliminary_sample_size=10,
            sqlite_busy_timeout_ms=5000,
            required_fields={
                "ticker": ["lastPrice"], "kline": ["open"], "funding_rate": ["fundingRate"],
                "open_interest": ["openInterest"], "contracts": ["symbol"],
            },
            screener_timeframes=["1h"],
            bingx_base_url="https://open-api.bingx.com",
            bingx_requests_per_second=10,
            bingx_cache_ttl_seconds=5,
            bingx_max_retries=3,
            kline_consistency_tolerance_pct=Decimal("0.5"),
        )


def test_pipeline_config_rejects_missing_required_fields_key():
    with pytest.raises(ValidationError):
        PipelineConfig(
            discovery_interval_minutes=15,
            monitoring_interval_seconds=30,
            top_n=30,
            cooldown_minutes=60,
            max_data_age_seconds={
                "ticker": 30, "kline": 120, "funding_rate": 3600,
                "open_interest": 300, "contracts": 86400,
            },
            min_sample_size_for_calibration=30,
            calibration_preliminary_sample_size=10,
            sqlite_busy_timeout_ms=5000,
            required_fields={"ticker": ["lastPrice"]},
            screener_timeframes=["1h"],
            bingx_base_url="https://open-api.bingx.com",
            bingx_requests_per_second=10,
            bingx_cache_ttl_seconds=5,
            bingx_max_retries=3,
            kline_consistency_tolerance_pct=Decimal("0.5"),
        )
```

Lägg till `PipelineConfig` i importen från `crypto_trading.config.loader` (redan importerad i filen sedan Phase 0).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/config/test_loader.py -v`
Expected: `test_get_settings_loads_phase1_fields` FAIL med `AttributeError` (fälten finns inte än). De två övriga FAIL eftersom `PipelineConfig(...)` inte accepterar de nya nyckelordsargumenten (`TypeError`).

- [ ] **Step 3: Modify `pipeline.yaml`**

I `crypto_trading/config/pipeline.yaml`, ändra `max_data_age_seconds`-blocket och lägg till nya nycklar på slutet:

```yaml
discovery_interval_minutes: 15
monitoring_interval_seconds: 30
top_n: 30
cooldown_minutes: 60
max_data_age_seconds:
  ticker: 30
  kline: 120
  funding_rate: 3600
  open_interest: 300
  contracts: 86400
min_sample_size_for_calibration: 30
calibration_preliminary_sample_size: 10
sqlite_busy_timeout_ms: 5000
required_fields:
  ticker: [lastPrice, askPrice, bidPrice, quoteVolume, closeTime]
  kline: [open, high, low, close, volume, time]
  funding_rate: [fundingRate, fundingTime, markPrice]
  open_interest: [openInterest, time]
  contracts: [symbol, status]
screener_timeframes: ["1h", "4h"]
bingx_base_url: "https://open-api.bingx.com"
bingx_requests_per_second: 10
bingx_cache_ttl_seconds: 5
bingx_max_retries: 3
kline_consistency_tolerance_pct: "0.5"
```

- [ ] **Step 4: Modify `loader.py`**

Lägg till `field_validator`-import (redan importerad `Field, BaseModel` — lägg till `field_validator` i samma rad) och utöka `PipelineConfig`:

```python
from pydantic import BaseModel, Field, ValidationError, field_validator
```

```python
_REQUIRED_DATA_TYPES = {"ticker", "kline", "funding_rate", "open_interest", "contracts"}


class PipelineConfig(BaseModel):
    discovery_interval_minutes: int = Field(gt=0)
    monitoring_interval_seconds: int = Field(gt=0)
    top_n: int = Field(gt=0)
    cooldown_minutes: int = Field(gt=0)
    max_data_age_seconds: dict[str, int]
    min_sample_size_for_calibration: int = Field(gt=0)
    calibration_preliminary_sample_size: int = Field(gt=0)
    sqlite_busy_timeout_ms: int = Field(gt=0)
    required_fields: dict[str, list[str]]
    screener_timeframes: list[str]
    bingx_base_url: str
    bingx_requests_per_second: float = Field(gt=0)
    bingx_cache_ttl_seconds: float = Field(ge=0)
    bingx_max_retries: int = Field(gt=0)
    kline_consistency_tolerance_pct: Decimal = Field(gt=0, le=1)

    @field_validator("max_data_age_seconds")
    @classmethod
    def max_data_age_seconds_covers_all_data_types(cls, v: dict[str, int]) -> dict[str, int]:
        missing = _REQUIRED_DATA_TYPES - v.keys()
        if missing:
            raise ValueError(f"max_data_age_seconds missing required keys: {missing}")
        return v

    @field_validator("required_fields")
    @classmethod
    def required_fields_covers_all_data_types(
        cls, v: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        missing = _REQUIRED_DATA_TYPES - v.keys()
        if missing:
            raise ValueError(f"required_fields missing required keys: {missing}")
        return v
```

(`Decimal` är redan importerad i `loader.py` sedan Phase 0.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/config/test_loader.py -v`
Expected: PASS (8 tester: 5 från Phase 0 + 3 nya).

- [ ] **Step 6: Run full crypto_trading suite to verify Phase 0 is unaffected**

Run: `pytest tests/crypto_trading/ -v`
Expected: alla tester gröna (Phase 0:s 90 + de 3 nya = 93).

- [ ] **Step 7: Commit**

```bash
git add crypto_trading/config/loader.py crypto_trading/config/pipeline.yaml tests/crypto_trading/config/test_loader.py
git commit -m "crypto_trading Phase 1 steg 1: utöka config för BingX/data-quality (required_fields, thresholds, rate-limit)"
```

---

## Task 2: `schemas/market.py`

**Files:**
- Create: `crypto_trading/schemas/market.py`
- Test: `tests/crypto_trading/schemas/test_market.py`

**Interfaces:**
- Produces: `InstrumentMetadata`, `Kline`, `Ticker`, `FundingRate`, `OpenInterest` — var och en med en `from_raw(...)`-classmethod som mappar BingX:s verifierade råsvar. Konsumeras av Task 4 (connector) indirekt via Task 6/7.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/schemas/test_market.py
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.schemas.market import (
    FundingRate,
    InstrumentMetadata,
    Kline,
    OpenInterest,
    Ticker,
)

# Fixtures = riktiga svar, verifierade live mot https://open-api.bingx.com
# under Phase 1-brainstormingen (2026-08-25).

_RAW_CONTRACT = {
    "contractId": "100", "symbol": "BTC-USDT", "size": "0.0001",
    "quantityPrecision": 4, "pricePrecision": 1, "feeRate": 0.0005,
    "tradeMinUSDT": 2, "currency": "USDT", "asset": "BTC", "status": 1,
    "launchTime": 1586275200000, "displayName": "BTC-USDT",
}

_RAW_KLINE = {
    "open": "78162.6", "close": "77930.1", "high": "78260.0",
    "low": "77831.0", "volume": "361.4139", "time": 1787691600000,
}

_RAW_TICKER = {
    "symbol": "BTC-USDT", "priceChange": "-1019.8", "priceChangePercent": "-1.29",
    "lastPrice": "77955.4", "highPrice": "81263.0", "lowPrice": "77831.0",
    "volume": "16449.4100", "quoteVolume": "1306179101.75", "openPrice": "78975.2",
    "openTime": 1787605813000, "closeTime": 1787692213000,
    "askPrice": "77993.5", "askQty": "1.2853", "bidPrice": "77993.4", "bidQty": "24.1266",
}

_RAW_FUNDING_RATE = {
    "symbol": "BTC-USDT", "fundingRate": "0.00010000",
    "fundingTime": 1787673600000, "markPrice": "79463.4",
}

_RAW_OPEN_INTEREST = {
    "openInterest": "1100360743.1", "symbol": "BTC-USDT", "time": 1787692230396,
}


def test_instrument_metadata_from_raw_uses_decimal_not_float():
    fetched_at = datetime.now(UTC)
    instrument = InstrumentMetadata.from_raw(_RAW_CONTRACT, fetched_at=fetched_at)
    assert instrument.symbol == "BTC-USDT"
    assert instrument.status == 1
    assert instrument.trade_min_usdt == Decimal("2")
    assert isinstance(instrument.trade_min_usdt, Decimal)
    assert instrument.fetched_at == fetched_at


def test_kline_from_raw_maps_time_to_observed_at():
    kline = Kline.from_raw(_RAW_KLINE, instrument="BTC-USDT", interval="1h")
    assert kline.close == Decimal("77930.1")
    assert kline.high >= kline.low
    assert kline.observed_at == datetime.fromtimestamp(1787691600000 / 1000, tz=UTC)


def test_ticker_from_raw_maps_close_time_to_observed_at():
    ticker = Ticker.from_raw(_RAW_TICKER)
    assert ticker.instrument == "BTC-USDT"
    assert ticker.last_price == Decimal("77955.4")
    assert ticker.quote_volume == Decimal("1306179101.75")
    assert ticker.observed_at == datetime.fromtimestamp(1787692213000 / 1000, tz=UTC)


def test_funding_rate_from_raw():
    funding = FundingRate.from_raw(_RAW_FUNDING_RATE)
    assert funding.funding_rate == Decimal("0.00010000")
    assert funding.mark_price == Decimal("79463.4")
    assert funding.observed_at == datetime.fromtimestamp(1787673600000 / 1000, tz=UTC)


def test_open_interest_from_raw():
    oi = OpenInterest.from_raw(_RAW_OPEN_INTEREST)
    assert oi.open_interest == Decimal("1100360743.1")
    assert oi.observed_at == datetime.fromtimestamp(1787692230396 / 1000, tz=UTC)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/schemas/test_market.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/schemas/market.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/schemas/test_market.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/schemas/market.py tests/crypto_trading/schemas/test_market.py
git commit -m "crypto_trading Phase 1 steg 2: market-scheman (InstrumentMetadata/Kline/Ticker/FundingRate/OpenInterest)"
```

---

## Task 3: `connectors/exceptions.py`

**Files:**
- Create: `crypto_trading/connectors/exceptions.py`
- Create: `crypto_trading/connectors/__init__.py`
- Create: `tests/crypto_trading/connectors/__init__.py`

**Interfaces:**
- Produces: `ConnectorError` (bas), `ConnectorUnavailableError` — konsumeras av Task 4/5.

- [ ] **Step 1: Skapa filerna**

```python
# crypto_trading/connectors/exceptions.py
from __future__ import annotations


class ConnectorError(Exception):
    """Basklass för alla connector-fel i crypto_trading/."""


class ConnectorUnavailableError(ConnectorError):
    """BingX svarade inte (timeout/nätverksfel), gav ett icke-2xx-svar efter
    uttömd retry, eller ett API-nivå-fel (code != 0). Aldrig en gissning -
    anroparen ska klassa den underliggande candidate:n som DATA_INVALID
    nedströms (SPEC §8.2), inte krascha."""
```

`crypto_trading/connectors/__init__.py` och `tests/crypto_trading/connectors/__init__.py` skapas tomma (samma `tests.crypto_trading.*`-namngivningsprincip som Phase 0 låste — se Phase 0:s Task 1-rättning).

- [ ] **Step 2: Verifiera import**

Run: `python -c "from crypto_trading.connectors.exceptions import ConnectorError, ConnectorUnavailableError"`
Expected: inget fel.

- [ ] **Step 3: Commit**

```bash
git add crypto_trading/connectors/exceptions.py crypto_trading/connectors/__init__.py tests/crypto_trading/connectors/__init__.py
git commit -m "crypto_trading Phase 1 steg 3: connector-undantag"
```

---

## Task 4: `connectors/base.py` + `connectors/bingx_market_data.py`

**Files:**
- Create: `crypto_trading/connectors/base.py`
- Create: `crypto_trading/connectors/bingx_market_data.py`
- Test: `tests/crypto_trading/connectors/test_bingx_market_data.py`

**Interfaces:**
- Produces: `BaseMarketDataConnector` (delad infra), `BingXMarketDataConnector` med `get_contracts()`, `get_ticker(symbol)`, `get_klines(symbol, interval, limit)`, `get_funding_rate(symbol, limit)`, `get_open_interest(symbol)` — alla returnerar **rådata** (dict/list[dict]), ingen Pydantic-mappning här. Konsumeras av Task 5 (retry-tester), Task 7 (integrationstest).

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/connectors/test_bingx_market_data.py
import respx
from httpx import Response

from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector

_BASE_URL = "https://open-api.bingx.com"


def _connector(**overrides) -> BingXMarketDataConnector:
    defaults = dict(
        base_url=_BASE_URL,
        timeout_seconds=5,
        max_retries=3,
        requests_per_second=1000,  # ingen konstgjord väntan i dessa tester
        cache_ttl_seconds=0,
    )
    defaults.update(overrides)
    return BingXMarketDataConnector(**defaults)


@respx.mock
def test_get_ticker_returns_raw_dict_unmapped():
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": {"symbol": "BTC-USDT", "lastPrice": "77955.4"}})
    )
    result = _connector().get_ticker("BTC-USDT")
    assert result == {"symbol": "BTC-USDT", "lastPrice": "77955.4"}


@respx.mock
def test_get_klines_returns_raw_list():
    respx.get(f"{_BASE_URL}/openApi/swap/v3/quote/klines").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": [{"open": "1", "close": "2", "high": "3", "low": "0.5", "volume": "10", "time": 1}]})
    )
    result = _connector().get_klines("BTC-USDT", interval="1h", limit=1)
    assert isinstance(result, list)
    assert result[0]["close"] == "2"


@respx.mock
def test_get_contracts_returns_raw_list():
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/contracts").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": [{"symbol": "BTC-USDT", "status": 1}]})
    )
    result = _connector().get_contracts()
    assert result == [{"symbol": "BTC-USDT", "status": 1}]


@respx.mock
def test_get_funding_rate_returns_raw_list():
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/fundingRate").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": [{"symbol": "BTC-USDT", "fundingRate": "0.0001"}]})
    )
    result = _connector().get_funding_rate("BTC-USDT")
    assert result[0]["fundingRate"] == "0.0001"


@respx.mock
def test_get_open_interest_returns_raw_dict():
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/openInterest").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": {"symbol": "BTC-USDT", "openInterest": "123"}})
    )
    result = _connector().get_open_interest("BTC-USDT")
    assert result == {"symbol": "BTC-USDT", "openInterest": "123"}


def test_connector_only_calls_whitelisted_market_data_paths():
    """Positivt bevis (utöver Phase 0:s generella grep-test): connectorns
    egna endpoint-konstanter är EXAKT de fem verifierade market-data-
    endpointsen, inget mer - ingen account-/order-path kan smygas in utan
    att detta testet upptäcker det."""
    import crypto_trading.connectors.bingx_market_data as module

    paths = {
        module._CONTRACTS_PATH,
        module._TICKER_PATH,
        module._KLINES_PATH,
        module._FUNDING_RATE_PATH,
        module._OPEN_INTEREST_PATH,
    }
    assert paths == {
        "/openApi/swap/v2/quote/contracts",
        "/openApi/swap/v2/quote/ticker",
        "/openApi/swap/v3/quote/klines",
        "/openApi/swap/v2/quote/fundingRate",
        "/openApi/swap/v2/quote/openInterest",
    }
    for path in paths:
        assert "/account" not in path
        assert "/order" not in path
        assert "/trade" not in path
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/connectors/test_bingx_market_data.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/connectors/base.py
from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from crypto_trading.connectors.exceptions import ConnectorUnavailableError


class BaseMarketDataConnector:
    """Delad infrastruktur för market-data-connectors: timeout, retry,
    rate-limit, TTL-cache. En BingX-connector har flera distinkta endpoint-
    metoder istället för en enda fetch() - medveten avvikelse från Fas 1:s
    BaseConnector-form (som passar en connector med EN datatyp), se
    SPEC_CRYPTO.md §15 och Phase 1-designbeslutet i konversationshistoriken."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        max_retries: int,
        requests_per_second: float,
        cache_ttl_seconds: float,
    ):
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._min_interval_seconds = 1.0 / requests_per_second
        self._cache_ttl_seconds = cache_ttl_seconds
        self._last_call_at: float | None = None
        self._cache: dict[str, tuple[float, object]] = {}

    def _rate_limit(self) -> None:
        now = time.monotonic()
        if self._last_call_at is not None:
            elapsed = now - self._last_call_at
            wait = self._min_interval_seconds - elapsed
            if wait > 0:
                time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _cache_get(self, key: str) -> object | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > self._cache_ttl_seconds:
            del self._cache[key]
            return None
        return value

    def _cache_set(self, key: str, value: object) -> None:
        self._cache[key] = (time.monotonic(), value)

    def _get(self, path: str, params: dict) -> object:
        cache_key = f"{path}?{sorted(params.items())}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        try:
            data = self._get_with_retry(path, params)
        except httpx.HTTPError as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            detail = f"HTTP {status_code}" if status_code is not None else type(exc).__name__
            raise ConnectorUnavailableError(f"BingX otillgänglig: {path} ({detail})") from exc
        self._cache_set(cache_key, data)
        return data

    def _get_with_retry(self, path: str, params: dict) -> object:
        @retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=0.5, max=5),
            retry=retry_if_exception_type(httpx.HTTPError),
            reraise=True,
        )
        def _do() -> object:
            self._rate_limit()
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.get(f"{self._base_url}{path}", params=params)
                response.raise_for_status()
                body = response.json()
            if body.get("code") != 0:
                raise ConnectorUnavailableError(
                    f"BingX API-fel {body.get('code')}: {body.get('msg')} ({path})"
                )
            return body["data"]

        return _do()

    def _now(self) -> datetime:
        return datetime.now(UTC)
```

```python
# crypto_trading/connectors/bingx_market_data.py
from __future__ import annotations

import time

from crypto_trading.connectors.base import BaseMarketDataConnector

# Verifierade live 2026-08-25 mot https://open-api.bingx.com - se
# SPEC_CRYPTO.md §14 och konversationshistoriken för de faktiska svaren.
_CONTRACTS_PATH = "/openApi/swap/v2/quote/contracts"
_TICKER_PATH = "/openApi/swap/v2/quote/ticker"
_KLINES_PATH = "/openApi/swap/v3/quote/klines"
_FUNDING_RATE_PATH = "/openApi/swap/v2/quote/fundingRate"
_OPEN_INTEREST_PATH = "/openApi/swap/v2/quote/openInterest"


class BingXMarketDataConnector(BaseMarketDataConnector):
    """Uteslutande publika BingX swap (USDT-marginerade futures) market-
    data-endpoints. Ingen kod här refererar ett konto, en order eller en
    broker-credential (SPEC §1/§19)."""

    def get_contracts(self) -> list[dict]:
        return self._get(_CONTRACTS_PATH, {"timestamp": self._timestamp_ms()})

    def get_ticker(self, symbol: str) -> dict:
        return self._get(_TICKER_PATH, {"symbol": symbol, "timestamp": self._timestamp_ms()})

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list[dict]:
        return self._get(
            _KLINES_PATH,
            {
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "timestamp": self._timestamp_ms(),
            },
        )

    def get_funding_rate(self, symbol: str, limit: int = 1) -> list[dict]:
        return self._get(
            _FUNDING_RATE_PATH,
            {"symbol": symbol, "limit": limit, "timestamp": self._timestamp_ms()},
        )

    def get_open_interest(self, symbol: str) -> dict:
        return self._get(
            _OPEN_INTEREST_PATH, {"symbol": symbol, "timestamp": self._timestamp_ms()}
        )

    @staticmethod
    def _timestamp_ms() -> int:
        return int(time.time() * 1000)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/connectors/test_bingx_market_data.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/connectors/base.py crypto_trading/connectors/bingx_market_data.py tests/crypto_trading/connectors/test_bingx_market_data.py
git commit -m "crypto_trading Phase 1 steg 4: BaseMarketDataConnector + BingXMarketDataConnector (rådata-hämtning)"
```

---

## Task 5: Retry/timeout/rate-limit/cache-tester (AC2)

**Files:**
- Modify: `tests/crypto_trading/connectors/test_bingx_market_data.py`

**Interfaces:**
- Consumes: `BingXMarketDataConnector` (Task 4).

**Rättad under exekvering:** `test_cache_avoids_duplicate_http_call_within_ttl` avslöjade en verklig bugg i Task 4:s `base.py`: cache-nyckeln inkluderade `timestamp`-parametern, som ändras varje anrop (BingX-signeringsbrus) — cachen missade därför alltid, oavsett `cache_ttl_seconds`. Åtgärdat i `_get()`: cache-nyckeln beräknas nu från `params` med `timestamp` explicit exkluderad, eftersom det inte är del av förfrågans semantiska identitet. Se separat commit.

- [ ] **Step 1: Write the failing tests**

Lägg till i samma testfil (`import time`, `pytest`, `TimeoutException` läggs till i importsektionen):

```python
# tillägg i tests/crypto_trading/connectors/test_bingx_market_data.py
import time

import pytest
from httpx import TimeoutException

from crypto_trading.connectors.exceptions import ConnectorUnavailableError


@respx.mock
def test_transient_5xx_is_retried_then_succeeds():
    route = respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        side_effect=[
            Response(500),
            Response(200, json={"code": 0, "msg": "", "data": {"symbol": "BTC-USDT"}}),
        ]
    )
    result = _connector(max_retries=3).get_ticker("BTC-USDT")
    assert result == {"symbol": "BTC-USDT"}
    assert route.call_count == 2


@respx.mock
def test_persistent_failure_raises_connector_unavailable_not_a_crash():
    """BingX-nere-scenariot: pipelinen ska aldrig gissa eller krascha,
    bara signalera tydligt att data saknas (SPEC §8.2 - instrumentet blir
    DATA_INVALID nedströms i Phase 2, inte här)."""
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(return_value=Response(500))
    with pytest.raises(ConnectorUnavailableError):
        _connector(max_retries=2).get_ticker("BTC-USDT")


@respx.mock
def test_timeout_raises_connector_unavailable():
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        side_effect=TimeoutException("timed out")
    )
    with pytest.raises(ConnectorUnavailableError):
        _connector(max_retries=1).get_ticker("BTC-USDT")


@respx.mock
def test_api_level_error_code_raises_without_retry():
    route = respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 100001, "msg": "invalid symbol", "data": None})
    )
    with pytest.raises(ConnectorUnavailableError):
        _connector().get_ticker("NOTREAL-USDT")
    assert route.call_count == 1  # ett applikationsfel (fel symbol) retryas inte i onödan


@respx.mock
def test_rate_limiter_enforces_minimum_interval_between_calls():
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": {"symbol": "BTC-USDT"}})
    )
    connector = _connector(requests_per_second=5, cache_ttl_seconds=0)  # min_interval = 0.2s
    started = time.monotonic()
    connector.get_ticker("BTC-USDT")
    connector.get_ticker("ETH-USDT")
    elapsed = time.monotonic() - started
    assert elapsed >= 0.15, f"andra anropet verkar inte ha väntat (elapsed={elapsed:.3f}s)"


@respx.mock
def test_cache_avoids_duplicate_http_call_within_ttl():
    route = respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": {"symbol": "BTC-USDT"}})
    )
    connector = _connector(cache_ttl_seconds=10)
    connector.get_ticker("BTC-USDT")
    connector.get_ticker("BTC-USDT")
    assert route.call_count == 1  # andra anropet kom från cachen, inget nytt HTTP-anrop
```

- [ ] **Step 2: Run tests to verify they fail or pass honestly**

Run: `pytest tests/crypto_trading/connectors/test_bingx_market_data.py -v`
Expected: samtliga sex nya tester PASS direkt — implementationen från Task 4 (retry/rate-limit/cache i `base.py`) uppfyller redan kraven. Detta bekräftar AC2 med explicita tester, inget nytt produktionskod behövs (avsiktligt inte ett rött-grönt TDD-steg för just dessa sex, se rubriken "Run tests to verify they fail or pass honestly").

*(Om något FAILAR: `base.py`s retry/rate-limit/cache-logik från Task 4 har en bugg — åtgärda där, inte här.)*

- [ ] **Step 3: Run full connector test file to verify total count**

Run: `pytest tests/crypto_trading/connectors/test_bingx_market_data.py -v`
Expected: PASS (12 tester: 6 från Task 4 + 6 nya).

- [ ] **Step 4: Commit**

```bash
git add tests/crypto_trading/connectors/test_bingx_market_data.py
git commit -m "crypto_trading Phase 1 steg 5: retry/timeout/rate-limit/cache-tester (BingX-nere-scenario, AC2)"
```

---

## Task 6: `connectors/data_quality.py` (AC1, AC4)

**Files:**
- Create: `crypto_trading/connectors/data_quality.py`
- Test: `tests/crypto_trading/connectors/test_data_quality.py`

**Interfaces:**
- Produces: `DataQualityResult = Literal["ok", "invalid"]`, `check_completeness(raw, required_fields) -> DataQualityResult`, `check_staleness(observed_at, now, max_age_seconds) -> DataQualityResult`, `check_kline_consistency(klines, tolerance_pct) -> DataQualityResult`, `classify(*results) -> DataQualityResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/connectors/test_data_quality.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.connectors.data_quality import (
    check_completeness,
    check_kline_consistency,
    check_staleness,
    classify,
)
from crypto_trading.schemas.market import Kline


def _kline(close: str, high: str = "100", low: str = "1", volume: str = "10") -> Kline:
    return Kline(
        instrument="BTC-USDT", interval="1h",
        open=Decimal(close), high=Decimal(high), low=Decimal(low),
        close=Decimal(close), volume=Decimal(volume),
        observed_at=datetime.now(UTC),
    )


# --- completeness (§8.1 "ofullständig") ---

def test_completeness_ok_when_all_required_fields_present():
    raw = {"lastPrice": "1", "askPrice": "1", "bidPrice": "1"}
    assert check_completeness(raw, required_fields=["lastPrice", "askPrice", "bidPrice"]) == "ok"


def test_completeness_invalid_when_a_required_field_is_missing():
    raw = {"lastPrice": "1", "askPrice": "1"}
    assert check_completeness(raw, required_fields=["lastPrice", "askPrice", "bidPrice"]) == "invalid"


def test_completeness_invalid_when_required_field_is_none():
    raw = {"lastPrice": "1", "askPrice": None, "bidPrice": "1"}
    assert check_completeness(raw, required_fields=["lastPrice", "askPrice", "bidPrice"]) == "invalid"


def test_completeness_threshold_is_config_driven_not_hardcoded():
    """Ändra vilka fält som krävs via parametern (=config i praktiken) och
    bevisa att beteendet ändras i takt - inget hårdkodat i Python (AC4)."""
    raw = {"lastPrice": "1"}
    assert check_completeness(raw, required_fields=["lastPrice"]) == "ok"
    assert check_completeness(raw, required_fields=["lastPrice", "askPrice"]) == "invalid"


# --- staleness (§8.1 "stale") ---

def test_staleness_ok_within_max_age():
    observed_at = datetime.now(UTC) - timedelta(seconds=10)
    assert check_staleness(observed_at, datetime.now(UTC), max_age_seconds=30) == "ok"


def test_staleness_invalid_beyond_max_age():
    observed_at = datetime.now(UTC) - timedelta(seconds=61)
    assert check_staleness(observed_at, datetime.now(UTC), max_age_seconds=30) == "invalid"


def test_staleness_invalid_for_future_timestamp():
    observed_at = datetime.now(UTC) + timedelta(seconds=5)
    assert check_staleness(observed_at, datetime.now(UTC), max_age_seconds=30) == "invalid"


def test_staleness_threshold_is_config_driven_not_hardcoded():
    observed_at = datetime.now(UTC) - timedelta(seconds=45)
    now = datetime.now(UTC)
    assert check_staleness(observed_at, now, max_age_seconds=30) == "invalid"
    assert check_staleness(observed_at, now, max_age_seconds=3600) == "ok"


# --- consistency (§8.1 "inkonsekvent") ---

def test_kline_consistency_ok_for_sane_data():
    klines = [_kline("100"), _kline("101"), _kline("99")]
    assert check_kline_consistency(klines, tolerance_pct=Decimal("0.5")) == "ok"


def test_kline_consistency_invalid_when_high_below_low():
    klines = [_kline(close="50", high="10", low="20")]
    assert check_kline_consistency(klines, tolerance_pct=Decimal("0.5")) == "invalid"


def test_kline_consistency_invalid_for_negative_volume():
    klines = [_kline(close="50", volume="-1")]
    assert check_kline_consistency(klines, tolerance_pct=Decimal("0.5")) == "invalid"


def test_kline_consistency_invalid_for_outlier_beyond_tolerance():
    klines = [_kline("100"), _kline("101"), _kline("99"), _kline("100000")]
    assert check_kline_consistency(klines, tolerance_pct=Decimal("0.5")) == "invalid"


def test_kline_consistency_tolerance_is_config_driven_not_hardcoded():
    klines = [_kline("100"), _kline("100"), _kline("100"), _kline("140")]
    assert check_kline_consistency(klines, tolerance_pct=Decimal("0.2")) == "invalid"
    assert check_kline_consistency(klines, tolerance_pct=Decimal("0.9")) == "ok"


# --- classify (kombinerar + typnivå-garanti) ---

def test_classify_ok_when_all_ok():
    assert classify("ok", "ok", "ok") == "ok"


def test_classify_invalid_if_any_invalid():
    assert classify("ok", "invalid", "ok") == "invalid"


def test_classify_return_type_excludes_degraded():
    """Typnivå-garanti: Literal['ok','invalid'] gör 'degraded' omöjligt att
    returnera från Phase 1:s klassificering överhuvudtaget - all BingX-data
    är kritisk (SPEC §14), degraded blir bara möjligt i senare faser."""
    import typing

    from crypto_trading.connectors.data_quality import DataQualityResult

    assert typing.get_args(DataQualityResult) == ("ok", "invalid")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/connectors/test_data_quality.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crypto_trading/connectors/data_quality.py
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


def check_kline_consistency(
    klines: list[Kline], tolerance_pct: Decimal
) -> DataQualityResult:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/crypto_trading/connectors/test_data_quality.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add crypto_trading/connectors/data_quality.py tests/crypto_trading/connectors/test_data_quality.py
git commit -m "crypto_trading Phase 1 steg 6: data_quality.py — §8.1 stale/ofullständig/inkonsekvent, ok/invalid-typgaranti"
```

---

## Task 7: Integrationstest — hämta → mappa → klassificera

**Files:**
- Create: `tests/crypto_trading/connectors/test_market_data_integration.py`

**Interfaces:**
- Consumes: `BingXMarketDataConnector` (Task 4), `Ticker.from_raw`/`Kline.from_raw` (Task 2), `check_completeness`/`check_staleness`/`check_kline_consistency`/`classify` (Task 6). Inga nya produktionsfiler — bevisar att de tre lagren faktiskt fogas ihop korrekt.

- [ ] **Step 1: Write the failing test**

```python
# tests/crypto_trading/connectors/test_market_data_integration.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import respx
from httpx import Response

from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
from crypto_trading.connectors.data_quality import check_completeness, check_staleness, classify
from crypto_trading.schemas.market import Ticker

_BASE_URL = "https://open-api.bingx.com"
_REQUIRED_TICKER_FIELDS = ["lastPrice", "askPrice", "bidPrice", "quoteVolume", "closeTime"]


@respx.mock
def test_fresh_ticker_flows_through_fetch_map_classify_as_ok():
    fresh_close_time_ms = int(datetime.now(UTC).timestamp() * 1000)
    raw_ticker = {
        "symbol": "BTC-USDT", "lastPrice": "77955.4", "askPrice": "77993.5",
        "bidPrice": "77993.4", "quoteVolume": "1306179101.75",
        "priceChange": "0", "priceChangePercent": "0", "highPrice": "0", "lowPrice": "0",
        "volume": "0", "openPrice": "0", "openTime": fresh_close_time_ms,
        "closeTime": fresh_close_time_ms, "askQty": "1", "bidQty": "1",
    }
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": raw_ticker})
    )
    connector = BingXMarketDataConnector(
        base_url=_BASE_URL, timeout_seconds=5, max_retries=1,
        requests_per_second=1000, cache_ttl_seconds=0,
    )

    raw = connector.get_ticker("BTC-USDT")
    completeness = check_completeness(raw, required_fields=_REQUIRED_TICKER_FIELDS)
    ticker = Ticker.from_raw(raw)
    staleness = check_staleness(ticker.observed_at, datetime.now(UTC), max_age_seconds=30)
    overall = classify(completeness, staleness)

    assert isinstance(ticker.last_price, Decimal)
    assert overall == "ok"


@respx.mock
def test_stale_ticker_flows_through_as_invalid_never_silently_ok():
    stale_close_time_ms = int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000)
    raw_ticker = {
        "symbol": "BTC-USDT", "lastPrice": "77955.4", "askPrice": "77993.5",
        "bidPrice": "77993.4", "quoteVolume": "1306179101.75",
        "priceChange": "0", "priceChangePercent": "0", "highPrice": "0", "lowPrice": "0",
        "volume": "0", "openPrice": "0", "openTime": stale_close_time_ms,
        "closeTime": stale_close_time_ms, "askQty": "1", "bidQty": "1",
    }
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": raw_ticker})
    )
    connector = BingXMarketDataConnector(
        base_url=_BASE_URL, timeout_seconds=5, max_retries=1,
        requests_per_second=1000, cache_ttl_seconds=0,
    )

    raw = connector.get_ticker("BTC-USDT")
    completeness = check_completeness(raw, required_fields=_REQUIRED_TICKER_FIELDS)
    ticker = Ticker.from_raw(raw)
    staleness = check_staleness(ticker.observed_at, datetime.now(UTC), max_age_seconds=30)
    overall = classify(completeness, staleness)

    assert overall == "invalid"  # 1 timme gammal ticker, max_age_seconds=30 - aldrig "ok"


@respx.mock
def test_incomplete_ticker_is_invalid_before_even_reaching_pydantic():
    raw_ticker = {"symbol": "BTC-USDT", "lastPrice": "77955.4"}  # saknar askPrice/bidPrice/etc
    respx.get(f"{_BASE_URL}/openApi/swap/v2/quote/ticker").mock(
        return_value=Response(200, json={"code": 0, "msg": "", "data": raw_ticker})
    )
    connector = BingXMarketDataConnector(
        base_url=_BASE_URL, timeout_seconds=5, max_retries=1,
        requests_per_second=1000, cache_ttl_seconds=0,
    )

    raw = connector.get_ticker("BTC-USDT")
    completeness = check_completeness(raw, required_fields=_REQUIRED_TICKER_FIELDS)

    assert completeness == "invalid"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/connectors/test_market_data_integration.py -v`
Expected: Om Task 2/4/6 redan är korrekt implementerade PASSAR detta direkt (inget nytt produktionskod). Om något FAILAR avslöjar det en integrationsbugg mellan lagren — åtgärda i det berörda lagret (schema-mappning, connector, eller data-quality), inte här.

- [ ] **Step 3: Run test to verify it passes**

Run: `pytest tests/crypto_trading/connectors/test_market_data_integration.py -v`
Expected: PASS (3 tests)

- [ ] **Step 4: Commit**

```bash
git add tests/crypto_trading/connectors/test_market_data_integration.py
git commit -m "crypto_trading Phase 1 steg 7: integrationstest — hämta→mappa→klassificera-kedjan"
```

---

## Task 8: Manuell engångsverifiering mot riktig BingX-data (AC5)

**Files:**
- Create: `tests/crypto_trading/connectors/test_bingx_live.py`

**Interfaces:**
- Consumes: `BingXMarketDataConnector`, `InstrumentMetadata`/`Ticker`/`Kline`/`FundingRate`/`OpenInterest`. Märkt `@pytest.mark.live` — exkluderad från default `pytest`, kräver riktig nätverksåtkomst.

- [ ] **Step 1: Write the live test**

```python
# tests/crypto_trading/connectors/test_bingx_live.py
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
```

Registrera `live`-markören i `pyproject.toml` under `[tool.pytest.ini_options]` (undviker en `PytestUnknownMarkWarning`):

```toml
markers = [
    "live: kräver riktig nätverksåtkomst, exkluderad från default pytest-körning",
]
```

- [ ] **Step 2: Verifiera att default pytest INTE kör detta test**

Run: `pytest tests/crypto_trading/ -v`
Expected: `test_all_five_endpoints_work_against_real_bingx_api` syns INTE i outputen (varken PASS, FAIL eller SKIP) — default-körningen filtrerar bort `live`-markerade tester helt, kräver ingen nätverksåtkomst.

*(Om testet visas som `deselected` i en summary-rad är det också korrekt — poängen är att det inte försöker köras.)*

- [ ] **Step 3: Kör testet manuellt mot riktig BingX-data**

Run: `pytest tests/crypto_trading/connectors/test_bingx_live.py -v -m live`
Expected: PASS mot riktig, aktuell BingX-data. Dokumentera det faktiska resultatet (pris, antal kontrakt etc.) i commit-meddelandet som bevis — samma mönster som Fas 1:s slutliga körning mot riktig Hacker News-data.

- [ ] **Step 4: Commit**

```bash
git add tests/crypto_trading/connectors/test_bingx_live.py pyproject.toml
git commit -m "crypto_trading Phase 1 steg 8: manuell live-verifiering mot riktig BingX-data (AC5)"
```

---

## Task 9: Slutverifiering

**Files:** inga nya — verifierar hela Phase 1.

- [ ] **Step 1: Full testsvit för crypto_trading (utan live-tester)**

Run: `pytest tests/crypto_trading/ -v`
Expected: alla tester gröna.

- [ ] **Step 2: Ruff check + format**

Run: `ruff check crypto_trading/ tests/crypto_trading/`
Expected: inga fel.

Run: `ruff format --check crypto_trading/ tests/crypto_trading/`
Expected: inga diff.

- [ ] **Step 3: Verifiera att intelligence/ fortfarande är orört**

Run: `git diff master -- intelligence/`
Expected: tom output.

- [ ] **Step 4: Full repo-testsvit (bekräfta att inget i Phase 0/intelligence gick sönder)**

Run: `pytest -v`
Expected: alla tester (crypto_trading Phase 0 + Phase 1, intelligence, test_setup) gröna, ingen regression.

- [ ] **Step 5: Verifiera importgräns och broker-frihet fortfarande håller**

Run: `pytest tests/crypto_trading/test_no_intelligence_coupling.py -v`
Expected: PASS — testet globar hela `crypto_trading/` och fångar därmed automatiskt Phase 1:s nya filer utan att ha ändrats.

- [ ] **Step 6: Uppdatera PLAN_CRYPTO_PHASE1.md**

Kryssa i samtliga `- [ ]` i denna fil till `- [x]` och lägg till en statusbanner högst upp med exakt testantal och ev. avvikelser upptäckta under exekvering.

---

## Self-review (utfört innan planen sparas)

**Spec-täckning:** officiell endpoint-verifiering (Global Constraints + kodkommentarer i Task 4, gjord live under brainstorming), `connectors/bingx_market_data.py` med uteslutande market-data-endpoints (Task 4 + dedikerad whitelist-test), `connectors/base.py` timeout/retry/rate-limit/TTL-cache/loggning (Task 4), data-quality-lager med §8.1:s tre kategorier och config-drivna trösklar (Task 6), fullständigt respx-mockat testlager (alla tasks utom Task 8), AC1 (Task 6/7), AC2 (Task 5), AC3 (Task 4:s whitelist-test + Phase 0:s befintliga grep-test), AC4 (Task 1:s och Task 6:s "config-driven, inte hårdkodat"-tester), AC5 (Task 8, `@pytest.mark.live`). Ingen kvarstående lucka.

**Placeholder-scan:** inga TBD/TODO. Alla exempel-payloads i Task 2/4/7 är riktiga, verifierade BingX-svar — inga antagna fält.

**Typkonsekvens:** `DataQualityResult = Literal["ok","invalid"]` (Task 6) används konsekvent i `check_completeness`/`check_staleness`/`check_kline_consistency`/`classify`. `BingXMarketDataConnector`s fem metodnamn (`get_contracts`/`get_ticker`/`get_klines`/`get_funding_rate`/`get_open_interest`) matchar exakt mellan Task 4:s implementation och alla konsumerande tester i Task 5/7/8. `from_raw`-signaturerna i `schemas/market.py` (Task 2) matchar exakt hur de anropas i Task 7/8.

**Scope-kontroll:** ingen skrivning till SQLite, ingen `screening/`-logik (eligibility filter, quant screener), ingen `Candidate`-konstruktion — allt det är Phase 2. `intelligence/` refereras inte någonstans.

---

**Plan complete and saved to `PLAN_CRYPTO_PHASE1.md`** (repo-rot, matchar `PLAN_CRYPTO_PHASE0.md`s konvention).
