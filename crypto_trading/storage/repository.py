from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

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
from crypto_trading.schemas.event import Event
from crypto_trading.schemas.evidence import CandidateEvidenceRecord
from crypto_trading.schemas.forecast import ForecastRecord
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
    ) -> None: ...
    def start_run(self, run_id: str, run_type: str, started_at: datetime) -> None: ...
    def complete_run(
        self, run_id: str, completed_at: datetime, status: str, errors: list[str]
    ) -> None: ...
    def record_ai_call_event(self, event: Event) -> None: ...
    def count_ai_calls_since(self, cutoff: datetime) -> int: ...
    def save_forecast_record(self, record: ForecastRecord) -> None: ...
    def get_forecast_record(self, candidate_id: str) -> ForecastRecord | None: ...


class SQLiteRepository:
    def __init__(self, path: Path, busy_timeout_ms: int = 5000):
        self._conn = get_connection(path, busy_timeout_ms=busy_timeout_ms)

    def create_candidate_with_event(self, candidate: Candidate, event: Event) -> bool:
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO candidates "
                "(candidate_id, idempotency_key, instrument, discovery_run_id, evidence_hash, "
                "status, evidence_record, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
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
    ) -> None:
        try:
            self._conn.execute(
                "UPDATE positions SET status = 'CLOSED', theoretical_exit = ?, "
                "simulated_fill_exit = ?, exit_reason = ?, fees = ?, funding = ?, closed_at = ? "
                "WHERE position_id = ?",
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
            self._insert_event(event)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

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

    def start_run(self, run_id: str, run_type: str, started_at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO runs (run_id, run_type, started_at, status) VALUES (?,?,?,'running')",
            (run_id, run_type, started_at.isoformat()),
        )
        self._conn.commit()

    def complete_run(
        self, run_id: str, completed_at: datetime, status: str, errors: list[str]
    ) -> None:
        self._conn.execute(
            "UPDATE runs SET completed_at = ?, status = ?, errors = ? WHERE run_id = ?",
            (completed_at.isoformat(), status, json.dumps(errors), run_id),
        )
        self._conn.commit()

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
