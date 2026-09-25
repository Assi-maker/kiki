"""Prediction Error Loop: EXPECTED / ACTUAL / ERROR / CAUSE / LESSON.

Requirement 6, and the one that makes the difference between a learning
system and what the user called "an expensive diary": every component
that commits to an expectation before a trade gets that expectation
scored against reality afterwards, in a fixed five-field shape that can
be counted, grouped and calibrated.

The critical constraint is the requirement's own last line - "this must
then be able to influence future decisions ONLY when there is enough
evidence". That is enforced here mechanically, not by good intentions:
every record carries `detail["actionable"]`, and it is True only when
Experience Memory has independently classified the corresponding pattern
as `EDGE` or `FAILURE_PATTERN`. A lesson learned from three trades is
recorded - it is data, and throwing it away would be worse - but it is
recorded as `OBSERVATION ONLY`, and nothing downstream is permitted to
act on it.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from crypto_trading.schemas.godfather import (
    DecisionAudit,
    PredictionErrorRecord,
    PredictionErrorSource,
    TradeInvestigation,
)

_ZERO = Decimal("0")
_ACTIONABLE_EDGE_CLASSES = ("EDGE", "FAILURE_PATTERN")

_OBSERVATION_PREFIX = "OBSERVATION ONLY (insufficient evidence to act): "


def _record_id(position_id: str, source: str) -> str:
    return hashlib.sha256(f"{position_id}:{source}".encode()).hexdigest()


def _lesson(text: str, actionable: bool) -> str:
    """One place where a lesson is turned into a sentence, so the
    'not yet actionable' marker can never be forgotten at a call site."""
    return text if actionable else f"{_OBSERVATION_PREFIX}{text}"


def _component(audit: DecisionAudit, name: str):
    return next((c for c in audit.components if c.component == name), None)


def _thesis_error(
    investigation: TradeInvestigation, audit: DecisionAudit, actionable: bool
) -> PredictionErrorRecord | None:
    """The trade's own thesis: we expected the entry signal to produce a
    tradeable move that we would convert into profit."""
    bull = _component(audit, "bull_thesis")
    during = investigation.during
    after = investigation.after
    pnl = after.get("realized_pnl_usdt")
    if pnl is None:
        return None

    mfe = during.get("mfe_pct")
    minutes_to_mfe = during.get("minutes_to_mfe")
    giveback = during.get("giveback_ratio")
    classification = investigation.classification

    expected = (
        bull.expectation
        if bull is not None and bull.expectation
        else "the entry signal continues into a tradeable move"
    )
    actual = (
        f"favourable excursion {mfe}% reached at minute {minutes_to_mfe}; "
        f"exit via {after.get('exit_reason')} at {pnl} USDT"
    )

    if classification in ("BAD_ENTRY", "FALSE_BREAKOUT"):
        error = "overestimated the probability that the signal would continue at all"
        cause = _cause_from(investigation, audit)
        lesson = (
            f"the {classification} shape recurs for this trigger combination - "
            "entry selectivity, not management, is what would have prevented it"
        )
    elif classification in ("EXIT_TOO_LATE", "GOOD_ENTRY_BAD_MANAGEMENT"):
        error = (
            "the direction was predicted correctly; what was misjudged was how long "
            f"the move would hold (gave back {giveback} of it)"
        )
        cause = _cause_from(investigation, audit)
        lesson = (
            "for this shape the binding constraint is exit timing, not signal quality - "
            "a management rule, not a stricter entry filter, is the lever"
        )
    elif classification == "GOOD_ENTRY_GOOD_MANAGEMENT":
        error = "none material: expectation and outcome agree"
        cause = "signal, regime and management were aligned"
        lesson = "this combination is worth reinforcing if it survives significance testing"
    else:
        error = f"outcome classified {classification}, which the thesis did not anticipate"
        cause = _cause_from(investigation, audit)
        lesson = "not yet a recognised failure mode for this trigger combination"

    magnitude = None
    try:
        magnitude = float(Decimal(str(pnl)))
    except (TypeError, ValueError):
        magnitude = None

    return PredictionErrorRecord(
        prediction_error_id=_record_id(investigation.position_id, "trade_thesis"),
        position_id=investigation.position_id,
        source="trade_thesis",
        created_at=investigation.created_at,
        expected=expected[:800],
        actual=actual[:800],
        error=error[:800],
        cause=cause[:800],
        lesson=_lesson(lesson, actionable)[:800],
        magnitude=magnitude,
        detail={
            "classification": classification,
            "actionable": actionable,
            "entry_verdict": investigation.entry_verdict,
            "management_verdict": investigation.management_verdict,
        },
        run_id=investigation.run_id,
    )


def _cause_from(investigation: TradeInvestigation, audit: DecisionAudit) -> str:
    """Cause from observed conditions, never from narrative invention.

    Built out of three already-computed, already-structured sources: the
    decay factors that were sustained through the trade, the pre-entry
    conflicts the auditor counted, and which weighted components turned
    out to be wrong. When none of those fired, the honest answer is that
    no specific cause is identifiable - which is said plainly rather than
    filled in with a plausible-sounding story.
    """
    parts: list[str] = []
    sustained = investigation.during.get("sustained_decay_factors") or {}
    elevated = [name for name, value in sustained.items() if value]
    if elevated:
        parts.append("sustained " + ", ".join(sorted(elevated)))
    if audit.conflicts:
        parts.append("pre-entry conflicts: " + ", ".join(audit.conflicts[:4]))
    if audit.misleading_components:
        parts.append("misled by: " + ", ".join(audit.misleading_components))
    if audit.missing_information:
        parts.append("missing at decision time: " + ", ".join(audit.missing_information[:4]))
    if not parts:
        return "no specific cause identifiable from the recorded evidence"
    return "; ".join(parts)


def _forecast_error(
    investigation: TradeInvestigation, audit: DecisionAudit, actionable: bool
) -> PredictionErrorRecord | None:
    """Calibration, not just correctness.

    `magnitude` is `1 - p(realized)`: the probability mass the forecaster
    put on things that did not happen. Averaged over many trades this is
    the forecaster's calibration error, which is the only way to tell a
    forecaster that is genuinely informative from one that is merely
    often directionally lucky.
    """
    forecast = _component(audit, "forecast")
    if forecast is None or forecast.verdict == "UNSCORABLE":
        return None
    realized = forecast.evidence.get("realized_scenario")
    assigned = forecast.evidence.get("probability_assigned_to_realized")
    if realized is None:
        return None
    magnitude = None if assigned is None else round(1.0 - float(assigned), 4)
    return PredictionErrorRecord(
        prediction_error_id=_record_id(investigation.position_id, "forecast_agent"),
        position_id=investigation.position_id,
        source="forecast_agent",
        created_at=investigation.created_at,
        expected=forecast.expectation[:800],
        actual=f"realized scenario '{realized}'",
        error=(
            f"forecast was {forecast.verdict.lower()}; it assigned p={assigned} to what "
            "actually happened"
        ),
        cause=_cause_from(investigation, audit),
        lesson=_lesson(
            "forecast calibration for this scenario shape should be tracked before any "
            "decision is allowed to weight it",
            actionable,
        )[:800],
        magnitude=magnitude,
        detail={
            "scenario_probabilities": forecast.evidence.get("scenario_probabilities"),
            "realized_scenario": realized,
            "actionable": actionable,
        },
        run_id=investigation.run_id,
    )


def _risk_error(
    investigation: TradeInvestigation, audit: DecisionAudit, actionable: bool
) -> PredictionErrorRecord | None:
    risk = _component(audit, "risk")
    if risk is None or risk.verdict == "UNSCORABLE":
        return None
    during = investigation.during
    return PredictionErrorRecord(
        prediction_error_id=_record_id(investigation.position_id, "risk_agent"),
        position_id=investigation.position_id,
        source="risk_agent",
        created_at=investigation.created_at,
        expected=f"stop at {risk.evidence.get('stop_loss')}, target at "
        f"{risk.evidence.get('target')}"[:800],
        actual=(
            f"adverse excursion {during.get('mae_pct')}%, favourable excursion "
            f"{during.get('mfe_pct')}%, max progress toward target "
            f"{during.get('max_progress_ratio')}"
        )[:800],
        error=f"risk placement graded {risk.verdict.lower()} ({investigation.classification})",
        cause=_cause_from(investigation, audit),
        lesson=_lesson(
            "stop/target distances for this instrument and trigger shape are the lever, "
            "not the entry decision itself",
            actionable,
        )[:800],
        magnitude=None,
        detail={"classification": investigation.classification, "actionable": actionable},
        run_id=investigation.run_id,
    )


def build_prediction_errors(
    investigation: TradeInvestigation,
    audit: DecisionAudit,
    actionable_sources: set[PredictionErrorSource] | None = None,
) -> list[PredictionErrorRecord]:
    """Every scorable expectation for one closed trade.

    `actionable_sources` is supplied by `pipeline.py` from Experience
    Memory's own verdicts - a source appears there only when a pattern
    covering this trade reached EDGE or FAILURE_PATTERN. Nothing in this
    module can grant itself that flag.
    """
    actionable = actionable_sources or set()
    records = [
        _thesis_error(investigation, audit, "trade_thesis" in actionable),
        _forecast_error(investigation, audit, "forecast_agent" in actionable),
        _risk_error(investigation, audit, "risk_agent" in actionable),
    ]
    return [record for record in records if record is not None]


def summarise_prediction_errors(rows: list[dict]) -> dict[str, dict]:
    """Per-source aggregate: how wrong, how often, and is it improving?

    The forecast row's `mean_magnitude` is a genuine calibration score
    (probability mass placed on things that did not happen, averaged);
    lower is better and 0.5 is roughly what a three-way coin flip
    produces.
    """
    summary: dict[str, dict] = {}
    for row in rows:
        source = str(row.get("source"))
        bucket = summary.setdefault(
            source, {"n": 0, "actionable": 0, "magnitudes": [], "observation_only": 0}
        )
        bucket["n"] += 1
        lesson = str(row.get("lesson") or "")
        if lesson.startswith(_OBSERVATION_PREFIX):
            bucket["observation_only"] += 1
        else:
            bucket["actionable"] += 1
        magnitude = row.get("magnitude")
        if magnitude is not None:
            bucket["magnitudes"].append(float(magnitude))
    for bucket in summary.values():
        magnitudes = bucket.pop("magnitudes")
        bucket["mean_magnitude"] = (
            round(sum(magnitudes) / len(magnitudes), 4) if magnitudes else None
        )
    return summary
