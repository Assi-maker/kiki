"""Historical replay support for the shadow evaluation (2026-09-28).

Candidates decided before P1 have a gate_decisions row but no
gate_evaluations row. This rebuilds the metrics and shadow flags from their
stored assessments with the P1 content checks, and stores the outcome that
REALLY happened (never the recomputed one), marked `backfilled`. Those rows
are IN-SAMPLE: they are the data the hypotheses came from.
"""
from __future__ import annotations

import json
from datetime import datetime

from crypto_trading.gate.risk_signal_gate import evaluate_risk_signal_gate
from crypto_trading.logging import log_event


def backfill_gate_evaluations(repo, policy) -> int:
    done = 0
    for candidate_id, historical, reasons_json, evaluated_at in repo.find_gate_decisions_without_evaluation():
        try:
            candidate = repo.get_candidate(candidate_id)
        except Exception as exc:  # noqa: BLE001 - one corrupt historical row never stops the batch
            log_event("shadow", event="shadow_backfill_candidate_skipped", candidate_id=candidate_id,
                      error_type=type(exc).__name__, error=str(exc)[:300])
            continue
        if candidate is None:
            continue
        moment = datetime.fromisoformat(evaluated_at)
        recomputed = evaluate_risk_signal_gate(candidate, 0, 10**9, policy=policy, now=moment)
        repo.record_gate_evaluation(candidate_id, moment, historical, {
            "backfilled": True,
            "historical_reasons": json.loads(reasons_json),
            "recomputed_outcome": recomputed.outcome,
            "reasons": recomputed.reasons,
            "enforced_failed": recomputed.enforced_failed,
            "shadow": recomputed.shadow,
            "metrics": recomputed.metrics,
        })
        done += 1
    return done
