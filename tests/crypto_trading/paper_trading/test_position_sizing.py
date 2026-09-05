from decimal import Decimal

from crypto_trading.paper_trading.position_sizing import compute_position_size

# Hög konstant som effektivt inaktiverar per-trade-taket i tester som
# specifikt handlar om risk-formeln/exponeringspoolen, inte om taket självt
# (samma isoleringsprincip som max_total_exposure_pct=1.0 redan används för
# att isolera risk-formeln från exponeringstaket i testet nedan).
_NO_CAP = Decimal("1000000")


def test_position_size_matches_hand_calculation():
    # entry=50000, stop=49000 -> 2% stop-avstånd. capital=10000, risk=1% -> risk_amount=100.
    # size = 100 / 0.02 = 5000. max_total_exposure_pct=1.0 (100%) isolerar
    # risk-formeln från exponeringstaket - det testas separat nedan.
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("0"),
        max_total_exposure_pct=Decimal("1.0"),
        max_position_notional=_NO_CAP,
    )
    assert size == Decimal("5000")


def test_position_size_capped_by_remaining_exposure():
    # max_exposure = 10000 * 0.25 = 2500. Redan 2000 använt -> bara 500 kvar.
    # Rå storlek (5000) klipps till 500.
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("2000"),
        max_total_exposure_pct=Decimal("0.25"),
        max_position_notional=_NO_CAP,
    )
    assert size == Decimal("500")


def test_position_size_is_zero_when_exposure_already_full():
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("2500"),
        max_total_exposure_pct=Decimal("0.25"),
        max_position_notional=_NO_CAP,
    )
    assert size == Decimal("0")


def test_position_size_is_zero_for_degenerate_zero_distance_stop():
    """Fail-closed: stop == entry ger odefinierat stop-avstånd, aldrig en gissad storlek."""
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("50000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("0"),
        max_total_exposure_pct=Decimal("0.25"),
        max_position_notional=_NO_CAP,
    )
    assert size == Decimal("0")


def test_position_size_at_new_full_exposure_default_leaves_room_for_many_positions():
    """PAPER-kapacitet (2026-09-04): med max_total_exposure_pct höjt till
    1.00 (100%, config/risk_limits.yaml) ryms betydligt fler samtidiga
    icke-nollstora positioner innan exponeringspoolen är slut, jämfört med
    den gamla 0.25 (2500 USDT)-gränsen - se dess kommentar i
    config/risk_limits.yaml för den fulla räkningen."""
    # max_exposure = 10000 * 1.00 = 10000. Med 9500 redan använt av tidigare
    # positioner finns fortfarande 500 kvar (jämför: med gamla 0.25 hade
    # redan 2500 räckt för att helt tömma poolen).
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("9500"),
        max_total_exposure_pct=Decimal("1.00"),
        max_position_notional=_NO_CAP,
    )
    assert size == Decimal("500")


def test_position_size_still_zero_when_new_full_exposure_pool_is_actually_exhausted():
    """`blocked_by_exposure` ska fortfarande kunna inträffa - bara när den
    NYA, högre poolen faktiskt är full, inte som en rutinmässig konsekvens
    av den gamla 2500 USDT-gränsen (explicit användarkrav)."""
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("10000"),
        max_total_exposure_pct=Decimal("1.00"),
        max_position_notional=_NO_CAP,
    )
    assert size == Decimal("0")


def test_position_size_capped_by_fixed_max_position_notional():
    """2026-09-05, explicit användarkrav: en enda trade med smalt
    stop-avstånd fick tidigare en så stor raw_size (t.ex. 2500-5000 USDT)
    att bara 2-5 trades kunde tömma hela den nya 10000 USDT-poolen, långt
    under målet 10-20 samtidiga positioner. Ett nytt, lägre per-trade-tak
    (max_position_notional_usdt i risk_limits.yaml) löser detta UTAN att
    röra risk_per_trade_pct eller exponeringslogiken - se dess kommentar
    för den fulla motiveringen. Här: 2% stop-avstånd hade utan taket gett
    5000 (samma som test_position_size_matches_hand_calculation ovan),
    men taket på 1000 klipper ner den."""
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("0"),
        max_total_exposure_pct=Decimal("1.00"),
        max_position_notional=Decimal("1000"),
    )
    assert size == Decimal("1000")


def test_position_size_fixed_cap_never_increases_size_beyond_risk_formula():
    """Taket är bara ett GOLV nedåt, aldrig ett tak uppåt utöver vad
    risk_per_trade_pct redan tillåter - ett brett stop-avstånd som redan
    ger en raw_size under 1000 ska vara opåverkat (risk_per_trade_pct-
    garantin, att aldrig riskera mer än 1% av kapitalet vid stop, får
    aldrig försvagas av det nya taket)."""
    # entry=50000, stop=45000 -> 10% stop-avstånd. risk_amount=100.
    # raw_size = 100 / 0.10 = 1000... choose a wider stop to go below 1000.
    # entry=50000, stop=40000 -> 20% stop-avstånd. raw_size = 100/0.20 = 500.
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("40000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("0"),
        max_total_exposure_pct=Decimal("1.00"),
        max_position_notional=Decimal("1000"),
    )
    assert size == Decimal("500")


def test_position_size_fixed_cap_still_respects_remaining_exposure():
    """Taket (1000) och exponeringspoolen samverkar - om bara 400 USDT
    återstår i poolen ska det vinna över både raw_size och det fasta taket."""
    size = compute_position_size(
        entry_price=Decimal("50000"),
        stop_loss_price=Decimal("49000"),
        capital=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.01"),
        open_positions_notional=Decimal("9600"),
        max_total_exposure_pct=Decimal("1.00"),
        max_position_notional=Decimal("1000"),
    )
    assert size == Decimal("400")
