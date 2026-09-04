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
