from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from crypto_trading.logging import redact_error_list
from crypto_trading.schemas.assessments import (
    AssessmentBase,
    BearAdversarialAssessment,
    BullThesisAssessment,
    ForecastAssessment,
    NewsSentimentAssessment,
    QAAssessment,
    RiskAssessment,
    TechnicalAssessment,
)
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.detective import DetectiveAnalysisRecord
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import CandidateEvidenceRecord
from crypto_trading.schemas.forecast import ForecastRecord
from crypto_trading.schemas.guardian import GuardianObservation
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.db import get_connection
from crypto_trading.storage.exceptions import CorruptCandidateStateError

# Oberoende av crypto_trading.agents.roles.ROLE_MAP med avsikt - storage/ ska
# aldrig bero på agents/ (se PLAN_CRYPTO_PHASE3.md Global Constraints/
# Self-review). Innehållsmässigt identisk mappning, medvetet duplicerad.
_ASSESSMENT_FIELD_TYPES: dict[str, type[AssessmentBase]] = {
    "news_sentiment": NewsSentimentAssessment,
    "technical": TechnicalAssessment,
    "bull_thesis": BullThesisAssessment,
    "forecast": ForecastAssessment,
    "risk": RiskAssessment,
    "bear_adversarial": BearAdversarialAssessment,
    "qa": QAAssessment,
}


class Repository(Protocol):
    def create_candidate_with_event(self, candidate: Candidate, event: Event) -> bool: ...
    def get_candidate(self, candidate_id: str) -> Candidate | None: ...
    def find_candidates_by_status(self, status: str) -> list[Candidate]: ...
    def find_latest_candidate_by_instrument_and_status(
        self, instrument: str, status: str
    ) -> Candidate | None: ...
    def transition_candidate_with_event(
        self, candidate_id: str, new_status: str, updated_at: datetime, event: Event
    ) -> None: ...
    def save_assessment(
        self, candidate_id: str, field_name: str, assessment: AssessmentBase
    ) -> None: ...
    def save_gate_decision(
        self, candidate_id: str, decision: str, reasons: list[str], evaluated_at: datetime
    ) -> None: ...
    def count_open_positions(self) -> int: ...
    def sum_open_positions_notional(self) -> Decimal: ...
    def create_position_with_event(self, position: Position, event: Event) -> bool: ...
    def get_position(self, position_id: str) -> Position | None: ...
    def find_open_positions(self) -> list[Position]: ...
    def claim_demo_execution(self, position_id: str, claimed_at: datetime) -> bool: ...
    def get_demo_execution(self, position_id: str) -> dict | None: ...
    def find_positions_pending_demo_execution(self, limit: int) -> list[Position]: ...
    def find_active_demo_executions(self) -> list[dict]: ...
    def find_stale_claimed_demo_executions(self, older_than: datetime) -> list[dict]: ...
    def update_demo_execution_submitted(
        self,
        position_id: str,
        entry_client_order_id: str,
        entry_exchange_order_id: str,
        entry_quantity: str,
        exchange_fill_entry: str,
        sl_exchange_order_id: str | None,
        tp_exchange_order_id: str | None,
        updated_at: datetime,
    ) -> None: ...
    def close_demo_execution(
        self, position_id: str, exit_reason: str, exchange_fill_exit: str, closed_at: datetime
    ) -> None: ...
    def mark_demo_execution_failed(
        self, position_id: str, last_error: str, updated_at: datetime
    ) -> None: ...
    def save_guardian_observation(self, observation: GuardianObservation) -> bool: ...
    def find_latest_guardian_observation(self, position_id: str) -> dict | None: ...
    def find_guardian_observations_for_position(self, position_id: str) -> list[dict]: ...
    def claim_live_execution(
        self, position_id: str, claimed_at: datetime, margin_usdt: str,
        notional_usdt: str, leverage: str,
    ) -> bool: ...
    def get_live_execution(self, position_id: str) -> dict | None: ...
    def find_positions_pending_live_execution(self, limit: int) -> list[Position]: ...
    def get_candidate_confirmed_at(self, candidate_id: str) -> datetime | None: ...
    def find_active_live_executions(self) -> list[dict]: ...
    def find_stale_claimed_live_executions(self, older_than: datetime) -> list[dict]: ...
    def mark_live_execution_entry_submitted(
        self, position_id: str, entry_client_order_id: str, updated_at: datetime
    ) -> None: ...
    def update_live_execution_submitted(
        self,
        position_id: str,
        entry_client_order_id: str,
        entry_exchange_order_id: str,
        entry_quantity: str,
        exchange_fill_entry: str,
        sl_exchange_order_id: str | None,
        tp_exchange_order_id: str | None,
        updated_at: datetime,
    ) -> None: ...
    def close_live_execution(
        self,
        position_id: str,
        exit_reason: str,
        exchange_fill_exit: str,
        closed_at: datetime,
        realized_fees_usdt: str | None = None,
        realized_funding_usdt: str | None = None,
    ) -> None: ...
    def mark_live_execution_failed(
        self, position_id: str, last_error: str, updated_at: datetime
    ) -> None: ...
    def mark_live_execution_skipped(
        self, position_id: str, reason: str, updated_at: datetime
    ) -> None: ...
    def claim_live_profit_protection(
        self,
        position_id: str,
        threshold_pct: str,
        trigger_mark_price: str,
        breakeven_price: str,
        new_sl_client_order_id: str,
        claimed_at: datetime,
    ) -> bool: ...
    def get_live_profit_protection(self, position_id: str) -> dict | None: ...
    def find_claimed_live_profit_protection(self) -> list[dict]: ...
    def update_live_profit_protection_old_sl(
        self, position_id: str, old_sl_order_id: str, old_sl_price: str, updated_at: datetime
    ) -> None: ...
    def update_live_profit_protection_new_sl(
        self, position_id: str, new_sl_order_id: str, updated_at: datetime
    ) -> None: ...
    def set_live_profit_protection_status(
        self, position_id: str, status: str, updated_at: datetime, last_error: str | None = None
    ) -> None: ...
    def close_position_with_event(
        self,
        position_id: str,
        theoretical_exit: Decimal,
        simulated_fill_exit: Decimal,
        exit_reason: str,
        fees: Decimal,
        funding: Decimal,
        closed_at: datetime,
        event: Event,
    ) -> bool: ...
    def get_recovery_sweep_activated_at(self) -> datetime | None: ...
    def set_recovery_sweep_activated_at_if_missing(self, activated_at: datetime) -> bool: ...
    def get_profit_protection_activated_at(self) -> datetime | None: ...
    def set_profit_protection_activated_at_if_missing(self, activated_at: datetime) -> bool: ...
    def seed_profit_protection_shadow(
        self,
        shadow_id: str,
        position_id: str,
        instrument: str,
        threshold_pct: str,
        entry_price: Decimal,
        original_stop_loss: Decimal,
        target: Decimal,
        threshold_price: Decimal,
        opened_at: datetime,
        created_at: datetime,
    ) -> bool: ...
    def get_profit_protection_shadow(self, shadow_id: str) -> dict | None: ...
    def find_open_profit_protection_shadows(self) -> list[dict]: ...
    def find_all_profit_protection_shadows(self) -> list[dict]: ...
    def record_profit_protection_tick(
        self, shadow_id: str, mfe: Decimal, mae: Decimal, updated_at: datetime
    ) -> None: ...
    def activate_profit_protection_breakeven(
        self,
        shadow_id: str,
        breakeven_stop_loss: Decimal,
        threshold_reached_at: datetime,
        updated_at: datetime,
    ) -> None: ...
    def close_profit_protection_shadow(
        self,
        shadow_id: str,
        exit_reason: str,
        theoretical_exit: Decimal,
        simulated_fill_exit: Decimal,
        fees: Decimal,
        funding: Decimal,
        closed_at: datetime,
        shadow_realized_pnl: Decimal,
        updated_at: datetime,
    ) -> None: ...
    def backfill_profit_protection_baseline_outcome(
        self, position_id: str, exit_reason: str, baseline_pnl: Decimal, updated_at: datetime
    ) -> None: ...
    def abandon_profit_protection_shadow(self, shadow_id: str, abandoned_at: datetime) -> None: ...
    def start_run(self, run_id: str, run_type: str, started_at: datetime) -> None: ...
    def complete_run(
        self,
        run_id: str,
        completed_at: datetime,
        status: str,
        errors: list[str],
        instruments_scanned: int | None = None,
    ) -> None: ...
    def record_ai_call_event(self, event: Event) -> None: ...
    def count_ai_calls_since(self, cutoff: datetime) -> int: ...
    def sum_ai_cost_since(self, cutoff: datetime) -> Decimal: ...
    def save_forecast_record(self, record: ForecastRecord) -> None: ...
    def get_forecast_record(self, candidate_id: str) -> ForecastRecord | None: ...
    def record_telegram_event(
        self, telegram_event_id: str, notification_type: str, sent_at: datetime
    ) -> bool: ...
    def has_telegram_event_been_sent(self, telegram_event_id: str) -> bool: ...
    def find_candidates_pending_notification(self, status: str) -> list[Candidate]: ...
    def find_positions_pending_notification(self) -> list[Position]: ...
    def count_candidates_created_since(self, cutoff: datetime) -> int: ...
    def count_candidates_by_status_since(self, status: str, cutoff: datetime) -> int: ...
    def count_runs_by_status_since(self, status: str, cutoff: datetime) -> int: ...
    def sum_instruments_scanned_since(self, cutoff: datetime) -> int: ...
    def find_no_trade_candidates_pending_notification(
        self,
    ) -> list[tuple[Candidate, list[str]]]: ...
    def find_error_runs_pending_notification(self) -> list[dict]: ...
    def find_all_candidates(self, limit: int, offset: int = 0) -> list[Candidate]: ...
    def find_all_positions(self, limit: int, offset: int = 0) -> list[Position]: ...
    def get_gate_decision(self, candidate_id: str) -> dict | None: ...
    def find_latest_run(self, run_type: str) -> dict | None: ...
    def find_latest_completed_run(self, run_type: str) -> dict | None: ...
    def find_recent_runs(self, limit: int, offset: int = 0) -> list[dict]: ...
    def find_all_forecasts(self, limit: int, offset: int = 0) -> list[ForecastRecord]: ...
    def find_closed_positions(self) -> list[Position]: ...
    def find_forecasts_with_outcome(self) -> list[ForecastRecord]: ...
    def find_closed_positions_pending_detective_analysis(self, limit: int) -> list[Position]: ...
    def count_closed_positions_pending_detective_analysis(self) -> int: ...
    def save_detective_analysis(self, record: DetectiveAnalysisRecord) -> None: ...
    def find_detective_analyses(
        self, limit: int, offset: int = 0
    ) -> list[DetectiveAnalysisRecord]: ...
    def save_guardian_authority_decision(
        self,
        decision_id: str,
        position_id: str | None,
        candidate_id: str,
        decision_type: str,
        decided_at: datetime,
        reasoning: str,
        expected_outcome: str,
        expected_direction: str,
        confidence: float | None,
        run_id: str,
        old_sl: str | None = None,
        new_sl: str | None = None,
        intervention_applied: bool | None = None,
    ) -> bool: ...
    def get_guardian_authority_decision(self, decision_id: str) -> dict | None: ...
    def find_pending_guardian_authority_decisions(self) -> list[dict]: ...
    def find_resolved_guardian_authority_decisions(self) -> list[dict]: ...
    def resolve_guardian_authority_decision(
        self,
        decision_id: str,
        actual_exit_reason: str,
        actual_pnl_usdt: str,
        expectation_correct: bool | None,
        resolved_at: datetime,
    ) -> None: ...
    def mark_guardian_authority_decision_intervention_applied(
        self, decision_id: str, applied: bool, updated_at: datetime
    ) -> None: ...
    def find_guardian_authority_heuristics(self) -> list[dict]: ...
    def upsert_guardian_authority_heuristic(
        self,
        heuristic_id: str,
        description: str,
        condition_json: str,
        adjustment: float,
        confidence: float,
        sample_size: int,
        updated_at: datetime,
    ) -> None: ...
    def tighten_position_stop_loss(
        self, position_id: str, new_stop_loss: Decimal, updated_at: datetime
    ) -> bool: ...
    def claim_guardian_authority_live_sl_action(
        self,
        position_id: str,
        new_sl_price: str,
        new_sl_client_order_id: str,
        claimed_at: datetime,
    ) -> bool: ...
    def get_guardian_authority_live_sl_action(self, position_id: str) -> dict | None: ...
    def find_claimed_guardian_authority_live_sl_actions(self) -> list[dict]: ...
    def update_guardian_authority_live_sl_action_old_sl(
        self, position_id: str, old_sl_order_id: str, old_sl_price: str, updated_at: datetime
    ) -> None: ...
    def update_guardian_authority_live_sl_action_new_sl(
        self, position_id: str, new_sl_order_id: str, updated_at: datetime
    ) -> None: ...
    def set_guardian_authority_live_sl_action_status(
        self, position_id: str, status: str, updated_at: datetime, last_error: str | None = None
    ) -> None: ...
    def seed_guardian_authority_shadow(
        self,
        shadow_id: str,
        position_id: str,
        candidate_id: str,
        instrument: str,
        opened_at: datetime,
        created_at: datetime,
        run_id: str,
    ) -> bool: ...
    def get_guardian_authority_shadow(self, shadow_id: str) -> dict | None: ...
    def find_open_guardian_authority_shadows(self) -> list[dict]: ...
    def record_guardian_authority_shadow_tick(
        self,
        shadow_id: str,
        mfe: Decimal,
        mae: Decimal,
        factors_json: str,
        updated_at: datetime,
    ) -> None: ...
    def decide_guardian_authority_shadow(
        self,
        shadow_id: str,
        decision: str,
        decided_at: datetime,
        expected_outcome: str,
        expected_direction: str,
        confidence: float,
        factors_json: str,
        proposed_new_sl: Decimal | None,
        updated_at: datetime,
    ) -> bool: ...
    def resolve_guardian_authority_shadow_no_action(
        self,
        shadow_id: str,
        factors_json: str,
        actual_exit_reason: str,
        actual_pnl_usdt: Decimal,
        actual_closed_at: datetime,
        updated_at: datetime,
    ) -> bool: ...
    def resolve_guardian_authority_shadow_decided(
        self,
        shadow_id: str,
        actual_exit_reason: str,
        actual_pnl_usdt: Decimal,
        actual_closed_at: datetime,
        expectation_correct: bool | None,
        prediction_error: float | None,
        updated_at: datetime,
    ) -> bool: ...
    def abandon_guardian_authority_shadow(self, shadow_id: str, abandoned_at: datetime) -> None: ...
    def find_resolved_guardian_authority_shadows(self) -> list[dict]: ...
    def find_abandoned_guardian_authority_shadows(self) -> list[dict]: ...
    def find_guardian_authority_shadow_heuristics(self) -> list[dict]: ...
    def upsert_guardian_authority_shadow_heuristic(
        self,
        heuristic_id: str,
        description: str,
        condition_json: str,
        adjustment: float,
        confidence: float,
        sample_size: int,
        updated_at: datetime,
    ) -> None: ...
    def save_guardian_authority_pre_entry_shadow(
        self,
        shadow_id: str,
        candidate_id: str,
        instrument: str,
        shadow_decision: str,
        expected_outcome: str,
        expected_direction: str,
        confidence: float,
        factors_json: str,
        run_id: str,
        created_at: datetime,
    ) -> bool: ...
    def get_guardian_authority_pre_entry_shadow(self, shadow_id: str) -> dict | None: ...
    def find_pending_guardian_authority_pre_entry_shadows(self) -> list[dict]: ...
    def resolve_guardian_authority_pre_entry_shadow(
        self,
        shadow_id: str,
        actual_exit_reason: str,
        actual_pnl_usdt: Decimal,
        actual_closed_at: datetime,
        updated_at: datetime,
    ) -> bool: ...
    def find_resolved_guardian_authority_pre_entry_shadows(self) -> list[dict]: ...


class SQLiteRepository:
    def __init__(self, path: Path, busy_timeout_ms: int = 5000):
        self._conn = get_connection(path, busy_timeout_ms=busy_timeout_ms)

    def create_candidate_with_event(self, candidate: Candidate, event: Event) -> bool:
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO candidates "
                "(candidate_id, idempotency_key, instrument, discovery_run_id, evidence_hash, "
                "status, evidence_record, created_at, updated_at, reference_price) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    candidate.candidate_id,
                    candidate.idempotency_key,
                    candidate.instrument,
                    candidate.discovery_run_id,
                    candidate.evidence_hash,
                    candidate.status,
                    candidate.evidence_record.model_dump_json(),
                    candidate.created_at.isoformat(),
                    candidate.updated_at.isoformat(),
                    str(candidate.reference_price) if candidate.reference_price is not None else None,
                ),
            )
            created = cur.rowcount > 0
            if created:
                self._insert_event(event)
            self._conn.commit()
            return created
        except Exception:
            self._conn.rollback()
            raise

    def transition_candidate_with_event(
        self, candidate_id: str, new_status: str, updated_at: datetime, event: Event
    ) -> None:
        try:
            self._conn.execute(
                "UPDATE candidates SET status = ?, updated_at = ? WHERE candidate_id = ?",
                (new_status, updated_at.isoformat(), candidate_id),
            )
            self._insert_event(event)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _insert_event(self, event: Event) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO events "
            "(event_id, event_type, aggregate_type, aggregate_id, occurred_at, run_id, "
            "schema_version, payload) VALUES (?,?,?,?,?,?,?,?)",
            (
                event.event_id,
                event.event_type,
                event.aggregate_type,
                event.aggregate_id,
                event.occurred_at.isoformat(),
                event.run_id,
                event.schema_version,
                json.dumps(event.payload, default=str),
            ),
        )
        return cur.rowcount > 0

    def get_candidate(self, candidate_id: str) -> Candidate | None:
        """Läser och deserialiserar en candidate-rad.

        Klassas som korrupt persistent state (CorruptCandidateStateError +
        CORRUPT_STATE_DETECTED), ALDRIG som ett delvis konstruerat Candidate:
        - evidence_record: ValidationError eller ValueError (json.JSONDecodeError
          ärver ValueError) vid CandidateEvidenceRecord.model_validate_json().
        - created_at/updated_at: ValueError vid datetime.fromisoformat().
        - övriga fält (i praktiken status, det enda återstående fältet med en
          begränsande typ - Literal): ValidationError vid den slutliga
          Candidate(**data)-konstruktionen.

        Fångar MEDVETET INTE bredare undantagstyper (KeyError, TypeError,
        AttributeError, ...) - de indikerar ett verkligt programmeringsfel
        (t.ex. ett schema/kod-mismatch efter en migrering), inte korrupt
        lagrad data, och ska propagera okontrollerat istället för att
        felaktigt klassas som CorruptCandidateStateError.
        """
        row = self._conn.execute(
            "SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        raw_status = data["status"]

        try:
            data["evidence_record"] = CandidateEvidenceRecord.model_validate_json(
                data["evidence_record"]
            )
        except (ValidationError, ValueError) as exc:
            self._insert_corrupt_state_event(candidate_id, raw_status, "evidence_record")
            raise CorruptCandidateStateError(candidate_id, raw_status, "evidence_record") from exc

        try:
            data["created_at"] = datetime.fromisoformat(data["created_at"])
            data["updated_at"] = datetime.fromisoformat(data["updated_at"])
        except ValueError as exc:
            self._insert_corrupt_state_event(candidate_id, raw_status, "timestamp")
            raise CorruptCandidateStateError(candidate_id, raw_status, "timestamp") from exc

        try:
            data["reference_price"] = (
                Decimal(data["reference_price"]) if data["reference_price"] is not None else None
            )
        except InvalidOperation as exc:
            self._insert_corrupt_state_event(candidate_id, raw_status, "reference_price")
            raise CorruptCandidateStateError(candidate_id, raw_status, "reference_price") from exc

        assessment_rows = self._conn.execute(
            "SELECT field_name, payload FROM assessments WHERE candidate_id = ?", (candidate_id,)
        ).fetchall()
        for assessment_row in assessment_rows:
            field_name = assessment_row["field_name"]
            assessment_type = _ASSESSMENT_FIELD_TYPES.get(field_name)
            if assessment_type is None:
                continue  # okänt fältnamn i tabellen - ignoreras, inte ett candidate-korrupt-fel
            try:
                data[field_name] = assessment_type.model_validate_json(assessment_row["payload"])
            except (ValidationError, ValueError) as exc:
                self._insert_corrupt_state_event(
                    candidate_id, raw_status, f"assessment:{field_name}"
                )
                raise CorruptCandidateStateError(
                    candidate_id, raw_status, f"assessment:{field_name}"
                ) from exc

        try:
            return Candidate(**data)
        except ValidationError as exc:
            status_error = any(err["loc"] == ("status",) for err in exc.errors())
            corrupted_field = "status" if status_error else "candidate"
            self._insert_corrupt_state_event(candidate_id, raw_status, corrupted_field)
            raise CorruptCandidateStateError(candidate_id, raw_status, corrupted_field) from exc

    def find_latest_candidate_by_instrument_and_status(
        self, instrument: str, status: str
    ) -> Candidate | None:
        """Till skillnad från `find_candidates_by_status()` sväljer denna
        metod INTE ett `CorruptCandidateStateError` - den returnerar en
        specifik, namngiven rad, och om just den raden är korrupt är det
        direkt relevant för anroparen (dedup/cooldown-beslutet får då
        fail-closed genom att låta felet propagera, inte tyst falla
        tillbaka till "ingen cooldown finns")."""
        row = self._conn.execute(
            "SELECT candidate_id FROM candidates WHERE instrument = ? AND status = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (instrument, status),
        ).fetchone()
        if row is None:
            return None
        return self.get_candidate(row["candidate_id"])

    def _insert_corrupt_state_event(
        self, candidate_id: str, raw_status: str, corrupted_field: str
    ) -> None:
        event = Event(
            event_id=f"CORRUPT_STATE_DETECTED:{candidate_id}:{corrupted_field}",
            event_type="CORRUPT_STATE_DETECTED",
            aggregate_type="candidate",
            aggregate_id=candidate_id,
            occurred_at=datetime.now(UTC),
            run_id=None,
            schema_version=1,
            payload={"raw_status": raw_status, "corrupted_field": corrupted_field},
        )
        self._insert_event(event)
        self._conn.commit()

    def save_assessment(
        self, candidate_id: str, field_name: str, assessment: AssessmentBase
    ) -> None:
        self._conn.execute(
            "INSERT INTO assessments (candidate_id, field_name, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(candidate_id, field_name) DO UPDATE SET payload = excluded.payload",
            (candidate_id, field_name, assessment.model_dump_json()),
        )
        self._conn.commit()

    def save_gate_decision(
        self, candidate_id: str, decision: str, reasons: list[str], evaluated_at: datetime
    ) -> None:
        self._conn.execute(
            "INSERT INTO gate_decisions (candidate_id, decision, reasons, evaluated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(candidate_id) DO UPDATE SET "
            "decision = excluded.decision, reasons = excluded.reasons, "
            "evaluated_at = excluded.evaluated_at",
            (candidate_id, decision, json.dumps(reasons), evaluated_at.isoformat()),
        )
        self._conn.commit()

    def count_open_positions(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM positions WHERE status = 'OPEN_POSITION'"
        ).fetchone()
        return row["n"]

    def sum_open_positions_notional(self) -> Decimal:
        rows = self._conn.execute(
            "SELECT size FROM positions WHERE status = 'OPEN_POSITION'"
        ).fetchall()
        return sum((Decimal(row["size"]) for row in rows), Decimal("0"))

    def create_position_with_event(self, position: Position, event: Event) -> bool:
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO positions "
                "(position_id, candidate_id, instrument, direction, status, theoretical_entry, "
                "simulated_fill_entry, stop_loss, target, size, fill_model_version, opened_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    position.position_id,
                    position.candidate_id,
                    position.instrument,
                    position.direction,
                    position.status,
                    str(position.theoretical_entry),
                    str(position.simulated_fill_entry),
                    str(position.stop_loss),
                    str(position.target),
                    str(position.size),
                    position.fill_model_version,
                    position.opened_at.isoformat(),
                ),
            )
            created = cur.rowcount > 0
            if created:
                self._insert_event(event)
            self._conn.commit()
            return created
        except Exception:
            self._conn.rollback()
            raise

    def get_position(self, position_id: str) -> Position | None:
        row = self._conn.execute(
            "SELECT * FROM positions WHERE position_id = ?", (position_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_position(row)

    @staticmethod
    def _row_to_position(row) -> Position:
        data = dict(row)
        data["theoretical_entry"] = Decimal(data["theoretical_entry"])
        data["simulated_fill_entry"] = Decimal(data["simulated_fill_entry"])
        data["stop_loss"] = Decimal(data["stop_loss"])
        data["target"] = Decimal(data["target"])
        data["size"] = Decimal(data["size"])
        data["opened_at"] = datetime.fromisoformat(data["opened_at"])
        data["theoretical_exit"] = (
            Decimal(data["theoretical_exit"]) if data["theoretical_exit"] is not None else None
        )
        data["simulated_fill_exit"] = (
            Decimal(data["simulated_fill_exit"])
            if data["simulated_fill_exit"] is not None
            else None
        )
        data["fees"] = Decimal(data["fees"]) if data["fees"] is not None else None
        data["funding"] = Decimal(data["funding"]) if data["funding"] is not None else None
        data["closed_at"] = (
            datetime.fromisoformat(data["closed_at"]) if data["closed_at"] is not None else None
        )
        return Position(**data)

    def find_open_positions(self) -> list[Position]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN_POSITION'"
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def claim_demo_execution(self, position_id: str, claimed_at: datetime) -> bool:
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO demo_executions "
                "(position_id, phase, claimed_at, updated_at) VALUES (?, 'CLAIMED', ?, ?)",
                (position_id, claimed_at.isoformat(), claimed_at.isoformat()),
            )
            claimed = cur.rowcount > 0
            self._conn.commit()
            return claimed
        except Exception:
            self._conn.rollback()
            raise

    def get_demo_execution(self, position_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM demo_executions WHERE position_id = ?", (position_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def find_positions_pending_demo_execution(self, limit: int) -> list[Position]:
        # size = '0' excludes positions the exposure cap pressed to zero
        # (paper_trading/position_sizing.py::compute_position_size) - zero
        # real market exposure, same convention performance/
        # paper_track_report.py::_is_blocked_by_exposure() already applies.
        # Mirroring one to BingX Demo would just fail ("quantity or
        # quoteOrderQty is must", confirmed live 2026-09-04) - never a real
        # trade, never worth a demo order attempt.
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN_POSITION' AND size != '0' "
            "AND position_id NOT IN (SELECT position_id FROM demo_executions) "
            "ORDER BY opened_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def find_active_demo_executions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM demo_executions WHERE phase = 'ACTIVE'"
        ).fetchall()
        return [dict(row) for row in rows]

    def find_stale_claimed_demo_executions(self, older_than: datetime) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM demo_executions WHERE phase = 'CLAIMED' AND claimed_at < ?",
            (older_than.isoformat(),),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_demo_execution_submitted(
        self,
        position_id: str,
        entry_client_order_id: str,
        entry_exchange_order_id: str,
        entry_quantity: str,
        exchange_fill_entry: str,
        sl_exchange_order_id: str | None,
        tp_exchange_order_id: str | None,
        updated_at: datetime,
    ) -> None:
        self._conn.execute(
            "UPDATE demo_executions SET phase = 'ACTIVE', entry_client_order_id = ?, "
            "entry_exchange_order_id = ?, entry_quantity = ?, exchange_fill_entry = ?, "
            "sl_exchange_order_id = ?, tp_exchange_order_id = ?, updated_at = ? "
            "WHERE position_id = ?",
            (
                entry_client_order_id,
                entry_exchange_order_id,
                entry_quantity,
                exchange_fill_entry,
                sl_exchange_order_id,
                tp_exchange_order_id,
                updated_at.isoformat(),
                position_id,
            ),
        )
        self._conn.commit()

    def close_demo_execution(
        self, position_id: str, exit_reason: str, exchange_fill_exit: str, closed_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE demo_executions SET phase = 'CLOSED', exit_reason = ?, "
            "exchange_fill_exit = ?, closed_at = ?, updated_at = ? WHERE position_id = ?",
            (exit_reason, exchange_fill_exit, closed_at.isoformat(), closed_at.isoformat(), position_id),
        )
        self._conn.commit()

    def mark_demo_execution_failed(
        self, position_id: str, last_error: str, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE demo_executions SET phase = 'FAILED', last_error = ?, updated_at = ? "
            "WHERE position_id = ?",
            (last_error, updated_at.isoformat(), position_id),
        )
        self._conn.commit()

    def claim_live_execution(
        self, position_id: str, claimed_at: datetime, margin_usdt: str,
        notional_usdt: str, leverage: str,
    ) -> bool:
        try:
            # Race defense (spec §17.6): re-verifies positions.status is
            # still OPEN_POSITION as part of the SAME atomic statement as
            # the claim insert - never trusts a Position object read
            # earlier in the tick. Closes the exact race observed live,
            # where PAPER's own time-limit closer and LIVE's independent
            # claiming thread acted on the same position within the same
            # second, with no lock between them.
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO live_executions "
                "(position_id, phase, margin_usdt, notional_usdt, leverage, "
                "claimed_at, updated_at) "
                "SELECT ?, 'CLAIMED', ?, ?, ?, ?, ? WHERE EXISTS ("
                "SELECT 1 FROM positions WHERE position_id = ? AND status = 'OPEN_POSITION')",
                (
                    position_id, margin_usdt, notional_usdt, leverage,
                    claimed_at.isoformat(), claimed_at.isoformat(),
                    position_id,
                ),
            )
            claimed = cur.rowcount > 0
            self._conn.commit()
            return claimed
        except Exception:
            self._conn.rollback()
            raise

    def get_live_execution(self, position_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM live_executions WHERE position_id = ?", (position_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def find_positions_pending_live_execution(self, limit: int) -> list[Position]:
        """2026-09-13 pipeline-queue fix: `opened_at DESC`, not `ASC`. A
        never-claimed old position (stale past LIVE's own signal TTL -
        `_signal_is_fresh()` in live_execution.py - and therefore permanent,
        since freshness only ever decreases) used to sit in this result
        forever under ASC ordering, and a large enough backlog of such rows
        would fully occupy every `limit`-sized page, silently hiding any
        genuinely fresh position from ever being seen by
        process_pending_positions() - not just skipped for being stale
        (that part already worked correctly), but never looked at in the
        first place. DESC guarantees a fresh position is always at/near the
        front of the window. Freshness/TTL enforcement itself is unchanged -
        still decided entirely by _signal_is_fresh() after this query
        returns; this only changes which candidates are visible within a
        bounded page. Purely a SELECT: never deletes, closes, or otherwise
        mutates any row it doesn't return."""
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN_POSITION' "
            "AND position_id NOT IN (SELECT position_id FROM live_executions) "
            "ORDER BY opened_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def get_candidate_confirmed_at(self, candidate_id: str) -> datetime | None:
        """Authoritative signal timestamp for LIVE's TTL check (spec §17.3):
        the moment Gate actually confirmed the candidate, sourced from the
        append-only events table - never positions.opened_at (stamped with
        discovery-creation time, not confirmation time, per the incident
        forensic trace) and never LIVE claim time. ASC LIMIT 1 picks the
        first CONFIRMED transition deterministically, in the (currently
        unseen) event a candidate were ever re-confirmed more than once."""
        row = self._conn.execute(
            "SELECT occurred_at FROM events WHERE aggregate_id = ? "
            "AND event_type = 'CANDIDATE_TRANSITIONED' "
            "AND json_extract(payload, '$.to') = 'CONFIRMED' "
            "ORDER BY occurred_at ASC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(row["occurred_at"])

    def find_active_live_executions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM live_executions "
            "WHERE phase IN ('CLAIMED', 'ENTRY_SUBMITTED', 'ACTIVE')"
        ).fetchall()
        return [dict(row) for row in rows]

    def find_stale_claimed_live_executions(self, older_than: datetime) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM live_executions WHERE phase = 'CLAIMED' AND claimed_at < ?",
            (older_than.isoformat(),),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_live_execution_entry_submitted(
        self, position_id: str, entry_client_order_id: str, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE live_executions SET phase = 'ENTRY_SUBMITTED', entry_client_order_id = ?, "
            "updated_at = ? WHERE position_id = ?",
            (entry_client_order_id, updated_at.isoformat(), position_id),
        )
        self._conn.commit()

    def update_live_execution_submitted(
        self,
        position_id: str,
        entry_client_order_id: str,
        entry_exchange_order_id: str,
        entry_quantity: str,
        exchange_fill_entry: str,
        sl_exchange_order_id: str | None,
        tp_exchange_order_id: str | None,
        updated_at: datetime,
    ) -> None:
        self._conn.execute(
            "UPDATE live_executions SET phase = 'ACTIVE', entry_client_order_id = ?, "
            "entry_exchange_order_id = ?, entry_quantity = ?, exchange_fill_entry = ?, "
            "sl_exchange_order_id = ?, tp_exchange_order_id = ?, updated_at = ? "
            "WHERE position_id = ?",
            (
                entry_client_order_id,
                entry_exchange_order_id,
                entry_quantity,
                exchange_fill_entry,
                sl_exchange_order_id,
                tp_exchange_order_id,
                updated_at.isoformat(),
                position_id,
            ),
        )
        self._conn.commit()

    def close_live_execution(
        self,
        position_id: str,
        exit_reason: str,
        exchange_fill_exit: str,
        closed_at: datetime,
        realized_fees_usdt: str | None = None,
        realized_funding_usdt: str | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE live_executions SET phase = 'CLOSED', exit_reason = ?, "
            "exchange_fill_exit = ?, realized_fees_usdt = ?, realized_funding_usdt = ?, "
            "closed_at = ?, updated_at = ? WHERE position_id = ?",
            (
                exit_reason, exchange_fill_exit, realized_fees_usdt, realized_funding_usdt,
                closed_at.isoformat(), closed_at.isoformat(), position_id,
            ),
        )
        self._conn.commit()

    def mark_live_execution_failed(
        self, position_id: str, last_error: str, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE live_executions SET phase = 'FAILED', last_error = ?, updated_at = ? "
            "WHERE position_id = ?",
            (last_error, updated_at.isoformat(), position_id),
        )
        self._conn.commit()

    def mark_live_execution_skipped(
        self, position_id: str, reason: str, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE live_executions SET phase = 'SKIPPED', last_error = ?, updated_at = ? "
            "WHERE position_id = ?",
            (reason, updated_at.isoformat(), position_id),
        )
        self._conn.commit()

    def claim_live_profit_protection(
        self,
        position_id: str,
        threshold_pct: str,
        trigger_mark_price: str,
        breakeven_price: str,
        new_sl_client_order_id: str,
        claimed_at: datetime,
    ) -> bool:
        # Idempotency gate: "PP only activates once per position" (spec's
        # Data model section). No WHERE EXISTS positions race-guard, unlike
        # claim_live_execution - the caller (Task 5) already verifies the
        # live position itself before ever calling this claim, so a plain
        # INSERT OR IGNORE on the position_id primary key is sufficient.
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO live_profit_protection "
                "(position_id, status, threshold_pct, trigger_mark_price, breakeven_price, "
                "new_sl_client_order_id, claimed_at, updated_at) "
                "VALUES (?, 'CLAIMED', ?, ?, ?, ?, ?, ?)",
                (
                    position_id,
                    threshold_pct,
                    trigger_mark_price,
                    breakeven_price,
                    new_sl_client_order_id,
                    claimed_at.isoformat(),
                    claimed_at.isoformat(),
                ),
            )
            claimed = cur.rowcount > 0
            self._conn.commit()
            return claimed
        except Exception:
            self._conn.rollback()
            raise

    def get_live_profit_protection(self, position_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM live_profit_protection WHERE position_id = ?", (position_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def find_claimed_live_profit_protection(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM live_profit_protection WHERE status = 'CLAIMED'"
        ).fetchall()
        return [dict(row) for row in rows]

    def update_live_profit_protection_old_sl(
        self, position_id: str, old_sl_order_id: str, old_sl_price: str, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE live_profit_protection SET old_sl_order_id = ?, old_sl_price = ?, "
            "updated_at = ? WHERE position_id = ?",
            (old_sl_order_id, old_sl_price, updated_at.isoformat(), position_id),
        )
        self._conn.commit()

    def update_live_profit_protection_new_sl(
        self, position_id: str, new_sl_order_id: str, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE live_profit_protection SET new_sl_order_id = ?, updated_at = ? "
            "WHERE position_id = ?",
            (new_sl_order_id, updated_at.isoformat(), position_id),
        )
        self._conn.commit()

    def set_live_profit_protection_status(
        self, position_id: str, status: str, updated_at: datetime, last_error: str | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE live_profit_protection SET status = ?, last_error = ?, updated_at = ? "
            "WHERE position_id = ?",
            (status, last_error, updated_at.isoformat(), position_id),
        )
        self._conn.commit()

    def save_guardian_observation(self, observation: GuardianObservation) -> bool:
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO guardian_observations "
                "(observation_id, position_id, observed_at, state, decay_score, "
                "progress_ratio, unrealized_pnl, factors, ai_reasoning, ai_cost_usd, run_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    observation.observation_id,
                    observation.position_id,
                    observation.observed_at.isoformat(),
                    observation.state,
                    str(observation.decay_score),
                    str(observation.progress_ratio),
                    str(observation.unrealized_pnl),
                    json.dumps(observation.factors),
                    observation.ai_reasoning,
                    str(observation.ai_cost_usd) if observation.ai_cost_usd is not None else None,
                    observation.run_id,
                ),
            )
            created = cur.rowcount > 0
            self._conn.commit()
            return created
        except Exception:
            self._conn.rollback()
            raise

    def find_latest_guardian_observation(self, position_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM guardian_observations WHERE position_id = ? "
            "ORDER BY observed_at DESC LIMIT 1",
            (position_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def find_guardian_observations_for_position(self, position_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM guardian_observations WHERE position_id = ? ORDER BY observed_at ASC",
            (position_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def close_position_with_event(
        self,
        position_id: str,
        theoretical_exit: Decimal,
        simulated_fill_exit: Decimal,
        exit_reason: str,
        fees: Decimal,
        funding: Decimal,
        closed_at: datetime,
        event: Event,
    ) -> bool:
        """P4 remediation (2026-09-11): the WHERE status = 'OPEN_POSITION'
        clause makes this atomic against a concurrent close of the same
        position - only the caller whose UPDATE actually flips status ever
        gets True/inserts the event; a second, racing caller's UPDATE
        affects zero rows and returns False, never silently overwriting the
        first close's exit data or inserting a duplicate POSITION_CLOSED
        event. Same statement-level race-defense pattern already used by
        claim_live_execution()'s own WHERE EXISTS guard."""
        try:
            cur = self._conn.execute(
                "UPDATE positions SET status = 'CLOSED', theoretical_exit = ?, "
                "simulated_fill_exit = ?, exit_reason = ?, fees = ?, funding = ?, closed_at = ? "
                "WHERE position_id = ? AND status = 'OPEN_POSITION'",
                (
                    str(theoretical_exit),
                    str(simulated_fill_exit),
                    exit_reason,
                    str(fees),
                    str(funding),
                    closed_at.isoformat(),
                    position_id,
                ),
            )
            closed = cur.rowcount > 0
            if closed:
                self._insert_event(event)
            self._conn.commit()
            return closed
        except Exception:
            self._conn.rollback()
            raise

    def get_recovery_sweep_activated_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'recovery_sweep_activated_at'"
        ).fetchone()
        return datetime.fromisoformat(row["value"]) if row is not None else None

    def set_recovery_sweep_activated_at_if_missing(self, activated_at: datetime) -> bool:
        """P2 remediation (2026-09-11): one-time activation watermark for
        paper_trading/recovery_sweep.py - same INSERT OR IGNORE first-writer-
        wins idempotency already used for schema_meta's own schema_version
        row. Whichever timestamp is set FIRST (this database's very first
        ever recovery-sweep call) is authoritative forever, never overwritten
        by a later restart - this is what makes the sweep strictly forward-
        looking: any CONFIRMED candidate from before this moment is
        permanently excluded from automatic recovery."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta (key, value) VALUES "
            "('recovery_sweep_activated_at', ?)",
            (activated_at.isoformat(),),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_profit_protection_activated_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'profit_protection_activated_at'"
        ).fetchone()
        return datetime.fromisoformat(row["value"]) if row is not None else None

    def set_profit_protection_activated_at_if_missing(self, activated_at: datetime) -> bool:
        """One-time activation watermark (spec G6, plan correction C1) -
        same INSERT OR IGNORE first-writer-wins idempotency as
        set_recovery_sweep_activated_at_if_missing(). Whichever timestamp is
        set FIRST is authoritative forever: any position opened before it
        is permanently excluded from the experiment."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO schema_meta (key, value) VALUES "
            "('profit_protection_activated_at', ?)",
            (activated_at.isoformat(),),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def seed_profit_protection_shadow(
        self,
        shadow_id: str,
        position_id: str,
        instrument: str,
        threshold_pct: str,
        entry_price: Decimal,
        original_stop_loss: Decimal,
        target: Decimal,
        threshold_price: Decimal,
        opened_at: datetime,
        created_at: datetime,
    ) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO profit_protection_shadow_positions "
            "(shadow_id, position_id, instrument, threshold_pct, entry_price, "
            "original_stop_loss, target, threshold_price, opened_at, status, "
            "threshold_reached, mfe, mae, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', 0, '0', '0', ?, ?)",
            (
                shadow_id, position_id, instrument, threshold_pct, str(entry_price),
                str(original_stop_loss), str(target), str(threshold_price),
                opened_at.isoformat(), created_at.isoformat(), created_at.isoformat(),
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_profit_protection_shadow(self, shadow_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM profit_protection_shadow_positions WHERE shadow_id = ?",
            (shadow_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def find_open_profit_protection_shadows(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM profit_protection_shadow_positions WHERE status = 'OPEN'"
        ).fetchall()
        return [dict(row) for row in rows]

    def find_all_profit_protection_shadows(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM profit_protection_shadow_positions"
        ).fetchall()
        return [dict(row) for row in rows]

    def record_profit_protection_tick(
        self, shadow_id: str, mfe: Decimal, mae: Decimal, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE profit_protection_shadow_positions SET mfe = ?, mae = ?, updated_at = ? "
            "WHERE shadow_id = ? AND status = 'OPEN'",
            (str(mfe), str(mae), updated_at.isoformat(), shadow_id),
        )
        self._conn.commit()

    def activate_profit_protection_breakeven(
        self,
        shadow_id: str,
        breakeven_stop_loss: Decimal,
        threshold_reached_at: datetime,
        updated_at: datetime,
    ) -> None:
        # WHERE threshold_reached = 0 makes this a one-time transition -
        # a later, accidental second call (e.g. a retried tick) can never
        # move an already-active breakeven level, spec G7/G8's "next tick
        # only" guarantee holds even under retries.
        self._conn.execute(
            "UPDATE profit_protection_shadow_positions SET threshold_reached = 1, "
            "threshold_reached_at = ?, breakeven_stop_loss = ?, updated_at = ? "
            "WHERE shadow_id = ? AND status = 'OPEN' AND threshold_reached = 0",
            (
                threshold_reached_at.isoformat(), str(breakeven_stop_loss),
                updated_at.isoformat(), shadow_id,
            ),
        )
        self._conn.commit()

    def close_profit_protection_shadow(
        self,
        shadow_id: str,
        exit_reason: str,
        theoretical_exit: Decimal,
        simulated_fill_exit: Decimal,
        fees: Decimal,
        funding: Decimal,
        closed_at: datetime,
        shadow_realized_pnl: Decimal,
        updated_at: datetime,
    ) -> None:
        row = self._conn.execute(
            "SELECT hypothetical_baseline_pnl FROM profit_protection_shadow_positions "
            "WHERE shadow_id = ?",
            (shadow_id,),
        ).fetchone()
        pnl_difference = None
        if row is not None and row["hypothetical_baseline_pnl"] is not None:
            pnl_difference = shadow_realized_pnl - Decimal(row["hypothetical_baseline_pnl"])
        self._conn.execute(
            "UPDATE profit_protection_shadow_positions SET status = 'CLOSED', "
            "exit_reason = ?, theoretical_exit = ?, simulated_fill_exit = ?, fees = ?, "
            "funding = ?, closed_at = ?, shadow_realized_pnl = ?, pnl_difference = ?, "
            "updated_at = ? WHERE shadow_id = ? AND status = 'OPEN'",
            (
                exit_reason, str(theoretical_exit), str(simulated_fill_exit), str(fees),
                str(funding), closed_at.isoformat(), str(shadow_realized_pnl),
                str(pnl_difference) if pnl_difference is not None else None,
                updated_at.isoformat(), shadow_id,
            ),
        )
        self._conn.commit()

    def backfill_profit_protection_baseline_outcome(
        self, position_id: str, exit_reason: str, baseline_pnl: Decimal, updated_at: datetime
    ) -> None:
        """Called once per real position close (spec §5.5) - updates every
        threshold's shadow row for that position_id. Whichever of
        (shadow close, this backfill) happens second is what fills
        pnl_difference (spec §4.3) - this method fills it here if the
        shadow already closed; close_profit_protection_shadow() fills it
        there if this backfill already ran first."""
        rows = self._conn.execute(
            "SELECT shadow_id, status, shadow_realized_pnl "
            "FROM profit_protection_shadow_positions WHERE position_id = ?",
            (position_id,),
        ).fetchall()
        for row in rows:
            pnl_difference = None
            if row["status"] == "CLOSED" and row["shadow_realized_pnl"] is not None:
                pnl_difference = Decimal(row["shadow_realized_pnl"]) - baseline_pnl
            self._conn.execute(
                "UPDATE profit_protection_shadow_positions SET "
                "hypothetical_baseline_exit_reason = ?, hypothetical_baseline_pnl = ?, "
                "pnl_difference = COALESCE(?, pnl_difference), updated_at = ? "
                "WHERE shadow_id = ?",
                (
                    exit_reason, str(baseline_pnl),
                    str(pnl_difference) if pnl_difference is not None else None,
                    updated_at.isoformat(), row["shadow_id"],
                ),
            )
        self._conn.commit()

    def abandon_profit_protection_shadow(self, shadow_id: str, abandoned_at: datetime) -> None:
        """Review finding 1 (final whole-branch review): marks an OPEN
        shadow whose real position is no longer open - for ANY reason
        (closed during monitoring_catchup.py's replay without this
        experiment's tick ever running for that close, a prior per-shadow
        advance failure, or a missing real position row) - as ABANDONED,
        so it can never be left sitting OPEN forever with no path to
        resolution, nor silently keep advancing against candle data that
        (once its own real position is gone) may belong to an unrelated
        position sharing the same instrument. `status` has no CHECK
        constraint (see db.py) - 'ABANDONED' is simply a new value for the
        same TEXT column already holding 'OPEN'/'CLOSED'. A no-op if the
        shadow is not currently OPEN, same guard style as
        close_profit_protection_shadow's own WHERE status = 'OPEN'."""
        self._conn.execute(
            "UPDATE profit_protection_shadow_positions SET status = 'ABANDONED', "
            "updated_at = ? WHERE shadow_id = ? AND status = 'OPEN'",
            (abandoned_at.isoformat(), shadow_id),
        )
        self._conn.commit()

    def save_forecast_record(self, record: ForecastRecord) -> None:
        self._conn.execute(
            "INSERT INTO forecasts (forecast_id, candidate_id, instrument, "
            "forecast_timestamp, horizon, scenario_probabilities, forecast_version, "
            "market_state_metadata) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(forecast_id) DO UPDATE SET "
            "candidate_id = excluded.candidate_id, instrument = excluded.instrument, "
            "forecast_timestamp = excluded.forecast_timestamp, horizon = excluded.horizon, "
            "scenario_probabilities = excluded.scenario_probabilities, "
            "forecast_version = excluded.forecast_version, "
            "market_state_metadata = excluded.market_state_metadata",
            (
                record.forecast_id,
                record.candidate_id,
                record.instrument,
                record.forecast_timestamp.isoformat(),
                record.horizon,
                json.dumps(record.scenario_probabilities),
                record.forecast_version,
                json.dumps(record.market_state_metadata),
            ),
        )
        self._conn.commit()

    def get_forecast_record(self, candidate_id: str) -> ForecastRecord | None:
        row = self._conn.execute(
            "SELECT * FROM forecasts WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["scenario_probabilities"] = json.loads(data["scenario_probabilities"])
        data["market_state_metadata"] = json.loads(data["market_state_metadata"])
        data["forecast_timestamp"] = datetime.fromisoformat(data["forecast_timestamp"])
        data["outcome_timestamp"] = (
            datetime.fromisoformat(data["outcome_timestamp"])
            if data["outcome_timestamp"] is not None
            else None
        )
        return ForecastRecord(**data)

    def record_ai_call_event(self, event: Event) -> None:
        self._insert_event(event)
        self._conn.commit()

    def count_ai_calls_since(self, cutoff: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE event_type = 'AI_CALL_MADE' "
            "AND occurred_at >= ?",
            (cutoff.isoformat(),),
        ).fetchone()
        return row["n"]

    def sum_ai_cost_since(self, cutoff: datetime) -> Decimal:
        """Kostnadsbudget (2026-09-03): samma Python-sidans Decimal-säkra
        aggregeringsmönster som sum_open_positions_notional() - undviker
        SQLite/float-precisionsproblem för pengar, och kräver ingen
        JSON1-SQL-funktion. Bara AI_CALL_MADE-rader existerar överhuvudtaget
        för anrop som faktiskt nådde modellen (Orchestrator skriver aldrig
        en sådan rad för ett anrop som aldrig fakturerades - se
        orchestrator.py::process_candidate()), så alla rader denna metod
        ser är redan giltiga att summera."""
        rows = self._conn.execute(
            "SELECT payload FROM events WHERE event_type = 'AI_CALL_MADE' "
            "AND occurred_at >= ?",
            (cutoff.isoformat(),),
        ).fetchall()
        return sum(
            (Decimal(json.loads(row["payload"]).get("cost_usd", "0")) for row in rows),
            Decimal("0"),
        )

    def start_run(self, run_id: str, run_type: str, started_at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO runs (run_id, run_type, started_at, status) VALUES (?,?,?,'running')",
            (run_id, run_type, started_at.isoformat()),
        )
        self._conn.commit()

    def complete_run(
        self,
        run_id: str,
        completed_at: datetime,
        status: str,
        errors: list[str],
        instruments_scanned: int | None = None,
    ) -> None:
        # Fas 6-fynd (code review 2026-08-29): redact() opererar bara på
        # dict-värden, så errors (en bar list[str]) gick tidigare förbi den
        # helt. Redigeras HÄR, vid persistering - inte bara vid visning -
        # så en secret aldrig ens når disk, oavsett vilken framtida
        # konsument (dashboard, Telegram debug-notis, ...) som senare läser
        # runs.errors.
        safe_errors = redact_error_list(errors)
        if instruments_scanned is not None:
            self._conn.execute(
                "UPDATE runs SET completed_at = ?, status = ?, errors = ?, "
                "instruments_scanned = ? WHERE run_id = ?",
                (
                    completed_at.isoformat(),
                    status,
                    json.dumps(safe_errors),
                    instruments_scanned,
                    run_id,
                ),
            )
        else:
            self._conn.execute(
                "UPDATE runs SET completed_at = ?, status = ?, errors = ? WHERE run_id = ?",
                (completed_at.isoformat(), status, json.dumps(safe_errors), run_id),
            )
        self._conn.commit()

    def count_candidates_created_since(self, cutoff: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE created_at >= ?", (cutoff.isoformat(),)
        ).fetchone()
        return row["n"]

    def count_candidates_by_status_since(self, status: str, cutoff: datetime) -> int:
        """Fas 6 daily report: `updated_at` (inte `created_at`) - en
        candidate skapad igår men som nådde `status` idag ska räknas mot
        idag, samma princip som `find_latest_candidate_by_instrument_and_
        status()` redan använder `updated_at` för statusövergångar."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE status = ? AND updated_at >= ?",
            (status, cutoff.isoformat()),
        ).fetchone()
        return row["n"]

    def count_runs_by_status_since(self, status: str, cutoff: datetime) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE status = ? AND started_at >= ?",
            (status, cutoff.isoformat()),
        ).fetchone()
        return row["n"]

    def sum_instruments_scanned_since(self, cutoff: datetime) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(instruments_scanned), 0) AS n FROM runs "
            "WHERE run_type = 'discovery' AND started_at >= ?",
            (cutoff.isoformat(),),
        ).fetchone()
        return row["n"]

    def find_no_trade_candidates_pending_notification(self) -> list[tuple[Candidate, list[str]]]:
        """Returnerar (candidate, gate_decisions.reasons) för varje NO_TRADE-
        candidate som inte redan notifierats (nyckel `NO_TRADE:{candidate_id}`).
        Klassificeringen "relevant" (decisions-nivå) vs "övrig" (debug-nivå)
        görs av notify_loop.py utifrån `reasons` - denna metod gör bara den
        redan persisterade kopplingen mellan candidates/gate_decisions/
        telegram_events tillgänglig, ingen egen tolkning."""
        rows = self._conn.execute(
            "SELECT c.candidate_id, g.reasons FROM candidates c "
            "LEFT JOIN gate_decisions g ON g.candidate_id = c.candidate_id "
            "WHERE c.status = 'NO_TRADE' "
            "AND ('NO_TRADE:' || c.candidate_id) NOT IN "
            "(SELECT telegram_event_id FROM telegram_events)"
        ).fetchall()
        result = []
        for row in rows:
            reasons = json.loads(row["reasons"]) if row["reasons"] is not None else []
            try:
                candidate = self.get_candidate(row["candidate_id"])
            except CorruptCandidateStateError:
                continue
            if candidate is not None:
                result.append((candidate, reasons))
        return result

    def find_error_runs_pending_notification(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT run_id, run_type, started_at, errors FROM runs WHERE status = 'error' "
            "AND ('error_run:' || run_id) NOT IN "
            "(SELECT telegram_event_id FROM telegram_events)"
        ).fetchall()
        return [dict(row) for row in rows]

    def record_telegram_event(
        self, telegram_event_id: str, notification_type: str, sent_at: datetime
    ) -> bool:
        """INSERT OR IGNORE - samma idempotenskontrakt som _insert_event()/
        AI_CALL_MADE-events (Fas 5): True bara om raden faktiskt är ny,
        False om notisen redan skickats (omkörning/omstart av notify_loop,
        Fas 6 §8.6)."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO telegram_events "
            "(telegram_event_id, notification_type, sent_at) VALUES (?, ?, ?)",
            (telegram_event_id, notification_type, sent_at.isoformat()),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def has_telegram_event_been_sent(self, telegram_event_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM telegram_events WHERE telegram_event_id = ?", (telegram_event_id,)
        ).fetchone()
        return row is not None

    def find_candidates_pending_notification(self, status: str) -> list[Candidate]:
        """Anti-join mot telegram_events, nycklad `f'{status}:{candidate_id}'`
        (Fas 6 Beslut 4) - snabbare och enklare än att replaya hela
        events-loggen, fortfarande härlett från samma materialiserade
        sanningskälla."""
        rows = self._conn.execute(
            "SELECT candidate_id FROM candidates WHERE status = ? "
            "AND (? || ':' || candidate_id) NOT IN "
            "(SELECT telegram_event_id FROM telegram_events)",
            (status, status),
        ).fetchall()
        result = []
        for row in rows:
            try:
                candidate = self.get_candidate(row["candidate_id"])
            except CorruptCandidateStateError:
                continue
            if candidate is not None:
                result.append(candidate)
        return result

    def find_positions_pending_notification(self) -> list[Position]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status = 'CLOSED' "
            "AND ('CLOSED:' || position_id) NOT IN "
            "(SELECT telegram_event_id FROM telegram_events)"
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def find_candidates_by_status(self, status: str) -> list[Candidate]:
        """Ett korrupt candidate-state (CorruptCandidateStateError) hoppas
        över - redan auditerat av get_candidate() innan den kastade - och
        avbryter ALDRIG behandlingen av övriga, giltiga candidates i samma
        anrop (SPEC fail-safe-princip: ett trasigt objekt får inte blockera
        resten av systemet)."""
        rows = self._conn.execute(
            "SELECT candidate_id FROM candidates WHERE status = ?", (status,)
        ).fetchall()
        result = []
        for row in rows:
            try:
                candidate = self.get_candidate(row["candidate_id"])
            except CorruptCandidateStateError:
                continue
            if candidate is not None:
                result.append(candidate)
        return result

    def find_all_candidates(self, limit: int, offset: int = 0) -> list[Candidate]:
        """Fas 7 (dashboard TRADE HISTORY): read-only, paginerad, samma
        korrupt-rad-hoppa-över-princip som find_candidates_by_status() - ett
        trasigt objekt får aldrig blockera resten av listan."""
        rows = self._conn.execute(
            "SELECT candidate_id FROM candidates ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        result = []
        for row in rows:
            try:
                candidate = self.get_candidate(row["candidate_id"])
            except CorruptCandidateStateError:
                continue
            if candidate is not None:
                result.append(candidate)
        return result

    def find_all_positions(self, limit: int, offset: int = 0) -> list[Position]:
        """Fas 7 (dashboard TRADE HISTORY): till skillnad från
        find_open_positions() inkluderar denna CLOSED-positioner - all
        historik, paginerad."""
        rows = self._conn.execute(
            "SELECT * FROM positions ORDER BY opened_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def get_gate_decision(self, candidate_id: str) -> dict | None:
        """Fas 7 (dashboard LIVE/TRADE HISTORY): den enda redan persisterade
        gate-utfallsraden per candidate, oformaterad."""
        row = self._conn.execute(
            "SELECT decision, reasons, evaluated_at FROM gate_decisions WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "decision": row["decision"],
            "reasons": json.loads(row["reasons"]),
            "evaluated_at": row["evaluated_at"],
        }

    def find_latest_run(self, run_type: str) -> dict | None:
        """Fas 7 (dashboard LIVE): senaste run av given typ, oformaterad."""
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_type = ? ORDER BY started_at DESC LIMIT 1", (run_type,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def find_latest_completed_run(self, run_type: str) -> dict | None:
        """Same as find_latest_run() but only ever returns a row whose
        completed_at is set - the only kind of row a catch-up/recovery
        mechanism can safely treat as 'this run genuinely finished'. A row
        stuck at status='running' means the process died mid-tick and must
        never be used as a time anchor."""
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_type = ? AND completed_at IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT 1",
            (run_type,),
        ).fetchone()
        return dict(row) if row is not None else None

    def find_recent_runs(self, limit: int, offset: int = 0) -> list[dict]:
        """Fas 7 (dashboard SYSTEM HEALTH): senaste runs oavsett typ,
        paginerad, oformaterad."""
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        return [dict(row) for row in rows]

    def find_all_forecasts(self, limit: int, offset: int = 0) -> list[ForecastRecord]:
        """Fas 7 (dashboard FORECAST): all forecast-historik, paginerad,
        samma deserialisering som get_forecast_record()."""
        rows = self._conn.execute(
            "SELECT * FROM forecasts ORDER BY forecast_timestamp DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["scenario_probabilities"] = json.loads(data["scenario_probabilities"])
            data["market_state_metadata"] = json.loads(data["market_state_metadata"])
            data["forecast_timestamp"] = datetime.fromisoformat(data["forecast_timestamp"])
            data["outcome_timestamp"] = (
                datetime.fromisoformat(data["outcome_timestamp"])
                if data["outcome_timestamp"] is not None
                else None
            )
            result.append(ForecastRecord(**data))
        return result

    def find_closed_positions(self) -> list[Position]:
        """Fas 8 (performance-mått): till skillnad från Fas 7:s paginerade
        find_all_positions() (le=500) är denna medvetet OBEGRÄNSAD - en
        aggregatberäkning över hela handelshistoriken (cumulative PnL,
        drawdown, win rate, ...) behöver alla rader, inte en sida. Ingen
        ORDER BY garanteras - performance/metrics.py sorterar själv internt
        på closed_at, litar aldrig på radordningen här."""
        rows = self._conn.execute("SELECT * FROM positions WHERE status = 'CLOSED'").fetchall()
        return [self._row_to_position(row) for row in rows]

    def find_forecasts_with_outcome(self) -> list[ForecastRecord]:
        """Fas 8 (kalibrering): samma medvetet obegränsade princip som
        find_closed_positions() ovan. actual_outcome IS NOT NULL - endast
        forecasts där ett utfall redan persisterats (av vilken mekanism som
        helst; ingen sådan mekanism finns ännu i denna fas, se
        PLAN_CRYPTO_PHASE8.md §0 - det garanterade default-resultatet är
        alltså en tom lista)."""
        rows = self._conn.execute(
            "SELECT * FROM forecasts WHERE actual_outcome IS NOT NULL"
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["scenario_probabilities"] = json.loads(data["scenario_probabilities"])
            data["market_state_metadata"] = json.loads(data["market_state_metadata"])
            data["forecast_timestamp"] = datetime.fromisoformat(data["forecast_timestamp"])
            data["outcome_timestamp"] = (
                datetime.fromisoformat(data["outcome_timestamp"])
                if data["outcome_timestamp"] is not None
                else None
            )
            result.append(ForecastRecord(**data))
        return result

    def find_closed_positions_pending_detective_analysis(self, limit: int) -> list[Position]:
        rows = self._conn.execute(
            "SELECT * FROM positions WHERE status = 'CLOSED' "
            "AND position_id NOT IN (SELECT position_id FROM detective_analyzed_positions) "
            "ORDER BY closed_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def count_closed_positions_pending_detective_analysis(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM positions WHERE status = 'CLOSED' "
            "AND position_id NOT IN (SELECT position_id FROM detective_analyzed_positions)"
        ).fetchone()
        return row["n"]

    def save_detective_analysis(self, record: DetectiveAnalysisRecord) -> None:
        try:
            self._conn.execute(
                "INSERT INTO detective_analyses (analysis_id, created_at, position_ids, "
                "win_count, loss_count, breakeven_count, status, observations, "
                "winning_patterns, losing_patterns, stats_snapshot, ai_cost_usd) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.analysis_id,
                    record.created_at.isoformat(),
                    json.dumps(record.position_ids),
                    record.win_count,
                    record.loss_count,
                    record.breakeven_count,
                    record.status,
                    json.dumps(record.observations),
                    json.dumps(record.winning_patterns),
                    json.dumps(record.losing_patterns),
                    json.dumps(record.stats_snapshot, default=str),
                    str(record.ai_cost_usd),
                ),
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO detective_analyzed_positions (position_id, analysis_id) "
                "VALUES (?, ?)",
                [(position_id, record.analysis_id) for position_id in record.position_ids],
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def find_detective_analyses(
        self, limit: int, offset: int = 0
    ) -> list[DetectiveAnalysisRecord]:
        rows = self._conn.execute(
            "SELECT * FROM detective_analyses ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["created_at"] = datetime.fromisoformat(data["created_at"])
            data["position_ids"] = json.loads(data["position_ids"])
            data["observations"] = json.loads(data["observations"])
            data["winning_patterns"] = json.loads(data["winning_patterns"])
            data["losing_patterns"] = json.loads(data["losing_patterns"])
            data["stats_snapshot"] = json.loads(data["stats_snapshot"])
            data["ai_cost_usd"] = Decimal(data["ai_cost_usd"])
            result.append(DetectiveAnalysisRecord(**data))
        return result

    def save_guardian_authority_decision(
        self,
        decision_id: str,
        position_id: str | None,
        candidate_id: str,
        decision_type: str,
        decided_at: datetime,
        reasoning: str,
        expected_outcome: str,
        expected_direction: str,
        confidence: float | None,
        run_id: str,
        old_sl: str | None = None,
        new_sl: str | None = None,
        intervention_applied: bool | None = None,
    ) -> bool:
        # Idempotency gate: decision_id is the PK, so a duplicate call for
        # the same decision (e.g. a restart) can never produce two rows or
        # silently overwrite the original expectation - same INSERT OR
        # IGNORE claim-style shape as claim_live_profit_protection().
        #
        # I2 hardening fix (2026-09-14): intervention_applied is purely
        # additive write-outcome-adjacent metadata (same category as the
        # already-existing old_sl/new_sl params above) - NOT one of the
        # requirement-10-protected expectation fields. Defaults to None
        # ("not yet determined" - see db.py's migration docstring); real
        # callers pass True (PRE_ENTRY_VETO/CLOSE_EARLY, known-successful at
        # save time) or leave it None (TIGHTEN_SL, determined moments later
        # via mark_guardian_authority_decision_intervention_applied below,
        # once the write attempt's outcome is known).
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO guardian_authority_decisions "
                "(decision_id, position_id, candidate_id, decision_type, decided_at, "
                "reasoning, expected_outcome, expected_direction, confidence, "
                "old_sl, new_sl, run_id, intervention_applied) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id,
                    position_id,
                    candidate_id,
                    decision_type,
                    decided_at.isoformat(),
                    reasoning,
                    expected_outcome,
                    expected_direction,
                    confidence,
                    old_sl,
                    new_sl,
                    run_id,
                    intervention_applied,
                ),
            )
            saved = cur.rowcount > 0
            self._conn.commit()
            return saved
        except Exception:
            self._conn.rollback()
            raise

    def get_guardian_authority_decision(self, decision_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM guardian_authority_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def find_pending_guardian_authority_decisions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM guardian_authority_decisions WHERE outcome_status = 'PENDING'"
        ).fetchall()
        return [dict(row) for row in rows]

    def find_resolved_guardian_authority_decisions(self) -> list[dict]:
        # Task 9's self-critique step (update_heuristics_from_resolved_
        # decisions) re-derives from ALL resolved decisions on every run -
        # same additive, same-shape read as find_pending_guardian_authority_
        # decisions above, just the opposite outcome_status filter.
        rows = self._conn.execute(
            "SELECT * FROM guardian_authority_decisions WHERE outcome_status = 'RESOLVED'"
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_guardian_authority_decision(
        self,
        decision_id: str,
        actual_exit_reason: str,
        actual_pnl_usdt: str,
        expectation_correct: bool | None,
        resolved_at: datetime,
    ) -> None:
        # Requirement 10: only the actual-outcome columns and outcome_status/
        # resolved_at are ever written here - expected_outcome/
        # expected_direction/confidence/decided_at/reasoning are NEVER
        # referenced in this UPDATE, so they stay exactly as recorded at
        # save_guardian_authority_decision() time, immutable by construction.
        #
        # Task 8 widening: expectation_correct is bool | None (was bool) -
        # purely additive, the DB column already has no NOT NULL constraint.
        # No special-casing needed here: sqlite3's parameter binding already
        # converts a Python None to SQL NULL for any placeholder (same as
        # every other nullable column already written via this module, e.g.
        # old_sl/new_sl in save_guardian_authority_decision below), and a
        # Python bool binds as SQL INTEGER 0/1 exactly as before - confirmed
        # by test_resolve_guardian_authority_decision_accepts_none_
        # expectation_correct in test_repository_guardian_authority.py.
        self._conn.execute(
            "UPDATE guardian_authority_decisions SET outcome_status = 'RESOLVED', "
            "actual_exit_reason = ?, actual_pnl_usdt = ?, expectation_correct = ?, "
            "resolved_at = ? WHERE decision_id = ?",
            (
                actual_exit_reason,
                actual_pnl_usdt,
                expectation_correct,
                resolved_at.isoformat(),
                decision_id,
            ),
        )
        self._conn.commit()

    def mark_guardian_authority_decision_intervention_applied(
        self, decision_id: str, applied: bool, updated_at: datetime
    ) -> None:
        # I2 hardening fix (2026-09-14): touches ONLY the
        # intervention_applied column - same surgical-scope discipline as
        # resolve_guardian_authority_decision immediately above (never
        # referencing the requirement-10-protected expectation columns).
        # `updated_at` is accepted for interface consistency with sibling
        # repo methods but intentionally NOT bound into the UPDATE below -
        # same precedent as tighten_position_stop_loss's own unused
        # `updated_at` parameter (Task 4, see that method above): there is
        # no generic "last updated" column on guardian_authority_decisions
        # to bind it to, and adding one solely to hold this timestamp would
        # be scope creep this fix does not need.
        self._conn.execute(
            "UPDATE guardian_authority_decisions SET intervention_applied = ? "
            "WHERE decision_id = ?",
            (applied, decision_id),
        )
        self._conn.commit()

    def find_guardian_authority_heuristics(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM guardian_authority_heuristics"
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_guardian_authority_heuristic(
        self,
        heuristic_id: str,
        description: str,
        condition_json: str,
        adjustment: float,
        confidence: float,
        sample_size: int,
        updated_at: datetime,
    ) -> None:
        # INSERT OR REPLACE: unlike guardian_authority_decisions (which uses
        # INSERT OR IGNORE for immutable expectations), heuristics evolve and
        # are meant to be refined. A second upsert with the same heuristic_id
        # but different field values will overwrite the original row (spec
        # requirement: heuristics are living rules, continuously refined).
        self._conn.execute(
            "INSERT OR REPLACE INTO guardian_authority_heuristics "
            "(heuristic_id, description, condition_json, adjustment, confidence, "
            "sample_size, updated_at) VALUES (?,?,?,?,?,?,?)",
            (
                heuristic_id,
                description,
                condition_json,
                adjustment,
                confidence,
                sample_size,
                updated_at.isoformat(),
            ),
        )
        self._conn.commit()

    def tighten_position_stop_loss(
        self, position_id: str, new_stop_loss: Decimal, updated_at: datetime
    ) -> bool:
        """Task 4: Updates positions.stop_loss only if new_stop_loss > stop_loss
        (enforced in a single atomic WHERE clause to prevent race conditions).
        Returns True if the update applied, False if the position doesn't exist
        or the guard condition rejected the tightening (equal-or-lower new SL).
        """
        try:
            cur = self._conn.execute(
                "UPDATE positions SET stop_loss = ? WHERE position_id = ? "
                "AND CAST(? AS REAL) > CAST(stop_loss AS REAL)",
                (str(new_stop_loss), position_id, str(new_stop_loss)),
            )
            updated = cur.rowcount > 0
            self._conn.commit()
            return updated
        except Exception:
            self._conn.rollback()
            raise

    # --- Guardian Authority LIVE stop-loss tightening (Task 5) ------------
    # Deliberate, exact mirror of the claim_/get_/find_claimed_/update_/set_
    # quintet above for live_profit_protection, against the SEPARATE
    # guardian_authority_live_sl_actions table. Never merged with PP's
    # methods and never pointed at PP's table: the two mechanisms must be
    # able to claim the same position_id independently, each in its own
    # table, with its own primary key.

    def claim_guardian_authority_live_sl_action(
        self,
        position_id: str,
        new_sl_price: str,
        new_sl_client_order_id: str,
        claimed_at: datetime,
    ) -> bool:
        # Idempotency gate is the position_id primary key itself (INSERT OR
        # IGNORE), identical to claim_live_profit_protection: the caller
        # (crypto_trading/guardian/authority_live.py) verifies the real live
        # position before ever calling this, so no WHERE EXISTS positions
        # race-guard is needed. Returns False when a row already exists -
        # that is the whole concurrency defense against two observations
        # trying to tighten the same position at once.
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO guardian_authority_live_sl_actions "
                "(position_id, status, new_sl_price, new_sl_client_order_id, "
                "claimed_at, updated_at) VALUES (?, 'CLAIMED', ?, ?, ?, ?)",
                (
                    position_id,
                    new_sl_price,
                    new_sl_client_order_id,
                    claimed_at.isoformat(),
                    claimed_at.isoformat(),
                ),
            )
            claimed = cur.rowcount > 0
            self._conn.commit()
            return claimed
        except Exception:
            self._conn.rollback()
            raise

    def get_guardian_authority_live_sl_action(self, position_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM guardian_authority_live_sl_actions WHERE position_id = ?",
            (position_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def find_claimed_guardian_authority_live_sl_actions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM guardian_authority_live_sl_actions WHERE status = 'CLAIMED'"
        ).fetchall()
        return [dict(row) for row in rows]

    def update_guardian_authority_live_sl_action_old_sl(
        self, position_id: str, old_sl_order_id: str, old_sl_price: str, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE guardian_authority_live_sl_actions SET old_sl_order_id = ?, "
            "old_sl_price = ?, updated_at = ? WHERE position_id = ?",
            (old_sl_order_id, old_sl_price, updated_at.isoformat(), position_id),
        )
        self._conn.commit()

    def update_guardian_authority_live_sl_action_new_sl(
        self, position_id: str, new_sl_order_id: str, updated_at: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE guardian_authority_live_sl_actions SET new_sl_order_id = ?, "
            "updated_at = ? WHERE position_id = ?",
            (new_sl_order_id, updated_at.isoformat(), position_id),
        )
        self._conn.commit()

    def set_guardian_authority_live_sl_action_status(
        self, position_id: str, status: str, updated_at: datetime, last_error: str | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE guardian_authority_live_sl_actions SET status = ?, last_error = ?, "
            "updated_at = ? WHERE position_id = ?",
            (status, last_error, updated_at.isoformat(), position_id),
        )
        self._conn.commit()

    def seed_guardian_authority_shadow(
        self,
        shadow_id: str,
        position_id: str,
        candidate_id: str,
        instrument: str,
        opened_at: datetime,
        created_at: datetime,
        run_id: str,
    ) -> bool:
        # Same INSERT OR IGNORE claim-style idempotency as
        # seed_profit_protection_shadow - a duplicate seed call (e.g. a
        # retried tick) can never produce two rows or silently overwrite
        # the original opened_at/instrument/candidate_id.
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO guardian_authority_shadow_observations "
            "(shadow_id, position_id, candidate_id, instrument, opened_at, status, "
            "mfe, mae, created_at, updated_at, run_id) "
            "VALUES (?, ?, ?, ?, ?, 'OBSERVING', '0', '0', ?, ?, ?)",
            (
                shadow_id, position_id, candidate_id, instrument,
                opened_at.isoformat(), created_at.isoformat(), created_at.isoformat(),
                run_id,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_guardian_authority_shadow(self, shadow_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM guardian_authority_shadow_observations WHERE shadow_id = ?",
            (shadow_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def find_open_guardian_authority_shadows(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM guardian_authority_shadow_observations "
            "WHERE status IN ('OBSERVING', 'DECIDED')"
        ).fetchall()
        return [dict(row) for row in rows]

    def record_guardian_authority_shadow_tick(
        self,
        shadow_id: str,
        mfe: Decimal,
        mae: Decimal,
        factors_json: str,
        updated_at: datetime,
    ) -> None:
        # Same open-only guard as record_profit_protection_tick's own
        # WHERE status = 'OPEN', widened to this table's two open statuses
        # (OBSERVING/DECIDED) - a tick arriving after RESOLVED/ABANDONED is
        # a structural no-op, mfe/mae/last_factors_json stay frozen at
        # whatever they were at resolution/abandonment.
        self._conn.execute(
            "UPDATE guardian_authority_shadow_observations SET mfe = ?, mae = ?, "
            "last_factors_json = ?, updated_at = ? "
            "WHERE shadow_id = ? AND status IN ('OBSERVING', 'DECIDED')",
            (str(mfe), str(mae), factors_json, updated_at.isoformat(), shadow_id),
        )
        self._conn.commit()

    def decide_guardian_authority_shadow(
        self,
        shadow_id: str,
        decision: str,
        decided_at: datetime,
        expected_outcome: str,
        expected_direction: str,
        confidence: float,
        factors_json: str,
        proposed_new_sl: Decimal | None,
        updated_at: datetime,
    ) -> bool:
        # The ONE write that transitions OBSERVING -> DECIDED. WHERE
        # status = 'OBSERVING' makes a second call structurally a no-op -
        # it changes nothing and returns False - same requirement-10-style
        # one-time-transition discipline as guardian_authority_decisions'
        # own save_guardian_authority_decision (there via INSERT OR IGNORE
        # on a PK; here via a status-guarded UPDATE since the row already
        # exists from seed_guardian_authority_shadow).
        cur = self._conn.execute(
            "UPDATE guardian_authority_shadow_observations SET status = 'DECIDED', "
            "shadow_decision = ?, decided_at = ?, expected_outcome = ?, "
            "expected_direction = ?, confidence = ?, factors_json = ?, "
            "proposed_new_sl = ?, updated_at = ? "
            "WHERE shadow_id = ? AND status = 'OBSERVING'",
            (
                decision,
                decided_at.isoformat(),
                expected_outcome,
                expected_direction,
                confidence,
                factors_json,
                str(proposed_new_sl) if proposed_new_sl is not None else None,
                updated_at.isoformat(),
                shadow_id,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def resolve_guardian_authority_shadow_no_action(
        self,
        shadow_id: str,
        factors_json: str,
        actual_exit_reason: str,
        actual_pnl_usdt: Decimal,
        actual_closed_at: datetime,
        updated_at: datetime,
    ) -> bool:
        # Fires only for a position that closed while still OBSERVING
        # (never decided) - WHERE status = 'OBSERVING' makes this
        # structurally exclusive with decide_guardian_authority_shadow:
        # whichever transition happens first wins, the other becomes a
        # no-op. One write sets the decision-only subset (shadow_decision/
        # expected_direction/confidence/factors_json - no decided_at/
        # expected_outcome/proposed_new_sl, since no decision was ever
        # actually registered contemporaneously) AND the baseline fields
        # AND status = 'RESOLVED', all at once.
        cur = self._conn.execute(
            "UPDATE guardian_authority_shadow_observations SET status = 'RESOLVED', "
            "shadow_decision = 'NO_ACTION', expected_direction = 'neutral', "
            "confidence = 1.0, factors_json = ?, actual_exit_reason = ?, "
            "actual_pnl_usdt = ?, actual_closed_at = ?, updated_at = ? "
            "WHERE shadow_id = ? AND status = 'OBSERVING'",
            (
                factors_json,
                actual_exit_reason,
                str(actual_pnl_usdt),
                actual_closed_at.isoformat(),
                updated_at.isoformat(),
                shadow_id,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def resolve_guardian_authority_shadow_decided(
        self,
        shadow_id: str,
        actual_exit_reason: str,
        actual_pnl_usdt: Decimal,
        actual_closed_at: datetime,
        expectation_correct: bool | None,
        prediction_error: float | None,
        updated_at: datetime,
    ) -> bool:
        # Fires only for a position that closed AFTER a shadow decision was
        # already registered - WHERE status = 'DECIDED' is the mirror-image
        # guard of resolve_guardian_authority_shadow_no_action's own WHERE
        # status = 'OBSERVING'. Never touches shadow_decision/decided_at/
        # expected_outcome/expected_direction/confidence/factors_json/
        # proposed_new_sl - those stay exactly as decide_guardian_authority_
        # shadow set them, immutable by construction (same surgical-scope
        # discipline as resolve_guardian_authority_decision).
        cur = self._conn.execute(
            "UPDATE guardian_authority_shadow_observations SET status = 'RESOLVED', "
            "actual_exit_reason = ?, actual_pnl_usdt = ?, actual_closed_at = ?, "
            "expectation_correct = ?, prediction_error = ?, updated_at = ? "
            "WHERE shadow_id = ? AND status = 'DECIDED'",
            (
                actual_exit_reason,
                str(actual_pnl_usdt),
                actual_closed_at.isoformat(),
                expectation_correct,
                prediction_error,
                updated_at.isoformat(),
                shadow_id,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def abandon_guardian_authority_shadow(self, shadow_id: str, abandoned_at: datetime) -> None:
        # Same orphan-handling semantics as abandon_profit_protection_
        # shadow: a no-op if the shadow is not currently OBSERVING/DECIDED,
        # same guard style as close_profit_protection_shadow's own WHERE
        # status = 'OPEN'.
        self._conn.execute(
            "UPDATE guardian_authority_shadow_observations SET status = 'ABANDONED', "
            "updated_at = ? WHERE shadow_id = ? AND status IN ('OBSERVING', 'DECIDED')",
            (abandoned_at.isoformat(), shadow_id),
        )
        self._conn.commit()

    def find_resolved_guardian_authority_shadows(self) -> list[dict]:
        # Consumed by Task 9 and Task 10 (self-critique / reporting).
        rows = self._conn.execute(
            "SELECT * FROM guardian_authority_shadow_observations WHERE status = 'RESOLVED'"
        ).fetchall()
        return [dict(row) for row in rows]

    def find_abandoned_guardian_authority_shadows(self) -> list[dict]:
        # Task 9 fix round 1: ABANDONED (set by abandon_guardian_authority_
        # shadow, above) is a fourth, real, production-reachable status on
        # this table - a plain read, same SELECT-by-status shape as
        # find_resolved_guardian_authority_shadows just above, so the
        # report can count these rows instead of silently dropping them.
        rows = self._conn.execute(
            "SELECT * FROM guardian_authority_shadow_observations WHERE status = 'ABANDONED'"
        ).fetchall()
        return [dict(row) for row in rows]

    def find_guardian_authority_shadow_heuristics(self) -> list[dict]:
        # Task 8 (self-critique-from-shadow-data): unfiltered SELECT *,
        # same shape as find_guardian_authority_heuristics above, but
        # against the SEPARATE guardian_authority_shadow_heuristics table -
        # never read by the real decision engine (evaluate_heuristics /
        # decide_pre_entry / decide_open_position), only by
        # update_shadow_heuristics_from_resolved_shadow_observations and,
        # later, reporting.
        rows = self._conn.execute(
            "SELECT * FROM guardian_authority_shadow_heuristics"
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_guardian_authority_shadow_heuristic(
        self,
        heuristic_id: str,
        description: str,
        condition_json: str,
        adjustment: float,
        confidence: float,
        sample_size: int,
        updated_at: datetime,
    ) -> None:
        # Same INSERT OR REPLACE semantics as upsert_guardian_authority_
        # heuristic above (heuristics evolve, refining an existing row on
        # a re-run is correct) - targeting the separate shadow table only.
        self._conn.execute(
            "INSERT OR REPLACE INTO guardian_authority_shadow_heuristics "
            "(heuristic_id, description, condition_json, adjustment, confidence, "
            "sample_size, updated_at) VALUES (?,?,?,?,?,?,?)",
            (
                heuristic_id,
                description,
                condition_json,
                adjustment,
                confidence,
                sample_size,
                updated_at.isoformat(),
            ),
        )
        self._conn.commit()

    def save_guardian_authority_pre_entry_shadow(
        self,
        shadow_id: str,
        candidate_id: str,
        instrument: str,
        shadow_decision: str,
        expected_outcome: str,
        expected_direction: str,
        confidence: float,
        factors_json: str,
        run_id: str,
        created_at: datetime,
    ) -> bool:
        # Single-shot INSERT OR IGNORE, same claim-style idempotency as
        # seed_guardian_authority_shadow - a duplicate save call (e.g. a
        # retried candidate-confirm) can never produce two rows or silently
        # overwrite the original decision. Pre-entry has no separate "decide"
        # step (unlike the tick-time table): every decision-shaped column is
        # set here, at once, immutable from then on. shadow_id IS
        # candidate_id (controller simplification - see db.py's schema
        # comment) - no position_id column, no link step. created_at is
        # reused for updated_at too, same convention as seed_guardian_
        # authority_shadow's own created_at/updated_at pairing.
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO guardian_authority_shadow_pre_entry_observations "
            "(shadow_id, candidate_id, instrument, shadow_decision, expected_outcome, "
            "expected_direction, confidence, factors_json, status, created_at, "
            "updated_at, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)",
            (
                shadow_id,
                candidate_id,
                instrument,
                shadow_decision,
                expected_outcome,
                expected_direction,
                confidence,
                factors_json,
                created_at.isoformat(),
                created_at.isoformat(),
                run_id,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_guardian_authority_pre_entry_shadow(self, shadow_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM guardian_authority_shadow_pre_entry_observations "
            "WHERE shadow_id = ?",
            (shadow_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def find_pending_guardian_authority_pre_entry_shadows(self) -> list[dict]:
        # No position_id filter - shadow_id already IS the position_id a
        # real position would use (controller simplification), so there is
        # no separate NULL/non-NULL link state to filter on here. A later
        # task's resolution logic checks repo.get_position(shadow_id)
        # itself to find out whether a real position exists yet.
        rows = self._conn.execute(
            "SELECT * FROM guardian_authority_shadow_pre_entry_observations "
            "WHERE status = 'PENDING'"
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_guardian_authority_pre_entry_shadow(
        self,
        shadow_id: str,
        actual_exit_reason: str,
        actual_pnl_usdt: Decimal,
        actual_closed_at: datetime,
        updated_at: datetime,
    ) -> bool:
        # WHERE status = 'PENDING' makes a second/out-of-order resolve call
        # a structural no-op - same one-time-transition discipline as every
        # other resolve method in this module. expectation_correct is
        # deliberately never referenced here: per the design spec and the
        # controller's Task 8-aligned ruling, both APPROVE and
        # PRE_ENTRY_VETO shadow rows have no counterfactual to score, so the
        # column stays NULL forever, by construction, for every row of this
        # table.
        cur = self._conn.execute(
            "UPDATE guardian_authority_shadow_pre_entry_observations "
            "SET status = 'RESOLVED', actual_exit_reason = ?, actual_pnl_usdt = ?, "
            "actual_closed_at = ?, updated_at = ? "
            "WHERE shadow_id = ? AND status = 'PENDING'",
            (
                actual_exit_reason,
                str(actual_pnl_usdt),
                actual_closed_at.isoformat(),
                updated_at.isoformat(),
                shadow_id,
            ),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def find_resolved_guardian_authority_pre_entry_shadows(self) -> list[dict]:
        # Consumed by Task 10's report only (Task 9's self-critique is
        # TIGHTEN_SL-only, same scope note as the real Task 9 - pre-entry
        # shadow rows never feed self-critique).
        rows = self._conn.execute(
            "SELECT * FROM guardian_authority_shadow_pre_entry_observations "
            "WHERE status = 'RESOLVED'"
        ).fetchall()
        return [dict(row) for row in rows]
