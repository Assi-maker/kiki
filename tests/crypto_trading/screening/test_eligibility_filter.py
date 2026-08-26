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
        symbol="BTCUSDT",
        status=status,
        price_precision=2,
        quantity_precision=3,
        trade_min_usdt=Decimal("2"),
        fetched_at=datetime.now(UTC),
    )


def _ticker(quote_volume="10000000", ask="50010", bid="49990") -> Ticker:
    return Ticker(
        instrument="BTCUSDT",
        last_price=Decimal("50000"),
        price_change=Decimal("0"),
        price_change_percent=Decimal("0"),
        high_price=Decimal("50100"),
        low_price=Decimal("49900"),
        volume=Decimal("200"),
        quote_volume=Decimal(quote_volume),
        open_price=Decimal("50000"),
        ask_price=Decimal(ask),
        ask_qty=Decimal("1"),
        bid_price=Decimal(bid),
        bid_qty=Decimal("1"),
        observed_at=datetime.now(UTC),
    )


def test_compute_spread_pct_computes_relative_to_mid():
    ticker = _ticker(ask="101", bid="99")  # mid=100, spread=2 -> 0.02
    assert compute_spread_pct(ticker) == Decimal("0.02")


def test_compute_spread_pct_fails_closed_on_non_positive_mid():
    ticker = _ticker(ask="0", bid="0")
    assert compute_spread_pct(ticker) == Decimal("1")


def test_check_eligibility_passes_when_all_criteria_met():
    ok, reason = check_eligibility(
        _instrument(),
        _ticker(),
        "ok",
        min_quote_volume_24h_usdt=Decimal("5000000"),
        max_spread_pct=Decimal("0.002"),
    )
    assert ok is True
    assert reason == "eligible"


def test_check_eligibility_rejects_non_trading_status():
    ok, reason = check_eligibility(
        _instrument(status=0),
        _ticker(),
        "ok",
        min_quote_volume_24h_usdt=Decimal("5000000"),
        max_spread_pct=Decimal("0.002"),
    )
    assert ok is False
    assert reason == "not_trading"


def test_check_eligibility_rejects_invalid_data_quality():
    ok, reason = check_eligibility(
        _instrument(),
        _ticker(),
        "invalid",
        min_quote_volume_24h_usdt=Decimal("5000000"),
        max_spread_pct=Decimal("0.002"),
    )
    assert ok is False
    assert reason == "data_quality_invalid"


def test_check_eligibility_rejects_insufficient_liquidity():
    ok, reason = check_eligibility(
        _instrument(),
        _ticker(quote_volume="100"),
        "ok",
        min_quote_volume_24h_usdt=Decimal("5000000"),
        max_spread_pct=Decimal("0.002"),
    )
    assert ok is False
    assert reason == "insufficient_liquidity"


def test_check_eligibility_rejects_wide_spread():
    ok, reason = check_eligibility(
        _instrument(),
        _ticker(ask="51000", bid="49000"),
        "ok",
        min_quote_volume_24h_usdt=Decimal("5000000"),
        max_spread_pct=Decimal("0.002"),
    )
    assert ok is False
    assert reason == "spread_too_wide"


def test_select_top_n_ranks_by_quote_volume_descending():
    low = _ticker(quote_volume="1000000")
    high = _ticker(quote_volume="9000000").model_copy(update={"instrument": "ETHUSDT"})
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
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    assert not any(m.startswith("crypto_trading.storage") for m in imported_modules)


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
