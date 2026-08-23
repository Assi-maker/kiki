from __future__ import annotations

from pathlib import Path

import yaml

from intelligence.schemas.opportunity import Opportunity

_EXPECTED_WEIGHT_KEYS = frozenset(
    {
        "signal_strength",
        "data_quality",
        "source_reliability",
        "potential",
        "risk",
        "confidence",
        "novelty",
    }
)


def load_weights(path: Path) -> dict[str, float]:
    with open(path, encoding="utf-8") as f:
        weights = yaml.safe_load(f)

    # Finding #7: validate domain shape so a YAML typo fails loudly (ValueError)
    # instead of crashing later with a bare KeyError inside score_opportunity's
    # `weights[k]` lookup, or — worse — silently dropping a component if an
    # extra/misspelled key just gets ignored.
    actual_keys = set(weights.keys())
    missing = _EXPECTED_WEIGHT_KEYS - actual_keys
    extra = actual_keys - _EXPECTED_WEIGHT_KEYS
    if missing or extra:
        problems = []
        if missing:
            problems.append(f"saknade nycklar: {sorted(missing)}")
        if extra:
            problems.append(f"okända/extra nycklar: {sorted(extra)}")
        raise ValueError(
            f"scoring_weights.yaml har fel nyckeluppsättning ({'; '.join(problems)}); "
            f"förväntade exakt: {sorted(_EXPECTED_WEIGHT_KEYS)}"
        )

    total_weight = sum(weights.values())
    if abs(total_weight - 1.0) >= 0.01:
        raise ValueError(
            f"scoring_weights.yaml:s vikter summerar till {total_weight}, förväntat ~1.0"
        )

    return weights


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
    # Fler verifierade fakta + källor = högre datakvalitet,
    # capat vid 10 (facts + sources) för att undvika obegränsad skalning.
    if not opp.research:
        return 0.0
    facts = len(opp.research.verified_facts)
    sources = len(opp.research.source_references)
    return min(1.0, (facts + sources) / 10)


def _source_reliability(opp: Opportunity) -> float:
    # Fas 1: statisk approximation via antal källor (egen reliability-agent kommer senare fas).
    if not opp.research:
        return 0.0
    return min(1.0, len(opp.research.source_references) / 5)


def _potential(opp: Opportunity) -> float:
    # Upside potential, discounted by how sure the forecast is (not just raw probability).
    if not opp.forecast or not opp.forecast.scenarios:
        return 0.0
    max_probability = max((s.get("probability", 0.0) for s in opp.forecast.scenarios), default=0.0)
    # `probability` is an untyped dict value (ForecastAssessment.scenarios is
    # list[dict]) — an LLM can hand back a value outside [0, 1]. Clamp the same
    # way _signal_strength() already does, so one bad scenario can't push this
    # component (or the weighted total) outside its documented [0, 1] range.
    return min(1.0, max(0.0, max_probability * opp.forecast.confidence))


def _risk(opp: Opportunity) -> float:
    # Högre score = LÄGRE risk (så att det kan viktas positivt tillsammans med övriga komponenter).
    #
    # Fas 1 begränsning: RiskAssessment:s egna fält (downside, liquidity_risk,
    # model_risk, timing_risk) är fri text utan strukturerad/numerisk signal —
    # det finns inget ärligt sätt att räkna ut en risk-siffra ur dem utan att
    # låtsas att textinnehållet betyder något det inte nödvändigtvis gör (t.ex.
    # skulle "fler ifyllda fält" INTE betyda lägre risk — en välskriven risk-
    # bedömning som beskriver allvarliga risker är fortfarande hög risk). Denna
    # funktion tar därför bara sitt (enda numeriska) signal från bear.counter-
    # arguments — fler motargument från Fact-Checker/Bear-agenten = högre
    # bedömd risk. `opp.risk` krävs ändå (via gaten i state_machine.py) men
    # dess fält läses medvetet inte in numeriskt i Fas 1.
    if not opp.risk or not opp.bear:
        return 0.0
    counterarguments_penalty = min(1.0, len(opp.bear.counterarguments) / 5)
    return max(0.0, 1.0 - counterarguments_penalty)


def _confidence(opp: Opportunity) -> float:
    if not opp.forecast:
        return 0.0
    return opp.forecast.confidence


def _novelty(opp: Opportunity) -> float:
    # Fas 1-platshållare: läser INTE opportunity.summary eller någon deviation-
    # relaterad text. Returnerar bara en fast 0.5 när OpportunityAssessment
    # finns — och eftersom detta körs efter QA-gaten (som redan kräver alla 7
    # assessments), är opp.opportunity i praktiken alltid satt här. Den här
    # komponenten är alltså i praktiken en konstant i Fas 1, inte en riktig
    # novelty-signal. En riktig novelty-agent/algoritm är en framtida fas.
    return 0.5 if opp.opportunity is not None else 0.0
