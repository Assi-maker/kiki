"""P2 economic model (2026-09-28).

The paper book charged fees ONCE at close as size x fee_pct with fee_pct =
0.04 % - but a real BingX round trip costs 0.05 % taker per side = 0.10 %
(measured on every verified LIVE fill). The model understated costs 2.5x,
so paper results, GODFATHER counterfactuals and learning all looked better
than reality. Now:
- config/cost_model.yaml is the single documented cost model (entry fee,
  exit fee, round trip, stop slippage, version);
- risk_limits.fee_pct must equal its round trip (loader refuses otherwise);
- historical paper closes keep their stored v1 fees (never rewritten), but
  anything that LEARNS or REPORTS from them recosts with the current model
  and says so in its provenance;
- LIVE uses the exchange's actual entry and exit fees, stored separately,
  plus the actual stop slippage."""
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from crypto_trading.config.exceptions import ConfigError
from crypto_trading.config.loader import CostModelConfig, get_settings, validate_cost_model
from crypto_trading.paper_trading.execution import (
    compute_pnl,
    compute_pnl_with_cost_model,
    resolve_realized_pnl,
)
from crypto_trading.performance.metrics import trade_pnls
from crypto_trading.schemas.trade import Position

_NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _closed(fees: str, size="1000", entry="100", exit_="101", funding="0") -> Position:
    return Position(
        position_id="p", candidate_id="p", instrument="X-USDT", direction="LONG",
        status="CLOSED", theoretical_entry=D(entry), simulated_fill_entry=D(entry),
        stop_loss=D("95"), target=D("110"), size=D(size), fill_model_version="v1",
        opened_at=_NOW, theoretical_exit=D(exit_), simulated_fill_exit=D(exit_),
        exit_reason="target", fees=D(fees), funding=D(funding), closed_at=_NOW,
    )


def test_the_cost_model_is_the_measured_bingx_taker_cost():
    costs = get_settings().costs
    assert costs.entry_fee_pct == D("0.0005")
    assert costs.exit_fee_pct == D("0.0005")
    assert costs.round_trip_fee_pct == D("0.0010")
    assert costs.model_version == "v2-2026-09-28"


def test_paper_is_charged_the_real_round_trip():
    settings = get_settings()
    assert settings.risk_limits.fee_pct == settings.costs.round_trip_fee_pct


def test_the_loader_refuses_a_paper_fee_that_disagrees_with_the_cost_model():
    with pytest.raises(ConfigError):
        validate_cost_model(D("0.0004"), CostModelConfig())


def test_a_v1_paper_close_is_recosted_for_learning_and_reporting():
    position = _closed(fees="0.4000")  # 1000 x 0.0004 = the v1 model
    assert compute_pnl(position) == D("9.6000")  # the stored historical record
    assert compute_pnl_with_cost_model(position, CostModelConfig()) == D("9.0000")


def test_a_v2_paper_close_is_left_exactly_as_stored():
    position = _closed(fees="1.0000")
    assert compute_pnl_with_cost_model(position, CostModelConfig()) == compute_pnl(position)


def test_realized_pnl_provenance_says_when_a_paper_close_was_recosted():
    old = resolve_realized_pnl(_closed(fees="0.4000"), None, D("0.001"))
    assert old.pnl_usdt == D("9.0000")
    assert old.fees_source == "PAPER_MODEL_V1_RECOSTED"
    new = resolve_realized_pnl(_closed(fees="1.0000"), None, D("0.001"))
    assert new.pnl_usdt == D("9.0000")
    assert new.fees_source == "PAPER_MODEL"


def test_performance_metrics_use_the_current_cost_model():
    assert trade_pnls([_closed(fees="0.4000")]) == [D("9.0000")]
