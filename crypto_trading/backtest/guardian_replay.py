from __future__ import annotations

import json
from datetime import datetime

from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.storage.repository import Repository


def copy_guardian_history(
    source_repo: Repository, backtest_repo: Repository, position_id: str, up_to: datetime | None = None
) -> int:
    """Read-only against `source_repo` (production). Copies real
    historical Guardian observations for this position into the backtest
    DB so close_triggered_positions()'s existing staleness-guarded lookup
    (position_closing.py) finds them naturally during replay, unmodified.
    No recomputation of decay factors - see module docstring in the plan
    for why that would duplicate guardian/deterministic.py's own tested
    logic outside its boundary.

    `up_to`: when given, only observations with `observed_at <= up_to` are
    copied - the staleness guard in `find_latest_guardian_observation`/
    `_guardian_state_for` has no UPPER bound on `observed_at` (only a
    "not too old" check), so copying a position's entire history up front
    would let a Guardian observation dated AFTER the candle currently
    being replayed leak backwards and influence that candle's exit
    decision - a genuine look-ahead violation. Callers doing a tick-by-
    tick replay must pass `up_to=<current candle's observed_at>` on every
    tick so `backtest_repo` only ever contains what would have been
    visible at that point in time. `None` (the default) preserves the
    original copy-everything behavior for callers that aren't doing a
    time-bounded replay (e.g. Task 4's own existing tests)."""
    observations = source_repo.find_guardian_observations_for_position(position_id)
    if up_to is not None:
        observations = [o for o in observations if datetime.fromisoformat(o["observed_at"]) <= up_to]
    for row in observations:
        # The 'factors' field is stored as a JSON string in the database
        # and comes back as a string from SELECT *, so we need to parse it
        # before constructing the GuardianObservation model.
        if isinstance(row["factors"], str):
            row["factors"] = json.loads(row["factors"])
        backtest_repo.save_guardian_observation(GuardianObservation(**row))
    return len(observations)
