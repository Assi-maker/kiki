from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    run_id TEXT,
    schema_version INTEGER NOT NULL,
    payload TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only: DELETE is not permitted');
END;

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    instrument TEXT NOT NULL,
    discovery_run_id TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    evidence_record TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_instrument_status
    ON candidates(instrument, status, created_at);

CREATE TABLE IF NOT EXISTS assessments (
    candidate_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (candidate_id, field_name)
);

CREATE TABLE IF NOT EXISTS gate_decisions (
    candidate_id TEXT PRIMARY KEY,
    decision TEXT NOT NULL,
    reasons TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);

-- positions TÄCKER hela livscykeln öppen->stängd (ingen separat trades-tabell,
-- se "Implementationsanmärkningar" i planens header).
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    theoretical_entry TEXT NOT NULL,
    simulated_fill_entry TEXT NOT NULL,
    stop_loss TEXT NOT NULL,
    target TEXT NOT NULL,
    size TEXT NOT NULL,
    fill_model_version TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    theoretical_exit TEXT,
    simulated_fill_exit TEXT,
    exit_reason TEXT,
    fees TEXT,
    funding TEXT,
    closed_at TEXT
);

-- forecasts har utfallsfälten inbyggda (ingen separat forecast_outcomes-tabell,
-- se "Implementationsanmärkningar" i planens header).
CREATE TABLE IF NOT EXISTS forecasts (
    forecast_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    forecast_timestamp TEXT NOT NULL,
    horizon TEXT NOT NULL,
    scenario_probabilities TEXT NOT NULL,
    forecast_version TEXT NOT NULL,
    market_state_metadata TEXT NOT NULL,
    actual_outcome TEXT,
    outcome_timestamp TEXT
);

CREATE TABLE IF NOT EXISTS telegram_events (
    telegram_event_id TEXT PRIMARY KEY,
    notification_type TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT NOT NULL,
    run_type TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    status TEXT,
    errors TEXT
);
"""


def get_connection(path: Path, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # busy_timeout sätts före journal_mode=WAL som god praxis (gäller alla
    # efterföljande statements på anslutningen), men ENSAM räcker den INTE
    # för just journal_mode=WAL: SQLite verkar inte konsekvent respektera
    # busy_timeout-återförsöket för WAL-aktiveringens exklusiva lås när två
    # anslutningar råkar aktivera WAL på samma helt nya, ännu icke-
    # existerande fil samtidigt (upptäckt vid AC3-live-körningen 2026-08-29 -
    # run.py::main() startar discovery- och monitoring-tråden utan
    # synkronisering, båda mot samma nya fil - bekräftat empiriskt: samma
    # sqlite3.OperationalError: database is locked kvarstod efter bara
    # pragma-ordningsbytet, se tests/crypto_trading/storage/
    # test_repository_concurrency.py::
    # test_two_repositories_can_initialize_concurrently_on_a_brand_new_database_file).
    # Löst med en explicit, bounded retry-loop runt just detta anrop -
    # validerat 0/30 misslyckanden mot ordningsbytets ensamma ~15-20%.
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    _set_wal_mode_with_retry(conn, busy_timeout_ms)
    init_schema(conn)
    return conn


def _set_wal_mode_with_retry(conn: sqlite3.Connection, busy_timeout_ms: int) -> None:
    deadline = time.monotonic() + (busy_timeout_ms / 1000)
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
