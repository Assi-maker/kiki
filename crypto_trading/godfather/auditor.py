"""GODFATHER Decision Auditor: who said what, and who turned out right.

Requirement 3, as structured data. For every investigated trade this
reconstructs each pipeline component's pre-entry position and grades it
against what the price actually did:

    Quant Screener - Opportunity Screener - Technical - Bull - Bear -
    Forecast - Risk - QA - Gate - Guardian

Three rules keep this honest rather than merely opinionated:

**A component is only graded on a commitment it actually made.** QA
checks schema consistency, not market direction, so QA is UNSCORABLE by
construction and says so - not silently counted as right whenever the
trade won. The News/Sentiment role that found no instrument-specific
facts is likewise unscorable, and instead contributes to
`missing_information`. Grading a component on a claim it never made is
how a scoreboard becomes noise.

**Right/wrong is measured against the price path, not against P/L.** A
bullish component that correctly predicted a 2% favourable move was
RIGHT even if the trade lost, because the loss then belongs to position
management. That separation is what makes `fault_domain` meaningful, and
it is the direct answer to requirement 3's last question: "was this a
problem in signal selection or in position management?"

**Conflicts are counted, not resolved.** The auditor records that Bear
raised five counterarguments and the Forecast's most likely scenario was
bearish while the Gate still confirmed. Whether that pattern actually
predicts anything is `experience.py`'s job to test, with sample sizes -
never this module's job to assert.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from crypto_trading.godfather.path import PathMetrics
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.godfather import (
    ComponentStance,
    ComponentVerdict,
    ComponentVerdictValue,
    DecisionAudit,
    FaultDomain,
    QualityVerdict,
)
from crypto_trading.schemas.trade import Position

_ZERO = Decimal("0")

# Same two thresholds the investigator uses, and for the same reason: a
# favourable move under 0.3% never cleared round-trip friction, and one
# at or above 1.0% was a genuinely tradeable move.
_NOISE_EXCURSION_PCT = Decimal("0.3")
_REAL_MOVE_PCT = Decimal("1.0")

# A forecast scenario is "bullish"/"bearish" only outside this band. The
# band is the same 1.0% real-move threshold, so a forecast and a
# component stance are graded against one definition of "the price went
# somewhere", never two.
_SCENARIO_BAND_PCT = Decimal("1.0")


def _grade_directional(metrics: PathMetrics, bullish: bool) -> ComponentVerdictValue:
    """Grade a directional call against the real favourable excursion.

    UNSCORABLE in the middle band on purpose: between "never moved" and
    "moved properly" there is a region where the call was neither
    vindicated nor refuted, and forcing a verdict there manufactures
    signal out of nothing.
    """
    if metrics.point_count == 0 or metrics.mfe_pct is None:
        return "UNSCORABLE"
    moved = metrics.mfe_pct >= _REAL_MOVE_PCT
    flat = metrics.mfe_pct < _NOISE_EXCURSION_PCT
    if bullish:
        if moved:
            return "RIGHT"
        if flat:
            return "WRONG"
        return "UNSCORABLE"
    if flat:
        return "RIGHT"
    if moved:
        return "WRONG"
    return "UNSCORABLE"


def _grade_adverse(metrics: PathMetrics) -> ComponentVerdictValue:
    """Grade a downside WARNING against the real adverse excursion.

    The Bear role's commitment is "this setup carries material downside",
    not "this price will fall" - see .claude/agents/
    crypto-bear-adversarial.md, where its presence is a process
    requirement and a positive outcome is explicitly not its goal.
    Grading it against MFE (as the first version of this module did)
    scored it on a claim it never made, which is why its scoreboard came
    out at 31 RIGHT / 67 WRONG on the first real sweep while saying
    nothing about whether its warnings had substance.

    RIGHT when a 1%+ adverse excursion actually materialised; WRONG only
    when the price never went meaningfully against the position at all.
    """
    if metrics.point_count == 0 or metrics.mae_pct is None:
        return "UNSCORABLE"
    if metrics.mae_pct <= -_REAL_MOVE_PCT:
        return "RIGHT"
    if metrics.mae_pct > -_NOISE_EXCURSION_PCT:
        return "WRONG"
    return "UNSCORABLE"


def _realized_return_pct(position: Position) -> Decimal | None:
    if position.simulated_fill_exit is None or position.simulated_fill_entry == _ZERO:
        return None
    sign = Decimal("-1") if position.direction == "SHORT" else Decimal("1")
    return (
        sign
        * (position.simulated_fill_exit - position.simulated_fill_entry)
        / position.simulated_fill_entry
        * Decimal("100")
    )


def _realized_scenario(position: Position) -> str | None:
    realized = _realized_return_pct(position)
    if realized is None:
        return None
    if realized >= _SCENARIO_BAND_PCT:
        return "bullish"
    if realized <= -_SCENARIO_BAND_PCT:
        return "bearish"
    return "neutral"


def _quant_screener_verdict(candidate: Candidate | None, metrics: PathMetrics) -> ComponentVerdict:
    if candidate is None:
        return ComponentVerdict(
            component="quant_screener",
            stance="UNKNOWN",
            expectation="unavailable: the candidate record is missing",
            verdict="UNSCORABLE",
        )
    evidence = candidate.evidence_record
    triggers = list(evidence.trigger_reasons)
    return ComponentVerdict(
        component="quant_screener",
        stance="BULLISH" if triggers else "NEUTRAL",
        expectation=(
            f"deterministic triggers {triggers or '[]'} at score "
            f"{evidence.candidate_score} imply a tradeable move"
        ),
        verdict=_grade_directional(metrics, bullish=bool(triggers)),
        weight=float(evidence.candidate_score),
        evidence={
            "candidate_score": evidence.candidate_score,
            "trigger_reasons": triggers,
            "data_quality_status": evidence.data_quality_status,
        },
    )


def _opportunity_screener_verdict(
    screen: dict | None, metrics: PathMetrics
) -> ComponentVerdict:
    if screen is None:
        return ComponentVerdict(
            component="opportunity_screener",
            stance="UNKNOWN",
            expectation="unavailable: this candidate predates the cheap pre-screen",
            verdict="UNSCORABLE",
        )
    score = float(screen.get("opportunity_score") or 0.0)
    # The screener scores 0-10 and never makes a trade call (see
    # .claude/agents/crypto-opportunity-screener.md). Only the two ends
    # of its range are a commitment worth grading; the middle is exactly
    # the "worth a closer look" shrug it is designed to emit.
    if score >= 7.0:
        return ComponentVerdict(
            component="opportunity_screener",
            stance="BULLISH",
            expectation=f"opportunity_score {score} - clearly worth the full analysis",
            verdict=_grade_directional(metrics, bullish=True),
            weight=score / 10.0,
            evidence={"opportunity_score": score},
        )
    if score <= 3.0:
        return ComponentVerdict(
            component="opportunity_screener",
            stance="BEARISH",
            expectation=f"opportunity_score {score} - thin, one-sided evidence",
            verdict=_grade_directional(metrics, bullish=False),
            weight=score / 10.0,
            evidence={"opportunity_score": score},
        )
    return ComponentVerdict(
        component="opportunity_screener",
        stance="NEUTRAL",
        expectation=f"opportunity_score {score} - no commitment either way",
        verdict="UNSCORABLE",
        weight=score / 10.0,
        evidence={"opportunity_score": score},
    )


def _technical_verdict(candidate: Candidate | None, metrics: PathMetrics) -> ComponentVerdict:
    technical = getattr(candidate, "technical", None) if candidate else None
    if technical is None:
        return ComponentVerdict(
            component="technical",
            stance="UNKNOWN",
            expectation="unavailable",
            verdict="UNSCORABLE",
        )
    market = dict(getattr(technical, "market_data", {}) or {})
    triggered = [
        key
        for key, value in market.items()
        if isinstance(value, dict) and value.get("triggered") is True
    ]
    return ComponentVerdict(
        component="technical",
        stance="BULLISH" if triggered else "NEUTRAL",
        expectation=f"market structure triggers: {triggered or '[]'}",
        verdict=_grade_directional(metrics, bullish=bool(triggered)),
        weight=float(len(triggered)),
        evidence={"triggered_metrics": triggered},
    )


def _bull_verdict(candidate: Candidate | None, metrics: PathMetrics) -> ComponentVerdict:
    bull = getattr(candidate, "bull_thesis", None) if candidate else None
    if bull is None:
        return ComponentVerdict(
            component="bull_thesis",
            stance="UNKNOWN",
            expectation="unavailable",
            verdict="UNSCORABLE",
        )
    return ComponentVerdict(
        component="bull_thesis",
        stance="BULLISH",
        expectation=(getattr(bull, "hypothesis", "") or "")[:400],
        verdict=_grade_directional(metrics, bullish=True),
        weight=1.0,
        evidence={"catalyst": (getattr(bull, "catalyst", "") or "")[:300]},
    )


def _bear_verdict(candidate: Candidate | None, metrics: PathMetrics) -> ComponentVerdict:
    bear = getattr(candidate, "bear_adversarial", None) if candidate else None
    if bear is None:
        return ComponentVerdict(
            component="bear_adversarial",
            stance="UNKNOWN",
            expectation="unavailable",
            verdict="UNSCORABLE",
        )
    counterarguments = list(getattr(bear, "counterarguments", []) or [])
    if not counterarguments:
        return ComponentVerdict(
            component="bear_adversarial",
            stance="NEUTRAL",
            expectation="no counterargument raised",
            verdict="UNSCORABLE",
            evidence={"counterargument_count": 0},
        )
    return ComponentVerdict(
        component="bear_adversarial",
        stance="BEARISH",
        expectation=counterarguments[0][:400],
        verdict=_grade_adverse(metrics),
        weight=float(len(counterarguments)),
        evidence={
            "counterargument_count": len(counterarguments),
            "adverse_excursion_pct": (
                str(metrics.mae_pct) if metrics.mae_pct is not None else None
            ),
        },
    )


def _forecast_verdict(candidate: Candidate | None, position: Position) -> ComponentVerdict:
    forecast = getattr(candidate, "forecast", None) if candidate else None
    if forecast is None:
        return ComponentVerdict(
            component="forecast",
            stance="UNKNOWN",
            expectation="unavailable",
            verdict="UNSCORABLE",
        )
    probabilities = dict(getattr(forecast, "scenario_probabilities", {}) or {})
    if not probabilities:
        return ComponentVerdict(
            component="forecast",
            stance="UNKNOWN",
            expectation="no scenario probabilities recorded",
            verdict="UNSCORABLE",
        )
    top_scenario, top_probability = max(probabilities.items(), key=lambda kv: kv[1])
    realized = _realized_scenario(position)
    stance: ComponentStance = {
        "bullish": "BULLISH",
        "bearish": "BEARISH",
    }.get(top_scenario, "NEUTRAL")
    if realized is None:
        verdict: ComponentVerdictValue = "UNSCORABLE"
    else:
        verdict = "RIGHT" if realized == top_scenario else "WRONG"
    return ComponentVerdict(
        component="forecast",
        stance=stance,
        expectation=(
            f"most likely scenario '{top_scenario}' at p={top_probability} "
            f"over {getattr(forecast, 'horizon', 'unspecified horizon')}"
        ),
        verdict=verdict,
        weight=float(top_probability),
        evidence={
            "scenario_probabilities": probabilities,
            "realized_scenario": realized,
            # The probability this forecast assigned to what actually
            # happened. This is the raw material for calibration - a
            # forecaster that is "right" often but assigns 0.34 to it is
            # not a good forecaster, and only this number shows that.
            "probability_assigned_to_realized": (
                probabilities.get(realized) if realized is not None else None
            ),
        },
    )


def _risk_verdict(
    candidate: Candidate | None, classification: str, position: Position
) -> ComponentVerdict:
    risk = getattr(candidate, "risk", None) if candidate else None
    if risk is None:
        return ComponentVerdict(
            component="risk",
            stance="UNKNOWN",
            expectation="unavailable",
            verdict="UNSCORABLE",
        )
    # The Risk role advises the stop/target that the position actually
    # carried, so it is graded on whether that placement survived contact
    # with the market - never on the trade's direction, which is not its
    # job.
    if classification in ("SL_TOO_WIDE", "TARGET_TOO_FAR"):
        verdict: ComponentVerdictValue = "WRONG"
    elif (position.exit_reason or "").lower() == "target":
        verdict = "RIGHT"
    else:
        verdict = "UNSCORABLE"
    return ComponentVerdict(
        component="risk",
        stance="NEUTRAL",
        expectation=(getattr(risk, "suggested_stop_loss", "") or "")[:300],
        verdict=verdict,
        weight=1.0,
        evidence={
            "stop_loss": str(position.stop_loss),
            "target": str(position.target),
            "classification": classification,
        },
    )


def _qa_verdict(candidate: Candidate | None) -> ComponentVerdict:
    qa = getattr(candidate, "qa", None) if candidate else None
    if qa is None:
        return ComponentVerdict(
            component="qa",
            stance="UNKNOWN",
            expectation="unavailable",
            verdict="UNSCORABLE",
        )
    passed = bool(getattr(qa, "passed", False))
    return ComponentVerdict(
        component="qa",
        stance="PASS" if passed else "FAIL",
        expectation="schema completeness and internal consistency only",
        # Structurally unscorable, and that is a correct statement about
        # QA rather than a gap: QA makes no claim about the market, so no
        # market outcome can confirm or refute it.
        verdict="UNSCORABLE",
        weight=0.0,
        evidence={"passed": passed, "violations": list(getattr(qa, "violations", []) or [])},
    )


def _gate_verdict(
    gate_decision: dict | None, realized_pnl: Decimal | None
) -> ComponentVerdict:
    if gate_decision is None:
        return ComponentVerdict(
            component="gate",
            stance="UNKNOWN",
            expectation="unavailable",
            verdict="UNSCORABLE",
        )
    decision = str(gate_decision.get("decision"))
    if realized_pnl is None:
        verdict: ComponentVerdictValue = "UNSCORABLE"
    else:
        verdict = "RIGHT" if realized_pnl > _ZERO else "WRONG"
    return ComponentVerdict(
        component="gate",
        stance="PASS" if decision == "CONFIRMED" else "FAIL",
        expectation=f"gate decision {decision}",
        # The Gate is the one component graded on money rather than on
        # the price path: it is the component that decided to spend
        # capital, so the capital outcome is exactly its commitment.
        verdict=verdict,
        weight=1.0,
        evidence={"decision": decision, "reasons": gate_decision.get("reasons")},
    )


def _guardian_verdict(metrics: PathMetrics, realized_pnl: Decimal | None) -> ComponentVerdict:
    """Did Guardian's deterioration signal fire in time to have mattered?

    RIGHT means it flagged the position as questionable measurably before
    the stop paid for it. WRONG means a losing trade ran its whole life
    without Guardian ever flagging anything. That is a real, gradeable
    commitment - Guardian's decay score is a claim about the position's
    health, and a losing position it called healthy throughout is a
    falsified claim.
    """
    if metrics.point_count == 0 or realized_pnl is None:
        return ComponentVerdict(
            component="guardian",
            stance="UNKNOWN",
            expectation="no observed path or no scorable outcome",
            verdict="UNSCORABLE",
        )
    flagged = metrics.first_questionable_minutes
    if realized_pnl > _ZERO:
        return ComponentVerdict(
            component="guardian",
            stance="NEUTRAL",
            expectation="position health monitoring on a winning trade",
            verdict="UNSCORABLE",
            evidence={"first_questionable_minutes": flagged},
        )
    if flagged is None:
        return ComponentVerdict(
            component="guardian",
            stance="NEUTRAL",
            expectation="decay score stayed below the WATCH threshold throughout",
            verdict="WRONG",
            weight=1.0,
            evidence={"first_questionable_minutes": None},
        )
    stop_minutes = metrics.minutes_to_sl_touch or metrics.last_observed_minutes
    early = stop_minutes is not None and (stop_minutes - flagged) >= 30
    return ComponentVerdict(
        component="guardian",
        stance="BEARISH",
        expectation=f"flagged deterioration at minute {flagged:.0f}",
        verdict="RIGHT" if early else "UNSCORABLE",
        weight=1.0,
        evidence={
            "first_questionable_minutes": flagged,
            "first_invalid_minutes": metrics.first_invalid_minutes,
            "loss_realized_at_minutes": stop_minutes,
        },
    )


def _news_verdict(candidate: Candidate | None) -> ComponentVerdict:
    news = getattr(candidate, "news_sentiment", None) if candidate else None
    if news is None:
        return ComponentVerdict(
            component="news_sentiment",
            stance="UNKNOWN",
            expectation="unavailable",
            verdict="UNSCORABLE",
        )
    facts = list(getattr(news, "verified_facts", []) or [])
    return ComponentVerdict(
        component="news_sentiment",
        stance="NEUTRAL",
        expectation="separates verified facts from source claims; makes no directional call",
        verdict="UNSCORABLE",
        weight=0.0,
        evidence={"verified_fact_count": len(facts)},
    )


def detect_conflicts(candidate: Candidate | None, gate_decision: dict | None) -> list[str]:
    """Structured, countable conflict codes - not prose.

    Each code is a condition that was observable BEFORE entry and that a
    later Experience Memory sweep can group trades by. Nothing here
    asserts that a conflict is bad; it asserts only that it was present.
    """
    conflicts: list[str] = []
    if candidate is None:
        return conflicts

    bear = getattr(candidate, "bear_adversarial", None)
    counterargument_count = len(getattr(bear, "counterarguments", []) or [])
    if counterargument_count >= 3:
        conflicts.append(f"bear_raised_{counterargument_count}_counterarguments")

    forecast = getattr(candidate, "forecast", None)
    probabilities = dict(getattr(forecast, "scenario_probabilities", {}) or {})
    if probabilities:
        top_scenario = max(probabilities.items(), key=lambda kv: kv[1])[0]
        confirmed = gate_decision is not None and gate_decision.get("decision") == "CONFIRMED"
        if confirmed and top_scenario != "bullish":
            conflicts.append(f"confirmed_while_forecast_most_likely_{top_scenario}")
        if probabilities.get("bullish", 0.0) < 0.35 and confirmed:
            conflicts.append("confirmed_with_bullish_probability_below_0.35")

    evidence = candidate.evidence_record
    momentum = evidence.momentum_breakout_evidence
    volume = evidence.volume_evidence
    if momentum.triggered and not volume.triggered:
        conflicts.append("momentum_triggered_without_volume_confirmation")
    if momentum.triggered and momentum.value >= 80:
        conflicts.append("entry_rsi_at_or_above_80")
    if volume.value is not None and volume.value < 0:
        conflicts.append("entered_on_below_average_volume")
    if len(evidence.trigger_reasons) <= 1:
        conflicts.append("single_trigger_reason_only")
    return conflicts


def detect_missing_information(candidate: Candidate | None) -> list[str]:
    """What the decision did NOT have - requirement 3's "which important
    information was missing?"."""
    missing: list[str] = []
    if candidate is None:
        missing.append("candidate_record_unavailable")
        return missing

    evidence = candidate.evidence_record
    if evidence.data_quality_status != "ok":
        missing.append(f"data_quality_{evidence.data_quality_status}")
    if evidence.secondary_timeframe_evidence is None:
        missing.append("no_secondary_timeframe_evidence")

    news = getattr(candidate, "news_sentiment", None)
    if news is not None:
        facts = list(getattr(news, "verified_facts", []) or [])
        instrument_root = candidate.instrument.split("-")[0].lower()
        if not any(instrument_root in fact.lower() for fact in facts):
            missing.append("no_instrument_specific_news_facts")

    risk = getattr(candidate, "risk", None)
    if risk is not None:
        suggested = str(getattr(risk, "suggested_stop_loss", "") or "")
        if not any(char.isdigit() for char in suggested):
            missing.append("risk_agent_gave_no_numeric_stop_loss")

    if candidate.reference_price is None:
        missing.append("no_reference_price_for_risk_anchoring")
    return missing


def determine_fault_domain(
    entry_verdict: QualityVerdict, management_verdict: QualityVerdict
) -> FaultDomain:
    """Requirement 3's closing question, decided from the two quality
    verdicts the investigator already produced rather than re-derived -
    so the audit and the post-mortem can never disagree with each
    other."""
    if entry_verdict == "UNKNOWN" or management_verdict == "UNKNOWN":
        return "UNKNOWN"
    entry_bad = entry_verdict == "BAD"
    management_bad = management_verdict == "BAD"
    if entry_bad and management_bad:
        return "BOTH"
    if entry_bad:
        return "SIGNAL_SELECTION"
    if management_bad:
        return "POSITION_MANAGEMENT"
    return "NEITHER"


def audit_decision(
    position: Position,
    candidate: Candidate | None,
    opportunity_screen: dict | None,
    gate_decision: dict | None,
    metrics: PathMetrics,
    realized_pnl: Decimal | None,
    classification: str,
    entry_verdict: QualityVerdict,
    management_verdict: QualityVerdict,
    now: datetime,
    run_id: str,
) -> DecisionAudit:
    components = [
        _quant_screener_verdict(candidate, metrics),
        _opportunity_screener_verdict(opportunity_screen, metrics),
        _technical_verdict(candidate, metrics),
        _bull_verdict(candidate, metrics),
        _bear_verdict(candidate, metrics),
        _forecast_verdict(candidate, position),
        _risk_verdict(candidate, classification, position),
        _qa_verdict(candidate),
        _news_verdict(candidate),
        _gate_verdict(gate_decision, realized_pnl),
        _guardian_verdict(metrics, realized_pnl),
    ]
    # "Misleading" is deliberately narrower than "wrong": a component is
    # misleading only when it was wrong AND carried weight into the
    # decision. A zero-weight component that was wrong misled nobody.
    misleading = [
        component.component
        for component in components
        if component.verdict == "WRONG" and component.weight > 0.0
    ]
    return DecisionAudit(
        position_id=position.position_id,
        candidate_id=position.candidate_id,
        created_at=now,
        components=components,
        conflicts=detect_conflicts(candidate, gate_decision),
        misleading_components=misleading,
        missing_information=detect_missing_information(candidate),
        fault_domain=determine_fault_domain(entry_verdict, management_verdict),
        run_id=run_id,
    )
