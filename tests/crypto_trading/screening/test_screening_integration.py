"""Fullständig kedja: eligibility -> Top N -> quant screener -> candidate
engine -> repository, uteslutande på typade fixtures (inget nätverk),
samma anda som Phase 1:s test_market_data_integration.py."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from crypto_trading.schemas.market import FundingRate, InstrumentMetadata, Kline, Ticker
from crypto_trading.screening.candidate_engine import prioritize_and_apply_budget, process_evidence
from crypto_trading.screening.eligibility_filter import check_eligibility, select_top_n
from crypto_trading.screening.quant_screener import evaluate_candidate
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _instrument(symbol: str) -> InstrumentMetadata:
    return InstrumentMetadata(
        symbol=symbol,
        status=1,
        price_precision=2,
        quantity_precision=3,
        trade_min_usdt=Decimal("2"),
        fetched_at=_NOW,
    )


def _ticker(symbol: str, quote_volume: str) -> Ticker:
    return Ticker(
        instrument=symbol,
        last_price=Decimal("100"),
        price_change=Decimal("0"),
        price_change_percent=Decimal("0"),
        high_price=Decimal("101"),
        low_price=Decimal("99"),
        volume=Decimal("500"),
        quote_volume=Decimal(quote_volume),
        open_price=Decimal("100"),
        ask_price=Decimal("100.05"),
        ask_qty=Decimal("1"),
        bid_price=Decimal("99.95"),
        bid_qty=Decimal("1"),
        observed_at=_NOW,
    )


def _klines(symbol: str, spike: bool) -> list[Kline]:
    result = [
        Kline(
            instrument=symbol,
            interval="1h",
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("100"),
            observed_at=_NOW - timedelta(hours=21 - i),
        )
        for i in range(21)
    ]
    if spike:
        result.append(
            Kline(
                instrument=symbol,
                interval="1h",
                open=Decimal("100"),
                high=Decimal("115"),
                low=Decimal("100"),
                close=Decimal("112"),
                volume=Decimal("9000"),
                observed_at=_NOW,
            )
        )
    return result


def _funding(symbol: str) -> list[FundingRate]:
    return [
        FundingRate(
            instrument=symbol,
            funding_rate=Decimal("0.0001"),
            mark_price=Decimal("100"),
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
    for instrument, ticker, _spike in universe.values():
        ok, _reason = check_eligibility(
            instrument,
            ticker,
            "ok",
            min_quote_volume_24h_usdt=Decimal("5000000"),
            max_spread_pct=Decimal("0.01"),
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
            instrument=symbol,
            timeframes=["1h"],
            klines=_klines(symbol, spike=spike),
            funding_rates=_funding(symbol),
            data_quality_status="ok",
            evaluated_at=_NOW,
            price_volatility_threshold_pct=Decimal("2.0"),
            lookback=20,
            rsi_period=14,
            rsi_overbought_threshold=Decimal("70"),
            volume_zscore_threshold=Decimal("2.5"),
            funding_rate_threshold_pct=Decimal("0.05"),
        )
        candidate = process_evidence(repo, evidence, discovery_run_id="run-1", created_at=_NOW)
        if candidate is not None:
            created_candidates.append(candidate)

    # BTCUSDT hade en pris-/volymspik -> worth_deeper_analysis -> Candidate-rad.
    # ETHUSDT hade platta priser -> not_a_candidate -> ingen rad.
    assert [c.instrument for c in created_candidates] == ["BTCUSDT"]
    assert created_candidates[0].status == "CANDIDATE"

    within, limited = prioritize_and_apply_budget(
        repo,
        created_candidates,
        liquidity_by_instrument={"BTCUSDT": Decimal("9000000")},
        max_candidates_per_discovery_run=10,
        evaluated_at=_NOW,
        run_id="run-1",
    )
    assert len(within) == 1
    assert limited == []

    reloaded = repo.get_candidate(created_candidates[0].candidate_id)
    assert reloaded.status == "CANDIDATE"  # inom budget, oförändrad - Phase 3 tar vid härifrån
    assert reloaded.evidence_record.outcome == "worth_deeper_analysis"
    assert reloaded.evidence_record.data_quality_status == "ok"
