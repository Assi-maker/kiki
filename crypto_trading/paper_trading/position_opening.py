from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from crypto_trading.config.loader import RiskLimitsConfig
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.execution import FILL_MODEL_VERSION, compute_fill_price
from crypto_trading.paper_trading.position_sizing import compute_position_size
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

# LONG-only i denna fas (PLAN_CRYPTO_PHASE4.md beslut 1): de sju AI-rollerna
# har bara en Bull/Thesis Agent, ingen självständig kort-tes-genererande roll.
_DIRECTION = "LONG"


def open_position_for_candidate(
    candidate: Candidate,
    repo: Repository,
    risk_limits: RiskLimitsConfig,
    reference_price: Decimal,
    opened_at: datetime,
    run_id: str,
) -> Position | None:
    """SPEC §8.6: idempotent - position_id = candidate_id, en candidate kan
    bara nå CONFIRMED en gång (terminal state, Fas 0), så ett andra anrop
    för samma candidate returnerar den redan skapade positionen istället
    för att skapa en ny."""
    if candidate.status != "CONFIRMED" or candidate.risk is None:
        return None

    existing = repo.get_position(candidate.candidate_id)
    if existing is not None:
        return existing

    # Bugfix 2026-09-01: crypto-risk-agent's context (orchestrator.py::
    # _build_context()) never includes the actual current price - only
    # relative evidence (pct_change/RSI/zscore/funding). When it has no
    # absolute price to anchor to, it can legitimately answer with a
    # qualitative/relative description ("ca 3-4% under senaste pris...")
    # instead of a bare number, even though this field is typed `str`
    # specifically to allow that flexibility elsewhere. A CONFIRMED
    # candidate whose suggested_stop_loss/suggested_target isn't a clean
    # number must never crash the whole discovery tick (and, with it, every
    # other candidate's already-completed analysis in the same tick) -
    # same "one candidate's bad data never aborts the batch" principle as
    # every other per-candidate boundary in this codebase (see e.g.
    # ConnectorUnavailableError handling in market_snapshot.py). Root cause
    # (giving the risk agent an actual reference price to anchor to) is a
    # separate, larger change - not rushed here.
    try:
        stop_loss = Decimal(candidate.risk.suggested_stop_loss)
        target = Decimal(candidate.risk.suggested_target)
    except InvalidOperation:
        log_event(
            run_id,
            event="position_open_skipped_non_numeric_risk_values",
            candidate_id=candidate.candidate_id,
            instrument=candidate.instrument,
            suggested_stop_loss=candidate.risk.suggested_stop_loss,
            suggested_target=candidate.risk.suggested_target,
        )
        return None

    open_positions_notional = repo.sum_open_positions_notional()
    size = compute_position_size(
        entry_price=reference_price,
        stop_loss_price=stop_loss,
        capital=risk_limits.starting_capital_usdt,
        risk_per_trade_pct=risk_limits.risk_per_trade_pct,
        open_positions_notional=open_positions_notional,
        max_total_exposure_pct=risk_limits.max_total_exposure_pct,
    )
    simulated_fill_entry = compute_fill_price(
        reference_price, _DIRECTION, risk_limits.spread_pct, risk_limits.slippage_pct, "entry"
    )

    position = Position(
        position_id=candidate.candidate_id,
        candidate_id=candidate.candidate_id,
        instrument=candidate.instrument,
        direction=_DIRECTION,
        status="OPEN_POSITION",
        theoretical_entry=reference_price,
        simulated_fill_entry=simulated_fill_entry,
        stop_loss=stop_loss,
        target=target,
        size=size,
        fill_model_version=FILL_MODEL_VERSION,
        opened_at=opened_at,
    )
    event = Event(
        event_id=f"POSITION_OPENED:{position.position_id}",
        event_type="POSITION_OPENED",
        aggregate_type="position",
        aggregate_id=position.position_id,
        occurred_at=opened_at,
        run_id=run_id,
        schema_version=1,
        payload={"instrument": position.instrument, "size": str(size)},
    )
    repo.create_position_with_event(position, event)
    return position
