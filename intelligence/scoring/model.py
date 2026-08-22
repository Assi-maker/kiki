from __future__ import annotations

from pathlib import Path

import yaml

from intelligence.schemas.opportunity import Opportunity


def load_weights(path: Path) -> dict[str, float]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def score_opportunity(
    opportunity: Opportunity, weights: dict[str, float]
) -> tuple[float, dict[str, float]]:
    breakdown = {
        "signal_strength": _signal_strength(opportunity),
        "data_quality": _data_quality(opportunity),
        "source_reliability": _source_reliability(opportunity),
        "potential": _potential(opportunity),
        "risk": _risk(opportunity),
        "confidence": _confidence(opportunity),
        "novelty": _novelty(opportunity),
    }
    total = sum(weights[k] * breakdown[k] for k in weights)
    return total, breakdown


def _signal_strength(opp: Opportunity) -> float:
    # Fler oberoende scenarier med hög sannolikhet = starkare signal.
    if not opp.forecast or not opp.forecast.scenarios:
        return 0.0
    return min(1.0, max((s.get("probability", 0.0) for s in opp.forecast.scenarios), default=0.0))


def _data_quality(opp: Opportunity) -> float:
    # Fler verifierade fakta + kilder = högre datakvalitet,
    # capat vid 5 för att undvika obegränsad skalning.
    if not opp.research:
        return 0.0
    facts = len(opp.research.verified_facts)
    sources = len(opp.research.source_references)
    return min(1.0, (facts + sources) / 10)


def _source_reliability(opp: Opportunity) -> float:
    # Fas 1: statisk approximation via antal kilder (egen reliability-agent kommer senare fas).
    if not opp.research:
        return 0.0
    return min(1.0, len(opp.research.source_references) / 5)


def _potential(opp: Opportunity) -> float:
    # Upside potential, discounted by how sure the forecast is (not just raw probability).
    if not opp.forecast or not opp.forecast.scenarios:
        return 0.0
    max_probability = max((s.get("probability", 0.0) for s in opp.forecast.scenarios), default=0.0)
    return max_probability * opp.forecast.confidence


def _risk(opp: Opportunity) -> float:
    # Högre score = LÄGRE risk (så att det kan viktas positivt tillsammans med övriga komponenter).
    if not opp.risk or not opp.bear:
        return 0.0
    counterarguments_penalty = min(1.0, len(opp.bear.counterarguments) / 5)
    return max(0.0, 1.0 - counterarguments_penalty)


def _confidence(opp: Opportunity) -> float:
    if not opp.forecast:
        return 0.0
    return opp.forecast.confidence


def _novelty(opp: Opportunity) -> float:
    # Fas 1: proxy via deviation-relaterad text i opportunity.summary — konservativ default.
    return 0.5 if opp.opportunity is not None else 0.0
