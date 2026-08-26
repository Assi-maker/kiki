# Crypto Trading — Phase 2 (Universe + Quant Screening) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Status: EJ PÅBÖRJAD (skriven 2026-08-26)

Fas 1 är avslutad och mergad till `master` (commit `647bd12`). Denna plan väntar på användarens granskning och godkännande innan någon kod skrivs. Ingen exekvering har startat.

---

**Goal:** Bygga det helt deterministiska, AI-fria urvals- och screeninglagret: `eligibility_filter.py` (likviditet/spread/status/datakvalitet → dynamiskt Top N), `quant_screener.py` (fyra signaltyper → `CandidateEvidenceRecord`, reproducerbart) och `candidate_engine.py` (idempotent `Candidate`-skapande, dedup/cooldown, budget-baserad prioritering). Ingen riktning (BUY/SELL/LONG/SHORT) och inga AI-anrop förekommer någonstans i denna fas.

**Architecture:** `screening/` beror på Phase 0:s scheman (`schemas/evidence.py`, `schemas/candidate.py`, `schemas/common.py`), Phase 0:s `storage/repository.py` + `state_machine.py`, och Phase 1:s `schemas/market.py` + `connectors/data_quality.py` — men importerar aldrig `connectors/bingx_market_data.py` eller `connectors/base.py` direkt (screening-lagret konsumerar redan hämtad, redan typad marknadsdata; HTTP-hämtning är uteslutande Phase 1:s ansvar, samma lager-separation som `intelligence/pipeline/normalize.py` ⊥ `intelligence/connectors/`). Beroenderiktning enkelriktad, oförändrad: `schemas` beroendefritt, allt annat beror på `schemas`.

**Tech Stack:** Python 3.13, `pydantic` v2, `pytest`. Inga nya beroenden — `screening/` gör inga HTTP-anrop och behöver varken `httpx`, `respx` eller `tenacity`.

**Spec:** `SPEC_CRYPTO.md` §4 (`CandidateEvidenceRecord`), §5 (`CandidateStatus`), §7 (flöden, dedup/cooldown), §8.1/§8.2 (data quality, kritisk data), §8.4 (look-ahead-bias), §10 (kostnadskontroll). `PLAN_CRYPTO.md` Phase 2-avsnittet (Omfattning/Levererar/Acceptance criteria 1–5, citerade i respektive tasks nedan).

## Global Constraints

- **Ingen riktning, någonsin.** Inget fält i `CandidateEvidenceRecord` eller `Candidate` får heta eller innehålla BUY/SELL/LONG/SHORT — strukturellt omöjligt (schemat har inget sådant fält), verifierat explicit av ett dedikerat test (AC1, Task 5).
- **Inga AI-anrop.** `screening/` importerar aldrig `agents/`, gör inga LLM-anrop, sätter aldrig status `UNDER_AI_ANALYSIS`. Candidate Engine ordnar prioritet inom budget men lämnar budget-godkända candidates i status `CANDIDATE` — övergången till `UNDER_AI_ANALYSIS` är Phase 3:s jobb, när den faktiskt startar en AI-analys.
- **Determinism.** Samma indata (klines/funding rates/ticker) given till `quant_screener.evaluate_candidate()` två gånger ska ge bit-för-bit identisk `CandidateEvidenceRecord` (AC2, Task 5). Ingen `datetime.now()`/slumptal inuti screeningfunktionerna — `evaluated_at`/`now` skickas alltid in som parameter av anroparen.
- **Look-ahead-bias-guard (SPEC §8.4).** `evaluate_candidate()` ignorerar deterministiskt varje kline/funding-rate-post vars `observed_at` ligger efter det inskickade `evaluated_at` — testas explicit (Task 4).
- **Återanvändning, inte omskrivning.** `compute_evidence_hash`/`compute_candidate_idempotency_key` (Phase 0, `schemas/evidence.py`), `Repository`/`SQLiteRepository` (Phase 0, `storage/repository.py`), `can_transition` (Phase 0, `state_machine.py`), `check_completeness`/`check_staleness`/`check_kline_consistency`/`classify` (Phase 1, `connectors/data_quality.py`) återanvänds direkt — ingen dubblettlogik skrivs.
- **`NOT_A_CANDIDATE` persisteras aldrig.** Om screenern inte triggar något av de fyra signaltyperna skapas ingen `Candidate`-rad — bara en debug-loggrad (SPEC §5). En `Candidate`-rad skapas bara när (a) datakvaliteten är `invalid` (→ direkt `DATA_INVALID`, terminal) eller (b) minst en signal triggade (→ `CANDIDATE`, sedan budget-gate).
- Config-drivna trösklar (`pipeline.yaml`), aldrig hårdkodade i Python — samma princip som Phase 1.
- `intelligence/` rörs inte. `ruff` line-length 100, regler `E,F,I,UP,B`.

---

## Task 1: Utöka config för Phase 2 (`PipelineConfig` + `pipeline.yaml`)

**Files:**
- Modify: `crypto_trading/config/loader.py`
- Modify: `crypto_trading/config/pipeline.yaml`
- Modify: `tests/crypto_trading/config/test_loader.py`

**Interfaces:**
- Produces: `PipelineConfig` får tio nya fält (eligibility- och screener-trösklar + cooldown-override-tröskel), alla `Decimal` där de representerar penning-/procentvärden (samma "aldrig via float"-princip som `risk_limits.yaml`).

- [ ] **Step 1: Write the failing tests**

Lägg till i `tests/crypto_trading/config/test_loader.py`:

```python
def test_get_settings_loads_phase2_fields():
    settings = get_settings()
    assert settings.pipeline.eligibility_min_quote_volume_24h_usdt > 0
    assert 0 < settings.pipeline.eligibility_max_spread_pct <= 1
    assert settings.pipeline.screener_lookback_periods > 1
    assert settings.pipeline.screener_price_volatility_threshold_pct > 0
    assert settings.pipeline.screener_rsi_period > 1
    assert 0 < settings.pipeline.screener_rsi_overbought_threshold <= 100
    assert settings.pipeline.screener_volume_zscore_threshold > 0
    assert settings.pipeline.screener_funding_rate_threshold_pct > 0
    assert settings.pipeline.screener_funding_history_limit > 1
    assert settings.pipeline.evidence_change_threshold_for_reanalysis >= 0


def test_pipeline_config_rejects_negative_eligibility_min_volume():
    kwargs = _valid_phase2_pipeline_kwargs()
    kwargs["eligibility_min_quote_volume_24h_usdt"] = Decimal("-1")
    with pytest.raises(ValidationError):
        PipelineConfig(**kwargs)


def test_pipeline_config_rejects_spread_pct_above_one():
    kwargs = _valid_phase2_pipeline_kwargs()
    kwargs["eligibility_max_spread_pct"] = Decimal("1.5")
    with pytest.raises(ValidationError):
        PipelineConfig(**kwargs)
```

Lägg till en delad fixture-helper högst upp i testfilen (används av ovanstående och ersätter behovet av att duplicera hela kwargs-dicten per test):

```python
def _valid_phase2_pipeline_kwargs() -> dict:
    return dict(
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
        eligibility_min_quote_volume_24h_usdt=Decimal("5000000"),
        eligibility_max_spread_pct=Decimal("0.002"),
        screener_lookback_periods=20,
        screener_price_volatility_threshold_pct=Decimal("2.0"),
        screener_rsi_period=14,
        screener_rsi_overbought_threshold=Decimal("70"),
        screener_volume_zscore_threshold=Decimal("2.5"),
        screener_funding_rate_threshold_pct=Decimal("0.05"),
        screener_funding_history_limit=10,
        evidence_change_threshold_for_reanalysis=Decimal("0.15"),
    )
```

(Befintliga Phase 1-negativtester som konstruerar `PipelineConfig` direkt med en fullständig kwargs-dict måste också uppdateras att inkludera de nya obligatoriska fälten — annars blir de `TypeError` istället för det avsedda `ValidationError`. Uppdatera `test_pipeline_config_rejects_missing_max_data_age_seconds_key` och `test_pipeline_config_rejects_missing_required_fields_key` att använda `_valid_phase2_pipeline_kwargs()` som bas med respektive fält borttaget/trunkerat, istället för de hårdkodade dictarna.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/config/test_loader.py -v`
Expected: de tre nya testerna FAIL (`AttributeError`/`TypeError` — fälten finns inte än).

- [ ] **Step 3: Modify `pipeline.yaml`**

Lägg till efter `kline_consistency_tolerance_pct`:

```yaml
eligibility_min_quote_volume_24h_usdt: "5000000"
eligibility_max_spread_pct: "0.002"
screener_lookback_periods: 20
screener_price_volatility_threshold_pct: "2.0"
screener_rsi_period: 14
screener_rsi_overbought_threshold: "70"
screener_volume_zscore_threshold: "2.5"
screener_funding_rate_threshold_pct: "0.05"
screener_funding_history_limit: 10
evidence_change_threshold_for_reanalysis: "0.15"
```

(Trösklarna är strategi-parametrar, inte BingX-verifierade fakta som Phase 1:s `required_fields` — de är avsiktligt konservativa startvärden, fritt omställbara i config utan kodändring.)

- [ ] **Step 4: Modify `loader.py`**

Lägg till i `PipelineConfig` (efter `kline_consistency_tolerance_pct`):

```python
    eligibility_min_quote_volume_24h_usdt: Decimal = Field(gt=0)
    eligibility_max_spread_pct: Decimal = Field(gt=0, le=1)
    screener_lookback_periods: int = Field(gt=1)
    screener_price_volatility_threshold_pct: Decimal = Field(gt=0)
    screener_rsi_period: int = Field(gt=1)
    screener_rsi_overbought_threshold: Decimal = Field(gt=0, le=100)
    screener_volume_zscore_threshold: Decimal = Field(gt=0)
    screener_funding_rate_threshold_pct: Decimal = Field(gt=0)
    screener_funding_history_limit: int = Field(gt=1)
    evidence_change_threshold_for_reanalysis: Decimal = Field(ge=0)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/config/test_loader.py -v`
Expected: alla tester PASS, inklusive de tre nya och de två uppdaterade negativtesterna.

---

## Task 2: `storage/repository.py` — dedup/cooldown-uppslag

**Files:**
- Modify: `crypto_trading/storage/db.py`
- Modify: `crypto_trading/storage/repository.py`
- Modify: `tests/crypto_trading/storage/test_repository_candidate.py`

**Interfaces:**
- Produces: `Repository.find_latest_candidate_by_instrument_and_status(instrument: str, status: str) -> Candidate | None`. Ett index `idx_candidates_instrument_status` på `candidates(instrument, status, created_at)`.

- [ ] **Step 1: Write the failing tests**

Lägg till i `tests/crypto_trading/storage/test_repository_candidate.py`:

```python
def test_find_latest_candidate_by_instrument_and_status_returns_none_when_no_match(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    assert repo.find_latest_candidate_by_instrument_and_status("BTCUSDT", "REJECTED") is None


def test_find_latest_candidate_by_instrument_and_status_returns_most_recent(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    older = _make_candidate(candidate_id="cand-old", idempotency_key="key-old", status="REJECTED")
    newer = _make_candidate(candidate_id="cand-new", idempotency_key="key-new", status="REJECTED")
    # tvinga en entydig created_at-ordning (annars kan två snabba anrop dela samma mikrosekund)
    older = older.model_copy(update={"created_at": datetime(2026, 8, 20, tzinfo=UTC)})
    newer = newer.model_copy(update={"created_at": datetime(2026, 8, 21, tzinfo=UTC)})
    repo.create_candidate_with_event(older, _make_event(older, "CANDIDATE_CREATED"))
    repo.create_candidate_with_event(newer, _make_event(newer, "CANDIDATE_CREATED"))

    result = repo.find_latest_candidate_by_instrument_and_status("BTCUSDT", "REJECTED")

    assert result is not None
    assert result.candidate_id == "cand-new"


def test_find_latest_candidate_by_instrument_and_status_ignores_other_status(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate(status="CANDIDATE")
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))

    assert repo.find_latest_candidate_by_instrument_and_status("BTCUSDT", "REJECTED") is None


def test_find_latest_candidate_by_instrument_and_status_propagates_corrupt_state_error(tmp_path):
    repo = SQLiteRepository(tmp_path / "test.db")
    candidate = _make_candidate(status="REJECTED")
    repo.create_candidate_with_event(candidate, _make_event(candidate, "CANDIDATE_CREATED"))
    repo._conn.execute(
        "UPDATE candidates SET evidence_record = 'not valid json' WHERE candidate_id = 'cand-1'"
    )
    repo._conn.commit()

    with pytest.raises(CorruptCandidateStateError):
        repo.find_latest_candidate_by_instrument_and_status("BTCUSDT", "REJECTED")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/storage/test_repository_candidate.py -v`
Expected: de fyra nya testerna FAIL med `AttributeError` (metoden finns inte än).

- [ ] **Step 3: Modify `storage/db.py`**

Lägg till index i `_SCHEMA`, direkt efter `candidates`-tabellens definition:

```sql
CREATE INDEX IF NOT EXISTS idx_candidates_instrument_status
    ON candidates(instrument, status, created_at);
```

- [ ] **Step 4: Modify `storage/repository.py`**

Lägg till i `Repository`-protokollet:

```python
    def find_latest_candidate_by_instrument_and_status(
        self, instrument: str, status: str
    ) -> Candidate | None: ...
```

Lägg till i `SQLiteRepository` (efter `find_candidates_by_status`):

```python
    def find_latest_candidate_by_instrument_and_status(
        self, instrument: str, status: str
    ) -> Candidate | None:
        """Till skillnad från `find_candidates_by_status()` sväljer denna
        metod INTE ett `CorruptCandidateStateError` - den returnerar en
        specifik, namngiven rad, och om just den raden är korrupt är det
        direkt relevant för anroparen (dedup/cooldown-beslutet i Task 6 får
        då fail-closed genom att låta felet propagera, inte tyst falla
        tillbaka till "ingen cooldown finns")."""
        row = self._conn.execute(
            "SELECT candidate_id FROM candidates WHERE instrument = ? AND status = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (instrument, status),
        ).fetchone()
        if row is None:
            return None
        return self.get_candidate(row["candidate_id"])
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/storage/ -v`
Expected: alla tester PASS, inklusive de fyra nya och samtliga befintliga (ingen regression av index-tillägget).

---

## Task 3: `screening/eligibility_filter.py` (del av AC4)

**Files:**
- Create: `crypto_trading/screening/__init__.py`
- Create: `crypto_trading/screening/eligibility_filter.py`
- Create: `tests/crypto_trading/screening/__init__.py`
- Create: `tests/crypto_trading/screening/test_eligibility_filter.py`

**Interfaces:**
- Produces: `check_eligibility(instrument: InstrumentMetadata, ticker: Ticker, data_quality_status: Literal["ok","invalid"], min_quote_volume_24h_usdt: Decimal, max_spread_pct: Decimal) -> tuple[bool, str]`, `compute_spread_pct(ticker: Ticker) -> Decimal`, `select_top_n(eligible: list[Ticker], n: int) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

`tests/crypto_trading/screening/test_eligibility_filter.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.schemas.market import InstrumentMetadata, Ticker
from crypto_trading.screening.eligibility_filter import (
    check_eligibility,
    compute_spread_pct,
    select_top_n,
)


def _instrument(status: int = 1) -> InstrumentMetadata:
    return InstrumentMetadata(
        symbol="BTCUSDT", status=status, price_precision=2, quantity_precision=3,
        trade_min_usdt=Decimal("2"), fetched_at=datetime.now(UTC),
    )


def _ticker(quote_volume="10000000", ask="50010", bid="49990") -> Ticker:
    return Ticker(
        instrument="BTCUSDT", last_price=Decimal("50000"), price_change=Decimal("0"),
        price_change_percent=Decimal("0"), high_price=Decimal("50100"),
        low_price=Decimal("49900"), volume=Decimal("200"), quote_volume=Decimal(quote_volume),
        open_price=Decimal("50000"), ask_price=Decimal(ask), ask_qty=Decimal("1"),
        bid_price=Decimal(bid), bid_qty=Decimal("1"), observed_at=datetime.now(UTC),
    )


def test_compute_spread_pct_computes_relative_to_mid():
    ticker = _ticker(ask="101", bid="99")  # mid=100, spread=2 -> 0.02
    assert compute_spread_pct(ticker) == Decimal("0.02")


def test_compute_spread_pct_fails_closed_on_non_positive_mid():
    ticker = _ticker(ask="0", bid="0")
    assert compute_spread_pct(ticker) == Decimal("1")


def test_check_eligibility_passes_when_all_criteria_met():
    ok, reason = check_eligibility(
        _instrument(), _ticker(), "ok",
        min_quote_volume_24h_usdt=Decimal("5000000"), max_spread_pct=Decimal("0.002"),
    )
    assert ok is True
    assert reason == "eligible"


def test_check_eligibility_rejects_non_trading_status():
    ok, reason = check_eligibility(
        _instrument(status=0), _ticker(), "ok",
        min_quote_volume_24h_usdt=Decimal("5000000"), max_spread_pct=Decimal("0.002"),
    )
    assert ok is False
    assert reason == "not_trading"


def test_check_eligibility_rejects_invalid_data_quality():
    ok, reason = check_eligibility(
        _instrument(), _ticker(), "invalid",
        min_quote_volume_24h_usdt=Decimal("5000000"), max_spread_pct=Decimal("0.002"),
    )
    assert ok is False
    assert reason == "data_quality_invalid"


def test_check_eligibility_rejects_insufficient_liquidity():
    ok, reason = check_eligibility(
        _instrument(), _ticker(quote_volume="100"), "ok",
        min_quote_volume_24h_usdt=Decimal("5000000"), max_spread_pct=Decimal("0.002"),
    )
    assert ok is False
    assert reason == "insufficient_liquidity"


def test_check_eligibility_rejects_wide_spread():
    ok, reason = check_eligibility(
        _instrument(), _ticker(ask="51000", bid="49000"), "ok",
        min_quote_volume_24h_usdt=Decimal("5000000"), max_spread_pct=Decimal("0.002"),
    )
    assert ok is False
    assert reason == "spread_too_wide"


def test_select_top_n_ranks_by_quote_volume_descending():
    low = _ticker(quote_volume="1000000")
    high = _ticker(quote_volume="9000000")
    high = high.model_copy(update={"instrument": "ETHUSDT"})
    result = select_top_n([low, high], n=2)
    assert result == ["ETHUSDT", "BTCUSDT"]


def test_select_top_n_truncates_to_n():
    tickers = [
        _ticker(quote_volume=str(1_000_000 * i)).model_copy(update={"instrument": f"SYM{i}USDT"})
        for i in range(5)
    ]
    result = select_top_n(tickers, n=2)
    assert len(result) == 2
    assert result[0] == "SYM4USDT"  # högst quote_volume


def test_select_top_n_membership_alone_never_touches_storage():
    """Strukturell garanti för AC4: eligibility_filter.py importerar aldrig
    storage/ - Top N-medlemskap kan därför per konstruktion aldrig skapa
    en Candidate-rad."""
    import ast
    from pathlib import Path

    source = Path("crypto_trading/screening/eligibility_filter.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any(m.startswith("crypto_trading.storage") for m in imported_modules)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/screening/test_eligibility_filter.py -v`
Expected: FAIL med `ModuleNotFoundError` (paketet finns inte än).

- [ ] **Step 3: Implement**

`crypto_trading/screening/__init__.py`: tom fil.

`crypto_trading/screening/eligibility_filter.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/screening/test_eligibility_filter.py -v`
Expected: alla tester PASS.

---

## Task 4: `screening/quant_screener.py` — Del A: price-volatility + momentum/breakout

**Files:**
- Create: `crypto_trading/screening/quant_screener.py`
- Create: `tests/crypto_trading/screening/test_quant_screener.py`

**Interfaces:**
- Produces: `build_price_volatility_evidence(klines, threshold_pct, lookback, evaluated_at) -> PriceVolatilityEvidence`, `build_momentum_breakout_evidence(klines, rsi_period, overbought_threshold, evaluated_at) -> MomentumBreakoutEvidence`.

- [ ] **Step 1: Write the failing tests**

`tests/crypto_trading/screening/test_quant_screener.py` (grund som utökas i Task 5):

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.schemas.market import Kline
from crypto_trading.screening.quant_screener import (
    build_momentum_breakout_evidence,
    build_price_volatility_evidence,
)

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _kline(close: str, offset_hours: int, high=None, low=None, volume="100") -> Kline:
    close_dec = Decimal(close)
    return Kline(
        instrument="BTCUSDT", interval="1h",
        open=close_dec, high=Decimal(high) if high else close_dec,
        low=Decimal(low) if low else close_dec, close=close_dec,
        volume=Decimal(volume), observed_at=_NOW - timedelta(hours=offset_hours),
    )


def _flat_klines(n: int, price: str = "100") -> list[Kline]:
    return [_kline(price, offset_hours=n - i) for i in range(n)]


def test_price_volatility_triggers_when_change_exceeds_threshold():
    klines = _flat_klines(21, price="100")
    klines.append(_kline("110", offset_hours=0))  # +10% senaste steget
    evidence = build_price_volatility_evidence(
        klines, threshold_pct=Decimal("2.0"), lookback=20, evaluated_at=_NOW
    )
    assert evidence.triggered is True
    assert evidence.metric == "pct_change"
    assert evidence.value == pytest.approx(10.0)
    assert evidence.threshold == 2.0


def test_price_volatility_does_not_trigger_on_flat_prices():
    klines = _flat_klines(22, price="100")
    evidence = build_price_volatility_evidence(
        klines, threshold_pct=Decimal("2.0"), lookback=20, evaluated_at=_NOW
    )
    assert evidence.triggered is False
    assert evidence.value == 0.0


def test_price_volatility_ignores_klines_after_evaluated_at():
    """SPEC §8.4: ingen framtida data får läcka in i beslutet."""
    klines = _flat_klines(21, price="100")
    future_kline = Kline(
        instrument="BTCUSDT", interval="1h", open=Decimal("500"), high=Decimal("500"),
        low=Decimal("500"), close=Decimal("500"), volume=Decimal("1"),
        observed_at=_NOW + timedelta(hours=1),
    )
    with_future = [*klines, future_kline]
    without_future = klines

    result_with = build_price_volatility_evidence(
        with_future, threshold_pct=Decimal("2.0"), lookback=20, evaluated_at=_NOW
    )
    result_without = build_price_volatility_evidence(
        without_future, threshold_pct=Decimal("2.0"), lookback=20, evaluated_at=_NOW
    )
    assert result_with == result_without


def test_momentum_breakout_triggers_on_high_rsi():
    # monotont stigande closes -> RSI mot 100 (inga losses i fönstret)
    klines = [_kline(str(100 + i), offset_hours=15 - i) for i in range(15)]
    evidence = build_momentum_breakout_evidence(
        klines, rsi_period=14, overbought_threshold=Decimal("70"), evaluated_at=_NOW
    )
    assert evidence.triggered is True
    assert evidence.metric == "rsi"
    assert evidence.value > 70.0
    assert evidence.baseline == 50.0


def test_momentum_breakout_does_not_trigger_on_flat_prices():
    klines = _flat_klines(15, price="100")
    evidence = build_momentum_breakout_evidence(
        klines, rsi_period=14, overbought_threshold=Decimal("70"), evaluated_at=_NOW
    )
    assert evidence.triggered is False
```

(`import pytest` läggs till högst upp för `pytest.approx`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/screening/test_quant_screener.py -v`
Expected: FAIL med `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`crypto_trading/screening/quant_screener.py` (grund + Del A):

```python
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from crypto_trading.schemas.evidence import MomentumBreakoutEvidence, PriceVolatilityEvidence
from crypto_trading.schemas.market import Kline


def _sorted_up_to(klines: list[Kline], evaluated_at: datetime) -> list[Kline]:
    """SPEC §8.4: filtrerar bort varje datapunkt daterad efter evaluated_at,
    sorterar sedan kronologiskt. Central, delad guard - alla evidence-
    byggare i denna modul går via denna funktion, aldrig direkt på rå
    inputlista."""
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
    baseline = sum(historical_changes) / len(historical_changes) if historical_changes else Decimal("0")

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/screening/test_quant_screener.py -v`
Expected: alla tester PASS.

---

## Task 5: `screening/quant_screener.py` — Del B: volume + funding/OI, kombination (AC1, AC2)

**Files:**
- Modify: `crypto_trading/screening/quant_screener.py`
- Modify: `tests/crypto_trading/screening/test_quant_screener.py`

**Interfaces:**
- Produces: `build_volume_evidence(...)`, `build_funding_oi_evidence(...)`, `evaluate_candidate(...) -> CandidateEvidenceRecord`.

**Verifierad upptäckt (dokumenterad, ej blockerande):** `BingXMarketDataConnector.get_open_interest()` (Phase 1) returnerar bara en aktuell engångssnapshot — ingen OI-historik-endpoint implementerades i Phase 1 (SPEC §14 nämner orderbok/spread och OI som tillgängliga, men ingen OI-tidsserie). `funding_oi_evidence` baseras därför i Phase 2 uteslutande på funding-rate-historik (redan stödd av `get_funding_rate(symbol, limit=N)`), vilket är tillräckligt för ett deterministiskt, reproducerbart signalmått. Aktuell `open_interest` är fortsatt en del av den kritiska data som §8.1/`required_fields` kräver för `data_quality_status`, men bidrar inte till detta signalmåttets numeriska baseline i Phase 2 — en enkel, SPEC-förenlig avgränsning, inte en spec-avvikelse (SPEC kräver bara att `funding_oi_evidence`-typen finns och är deterministisk).

- [ ] **Step 1: Write the failing tests**

Lägg till i `tests/crypto_trading/screening/test_quant_screener.py`:

```python
from crypto_trading.schemas.evidence import CandidateEvidenceRecord
from crypto_trading.schemas.market import FundingRate
from crypto_trading.screening.quant_screener import (
    build_funding_oi_evidence,
    build_volume_evidence,
    evaluate_candidate,
)


def _funding(rate: str, offset_hours: int) -> FundingRate:
    return FundingRate(
        instrument="BTCUSDT", funding_rate=Decimal(rate), mark_price=Decimal("50000"),
        observed_at=_NOW - timedelta(hours=offset_hours),
    )


def test_volume_evidence_triggers_on_high_zscore():
    klines = _flat_klines(20, price="100")
    spike = _kline("100", offset_hours=0, volume="10000")
    evidence = build_volume_evidence(
        [*klines, spike], zscore_threshold=Decimal("2.5"), lookback=20, evaluated_at=_NOW
    )
    assert evidence.triggered is True
    assert evidence.metric == "volume_zscore"


def test_volume_evidence_does_not_trigger_on_stable_volume():
    klines = _flat_klines(21, price="100")  # samtliga volume="100" (se _kline-default)
    evidence = build_volume_evidence(
        klines, zscore_threshold=Decimal("2.5"), lookback=20, evaluated_at=_NOW
    )
    assert evidence.triggered is False


def test_funding_oi_evidence_triggers_on_high_abs_funding_rate():
    history = [_funding("0.001", offset_hours=8 * i) for i in range(1, 6)]
    latest = _funding("0.08", offset_hours=0)
    evidence = build_funding_oi_evidence(
        [*history, latest], threshold_pct=Decimal("0.05"), evaluated_at=_NOW
    )
    assert evidence.triggered is True
    assert evidence.metric == "funding_rate_pct"


def test_funding_oi_evidence_does_not_trigger_on_low_funding_rate():
    history = [_funding("0.001", offset_hours=8 * i) for i in range(0, 5)]
    evidence = build_funding_oi_evidence(
        history, threshold_pct=Decimal("0.05"), evaluated_at=_NOW
    )
    assert evidence.triggered is False


def test_evaluate_candidate_never_exposes_a_direction_field():
    """AC1: screenern uttalar sig aldrig om riktning."""
    fields = set(CandidateEvidenceRecord.model_fields.keys())
    forbidden = {"direction", "side", "signal", "buy", "sell", "long", "short"}
    assert not (fields & forbidden)
    # samma garanti gäller rekursivt för de fyra evidence-subtyperna
    for sub in (
        CandidateEvidenceRecord.model_fields["price_volatility_evidence"].annotation,
        CandidateEvidenceRecord.model_fields["momentum_breakout_evidence"].annotation,
        CandidateEvidenceRecord.model_fields["volume_evidence"].annotation,
        CandidateEvidenceRecord.model_fields["funding_oi_evidence"].annotation,
    ):
        assert not (set(sub.model_fields.keys()) & forbidden)


def test_evaluate_candidate_is_deterministic():
    """AC2: samma indata given två gånger ger identisk candidate_score och
    identiska trigger_reasons."""
    klines = _flat_klines(21, price="100")
    klines.append(_kline("110", offset_hours=0))
    funding = [_funding("0.001", offset_hours=8 * i) for i in range(1, 6)]

    kwargs = dict(
        instrument="BTCUSDT", timeframes=["1h"], klines=klines, funding_rates=funding,
        data_quality_status="ok", evaluated_at=_NOW,
        price_volatility_threshold_pct=Decimal("2.0"), lookback=20,
        rsi_period=14, rsi_overbought_threshold=Decimal("70"),
        volume_zscore_threshold=Decimal("2.5"), funding_rate_threshold_pct=Decimal("0.05"),
    )
    first = evaluate_candidate(**kwargs)
    second = evaluate_candidate(**kwargs)

    assert first.candidate_score == second.candidate_score
    assert first.trigger_reasons == second.trigger_reasons
    assert first == second


def test_evaluate_candidate_sets_worth_deeper_analysis_when_a_signal_triggers():
    klines = _flat_klines(21, price="100")
    klines.append(_kline("110", offset_hours=0))
    funding = [_funding("0.001", offset_hours=8 * i) for i in range(1, 6)]
    record = evaluate_candidate(
        instrument="BTCUSDT", timeframes=["1h"], klines=klines, funding_rates=funding,
        data_quality_status="ok", evaluated_at=_NOW,
        price_volatility_threshold_pct=Decimal("2.0"), lookback=20,
        rsi_period=14, rsi_overbought_threshold=Decimal("70"),
        volume_zscore_threshold=Decimal("2.5"), funding_rate_threshold_pct=Decimal("0.05"),
    )
    assert record.outcome == "worth_deeper_analysis"
    assert "price_volatility" in record.trigger_reasons
    assert record.data_quality_status == "ok"


def test_evaluate_candidate_sets_not_a_candidate_when_nothing_triggers():
    klines = _flat_klines(21, price="100")
    funding = [_funding("0.001", offset_hours=8 * i) for i in range(0, 5)]
    record = evaluate_candidate(
        instrument="BTCUSDT", timeframes=["1h"], klines=klines, funding_rates=funding,
        data_quality_status="ok", evaluated_at=_NOW,
        price_volatility_threshold_pct=Decimal("2.0"), lookback=20,
        rsi_period=14, rsi_overbought_threshold=Decimal("70"),
        volume_zscore_threshold=Decimal("2.5"), funding_rate_threshold_pct=Decimal("0.05"),
    )
    assert record.outcome == "not_a_candidate"
    assert record.trigger_reasons == []


def test_evaluate_candidate_short_circuits_on_invalid_data_quality():
    record = evaluate_candidate(
        instrument="BTCUSDT", timeframes=["1h"], klines=[], funding_rates=[],
        data_quality_status="invalid", evaluated_at=_NOW,
        price_volatility_threshold_pct=Decimal("2.0"), lookback=20,
        rsi_period=14, rsi_overbought_threshold=Decimal("70"),
        volume_zscore_threshold=Decimal("2.5"), funding_rate_threshold_pct=Decimal("0.05"),
    )
    assert record.data_quality_status == "invalid"
    assert record.outcome == "not_a_candidate"
    assert record.trigger_reasons == []
    assert record.candidate_score == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/screening/test_quant_screener.py -v`
Expected: nya testerna FAIL med `ImportError`/`AttributeError`.

- [ ] **Step 3: Implement**

Lägg till i `crypto_trading/screening/quant_screener.py`:

```python
from typing import Literal

from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    VolumeEvidence,
)
from crypto_trading.schemas.market import FundingRate


def _compute_zscore(latest: Decimal, history: list[Decimal]) -> Decimal:
    if not history:
        return Decimal("0")
    mean = sum(history) / len(history)
    variance = sum((x - mean) ** 2 for x in history) / len(history)
    if variance <= 0:
        return Decimal("0")
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
    placeholder_kwargs = dict(triggered=False, metric="n/a", value=0.0, baseline=0.0, threshold=0.0)
    return CandidateEvidenceRecord(
        instrument=instrument,
        timeframes=timeframes,
        evaluated_at=evaluated_at,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder_kwargs),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder_kwargs),
        volume_evidence=VolumeEvidence(**placeholder_kwargs),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder_kwargs),
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
    datan är pålitlig."""
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
```

(Flytta `Literal`-importen och `CandidateEvidenceRecord`/`VolumeEvidence`/`FundingOpenInterestEvidence`/`FundingRate`-importerna upp till filens toppimport-block tillsammans med Del A:s importer, inte som separata mitt-i-filen-importer — ovan visat separat bara för läsbarhet i denna plan.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/screening/test_quant_screener.py -v`
Expected: alla tester PASS.

---

## Task 6: `screening/candidate_engine.py` — Del A: Candidate-konstruktion, DATA_INVALID, dedup/cooldown (AC3)

**Files:**
- Create: `crypto_trading/screening/candidate_engine.py`
- Create: `tests/crypto_trading/screening/test_candidate_engine.py`

**Interfaces:**
- Produces: `process_evidence(repo, evidence, discovery_run_id, created_at) -> Candidate | None`.

- [ ] **Step 1: Write the failing tests**

`tests/crypto_trading/screening/test_candidate_engine.py`:

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    FundingOpenInterestEvidence,
    MomentumBreakoutEvidence,
    PriceVolatilityEvidence,
    VolumeEvidence,
)
from crypto_trading.screening.candidate_engine import process_evidence
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _evidence(
    instrument="BTCUSDT", outcome="worth_deeper_analysis",
    data_quality_status="ok", candidate_score=0.5, trigger_reasons=None,
) -> CandidateEvidenceRecord:
    placeholder = dict(triggered=False, metric="m", value=0.0, baseline=0.0, threshold=1.0)
    return CandidateEvidenceRecord(
        instrument=instrument, timeframes=["1h"], evaluated_at=_NOW,
        price_volatility_evidence=PriceVolatilityEvidence(**placeholder),
        momentum_breakout_evidence=MomentumBreakoutEvidence(**placeholder),
        volume_evidence=VolumeEvidence(**placeholder),
        funding_oi_evidence=FundingOpenInterestEvidence(**placeholder),
        candidate_score=candidate_score,
        trigger_reasons=trigger_reasons or [],
        data_quality_status=data_quality_status,
        outcome=outcome,
    )


def test_process_evidence_creates_candidate_when_signal_triggered(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    evidence = _evidence(trigger_reasons=["price_volatility"])

    candidate = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)

    assert candidate is not None
    assert candidate.status == "CANDIDATE"
    reloaded = repo.get_candidate(candidate.candidate_id)
    assert reloaded is not None
    assert reloaded.status == "CANDIDATE"


def test_process_evidence_returns_none_and_persists_nothing_when_not_a_candidate(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    evidence = _evidence(outcome="not_a_candidate", trigger_reasons=[])

    candidate = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)

    assert candidate is None
    count = repo._conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 0


def test_process_evidence_creates_data_invalid_candidate_regardless_of_outcome(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    evidence = _evidence(data_quality_status="invalid", outcome="not_a_candidate")

    candidate = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)

    assert candidate is not None
    assert candidate.status == "DATA_INVALID"
    events = repo._conn.execute(
        "SELECT event_type FROM events WHERE aggregate_id = ? ORDER BY seq", (candidate.candidate_id,)
    ).fetchall()
    event_types = [e["event_type"] for e in events]
    assert "CANDIDATE_CREATED" in event_types
    assert "CANDIDATE_TRANSITIONED" in event_types


def test_process_evidence_is_idempotent_on_identical_evidence_and_run(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    evidence = _evidence(trigger_reasons=["price_volatility"])

    first = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)
    second = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)

    assert first.candidate_id == second.candidate_id
    count = repo._conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1


def test_process_evidence_skips_reanalysis_within_cooldown_when_score_unchanged(tmp_path):
    """AC3: en tidigare REJECTED-candidate återanalyseras inte inom
    cooldown-fönstret om evidensen inte förändrats över tröskeln."""
    repo = SQLiteRepository(tmp_path / "t.db")
    first_evidence = _evidence(trigger_reasons=["price_volatility"], candidate_score=0.5)
    first = process_evidence(repo, first_evidence, discovery_run_id="run-1", created_at=_NOW)
    repo.transition_candidate_with_event(
        first.candidate_id, "REJECTED", _NOW,
        _rejection_event(first.candidate_id),
    )

    later = _NOW + timedelta(minutes=30)  # inom 60 min cooldown
    similar_evidence = _evidence(trigger_reasons=["price_volatility"], candidate_score=0.55)  # delta 0.05 < 0.15

    result = process_evidence(
        repo, similar_evidence, discovery_run_id="run-2", created_at=later,
        cooldown_minutes=60, evidence_change_threshold=0.15,
    )

    assert result is None
    count = repo._conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    assert count == 1  # bara den ursprungliga REJECTED-raden


def test_process_evidence_allows_reanalysis_when_evidence_changed_enough(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    first_evidence = _evidence(trigger_reasons=["price_volatility"], candidate_score=0.5)
    first = process_evidence(repo, first_evidence, discovery_run_id="run-1", created_at=_NOW)
    repo.transition_candidate_with_event(
        first.candidate_id, "REJECTED", _NOW, _rejection_event(first.candidate_id)
    )

    later = _NOW + timedelta(minutes=30)
    changed_evidence = _evidence(trigger_reasons=["price_volatility"], candidate_score=0.9)  # delta 0.4 >= 0.15

    result = process_evidence(
        repo, changed_evidence, discovery_run_id="run-2", created_at=later,
        cooldown_minutes=60, evidence_change_threshold=0.15,
    )

    assert result is not None
    assert result.status == "CANDIDATE"


def test_process_evidence_allows_reanalysis_after_cooldown_expires(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    first_evidence = _evidence(trigger_reasons=["price_volatility"], candidate_score=0.5)
    first = process_evidence(repo, first_evidence, discovery_run_id="run-1", created_at=_NOW)
    repo.transition_candidate_with_event(
        first.candidate_id, "REJECTED", _NOW, _rejection_event(first.candidate_id)
    )

    after_cooldown = _NOW + timedelta(minutes=61)
    same_evidence = _evidence(trigger_reasons=["price_volatility"], candidate_score=0.5)

    result = process_evidence(
        repo, same_evidence, discovery_run_id="run-2", created_at=after_cooldown,
        cooldown_minutes=60, evidence_change_threshold=0.15,
    )

    assert result is not None


def _rejection_event(candidate_id: str):
    from crypto_trading.schemas.event import Event

    return Event(
        event_id=f"REJECTED:{candidate_id}", event_type="CANDIDATE_REJECTED",
        aggregate_type="candidate", aggregate_id=candidate_id, occurred_at=_NOW,
        run_id="run-1", schema_version=1, payload={},
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/screening/test_candidate_engine.py -v`
Expected: FAIL med `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

`crypto_trading/screening/candidate_engine.py`:

```python
from __future__ import annotations

from datetime import datetime

from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    compute_candidate_idempotency_key,
    compute_evidence_hash,
)
from crypto_trading.schemas.event import Event
from crypto_trading.state_machine import can_transition
from crypto_trading.storage.repository import Repository


def _build_candidate(
    evidence: CandidateEvidenceRecord, discovery_run_id: str, created_at: datetime
) -> Candidate:
    evidence_hash = compute_evidence_hash(evidence)
    # idempotency_key används direkt som candidate_id - garanterat stabil och
    # unik per (instrument, discovery_run_id, evidence_hash) redan genom sin
    # egen konstruktion (schemas/evidence.py, Phase 0), så ingen separat
    # UUID-generering eller extra kollisionslogik behövs.
    idempotency_key = compute_candidate_idempotency_key(
        evidence.instrument, discovery_run_id, evidence_hash
    )
    return Candidate(
        candidate_id=idempotency_key,
        idempotency_key=idempotency_key,
        instrument=evidence.instrument,
        discovery_run_id=discovery_run_id,
        evidence_hash=evidence_hash,
        status="CANDIDATE",
        evidence_record=evidence,
        created_at=created_at,
        updated_at=created_at,
    )


def _persist_new_candidate(
    repo: Repository, evidence: CandidateEvidenceRecord, discovery_run_id: str, created_at: datetime
) -> Candidate:
    candidate = _build_candidate(evidence, discovery_run_id, created_at)
    creation_event = Event(
        event_id=f"CANDIDATE_CREATED:{candidate.candidate_id}",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=created_at,
        run_id=discovery_run_id,
        schema_version=1,
        payload={"instrument": candidate.instrument, "candidate_score": evidence.candidate_score},
    )
    repo.create_candidate_with_event(candidate, creation_event)
    return candidate


def _transition_to_terminal(
    repo: Repository, candidate: Candidate, target_status: str, at: datetime, run_id: str
) -> Candidate:
    allowed, reason = can_transition(candidate.status, target_status)
    if not allowed:
        raise AssertionError(f"illegal transition attempted: {reason}")
    event = Event(
        event_id=f"CANDIDATE_TRANSITIONED:{candidate.candidate_id}:{target_status}",
        event_type="CANDIDATE_TRANSITIONED",
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=at,
        run_id=run_id,
        schema_version=1,
        payload={"from": candidate.status, "to": target_status},
    )
    repo.transition_candidate_with_event(candidate.candidate_id, target_status, at, event)
    return candidate.model_copy(update={"status": target_status, "updated_at": at})


def _is_within_cooldown_and_unchanged(
    repo: Repository,
    evidence: CandidateEvidenceRecord,
    now: datetime,
    cooldown_minutes: int,
    evidence_change_threshold: float,
) -> bool:
    latest_rejected = repo.find_latest_candidate_by_instrument_and_status(
        evidence.instrument, "REJECTED"
    )
    if latest_rejected is None:
        return False
    elapsed_minutes = (now - latest_rejected.updated_at).total_seconds() / 60
    if elapsed_minutes >= cooldown_minutes:
        return False
    score_delta = abs(evidence.candidate_score - latest_rejected.evidence_record.candidate_score)
    return score_delta < evidence_change_threshold


def process_evidence(
    repo: Repository,
    evidence: CandidateEvidenceRecord,
    discovery_run_id: str,
    created_at: datetime,
    cooldown_minutes: int = 60,
    evidence_change_threshold: float = 0.15,
) -> Candidate | None:
    """SPEC §5/§7: skapar en Candidate-rad bara när det finns anledning.

    - data_quality_status == "invalid" -> alltid en rad, direkt DATA_INVALID
      (terminal), oavsett vad screenern kom fram till för outcome.
    - outcome == "not_a_candidate" (och data ok) -> ingen rad alls, None.
    - outcome == "worth_deeper_analysis" -> dedup/cooldown-kontroll (AC3),
      sedan en ny CANDIDATE-rad om den klarar kontrollen.
    """
    if evidence.data_quality_status == "invalid":
        candidate = _persist_new_candidate(repo, evidence, discovery_run_id, created_at)
        return _transition_to_terminal(
            repo, candidate, "DATA_INVALID", created_at, discovery_run_id
        )

    if evidence.outcome == "not_a_candidate":
        return None

    if _is_within_cooldown_and_unchanged(
        repo, evidence, created_at, cooldown_minutes, evidence_change_threshold
    ):
        return None

    return _persist_new_candidate(repo, evidence, discovery_run_id, created_at)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/screening/test_candidate_engine.py -v`
Expected: alla tester PASS.

---

## Task 7: `screening/candidate_engine.py` — Del B: budget-baserad prioritering (§10)

**Files:**
- Modify: `crypto_trading/screening/candidate_engine.py`
- Modify: `tests/crypto_trading/screening/test_candidate_engine.py`

**Interfaces:**
- Produces: `prioritize_and_apply_budget(repo, candidates, liquidity_by_instrument, max_candidates_per_discovery_run, evaluated_at, run_id) -> tuple[list[Candidate], list[Candidate]]`.

- [ ] **Step 1: Write the failing tests**

Lägg till i `tests/crypto_trading/screening/test_candidate_engine.py`:

```python
from crypto_trading.screening.candidate_engine import prioritize_and_apply_budget


def _candidate_via_process_evidence(repo, instrument, score, run_id="run-1", at=_NOW):
    evidence = _evidence(instrument=instrument, trigger_reasons=["price_volatility"], candidate_score=score)
    return process_evidence(repo, evidence, discovery_run_id=run_id, created_at=at)


def test_prioritize_and_apply_budget_keeps_highest_score_within_budget(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    low = _candidate_via_process_evidence(repo, "AAAUSDT", score=0.2)
    high = _candidate_via_process_evidence(repo, "BBBUSDT", score=0.9)

    within, over = prioritize_and_apply_budget(
        repo, [low, high], liquidity_by_instrument={}, max_candidates_per_discovery_run=1,
        evaluated_at=_NOW, run_id="run-1",
    )

    assert [c.instrument for c in within] == ["BBBUSDT"]
    assert [c.instrument for c in over] == ["AAAUSDT"]


def test_prioritize_and_apply_budget_transitions_excess_to_budget_limited(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    low = _candidate_via_process_evidence(repo, "AAAUSDT", score=0.2)
    high = _candidate_via_process_evidence(repo, "BBBUSDT", score=0.9)

    prioritize_and_apply_budget(
        repo, [low, high], liquidity_by_instrument={}, max_candidates_per_discovery_run=1,
        evaluated_at=_NOW, run_id="run-1",
    )

    reloaded_low = repo.get_candidate(low.candidate_id)
    reloaded_high = repo.get_candidate(high.candidate_id)
    assert reloaded_low.status == "BUDGET_LIMITED"
    assert reloaded_high.status == "CANDIDATE"  # oförändrad - inga AI-anrop i denna fas


def test_prioritize_and_apply_budget_never_marks_excess_as_rejected(tmp_path):
    """SPEC §10: BUDGET_LIMITED, aldrig REJECTED - skiljer resursbrist från
    sakligt underkännande."""
    repo = SQLiteRepository(tmp_path / "t.db")
    only = _candidate_via_process_evidence(repo, "AAAUSDT", score=0.2)

    prioritize_and_apply_budget(
        repo, [only], liquidity_by_instrument={}, max_candidates_per_discovery_run=0,
        evaluated_at=_NOW, run_id="run-1",
    )

    reloaded = repo.get_candidate(only.candidate_id)
    assert reloaded.status == "BUDGET_LIMITED"


def test_prioritize_and_apply_budget_uses_liquidity_as_tiebreaker(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    a = _candidate_via_process_evidence(repo, "AAAUSDT", score=0.5)
    b = _candidate_via_process_evidence(repo, "BBBUSDT", score=0.5)  # samma score

    within, _ = prioritize_and_apply_budget(
        repo, [a, b],
        liquidity_by_instrument={"AAAUSDT": Decimal("1000"), "BBBUSDT": Decimal("9000")},
        max_candidates_per_discovery_run=1, evaluated_at=_NOW, run_id="run-1",
    )

    assert [c.instrument for c in within] == ["BBBUSDT"]  # högre likviditet vinner vid oavgjort
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/screening/test_candidate_engine.py -v`
Expected: nya testerna FAIL med `AttributeError`/`ImportError`.

- [ ] **Step 3: Implement**

Lägg till i `crypto_trading/screening/candidate_engine.py`:

```python
from decimal import Decimal


def prioritize_and_apply_budget(
    repo: Repository,
    candidates: list[Candidate],
    liquidity_by_instrument: dict[str, Decimal],
    max_candidates_per_discovery_run: int,
    evaluated_at: datetime,
    run_id: str,
) -> tuple[list[Candidate], list[Candidate]]:
    """SPEC §10: deterministisk prioriteringsordning (1) data quality - redan
    garanterat "ok" här (DATA_INVALID-candidates skickas aldrig in i denna
    funktion), (2) candidate_score fallande, (3) likviditet fallande,
    (4) färskhet (created_at fallande). Gör inga AI-anrop - candidates inom
    budget lämnas i status CANDIDATE, redo för Phase 3 att plocka upp."""
    ranked = sorted(
        candidates,
        key=lambda c: (
            -c.evidence_record.candidate_score,
            -liquidity_by_instrument.get(c.instrument, Decimal("0")),
            -c.created_at.timestamp(),
        ),
    )
    within_budget = ranked[:max_candidates_per_discovery_run]
    over_budget = ranked[max_candidates_per_discovery_run:]

    limited: list[Candidate] = []
    for candidate in over_budget:
        limited.append(
            _transition_to_terminal(repo, candidate, "BUDGET_LIMITED", evaluated_at, run_id)
        )
    return within_budget, limited
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/crypto_trading/screening/test_candidate_engine.py -v`
Expected: alla tester PASS.

---

## Task 8: Top N-dynamik (AC4)

**Files:**
- Modify: `tests/crypto_trading/screening/test_eligibility_filter.py`

**Interfaces:** inga nya — rent testtillägg mot befintlig `select_top_n`/`check_eligibility`.

- [ ] **Step 1: Write the failing test**

Lägg till i `tests/crypto_trading/screening/test_eligibility_filter.py`:

```python
def test_top_n_membership_changes_when_liquidity_rank_changes_between_runs():
    """AC4: Top N är verifierat dynamiskt - ett instrument som byter
    likviditetsrank kan gå in/ur universumet mellan körningar."""
    run_1 = [
        _ticker(quote_volume="9000000"),  # BTCUSDT
        _ticker(quote_volume="8000000").model_copy(update={"instrument": "ETHUSDT"}),
        _ticker(quote_volume="1000000").model_copy(update={"instrument": "DOGEUSDT"}),
    ]
    run_2 = [
        _ticker(quote_volume="500000"),  # BTCUSDT rasar i likviditet
        _ticker(quote_volume="8000000").model_copy(update={"instrument": "ETHUSDT"}),
        _ticker(quote_volume="9500000").model_copy(update={"instrument": "DOGEUSDT"}),
    ]

    top_2_run_1 = select_top_n(run_1, n=2)
    top_2_run_2 = select_top_n(run_2, n=2)

    assert top_2_run_1 == ["BTCUSDT", "ETHUSDT"]
    assert top_2_run_2 == ["DOGEUSDT", "ETHUSDT"]
    assert "BTCUSDT" in top_2_run_1
    assert "BTCUSDT" not in top_2_run_2  # föll ur universumet


def test_top_n_selection_alone_creates_no_candidate_rows():
    """Andra halvan av AC4: Top N-medlemskap i sig skapar aldrig en
    Candidate-rad eller något riktningsuttalande - bara ett urval av
    instrumentsymboler (str), inga scheman som kan persisteras som candidate."""
    result = select_top_n([_ticker()], n=1)
    assert result == ["BTCUSDT"]
    assert isinstance(result[0], str)  # inte ett Candidate/CandidateEvidenceRecord-objekt
```

(Den strukturella importgräns-testen `test_select_top_n_membership_alone_never_touches_storage` från Task 3 kompletterar redan denna AC — tillsammans täcker de båda halvorna av AC4.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/crypto_trading/screening/test_eligibility_filter.py -v -k top_n_membership_changes`
Expected: FAIL — Top N-funktionen finns redan (Task 3), så detta test ska egentligen redan kunna passera om Task 3 implementerades korrekt. Om det FAILAR av annan anledning än att testet är nytt (t.ex. sorteringsbugg), fixa `select_top_n` innan Step 3.

- [ ] **Step 3: Confirm passing (ingen ny produktionskod förväntas)**

Run: `pytest tests/crypto_trading/screening/test_eligibility_filter.py -v`
Expected: alla tester PASS, inklusive de två nya. Om `select_top_n` redan var korrekt implementerad i Task 3 är detta en ren regressions-/AC-bekräftelsetask utan kodändring.

---

## Task 9: Schema-oförväxlingsbarhet (AC5) + fullständigt integrationstest

**Files:**
- Create: `tests/crypto_trading/screening/test_evidence_schema_guard.py`
- Create: `tests/crypto_trading/screening/test_screening_integration.py`

**Interfaces:** inga nya — rena tester mot befintliga scheman/moduler.

- [ ] **Step 1: Write the failing tests**

`tests/crypto_trading/screening/test_evidence_schema_guard.py`:

```python
"""AC5: CandidateEvidenceRecord har inget fält som kan tolkas som
AI-confidence, forecast-sannolikhet eller trade-kvalitet. candidate_score
är typmässigt och namnmässigt oförväxlingsbart med de fälten som tillkommer
i Phase 3 (ForecastAssessment.scenario_probabilities m.fl., SPEC §4-tabellen)."""

from crypto_trading.schemas.assessments import ForecastAssessment
from crypto_trading.schemas.evidence import CandidateEvidenceRecord

_FORBIDDEN_NAME_FRAGMENTS = ("confidence", "probability", "quality_score", "trade_quality")


def test_candidate_evidence_record_has_no_ai_confidence_or_forecast_field():
    for field_name in CandidateEvidenceRecord.model_fields:
        assert field_name != "confidence"
        assert not any(frag in field_name for frag in _FORBIDDEN_NAME_FRAGMENTS)


def test_candidate_score_field_name_never_collides_with_forecast_assessment_fields():
    evidence_fields = set(CandidateEvidenceRecord.model_fields.keys())
    forecast_fields = set(ForecastAssessment.model_fields.keys())
    assert "candidate_score" in evidence_fields
    assert "candidate_score" not in forecast_fields
    assert evidence_fields.isdisjoint(forecast_fields)


def test_candidate_score_is_a_plain_float_not_a_probability_distribution():
    assert CandidateEvidenceRecord.model_fields["candidate_score"].annotation is float
```

`tests/crypto_trading/screening/test_screening_integration.py`:

```python
"""Fullständig kedja: eligibility -> Top N -> quant screener -> candidate
engine -> repository, uteslutande på typade fixtures (inget nätverk),
samma anda som Phase 1:s test_market_data_integration.py."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.schemas.market import FundingRate, InstrumentMetadata, Kline, Ticker
from crypto_trading.screening.candidate_engine import process_evidence, prioritize_and_apply_budget
from crypto_trading.screening.eligibility_filter import check_eligibility, select_top_n
from crypto_trading.screening.quant_screener import evaluate_candidate
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _instrument(symbol: str) -> InstrumentMetadata:
    return InstrumentMetadata(
        symbol=symbol, status=1, price_precision=2, quantity_precision=3,
        trade_min_usdt=Decimal("2"), fetched_at=_NOW,
    )


def _ticker(symbol: str, quote_volume: str) -> Ticker:
    return Ticker(
        instrument=symbol, last_price=Decimal("100"), price_change=Decimal("0"),
        price_change_percent=Decimal("0"), high_price=Decimal("101"), low_price=Decimal("99"),
        volume=Decimal("500"), quote_volume=Decimal(quote_volume), open_price=Decimal("100"),
        ask_price=Decimal("100.05"), ask_qty=Decimal("1"), bid_price=Decimal("99.95"),
        bid_qty=Decimal("1"), observed_at=_NOW,
    )


def _klines(symbol: str, spike: bool) -> list[Kline]:
    result = [
        Kline(
            instrument=symbol, interval="1h", open=Decimal("100"), high=Decimal("100"),
            low=Decimal("100"), close=Decimal("100"), volume=Decimal("100"),
            observed_at=_NOW - timedelta(hours=21 - i),
        )
        for i in range(21)
    ]
    if spike:
        result.append(Kline(
            instrument=symbol, interval="1h", open=Decimal("100"), high=Decimal("115"),
            low=Decimal("100"), close=Decimal("112"), volume=Decimal("9000"), observed_at=_NOW,
        ))
    return result


def _funding(symbol: str) -> list[FundingRate]:
    return [
        FundingRate(
            instrument=symbol, funding_rate=Decimal("0.001"), mark_price=Decimal("100"),
            observed_at=_NOW - timedelta(hours=8 * i),
        )
        for i in range(1, 6)
    ]


def test_full_discovery_chain_from_eligibility_to_persisted_candidate(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")

    universe = {
        "BTCUSDT": (_instrument("BTCUSDT"), _ticker("BTCUSDT", "9000000"), True),
        "ETHUSDT": (_instrument("ETHUSDT"), _ticker("ETHUSDT", "7000000"), False),
        "LOWVOLUSDT": (_instrument("LOWVOLUSDT"), _ticker("LOWVOLUSDT", "100"), False),
    }

    eligible_tickers = []
    for instrument, ticker, _ in universe.values():
        ok, _reason = check_eligibility(
            instrument, ticker, "ok",
            min_quote_volume_24h_usdt=Decimal("5000000"), max_spread_pct=Decimal("0.01"),
        )
        if ok:
            eligible_tickers.append(ticker)

    assert {t.instrument for t in eligible_tickers} == {"BTCUSDT", "ETHUSDT"}

    top_n = select_top_n(eligible_tickers, n=30)
    assert set(top_n) == {"BTCUSDT", "ETHUSDT"}  # LOWVOLUSDT föll bort på likviditet

    created_candidates = []
    for symbol in top_n:
        _instrument_meta, _ticker_obj, spike = universe[symbol]
        evidence = evaluate_candidate(
            instrument=symbol, timeframes=["1h"], klines=_klines(symbol, spike=spike),
            funding_rates=_funding(symbol), data_quality_status="ok", evaluated_at=_NOW,
            price_volatility_threshold_pct=Decimal("2.0"), lookback=20,
            rsi_period=14, rsi_overbought_threshold=Decimal("70"),
            volume_zscore_threshold=Decimal("2.5"), funding_rate_threshold_pct=Decimal("0.05"),
        )
        candidate = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)
        if candidate is not None:
            created_candidates.append(candidate)

    # BTCUSDT hade en pris-/volymspik -> worth_deeper_analysis -> Candidate-rad.
    # ETHUSDT hade platta priser -> not_a_candidate -> ingen rad.
    assert [c.instrument for c in created_candidates] == ["BTCUSDT"]
    assert created_candidates[0].status == "CANDIDATE"

    within, limited = prioritize_and_apply_budget(
        repo, created_candidates, liquidity_by_instrument={"BTCUSDT": Decimal("9000000")},
        max_candidates_per_discovery_run=10, evaluated_at=_NOW, run_id="run-1",
    )
    assert len(within) == 1
    assert limited == []

    reloaded = repo.get_candidate(created_candidates[0].candidate_id)
    assert reloaded.status == "CANDIDATE"  # inom budget, oförändrad - Phase 3 tar vid härifrån
    assert reloaded.evidence_record.outcome == "worth_deeper_analysis"
    assert reloaded.evidence_record.data_quality_status == "ok"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/crypto_trading/screening/test_evidence_schema_guard.py tests/crypto_trading/screening/test_screening_integration.py -v`
Expected: om alla tidigare tasks är korrekt implementerade FAILAR dessa bara om ett verkligt AC5-brott eller integrationsfel upptäcks — annars är de gröna direkt (rent verifieringstillägg, ingen ny produktionskod krävs). Kör ändå innan Step 3 för att bekräfta.

- [ ] **Step 3: Fix any discovered issues, then confirm passing**

Run: `pytest tests/crypto_trading/screening/ -v`
Expected: samtliga screening-tester PASS.

---

## Task 10: Slutverifiering

**Files:** inga (bara verifieringskommandon).

- [ ] **Step 1: Full testsvit för crypto_trading**

Run: `pytest tests/crypto_trading/ -v`
Expected: alla tester gröna, inklusive alla nya `screening/`-tester.

- [ ] **Step 2: Ruff check + format**

Run: `ruff check crypto_trading/ tests/crypto_trading/`
Expected: inga fel.

Run: `ruff format --check crypto_trading/ tests/crypto_trading/`
Expected: inga diff.

- [ ] **Step 3: Verifiera att intelligence/ fortfarande är orört**

Run: `git diff master -- intelligence/`
Expected: tom output.

- [ ] **Step 4: Full repo-testsvit**

Run: `pytest -v`
Expected: alla tester (crypto_trading Phase 0/1/2, intelligence, test_setup) gröna, ingen regression.

- [ ] **Step 5: Verifiera importgräns och broker-frihet fortfarande håller**

Run: `pytest tests/crypto_trading/test_no_intelligence_coupling.py -v`
Expected: PASS — testet globar hela `crypto_trading/` och fångar Phase 2:s nya filer automatiskt.

- [ ] **Step 6: Explicit grep-guard mot riktningsord i screening/**

Run: `grep -rniE "\b(buy|sell|long|short)\b" crypto_trading/screening/`
Expected: ingen träff (utöver ev. kommentarer som citerar SPEC:ens förbudslista själva — granska manuellt om något matchar).

- [ ] **Step 7: Uppdatera PLAN_CRYPTO_PHASE2.md**

Kryssa i samtliga `- [ ]` till `- [x]` och lägg till en statusbanner högst upp med exakt testantal och ev. avvikelser upptäckta under exekvering, i samma format som `PLAN_CRYPTO_PHASE1.md`.

---

## Self-review (utfört innan planen sparas)

**Spec-täckning:** `eligibility_filter.py` (likviditet/spread/status/datakvalitet, Task 3), dynamiskt Top N (Task 3 + AC4 i Task 8), `quant_screener.py` med alla fyra signaltyper (Task 4/5), `candidate_score`/`trigger_reasons`/`outcome` (Task 5), `candidate_engine.py` med dedup/cooldown (Task 6, AC3) och budget-prioritering (Task 7, §10), repository-stöd för cooldown-uppslag (Task 2). AC1 (Task 5), AC2 (Task 5), AC3 (Task 6), AC4 (Task 3 + Task 8), AC5 (Task 9). Look-ahead-bias-guard §8.4 (Task 4, delad `_sorted_up_to`/`_sorted_funding_up_to`-helper). Ingen kvarstående lucka mot `PLAN_CRYPTO.md` Phase 2-avsnittet.

**Placeholder-scan:** inga TBD/TODO. Screener-trösklarna (Task 1) är medvetet flaggade som egna strategival, inte SPEC-verifierade fakta — skiljs uttryckligen från Phase 1:s BingX-verifierade `required_fields`/`max_data_age_seconds`.

**Typkonsekvens:** `evaluate_candidate()`s signatur (Task 5) matchar exakt hur den anropas i Task 9:s integrationstest. `process_evidence()`/`prioritize_and_apply_budget()` (Task 6/7) matchar exakt mellan implementation och alla konsumerande tester. `Repository`-protokollets nya metod (Task 2) implementeras identiskt i `SQLiteRepository` och används oförändrad av `candidate_engine.py`.

**Scope-kontroll:** ingen AI/LLM-kod, ingen `agents/`-import, inget BUY/SELL/LONG/SHORT-fält någonstans (strukturellt omöjligt + explicit testat), ingen `UNDER_AI_ANALYSIS`-övergång (candidate_engine lämnar budget-godkända candidates i `CANDIDATE`, redo för Phase 3). Ingen `gate/`-, `paper_trading/`-, `notify/`- eller `dashboard/`-kod — allt det är senare faser. `intelligence/` refereras inte någonstans.

**Känd, dokumenterad avgränsning:** `funding_oi_evidence` baseras i Phase 2 uteslutande på funding-rate-historik, inte OI-historik (Task 5:s "Verifierad upptäckt"-notis) — en direkt konsekvens av att Phase 1:s connector bara exponerar en OI-engångssnapshot, inte en spec-avvikelse.
