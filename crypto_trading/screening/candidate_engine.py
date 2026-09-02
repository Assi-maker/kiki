from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from crypto_trading.agents.loader import AgentDefinition
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.logging import log_event
from crypto_trading.schemas.assessments import OpportunityScreenAssessment
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import (
    CandidateEvidenceRecord,
    compute_candidate_idempotency_key,
    compute_evidence_hash,
)
from crypto_trading.state_machine import can_transition
from crypto_trading.storage.repository import Repository


def _build_candidate(
    evidence: CandidateEvidenceRecord, discovery_run_id: str, created_at: datetime
) -> Candidate:
    evidence_hash = compute_evidence_hash(evidence)
    # idempotency_key används direkt som candidate_id - garanterat stabil och
    # unik per (instrument, discovery_run_id, evidence_hash) redan genom sin
    # egen konstruktion (schemas/evidence.py, Phase 0), så ingen separat
    # UUID-generering eller extra kollisionslogik behövs.
    idempotency_key = compute_candidate_idempotency_key(
        evidence.instrument, discovery_run_id, evidence_hash
    )
    return Candidate(
        candidate_id=idempotency_key,
        idempotency_key=idempotency_key,
        instrument=evidence.instrument,
        discovery_run_id=discovery_run_id,
        evidence_hash=evidence_hash,
        status="CANDIDATE",
        evidence_record=evidence,
        created_at=created_at,
        updated_at=created_at,
    )


def _persist_new_candidate(
    repo: Repository,
    evidence: CandidateEvidenceRecord,
    discovery_run_id: str,
    created_at: datetime,
) -> Candidate:
    candidate = _build_candidate(evidence, discovery_run_id, created_at)
    creation_event = Event(
        event_id=f"CANDIDATE_CREATED:{candidate.candidate_id}",
        event_type="CANDIDATE_CREATED",
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=created_at,
        run_id=discovery_run_id,
        schema_version=1,
        payload={"instrument": candidate.instrument, "candidate_score": evidence.candidate_score},
    )
    repo.create_candidate_with_event(candidate, creation_event)
    return candidate


def _transition_to_terminal(
    repo: Repository, candidate: Candidate, target_status: str, at: datetime, run_id: str
) -> Candidate:
    allowed, reason = can_transition(candidate.status, target_status)
    if not allowed:
        raise AssertionError(f"illegal transition attempted: {reason}")
    event = Event(
        event_id=f"CANDIDATE_TRANSITIONED:{candidate.candidate_id}:{target_status}",
        event_type="CANDIDATE_TRANSITIONED",
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=at,
        run_id=run_id,
        schema_version=1,
        payload={"from": candidate.status, "to": target_status},
    )
    repo.transition_candidate_with_event(candidate.candidate_id, target_status, at, event)
    return candidate.model_copy(update={"status": target_status, "updated_at": at})


def _is_within_cooldown_and_unchanged(
    repo: Repository,
    evidence: CandidateEvidenceRecord,
    now: datetime,
    cooldown_minutes: int,
    evidence_change_threshold: float,
) -> bool:
    latest_rejected = repo.find_latest_candidate_by_instrument_and_status(
        evidence.instrument, "REJECTED"
    )
    if latest_rejected is None:
        return False
    elapsed_minutes = (now - latest_rejected.updated_at).total_seconds() / 60
    if elapsed_minutes >= cooldown_minutes:
        return False
    score_delta = abs(evidence.candidate_score - latest_rejected.evidence_record.candidate_score)
    return score_delta < evidence_change_threshold


def process_evidence(
    repo: Repository,
    evidence: CandidateEvidenceRecord,
    discovery_run_id: str,
    created_at: datetime,
    cooldown_minutes: int = 60,
    evidence_change_threshold: float = 0.15,
) -> Candidate | None:
    """SPEC §5/§7: skapar en Candidate-rad bara när det finns anledning.

    - data_quality_status == "invalid" -> alltid en rad, direkt DATA_INVALID
      (terminal), oavsett vad screenern kom fram till för outcome.
    - outcome == "not_a_candidate" (och data ok) -> ingen rad alls, None.
    - outcome == "worth_deeper_analysis" -> dedup/cooldown-kontroll (AC3),
      sedan en ny CANDIDATE-rad om den klarar kontrollen.
    """
    if evidence.data_quality_status == "invalid":
        candidate = _persist_new_candidate(repo, evidence, discovery_run_id, created_at)
        return _transition_to_terminal(
            repo, candidate, "DATA_INVALID", created_at, discovery_run_id
        )

    if evidence.outcome == "not_a_candidate":
        return None

    if _is_within_cooldown_and_unchanged(
        repo, evidence, created_at, cooldown_minutes, evidence_change_threshold
    ):
        return None

    return _persist_new_candidate(repo, evidence, discovery_run_id, created_at)


def prioritize_and_apply_budget(
    repo: Repository,
    candidates: list[Candidate],
    liquidity_by_instrument: dict[str, Decimal],
    max_candidates_per_discovery_run: int,
    evaluated_at: datetime,
    run_id: str,
) -> tuple[list[Candidate], list[Candidate]]:
    """SPEC §10: deterministisk prioriteringsordning (1) data quality - redan
    garanterat "ok" här (DATA_INVALID-candidates skickas aldrig in i denna
    funktion), (2) candidate_score fallande, (3) likviditet fallande,
    (4) färskhet (created_at fallande). Gör inga AI-anrop - candidates inom
    budget lämnas i status CANDIDATE, redo för Phase 3 att plocka upp."""
    ranked = sorted(
        candidates,
        key=lambda c: (
            -c.evidence_record.candidate_score,
            -liquidity_by_instrument.get(c.instrument, Decimal("0")),
            -c.created_at.timestamp(),
        ),
    )
    within_budget = ranked[:max_candidates_per_discovery_run]
    over_budget = ranked[max_candidates_per_discovery_run:]

    limited: list[Candidate] = []
    for candidate in over_budget:
        limited.append(
            _transition_to_terminal(repo, candidate, "BUDGET_LIMITED", evaluated_at, run_id)
        )
    return within_budget, limited


def apply_opportunity_screening(
    repo: Repository,
    candidates: list[Candidate],
    screener_agent_def: AgentDefinition,
    screener_runner: AgentRunner,
    max_candidates_for_ai_prescreen: int,
    max_candidates_for_full_analysis: int,
    enforce: bool,
    evaluated_at: datetime,
    run_id: str,
) -> list[Candidate]:
    """Kostnadsoptimering (2026-09-02): billig förscreening på en separat,
    billigare modell (t.ex. Haiku 4.5 via `screener_runner`), INNAN den dyra
    fulla 7-rollskedjan. `candidates` är redan `prioritize_and_apply_budget`s
    `within_budget`-lista (redan rankad candidate_score->likviditet->
    färskhet) - denna funktion ändrar ALDRIG den rankningen, den lägger bara
    till ett andra, billigare urvalssteg ovanpå den.

    Bara de `max_candidates_for_ai_prescreen` högst rankade får ett
    screening-anrop alls - resten ("remainder") kostar noll extra AI-anrop
    oavsett `enforce`, eftersom de redan rankats lägre av det befintliga
    gratis steget.

    `enforce=False` (skuggläge): screeningen körs och persisteras för
    utvärdering, men påverkar INTE vilka kandidater som går vidare - alla
    `candidates` returneras oförändrat, exakt som innan denna funktion
    fanns. Ingen befintlig tradinglogik, Gate eller PAPER-exekvering rörs.

    `enforce=True`: bara de `max_candidates_for_full_analysis` högst
    poängsatta (av dem som faktiskt screenades OK) går vidare; resten -
    lägre poängsatta screenade kandidater OCH hela remainder-poolen -
    transitionas till BUDGET_LIMITED (samma terminal-status och princip som
    `prioritize_and_apply_budget` redan använder för resursbrist, aldrig
    REJECTED/NO_TRADE, som förutsätter att en faktisk bedömning gjordes).

    Fail-closed: ett screening-anrop som misslyckas (status != "ok") kan
    ALDRIG befordra en kandidat till full analys - även om alla andra
    kandidater också ser svaga ut. Ett osäkert billigt anrop öppnar aldrig
    en dörr; det stänger den bara för just den kandidaten den tick."""
    prescreen_pool = candidates[:max_candidates_for_ai_prescreen]
    remainder = candidates[max_candidates_for_ai_prescreen:]

    scored: list[tuple[Candidate, OpportunityScreenAssessment]] = []
    for candidate in prescreen_pool:
        context = {
            "candidate_id": candidate.candidate_id,
            "instrument": candidate.instrument,
            "evidence_record": candidate.evidence_record.model_dump(mode="json"),
            "run_id": run_id,
        }
        assessment = screener_runner.run(
            screener_agent_def, context, OpportunityScreenAssessment
        )
        repo.save_assessment(candidate.candidate_id, "opportunity_screen", assessment)
        log_event(
            run_id,
            event="opportunity_screen_completed",
            candidate_id=candidate.candidate_id,
            instrument=candidate.instrument,
            status=assessment.status,
            opportunity_score=assessment.opportunity_score if assessment.status == "ok" else None,
            enforce=enforce,
        )
        scored.append((candidate, assessment))

    ranked = sorted(
        scored,
        key=lambda pair: pair[1].opportunity_score if pair[1].status == "ok" else float("-inf"),
        reverse=True,
    )

    if not enforce:
        log_event(
            run_id,
            event="opportunity_screening_shadow_mode",
            prescreened=len(prescreen_pool),
            would_select=[
                c.candidate_id
                for c, a in ranked[:max_candidates_for_full_analysis]
                if a.status == "ok"
            ],
        )
        return candidates

    selected_ids = {
        c.candidate_id
        for c, a in ranked[:max_candidates_for_full_analysis]
        if a.status == "ok"
    }
    selected = [c for c in prescreen_pool if c.candidate_id in selected_ids]
    not_selected = [c for c in prescreen_pool if c.candidate_id not in selected_ids] + remainder

    for candidate in not_selected:
        _transition_to_terminal(repo, candidate, "BUDGET_LIMITED", evaluated_at, run_id)

    return selected
