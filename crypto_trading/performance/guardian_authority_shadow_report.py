"""Read-only Guardian Authority Shadow/Observation Mode report (Task 9 of
docs/superpowers/sdd/2026-09-15-guardian-authority-shadow/task-9-brief.md).

Modeled directly on the real (non-shadow) Guardian Authority's own report,
`crypto_trading/performance/guardian_authority_report.py`: `build_report(repo)
-> dict` + a `main()` CLI entry point. Never writes any shadow/heuristic
data, never started by run.py - run manually:
`python -m crypto_trading.performance.guardian_authority_shadow_report`.
(Constructing `SQLiteRepository` does run init_schema's idempotent `CREATE
TABLE IF NOT EXISTS`/`INSERT OR IGNORE schema_version` - harmless, same
property `guardian_authority_report.py` already names precisely rather than
the stronger "never writes to the DB".)

Pure downstream consumer of already-written shadow data, exactly like
`guardian_authority_report.py` is for the real decisions table: this module
must never import, and must never be imported by,
`crypto_trading/guardian/authority.py`, `authority_live.py`, `tick.py`, or
`crypto_trading/paper_trading/guardian_authority_shadow.py` (the shadow
module itself - the only relationship that would ever make sense is this
report reading what the shadow module already wrote, via the Repository
interface, and even that never happens: this module talks only to
`Repository.find_*`, never imports `guardian_authority_shadow.py`'s
functions directly). Only `find_*` repository methods are called anywhere
in this module - no write path exists here.

Section shapes:

- `tick_time_shadow`: one entry per `NO_ACTION`/`TIGHTEN_SL`/`CLOSE_EARLY`,
  read from `guardian_authority_shadow_observations`
  (`find_open_guardian_authority_shadows` + `find_resolved_guardian_
  authority_shadows` + `find_abandoned_guardian_authority_shadows`). The
  table has FOUR statuses, not three: `OBSERVING`, `DECIDED`, `RESOLVED`,
  and `ABANDONED` (set by `abandon_guardian_authority_shadow` when a
  shadow's real position vanishes from `open_positions` mid-observation -
  see `guardian_authority_shadow.py::run_guardian_authority_shadow_tick`'s
  stranded-shadow handling; this is a real, production-reachable path, not
  a theoretical one). Task 9 fix round 1: an earlier version of this
  module and this docstring claimed every row mapped to exactly one of
  three pending/resolved buckets with "no row ever left uncounted" - that
  was false, ABANDONED rows were silently excluded from every count.
  Corrected shape: every row maps to EXACTLY ONE of the three pending/
  resolved buckets below, OR is counted separately via that same type's
  own `n_abandoned` - no row is ever left out of the report:
    * a row still `OBSERVING` (no hypothetical decision registered yet) is
      "pending NO_ACTION" - the only state that type can be pending in,
      since `shadow_decision = 'NO_ACTION'` is only ever set at
      resolution (`resolve_guardian_authority_shadow_no_action`), never
      while open.
    * a row `DECIDED` always carries `shadow_decision` in
      `{'TIGHTEN_SL', 'CLOSE_EARLY'}` (paper_trading/guardian_authority_
      shadow.py::advance_shadow returns early on `NO_ACTION` before ever
      calling `decide_guardian_authority_shadow`) - "pending TIGHTEN_SL"/
      "pending CLOSE_EARLY" respectively.
    * a `RESOLVED` row always carries a `shadow_decision` (`NO_ACTION` via
      `resolve_guardian_authority_shadow_no_action`, or whatever it was
      `DECIDED` as, via `resolve_guardian_authority_shadow_decided`) -
      "resolved <type>".
    * an `ABANDONED` row (fires from either `OBSERVING` or `DECIDED`, and
      never touches `shadow_decision` - see `abandon_guardian_authority_
      shadow`'s own WHERE clause) is counted under `n_abandoned` on
      exactly one type's entry: `shadow_decision IS NULL` (abandoned while
      still `OBSERVING`, no decision was ever registered) counts under
      `NO_ACTION`'s `n_abandoned`; a non-NULL `shadow_decision` (abandoned
      after `DECIDED`, before it could ever resolve) counts under that
      decision type's own `n_abandoned`. `n_abandoned` is reported
      separately from `n_total`/`n_pending`/`n_resolved` - an abandoned
      row was never a real pending or resolved outcome, and is never
      folded into those counts, but it is always visible.
  For `TIGHTEN_SL` specifically, once resolved+scored rows exist:
  `n_scored`/`n_correct`/`win_rate`/`brier_score`/`brier_score_note`/
  `n_distinct_positions` - same formulas as the real report's own
  TIGHTEN_SL section (I3-hardened Brier score), computed over this
  table's resolved rows. No `intervention_applied`-style filter is
  applied or needed here: Task 4's design guarantees exactly one row per
  position (shadow_id = position_id, 1:1, no per-tick duplication, no
  "was it actually applied to a live order" question - shadow decisions
  are never applied anywhere), same reasoning Task 8's own
  `update_shadow_heuristics_from_resolved_shadow_observations` already
  established for this exact table (see that function's own docstring).

- `pre_entry_shadow`: one entry per `APPROVE`/`PRE_ENTRY_VETO`, read from
  `guardian_authority_shadow_pre_entry_observations`
  (`find_pending_guardian_authority_pre_entry_shadows` +
  `find_resolved_guardian_authority_pre_entry_shadows`). No win_rate/
  brier_score for either type: per Task 7's own ruling (repository.py's
  `resolve_guardian_authority_pre_entry_shadow` docstring),
  `expectation_correct` stays NULL forever for every row of this table,
  for both `APPROVE` and `PRE_ENTRY_VETO` alike - same reasoning the real
  Guardian Authority's own `PRE_ENTRY_VETO` ruling already established
  (no market-data infrastructure exists to evaluate the counterfactual of
  a candidate that was never opened).

- `active_shadow_heuristics_count`: `len(repo.find_guardian_authority_
  shadow_heuristics())` - the self-critique-from-shadow-data table (Task
  8), completely separate from the real `guardian_authority_heuristics`
  table the real decision engine reads.

- `generated_at`: `datetime.now(UTC).isoformat()`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from crypto_trading.config.loader import get_settings
from crypto_trading.storage.repository import Repository, SQLiteRepository

_TICK_SHADOW_DECISION_TYPES = ("NO_ACTION", "TIGHTEN_SL", "CLOSE_EARLY")
_PRE_ENTRY_SHADOW_DECISION_TYPES = ("APPROVE", "PRE_ENTRY_VETO")

_NO_ACTION_SHADOW_NOTE = (
    "NO_ACTION shadow rows never resolve with a scorable expectation "
    "(by construction: 'no action' means GODFATHER never registered a "
    "hypothetical decision for this position at all, so there is nothing "
    "to compare an outcome against) - no calibration rate can ever be "
    "computed for this type."
)
_CLOSE_EARLY_SHADOW_NOTE = (
    "CLOSE_EARLY shadow rows resolve, but expectation_correct is always "
    "None: CLOSE_EARLY predicts that continuing to hold the (paper) "
    "position would have gone unfavorably (a counterfactual of "
    "inaction), but the realized P/L recorded at resolution measures the "
    "outcome of the position's actual close, not that counterfactual - "
    "same reasoning the real (non-shadow) Guardian Authority's own "
    "CLOSE_EARLY ruling already established - so no calibration rate can "
    "be computed for this type."
)
_TICK_SHADOW_CALIBRATION_NOTE_BY_TYPE = {
    "NO_ACTION": _NO_ACTION_SHADOW_NOTE,
    "CLOSE_EARLY": _CLOSE_EARLY_SHADOW_NOTE,
}

_NO_TIGHTEN_SL_SHADOW_DATA_YET_NOTE = (
    "No TIGHTEN_SL shadow decisions have been scored yet (no resolved "
    "shadow rows of this type with a non-null expectation_correct)."
)

# Same I3-hardening reasoning as guardian_authority_report.py's own
# _BRIER_SCORE_NOTE, adapted to shadow language: `confidence` here is
# still a signal-strength score, not a probability of a favorable
# outcome, once the shadow self-critique heuristics (Task 8) start
# feeding non-default confidence values back into
# paper_trading/guardian_authority_shadow.py::advance_shadow's call to
# the real (unmodified) decide_open_position.
_BRIER_SCORE_NOTE = (
    "brier_score's forecast variable is `confidence` (a signal-strength "
    "score, as computed by the real decide_open_position/evaluate_"
    "heuristics logic this shadow module reuses unmodified), NOT a "
    "probability of a favorable outcome - it does not mean what a "
    "textbook Brier score's forecast probability means. A genuinely "
    "useful heuristic can have confidence well below 1.0, so a "
    "low-but-not-near-zero brier_score can still represent a genuinely "
    "useful, well-calibrated heuristic - do not read this value as "
    "'closer to 0 is always better' without also reading win_rate/"
    "n_scored alongside it."
)

_PRE_ENTRY_SHADOW_NOTE = (
    "Pre-entry shadow decisions never carry a real expectation_correct "
    "to score (expectation_correct stays NULL forever for every row of "
    "this table, both APPROVE and PRE_ENTRY_VETO alike): a hypothetical "
    "pre-entry veto's correctness is about a counterfactual (what would "
    "have happened had the candidate been opened/not opened) that this "
    "system has no market-data infrastructure to evaluate - same "
    "reasoning the real (non-shadow) Guardian Authority's own "
    "PRE_ENTRY_VETO ruling already established - so no calibration rate "
    "can ever be computed for either type."
)


def _tick_time_shadow_type_entry(
    rows: list[dict], abandoned_rows: list[dict], decision_type: str
) -> dict:
    if decision_type == "NO_ACTION":
        pending = [d for d in rows if d["status"] == "OBSERVING"]
        # abandon_guardian_authority_shadow never touches shadow_decision
        # (see its own WHERE clause) - a row abandoned while still
        # OBSERVING never reached a decision, so shadow_decision is still
        # NULL. That is this type's own abandoned bucket, same as how
        # OBSERVING is this type's own pending bucket above.
        abandoned = [d for d in abandoned_rows if d["shadow_decision"] is None]
    else:
        pending = [
            d for d in rows if d["status"] == "DECIDED" and d["shadow_decision"] == decision_type
        ]
        # A row abandoned after being DECIDED still carries the real
        # shadow_decision decide_guardian_authority_shadow set (abandon
        # never touches it) - counted under that same type here.
        abandoned = [d for d in abandoned_rows if d["shadow_decision"] == decision_type]
    resolved = [
        d for d in rows if d["status"] == "RESOLVED" and d["shadow_decision"] == decision_type
    ]

    entry: dict = {
        "n_total": len(pending) + len(resolved),
        "n_pending": len(pending),
        "n_resolved": len(resolved),
        # Task 9 fix round 1: reported separately, never folded into
        # n_total/n_pending/n_resolved above - an abandoned row was never
        # a real pending or resolved outcome (same "separate, not silent"
        # precedent as profit_protection_report.py's own n_abandoned).
        "n_abandoned": len(abandoned),
    }

    if decision_type in _TICK_SHADOW_CALIBRATION_NOTE_BY_TYPE:
        entry["calibration_note"] = _TICK_SHADOW_CALIBRATION_NOTE_BY_TYPE[decision_type]
        return entry

    # decision_type == "TIGHTEN_SL" - the only type for which
    # expectation_correct is ever non-null (resolve_guardian_authority_
    # shadow_decided sets a real value for TIGHTEN_SL, always None for
    # CLOSE_EARLY - see that function's own docstring). No
    # intervention_applied-style filter: unlike the real
    # guardian_authority_decisions table, this table has no such column
    # and needs none - see module docstring. (ABANDONED rows are excluded
    # here on purpose, same as they're excluded from `resolved` above -
    # they are counted, visibly, via n_abandoned instead, never silently.)
    scored = [d for d in resolved if d.get("expectation_correct") is not None]
    if scored:
        n_correct = sum(1 for d in scored if d["expectation_correct"])
        entry["n_scored"] = len(scored)
        entry["n_correct"] = n_correct
        entry["win_rate"] = n_correct / len(scored)
        entry["brier_score"] = (
            sum(
                (d["confidence"] - (1.0 if d["expectation_correct"] else 0.0)) ** 2
                for d in scored
            )
            / len(scored)
        )
        entry["brier_score_note"] = _BRIER_SCORE_NOTE
        entry["n_distinct_positions"] = len({d["position_id"] for d in scored})
    else:
        entry["calibration_note"] = _NO_TIGHTEN_SL_SHADOW_DATA_YET_NOTE

    return entry


def _pre_entry_shadow_type_entry(rows: list[dict], decision_type: str) -> dict:
    matching = [d for d in rows if d["shadow_decision"] == decision_type]
    n_pending = sum(1 for d in matching if d["status"] == "PENDING")
    n_resolved = sum(1 for d in matching if d["status"] == "RESOLVED")
    return {
        "n_total": len(matching),
        "n_pending": n_pending,
        "n_resolved": n_resolved,
        "calibration_note": _PRE_ENTRY_SHADOW_NOTE,
    }


def build_report(repo: Repository) -> dict:
    tick_rows = repo.find_open_guardian_authority_shadows() + repo.find_resolved_guardian_authority_shadows()
    abandoned_rows = repo.find_abandoned_guardian_authority_shadows()
    tick_time_shadow = {
        decision_type: _tick_time_shadow_type_entry(tick_rows, abandoned_rows, decision_type)
        for decision_type in _TICK_SHADOW_DECISION_TYPES
    }

    pre_entry_rows = (
        repo.find_pending_guardian_authority_pre_entry_shadows()
        + repo.find_resolved_guardian_authority_pre_entry_shadows()
    )
    pre_entry_shadow = {
        decision_type: _pre_entry_shadow_type_entry(pre_entry_rows, decision_type)
        for decision_type in _PRE_ENTRY_SHADOW_DECISION_TYPES
    }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "tick_time_shadow": tick_time_shadow,
        "pre_entry_shadow": pre_entry_shadow,
        "active_shadow_heuristics_count": len(repo.find_guardian_authority_shadow_heuristics()),
    }


def main() -> None:
    settings = get_settings()
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    report = build_report(repo)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
