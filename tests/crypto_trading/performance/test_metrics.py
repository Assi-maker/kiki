from datetime import UTC, datetime
from decimal import Decimal

from crypto_trading.performance.metrics import (
    compute_breakdown_by_direction,
    compute_breakdown_by_instrument,
    compute_cumulative_pnl,
    compute_drawdown,
    compute_equity_curve,
    compute_expectancy,
    compute_profit_factor,
    compute_win_rate,
    trade_pnls,
)
from crypto_trading.schemas.trade import Position

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _closed_position(
    position_id: str,
    entry: str,
    exit_price: str,
    size: str = "1000",
    fees: str = "0",
    funding: str = "0",
    direction: str = "LONG",
    closed_at: datetime = _NOW,
    instrument: str = "BTCUSDT",
) -> Position:
    return Position(
        position_id=position_id,
        candidate_id=f"cand-{position_id}",
        instrument=instrument,
        direction=direction,
        status="CLOSED",
        theoretical_entry=entry,
        simulated_fill_entry=entry,
        stop_loss="0",
        target="0",
        size=size,
        fill_model_version="v1",
        opened_at=closed_at,
        theoretical_exit=exit_price,
        simulated_fill_exit=exit_price,
        exit_reason="target",
        fees=fees,
        funding=funding,
        closed_at=closed_at,
    )


def _open_position(position_id: str) -> Position:
    return Position(
        position_id=position_id,
        candidate_id=f"cand-{position_id}",
        instrument="BTCUSDT",
        direction="LONG",
        status="OPEN_POSITION",
        theoretical_entry="100",
        simulated_fill_entry="100",
        stop_loss="90",
        target="110",
        size="1000",
        fill_model_version="v1",
        opened_at=_NOW,
    )


_WIN = ("100", "110")  # pnl = +100
_LOSS = ("100", "90")  # pnl = -100
_BREAKEVEN = ("100", "100")  # pnl = 0


# --- trade_pnls ----------------------------------------------------------


def test_trade_pnls_filters_to_closed_only():
    win = _closed_position("win", *_WIN)
    still_open = _open_position("open")

    result = trade_pnls([win, still_open])

    assert result == [Decimal("100")]


def test_trade_pnls_returns_empty_for_empty_list():
    assert trade_pnls([]) == []


# --- compute_cumulative_pnl -----------------------------------------------


def test_cumulative_pnl_sums_wins_and_losses():
    positions = [_closed_position("a", *_WIN), _closed_position("b", *_LOSS)]
    assert compute_cumulative_pnl(trade_pnls(positions)) == Decimal("0")


def test_cumulative_pnl_is_zero_not_none_for_empty_list():
    assert compute_cumulative_pnl([]) == Decimal("0")


# --- compute_win_rate ------------------------------------------------------


def test_win_rate_none_for_empty_list():
    assert compute_win_rate([]) is None


def test_win_rate_counts_only_strictly_positive_pnl():
    pnls = trade_pnls(
        [
            _closed_position("w", *_WIN),
            _closed_position("l", *_LOSS),
            _closed_position("b", *_BREAKEVEN),
        ]
    )
    # 1 win out of 3 trades - breakeven counts in the denominator, never the numerator
    assert compute_win_rate(pnls) == Decimal("1") / Decimal("3")


def test_win_rate_breakeven_only_is_zero():
    pnls = trade_pnls([_closed_position("b", *_BREAKEVEN)])
    assert compute_win_rate(pnls) == Decimal("0")


# --- compute_expectancy -----------------------------------------------------


def test_expectancy_none_for_empty_list():
    assert compute_expectancy([]) is None


def test_expectancy_is_average_pnl_per_closed_trade():
    """Explicit vald definition (PLAN_CRYPTO_PHASE8.md §0): genomsnittlig
    PnL per stängd trade, INTE vinstprocent x snittvinst-formeln."""
    pnls = trade_pnls([_closed_position("w", *_WIN), _closed_position("l", *_LOSS)])
    assert compute_expectancy(pnls) == Decimal("0")  # (100 + -100) / 2

    pnls_three = trade_pnls(
        [
            _closed_position("w1", *_WIN),
            _closed_position("w2", *_WIN),
            _closed_position("l", *_LOSS),
        ]
    )
    assert compute_expectancy(pnls_three) == Decimal("100") / Decimal("3")


# --- compute_profit_factor --------------------------------------------------


def test_profit_factor_none_for_empty_list():
    assert compute_profit_factor([]) is None


def test_profit_factor_none_when_all_wins_no_losses():
    """Division med noll (inga förluster) - odefinierat, aldrig
    Infinity/fabricerat."""
    pnls = trade_pnls([_closed_position("w1", *_WIN), _closed_position("w2", *_WIN)])
    assert compute_profit_factor(pnls) is None


def test_profit_factor_zero_when_all_losses_no_wins():
    """Skiljs medvetet från all-wins-fallet: 0/summa-förluster = 0, ett
    giltigt tal, inte odefinierat."""
    pnls = trade_pnls([_closed_position("l1", *_LOSS), _closed_position("l2", *_LOSS)])
    assert compute_profit_factor(pnls) == Decimal("0")


def test_profit_factor_handcalculated_example():
    # wins: 100 + 50 = 150, losses: 100 -> profit_factor = 150/100 = 1.5
    pnls = trade_pnls(
        [
            _closed_position("w1", *_WIN),
            _closed_position("w2", "100", "105"),  # pnl = +50
            _closed_position("l", *_LOSS),
        ]
    )
    assert compute_profit_factor(pnls) == Decimal("1.5")


# --- compute_drawdown -------------------------------------------------------


def test_drawdown_none_for_empty_list():
    assert compute_drawdown([]) is None


def test_drawdown_single_winning_trade_is_zero_not_none():
    """En trade, ingen nedgång från toppen - ett giltigt beräknat 0,
    skiljs medvetet från "ingen data" (None)."""
    positions = [_closed_position("w", *_WIN, closed_at=_NOW)]
    assert compute_drawdown(positions) == Decimal("0")


def test_drawdown_handcalculated_example():
    """Kronologisk sekvens (i closed_at-ordning) med size=1000: +10 %, +5 %,
    -20 %, +3 % prisrörelse -> pnl +100, +50, -200, +30. Kumulativ kurva:
    100, 150, -50, -20. Peak-spårning: peak=100(dd=0) -> peak=150(dd=0) ->
    peak=150,running=-50(dd=200) -> peak=150,running=-20(dd=170).
    Max drawdown = 200."""
    positions = [
        _closed_position("p1", "100", "110", closed_at=datetime(2026, 8, 30, 10, tzinfo=UTC)),
        _closed_position("p2", "100", "105", closed_at=datetime(2026, 8, 30, 11, tzinfo=UTC)),
        _closed_position("p3", "100", "80", closed_at=datetime(2026, 8, 30, 12, tzinfo=UTC)),
        _closed_position("p4", "100", "103", closed_at=datetime(2026, 8, 30, 13, tzinfo=UTC)),
    ]
    assert compute_drawdown(positions) == Decimal("200")


def test_drawdown_ignores_still_open_positions():
    positions = [_closed_position("w", *_WIN, closed_at=_NOW), _open_position("open")]
    assert compute_drawdown(positions) == Decimal("0")


# --- compute_equity_curve ---------------------------------------------------


def test_equity_curve_empty_for_empty_list():
    assert compute_equity_curve([]) == []


def test_equity_curve_is_chronological_regardless_of_input_order():
    """Positions skickas in i FEL (icke-kronologisk) ordning - output ska
    ändå vara sorterat på closed_at, beräknat internt av funktionen, aldrig
    beroende av anroparens ordning."""
    later = _closed_position("later", *_WIN, closed_at=datetime(2026, 8, 30, 14, tzinfo=UTC))
    earlier = _closed_position("earlier", *_LOSS, closed_at=datetime(2026, 8, 30, 10, tzinfo=UTC))

    curve = compute_equity_curve([later, earlier])

    assert [point["closed_at"] for point in curve] == [
        earlier.closed_at.isoformat(),
        later.closed_at.isoformat(),
    ]
    # Jämför via Decimal-värde, inte exakt strängrepresentation - Decimal
    # bevarar skalan från divisionen i compute_pnl() (t.ex. "-100.0" istället
    # för "-100"), matematiskt identiskt men inte textmässigt identiskt.
    assert Decimal(curve[0]["cumulative_pnl"]) == Decimal("-100")
    assert Decimal(curve[1]["cumulative_pnl"]) == Decimal("0")  # -100 + 100


def test_equity_curve_never_includes_a_position_still_open():
    positions = [_closed_position("w", *_WIN, closed_at=_NOW), _open_position("open")]
    curve = compute_equity_curve(positions)
    assert len(curve) == 1


# --- compute_breakdown_by_instrument ----------------------------------------


def test_breakdown_by_instrument_empty_for_empty_list():
    assert compute_breakdown_by_instrument([]) == {}


def test_breakdown_by_instrument_separates_two_instruments():
    positions = [
        _closed_position("btc-win", *_WIN, instrument="BTCUSDT"),
        _closed_position("eth-loss", *_LOSS, instrument="ETHUSDT"),
    ]

    result = compute_breakdown_by_instrument(positions)

    assert set(result.keys()) == {"BTCUSDT", "ETHUSDT"}
    assert result["BTCUSDT"]["trade_count"] == 1
    assert Decimal(result["BTCUSDT"]["cumulative_pnl"]) == Decimal("100")
    assert result["BTCUSDT"]["win_rate"] == str(Decimal("1"))
    assert result["ETHUSDT"]["trade_count"] == 1
    assert Decimal(result["ETHUSDT"]["cumulative_pnl"]) == Decimal("-100")
    assert result["ETHUSDT"]["win_rate"] == str(Decimal("0"))


def test_breakdown_by_instrument_ignores_still_open_positions():
    positions = [_closed_position("w", *_WIN, instrument="BTCUSDT"), _open_position("open")]
    result = compute_breakdown_by_instrument(positions)
    assert set(result.keys()) == {"BTCUSDT"}


# --- compute_breakdown_by_direction -----------------------------------------


def test_breakdown_by_direction_empty_for_empty_list():
    assert compute_breakdown_by_direction([]) == {}


def test_breakdown_by_direction_handles_a_manually_constructed_short_position():
    """Bevisar riktningsagnostisk kod: produktionspipelinen är LONG-only
    idag (paper_trading/position_closing.py: _DIRECTION="LONG", oförändrad
    i denna fas), men breakdown-funktionen antar aldrig bara LONG - en
    manuellt konstruerad SHORT-position (bara för detta test, ingen
    produktionskod skapar sådana) bevisar det."""
    positions = [
        _closed_position("long-win", *_WIN, direction="LONG"),
        _closed_position("short-win", *_WIN, direction="SHORT"),
    ]

    result = compute_breakdown_by_direction(positions)

    assert set(result.keys()) == {"LONG", "SHORT"}
    assert result["LONG"]["trade_count"] == 1
    assert result["SHORT"]["trade_count"] == 1


# --- determinism (PLAN_CRYPTO_PHASE8.md §4) ---------------------------------


def test_performance_summary_is_deterministic_on_repeated_calls():
    """Rena funktioner: samma list[Position] in två gånger -> identisk
    utdata från samtliga performance-funktioner, ingen dold state, inget
    datetime.now()/slumptal inuti funktionerna."""
    positions = [
        _closed_position("w1", *_WIN, closed_at=datetime(2026, 8, 30, 10, tzinfo=UTC)),
        _closed_position("l1", *_LOSS, closed_at=datetime(2026, 8, 30, 11, tzinfo=UTC)),
    ]

    pnls_a = trade_pnls(positions)
    pnls_b = trade_pnls(positions)
    assert pnls_a == pnls_b
    assert compute_cumulative_pnl(pnls_a) == compute_cumulative_pnl(pnls_b)
    assert compute_win_rate(pnls_a) == compute_win_rate(pnls_b)
    assert compute_expectancy(pnls_a) == compute_expectancy(pnls_b)
    assert compute_drawdown(positions) == compute_drawdown(positions)
    assert compute_equity_curve(positions) == compute_equity_curve(positions)
    assert compute_breakdown_by_instrument(positions) == compute_breakdown_by_instrument(positions)
