from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from crypto_trading.config.loader import GuardianConfig, RiskLimitsConfig, Settings
from crypto_trading.paper_trading.execution import (
    FILL_MODEL_VERSION,
    compute_fees,
    compute_fill_price,
    compute_funding,
    compute_pnl,
)
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

# Pre-registered hypotheses under test (2026-09-11, spec G9). Frozen for
# the duration of this experiment - not a tuning parameter, not read from
# config/YAML/env. Changing this set is a source-code change requiring the
# same review as any other logic change, never a deploy-time toggle.
FROZEN_THRESHOLDS_PCT: tuple[Decimal, ...] = (Decimal("0.010"), Decimal("0.015"))

_DIRECTION = "LONG"


def _guardian_state_for(
    repo: Repository, position_id: str, now: datetime, guardian_config: GuardianConfig
) -> str | None:
    """Read-only DUPLICATE of position_closing.py::close_triggered_positions's
    own staleness-guarded Guardian read (spec G4) - deliberately duplicated,
    not shared, so position_closing.py (baseline exit logic) stays
    completely untouched. See
    test_guardian_state_lookup_matches_close_triggered_positions_exactly in
    Task 9 for the parity proof against the real function."""
    if not guardian_config.assisted_exit_enabled:
        return None
    latest_observation = repo.find_latest_guardian_observation(position_id)
    if latest_observation is None:
        return None
    observed_at = datetime.fromisoformat(latest_observation["observed_at"])
    staleness_limit = timedelta(seconds=2 * guardian_config.check_interval_seconds)
    if now - observed_at <= staleness_limit:
        return latest_observation["state"]
    return None
