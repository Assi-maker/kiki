from __future__ import annotations

from pathlib import Path

from intelligence.schemas.opportunity import Opportunity


def render_report(opportunity: Opportunity) -> str:
    forecast_scenarios = opportunity.forecast.scenarios if opportunity.forecast else []
    # `scenarios` is an untyped list[dict] on ForecastAssessment — a schema-valid
    # LLM response can use different keys (e.g. {"scenario": ..., "prob": ...}).
    # Never strict-index it; fall back defensively, matching the convention
    # scoring/model.py already uses (s.get("probability", 0.0)).
    scenarios_lines = "\n".join(
        f"- {s.get('description', '?')}: {s.get('probability', 0.0):.0%}"
        for s in forecast_scenarios
    )
    bear_counterargs = opportunity.bear.counterarguments if opportunity.bear else []
    counterarguments = "\n".join(f"- {c}" for c in bear_counterargs)
    bear_alts = opportunity.bear.alternative_explanations if opportunity.bear else []
    alternatives = "\n".join(f"- {a}" for a in bear_alts)
    research_sources = opportunity.research.source_references if opportunity.research else []
    sources = "\n".join(f"- {s}" for s in research_sources)
    # Finding #6: MarketAssessment (the `market` role) previously fed neither
    # scoring/model.py nor reporting/report.py at all — render it here.
    market_data = opportunity.market.market_data if opportunity.market else {}
    market_data_lines = "\n".join(f"- {k}: {v}" for k, v in market_data.items())

    return f"""# OPPORTUNITY #{opportunity.opportunity_id}

## Vad hände?
{opportunity.opportunity.observed_data if opportunity.opportunity else "Ej tillgängligt"}

## Varför är detta intressant?
{opportunity.opportunity.interpretation if opportunity.opportunity else "Ej tillgängligt"}

## Vilka bevis finns?
{sources or "Inga källor registrerade"}

## Marknadsdata
{market_data_lines or "Ingen marknadsdata registrerad"}
{opportunity.market.interpretation if opportunity.market else "Ej tillgängligt"}

## Vad talar FÖR?
{opportunity.opportunity.hypothesis if opportunity.opportunity else "Ej tillgängligt"}

## Vad talar EMOT?
{counterarguments or "Inga motargument registrerade"}

## Vilka alternativa förklaringar finns?
{alternatives or "Inga alternativa förklaringar registrerade"}

## Vad kan hända?
{scenarios_lines or "Inga scenarier"}

## Sannolikheter:
{scenarios_lines or "Ej tillgängligt"}

## Risk:
Downside: {opportunity.risk.downside if opportunity.risk else "Ej tillgängligt"}
Likviditetsrisk: {opportunity.risk.liquidity_risk if opportunity.risk else "Ej tillgängligt"}
Modellrisk: {opportunity.risk.model_risk if opportunity.risk else "Ej tillgängligt"}
Timingrisk: {opportunity.risk.timing_risk if opportunity.risk else "Ej tillgängligt"}

## Historiska jämförelser:
Ej tillgängligt i Fas 1 — Historical/Backtest Agent byggs i Fas 3.

## Data quality:
{
        (
            opportunity.score_breakdown.get("data_quality")
            if opportunity.score_breakdown
            else "Ej tillgängligt"
        )
    }

## Confidence:
{opportunity.forecast.confidence if opportunity.forecast else "Ej tillgängligt"}

## Overall opportunity score:
{opportunity.score if opportunity.score is not None else "Ej tillgängligt"}

## Time horizon:
{opportunity.time_horizon}

## Vad skulle falsifiera hypotesen?
{opportunity.bear.falsification_conditions if opportunity.bear else "Ej tillgängligt"}

## Status:
{opportunity.status}

---
*Ej finansiell rådgivning: Detta är research. Inga verkliga trades har
genomförts eller föreslagits genomföras av mig.*
"""


def write_report(opportunity: Opportunity, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    date_str = opportunity.created_at.strftime("%Y-%m-%d")
    path = dest_dir / f"{date_str}-opportunity-{opportunity.opportunity_id}.md"
    path.write_text(render_report(opportunity), encoding="utf-8")
    return path
