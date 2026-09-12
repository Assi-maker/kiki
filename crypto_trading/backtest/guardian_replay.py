from __future__ import annotations

import json

from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.storage.repository import Repository


def copy_guardian_history(source_repo: Repository, backtest_repo: Repository, position_id: str) -> int:
    """Read-only against `source_repo` (production). Copies every real
    historical Guardian observation for this position into the backtest
    DB so close_triggered_positions()'s existing staleness-guarded lookup
    (position_closing.py) finds them naturally during replay, unmodified.
    No recomputation of decay factors - see module docstring in the plan
    for why that would duplicate guardian/deterministic.py's own tested
    logic outside its boundary."""
    observations = source_repo.find_guardian_observations_for_position(position_id)
    for row in observations:
        # The 'factors' field is stored as a JSON string in the database
        # and comes back as a string from SELECT *, so we need to parse it
        # before constructing the GuardianObservation model.
        if isinstance(row["factors"], str):
            row["factors"] = json.loads(row["factors"])
        backtest_repo.save_guardian_observation(GuardianObservation(**row))
    return len(observations)
