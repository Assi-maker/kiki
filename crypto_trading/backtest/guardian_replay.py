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
    decision - a genuine look-ahead violation. `None` (the default)
    preserves the original copy-everything behavior.

    NOTE (review round 2): `replay_engine.py`'s own tick loop no longer
    calls this function per-candle with a growing `up_to` - that was
    correct for no-look-ahead but O(candles x observations) (re-reading
    and re-filtering the ENTIRE source history, and re-attempting an
    INSERT OR IGNORE for every already-copied row, on every single tick).
    It now uses a one-shot fetch + local watermark pointer instead (see
    `replay_engine.py::replay_position`), reusing `_row_to_observation`
    below for the identical per-row construction logic. This function
    itself is kept, `up_to` included, for callers that want a single
    bounded copy without hand-rolling the watermark loop (and for this
    module's own existing tests)."""
    observations = source_repo.find_guardian_observations_for_position(position_id)
    if up_to is not None:
        observations = [o for o in observations if datetime.fromisoformat(o["observed_at"]) <= up_to]
    for row in observations:
        backtest_repo.save_guardian_observation(_row_to_observation(row))
    return len(observations)


def _row_to_observation(row: dict) -> GuardianObservation:
    """Shared by `copy_guardian_history` above and `replay_engine.py`'s
    incremental watermark-based copy (review round 2) - the exact same
    row -> model construction, including the `factors` JSON-string
    parsing (SELECT * returns it as a string; GuardianObservation wants
    a dict), must stay identical in both places. Exported despite the
    leading underscore - deliberate, documented cross-module reuse
    within `backtest/`, the same convention already used for
    `profit_protection_experiment.py::_guardian_state_for`/`_shadow_id`.
    Mutates `row["factors"]` in place (parses it once) - callers that
    pass the same row dict twice would double-parse harmlessly (json.loads
    is a no-op on an already-dict value guard below), but no caller here
    does that."""
    if isinstance(row["factors"], str):
        row["factors"] = json.loads(row["factors"])
    return GuardianObservation(**row)
