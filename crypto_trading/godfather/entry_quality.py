"""Entry Quality Layer: the second opinion that is allowed to say REJECT.

Requirement 7, and requirement 1's first problem statement - "the system
picks too many weak signals alongside the good ones". The existing Gate
answers a binary question (does this signal pass the rules?). This layer
answers the one that actually decides profitability:

    signal -> quality -> expected edge -> risk/reward -> regime
    compatibility -> historical similarity -> contradiction -> expected
    cost -> TRADE / WAIT / REJECT

Three properties make it safe and make it honest:

**It can only ever subtract.** There is no path from this module to
opening a position. It produces an advisory verdict, recorded in its own
table with `enforced=False` in this phase. When a later, separately
approved activation lets it act, the only action it can take is to stop
a trade the old Gate already approved - never to start one the Gate
rejected.

**Its notion of "edge" is borrowed, never self-assessed.** The expected
edge comes from `experience.lookup_edge_class`, which will answer
`INSUFFICIENT_DATA` for almost everything in a young system. That is the
correct answer and it does NOT produce a REJECT: refusing to trade
because nothing is proven yet would freeze the system and starve it of
the very data it needs. Only a `FAILURE_PATTERN` - a robustly,
significantly losing pattern - rejects on edge grounds.

**Costs are counted before the trade, not after.** Expected round-trip
friction is subtracted from the expected move, so a signal whose whole
theoretical edge is smaller than its own spread and fees is visibly not
worth taking.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import RiskLimitsConfig
from crypto_trading.godfather.experience import lookup_edge_class
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.godfather import EdgeClass, EntryQualityAssessment, EntryVerdict

_ZERO = Decimal("0")

# A setup must offer at least this much reward per unit of risk. 1.0 is
# not ambitious - it is the point below which a better-than-coin-flip win
# rate is mandatory just to break even, and this system's measured win
# rate is not better than a coin flip.
_MIN_RISK_REWARD = Decimal("1.0")

# Verdict cut points on the 0-1 weighted-average quality score.
#
# THESE ARE NOT CALIBRATED, and that is stated here rather than hidden:
# no out-of-sample evidence yet says where the right cut is, so they are
# round numbers on a normalised mean and the layer ships advisory. The
# honest test is `backtest_entry_quality` / the report's
# `entry_quality_backtest` section - if the P/L it would remove is not
# clearly negative, this layer must NOT be enforced. Tuning these two
# numbers until the historical split looks good would be curve-fitting on
# 137 trades, which is exactly the failure mode this subsystem exists to
# prevent.
_REJECT_SCORE = 0.35
_TRADE_SCORE = 0.60

# Sub-score weights. History dominates ON PURPOSE: the deterministic
# contradiction checks below are cheap heuristics, while Experience
# Memory is the only component that has actually been tested for
# significance. Today history returns the neutral 0.5 for almost
# everything (INSUFFICIENT_DATA), which by design makes this layer close
# to inert - a system that has proven nothing yet has not earned the
# right to veto anything. As real EDGE/FAILURE_PATTERN rows accumulate,
# the same weights give the layer teeth with no code change at all.
_WEIGHTS: dict[str, float] = {
    "history": 0.35,
    "evidence": 0.20,
    "confirmation": 0.20,
    "contradiction": 0.10,
    "risk_reward": 0.10,
    "cost": 0.05,
}

# Conflict codes with a direct mechanical reading as "this entry is late
# or unconfirmed". The weights are relative WITHIN the contradiction
# sub-score only - none of them can reject on its own.
_CONFLICT_PENALTIES: dict[str, float] = {
    "entry_rsi_at_or_above_80": 0.30,
    "momentum_triggered_without_volume_confirmation": 0.20,
    "entered_on_below_average_volume": 0.20,
    "single_trigger_reason_only": 0.15,
    "confirmed_with_bullish_probability_below_0.35": 0.15,
}

# A setup whose theoretical move is less than this multiple of its own
# round-trip friction has no margin for slippage or for being slightly
# early. 3x is where costs stop dominating the arithmetic.
_COST_COVERAGE_TARGET = Decimal("3")


def expected_round_trip_cost_usdt(size: Decimal, risk_limits: RiskLimitsConfig) -> Decimal:
    """Spread + slippage on both legs, plus the fee, on the notional.

    The same components paper_trading/execution.py charges at close - so
    what this layer subtracts up front is what the system will actually
    pay, not a placeholder.
    """
    friction_pct = (risk_limits.spread_pct + risk_limits.slippage_pct) * Decimal("2")
    return size * (friction_pct + risk_limits.fee_pct)


def _conflict_score(conflicts: list[str]) -> float:
    """Total penalty weight of the conflicts present, capped at 1.0."""
    return round(min(1.0, sum(_CONFLICT_PENALTIES.get(c, 0.0) for c in conflicts)), 4)


def _history_subscore(edge_class: EdgeClass) -> float:
    """0.5 is neutral, and INSUFFICIENT_DATA sits exactly on it.

    "We have never seen enough of this" is not evidence against a setup.
    Scoring it below neutral would make the system permanently refuse to
    explore anything it has not already done - a guaranteed way never to
    collect the data it needs to know better.
    """
    return {
        "EDGE": 1.0,
        "WEAK_EDGE": 0.65,
        "REGIME_DEPENDENT": 0.5,
        "NOISE": 0.5,
        "INSUFFICIENT_DATA": 0.5,
        "DECAYING_EDGE": 0.35,
        "FAILURE_PATTERN": 0.0,
    }.get(edge_class, 0.5)


def _confirmation_subscore(features: dict[str, object]) -> float:
    """How many independent things agree, not how loud one of them is.

    The measured problem in this book is single-signal entries: the
    2026-09-25 sweep found 127 of 146 trades fired on exactly one trigger
    reason and 104 entered on below-average volume. Independent
    confirmation is the dimension that separates them, so it gets its own
    sub-score instead of being buried in a penalty list.
    """
    checks = [
        features.get("volume_triggered") is True,
        features.get("secondary_confirmed") == "yes",
        str(features.get("trigger_count", "1")) != "1",
        features.get("price_volatility_triggered") is True,
        features.get("momentum_triggered") is True,
    ]
    return round(sum(1 for check in checks if check) / len(checks), 4)


def _cost_subscore(expected_move_usdt: Decimal, cost_usdt: Decimal) -> float:
    if cost_usdt <= _ZERO:
        return 1.0
    coverage = expected_move_usdt / cost_usdt
    return float(min(Decimal("1"), max(_ZERO, coverage / _COST_COVERAGE_TARGET)))


def _risk_reward_subscore(risk_reward: Decimal | None) -> float:
    if risk_reward is None:
        return 0.5
    return float(min(Decimal("1"), max(_ZERO, risk_reward / Decimal("2"))))


def assess_entry_quality(
    candidate: Candidate,
    features: dict[str, object],
    conflicts: list[str],
    experience_patterns: list[dict],
    planned_entry: Decimal,
    stop_loss: Decimal,
    target: Decimal,
    size: Decimal,
    risk_limits: RiskLimitsConfig,
    now: datetime,
    run_id: str,
    regime_compatible: bool | None = None,
    experience_evidence: dict | None = None,
) -> EntryQualityAssessment:
    """One advisory verdict, with every reason recorded as a code.

    The score is a WEIGHTED AVERAGE of six sub-scores, each in [0, 1] -
    not a starting value with penalties subtracted from it. The
    difference is not cosmetic: an additive penalty scheme saturates, and
    the first version of this function did exactly that on real data -
    it rejected 113 of 146 historical trades and therefore discriminated
    nothing. An average keeps every dimension's contribution bounded and
    legible.

    Reason codes rather than prose, for the same reason the trade
    classification taxonomy is closed: a reason nobody can count is a
    reason nobody can validate, and this layer's own verdicts are meant
    to become Experience Memory input later.
    """
    reason_codes: list[str] = []

    edge_class, expected_expectancy, matched_ids = lookup_edge_class(
        experience_patterns, features
    )
    reason_codes.append(f"historical_edge_class:{edge_class}")
    if matched_ids:
        reason_codes.append(f"matched_patterns:{len(matched_ids)}")

    risk = abs(planned_entry - stop_loss)
    risk_reward = (abs(target - planned_entry) / risk) if risk != _ZERO else None
    if risk_reward is None:
        reason_codes.append("risk_reward_undefined")
    elif risk_reward < _MIN_RISK_REWARD:
        reason_codes.append("risk_reward_below_1")

    cost = expected_round_trip_cost_usdt(size, risk_limits)
    expected_move_usdt = (
        size * abs(target - planned_entry) / planned_entry if planned_entry != _ZERO else _ZERO
    )
    if expected_move_usdt <= cost:
        reason_codes.append("expected_move_does_not_cover_round_trip_cost")

    conflict_penalty = _conflict_score(conflicts)
    for code in conflicts:
        if code in _CONFLICT_PENALTIES:
            reason_codes.append(f"conflict:{code}")

    history = _history_subscore(edge_class)
    if experience_evidence is not None:
        # Graded experience (Fas 2): 0.5 + 0.5 x the confidence-weighted
        # signed evidence. NOISE / INSUFFICIENT_DATA patterns weigh 0 by
        # construction, so an unproven history stays exactly neutral; a
        # FAILURE_PATTERN keeps its unconditional REJECT below regardless.
        history = 0.5 + 0.5 * float(experience_evidence.get("signed_weight", 0.0))
        reason_codes.append(f"experience:{experience_evidence.get('verdict')}")
    subscores = {
        "history": history,
        "evidence": float(candidate.evidence_record.candidate_score),
        "confirmation": _confirmation_subscore(features),
        "contradiction": 1.0 - conflict_penalty,
        "risk_reward": _risk_reward_subscore(risk_reward),
        "cost": _cost_subscore(expected_move_usdt, cost),
    }
    score = sum(subscores[name] * weight for name, weight in _WEIGHTS.items())
    if regime_compatible is False:
        # A multiplicative haircut rather than a subtraction, so a bad
        # regime scales the whole assessment down instead of being able
        # to dominate it on its own.
        score *= 0.85
        reason_codes.append("regime_incompatible")
    score = round(max(0.0, min(1.0, score)), 4)

    verdict: EntryVerdict
    if edge_class == "FAILURE_PATTERN":
        # The only unconditional rejection. A robustly, significantly
        # losing pattern is the one thing this system can state with
        # evidence, and it is the cheapest edge available to it.
        verdict = "REJECT"
        reason_codes.append("reject:matches_known_failure_pattern")
    elif score < _REJECT_SCORE:
        verdict = "REJECT"
        reason_codes.append("reject:quality_below_floor")
    elif score < _TRADE_SCORE:
        verdict = "WAIT"
        reason_codes.append("wait:quality_below_trade_threshold")
    else:
        verdict = "TRADE"
        reason_codes.append("trade:quality_acceptable")

    return EntryQualityAssessment(
        candidate_id=candidate.candidate_id,
        instrument=candidate.instrument,
        assessed_at=now,
        verdict=verdict,
        quality_score=score,
        expected_edge_class=edge_class,
        expected_expectancy_usdt=expected_expectancy,
        risk_reward=risk_reward,
        regime_compatible=regime_compatible,
        conflict_score=conflict_penalty,
        expected_cost_usdt=cost,
        # Advisory in this phase, always. The flag is data, not code, so
        # a later activation is visible in the rows themselves.
        enforced=False,
        reason_codes=reason_codes,
        detail={
            "matched_pattern_ids": matched_ids,
            "expected_move_usdt": str(expected_move_usdt),
            "subscores": subscores,
            "weights": _WEIGHTS,
            "features": {k: str(v) for k, v in features.items()},
            "experience": (
                None if experience_evidence is None else {
                    "verdict": experience_evidence.get("verdict"),
                    "signed_weight": experience_evidence.get("signed_weight"),
                    "matched": [
                        {k: m[k] for k in ("pattern_id", "sample_size", "edge_class",
                                           "entry_quality_class", "confidence", "weight")}
                        for m in experience_evidence.get("matched", [])
                    ],
                    "similar_cases": experience_evidence.get("similar_cases"),
                }
            ),
        },
        run_id=run_id,
    )


def backtest_entry_quality(
    assessments: list[EntryQualityAssessment], outcomes_by_candidate: dict[str, Decimal]
) -> dict:
    """What would this layer have done to the realised book?

    Splits the real P/L by the verdict this layer would have given. The
    honest test of an entry filter is not how many bad trades it catches
    but what the trades it would have BLOCKED actually did - a filter
    that removes 40 USDT of losses and 90 USDT of wins has made the
    system worse, and only this split shows it.
    """
    buckets: dict[str, dict] = {}
    for assessment in assessments:
        pnl = outcomes_by_candidate.get(assessment.candidate_id)
        if pnl is None:
            continue
        bucket = buckets.setdefault(
            assessment.verdict, {"n": 0, "total_pnl_usdt": _ZERO, "wins": 0}
        )
        bucket["n"] += 1
        bucket["total_pnl_usdt"] += pnl
        if pnl > _ZERO:
            bucket["wins"] += 1
    traded = buckets.get("TRADE", {"n": 0, "total_pnl_usdt": _ZERO})
    blocked_pnl = sum(
        (data["total_pnl_usdt"] for verdict, data in buckets.items() if verdict != "TRADE"),
        _ZERO,
    )
    return {
        "by_verdict": {
            verdict: {
                "n": data["n"],
                "total_pnl_usdt": str(data["total_pnl_usdt"]),
                "win_rate": (data["wins"] / data["n"]) if data["n"] else None,
            }
            for verdict, data in sorted(buckets.items())
        },
        "pnl_if_only_traded_verdict_trade_usdt": str(traded["total_pnl_usdt"]),
        "pnl_removed_by_filtering_usdt": str(blocked_pnl),
    }
