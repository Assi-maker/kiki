"""Read-only Guardian Authority decision/calibration report (Task 11,
controller-added - closes a gap between the Guardian Authority design
spec's own "Acceptance criteria" section, which requires a report showing
decision counts/resolution/calibration accuracy per type, and the original
10-task plan, which never built one). See
.superpowers/sdd/2026-09-14-guardian-authority/task-11-brief.md.

Modeled directly on the existing precedent
`crypto_trading/performance/profit_protection_report.py`: `build_report(repo)
-> dict` + a `main()` CLI entry point. Never writes any decision or
heuristic data, never started by run.py - run manually:
`python -m crypto_trading.performance.guardian_authority_report`.
(Constructing `SQLiteRepository` does run init_schema's idempotent `CREATE
TABLE IF NOT EXISTS`/`INSERT OR IGNORE schema_version` - harmless, and the
same property the precedent file above already names precisely rather than
the stronger "never writes to the DB".)

Pure downstream consumer of already-written data, exactly like
profit_protection_report.py is for Profit Protection: this module must
never import, and must never be imported by,
`crypto_trading/guardian/authority.py`, `authority_live.py`, or `tick.py`.
Only `find_*` repository methods are called anywhere in this module - no
write path exists here."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from crypto_trading.config.loader import get_settings
from crypto_trading.storage.repository import Repository, SQLiteRepository

_DECISION_TYPES = ("PRE_ENTRY_VETO", "TIGHTEN_SL", "CLOSE_EARLY")

# Task 8/9's own established reasoning (guardian/authority.py,
# resolve_pending_decisions docstring) for why these two types never carry
# a real calibration rate - reused verbatim in spirit, not re-litigated
# here.
_CALIBRATION_NOTE_BY_TYPE = {
    "PRE_ENTRY_VETO": (
        "PRE_ENTRY_VETO decisions never resolve (0 RESOLVED always, by "
        "construction): a veto's correctness is about a counterfactual "
        "(what would have happened had the candidate NOT been vetoed) "
        "that this system has no market-data infrastructure to evaluate, "
        "so expectation_correct is never set and no calibration rate can "
        "ever be computed for this type."
    ),
    "CLOSE_EARLY": (
        "CLOSE_EARLY decisions resolve, but expectation_correct is always "
        "None: CLOSE_EARLY predicts that continuing to hold would have "
        "gone unfavorably (a counterfactual of inaction), but the "
        "realized P/L recorded at resolution measures the outcome of the "
        "close itself, not that counterfactual - so no calibration rate "
        "can be computed for this type."
    ),
}

_NO_TIGHTEN_SL_DATA_YET_NOTE = (
    "No TIGHTEN_SL decisions have been scored yet (no resolved decisions "
    "of this type with a non-null expectation_correct AND a genuine "
    "applied intervention - intervention_applied is True)."
)


def _decision_type_entry(rows: list[dict], decision_type: str) -> dict:
    n_pending = sum(1 for d in rows if d["outcome_status"] == "PENDING")
    n_resolved = sum(1 for d in rows if d["outcome_status"] == "RESOLVED")
    entry: dict = {
        "n_total": len(rows),
        "n_pending": n_pending,
        "n_resolved": n_resolved,
    }

    if decision_type in _CALIBRATION_NOTE_BY_TYPE:
        entry["calibration_note"] = _CALIBRATION_NOTE_BY_TYPE[decision_type]
        return entry

    # TIGHTEN_SL - the only type for which expectation_correct is ever
    # non-null (Task 8/9's own ruling). Only compute real figures where a
    # row is both scored (non-null expectation_correct) AND a genuine
    # applied intervention (intervention_applied is True, added by the I2
    # hardening fix) - excludes decisions that were never actually applied
    # to the live stop-loss, e.g. a failed write. SQLite round-trips a
    # stored True as Python int 1, not the True singleton, hence bool(...)
    # rather than `is True`. Otherwise show why not, rather than a
    # misleading 0%/bare null.
    scored = [
        d
        for d in rows
        if d.get("expectation_correct") is not None and bool(d.get("intervention_applied"))
    ]
    if scored:
        n_correct = sum(1 for d in scored if d["expectation_correct"])
        entry["n_scored"] = len(scored)
        entry["n_correct"] = n_correct
        entry["win_rate"] = n_correct / len(scored)
        # I3 hardening fix (2026-09-15): a genuine Brier score, computed
        # entirely from already-persisted data - zero new schema. For
        # TIGHTEN_SL, expected_direction is a documented constant
        # ("favorable"), so expectation_correct == actual_favorable
        # exactly, letting brier_component reduce to
        # (confidence - (1.0 if expectation_correct else 0.0)) ** 2 per
        # decision. 0 = perfect calibration, 0.25 = uninformative constant
        # baseline, 1 = maximally miscalibrated. Evaluates whether the
        # VARYING confidence signal is well-calibrated, not just whether
        # TIGHTEN_SL wins on average (win_rate above). Same filtered
        # population as win_rate, per the controller's population-
        # consistency ruling - both describe the same set of decisions.
        entry["brier_score"] = (
            sum(
                (d["confidence"] - (1.0 if d["expectation_correct"] else 0.0)) ** 2
                for d in scored
            )
            / len(scored)
        )
    else:
        entry["calibration_note"] = _NO_TIGHTEN_SL_DATA_YET_NOTE

    return entry


def build_report(repo: Repository) -> dict:
    pending = repo.find_pending_guardian_authority_decisions()
    resolved = repo.find_resolved_guardian_authority_decisions()
    all_decisions = pending + resolved

    decision_types = {
        decision_type: _decision_type_entry(
            [d for d in all_decisions if d["decision_type"] == decision_type],
            decision_type,
        )
        for decision_type in _DECISION_TYPES
    }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "decision_types": decision_types,
        "active_heuristics_count": len(repo.find_guardian_authority_heuristics()),
    }


def main() -> None:
    settings = get_settings()
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    report = build_report(repo)
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
