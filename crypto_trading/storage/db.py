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

-- Detective (Post-Trade Analyst, 2026-09-04): analyserar EFTERHAND redan
-- stängda PAPER-trades, batchvis. Refererar bara till position_ids (ingen
-- duplicerad trade-/evidensdata - se schemas/detective.py::
-- DetectiveAnalysisRecord).
CREATE TABLE IF NOT EXISTS detective_analyses (
    analysis_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    position_ids TEXT NOT NULL,
    win_count INTEGER NOT NULL,
    loss_count INTEGER NOT NULL,
    breakeven_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    observations TEXT NOT NULL,
    winning_patterns TEXT NOT NULL,
    losing_patterns TEXT NOT NULL,
    stats_snapshot TEXT NOT NULL,
    ai_cost_usd TEXT NOT NULL
);

-- Restart-säker "redan analyserad"-markering (samma anti-join-mönster som
-- telegram_events ovan) - ingen separat cursor/pekare som kan hamna fel.
CREATE TABLE IF NOT EXISTS detective_analyzed_positions (
    position_id TEXT PRIMARY KEY,
    analysis_id TEXT NOT NULL
);

-- BingX Demo (VST) execution (2026-09-04): strictly additive parallel
-- observer of an already-Gate-approved PAPER position, never the other way
-- around - this table is NEVER joined-into or written-from
-- position_opening.py/position_closing.py, see
-- docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md.
-- phase: CLAIMED -> ACTIVE -> CLOSED / FAILED. Claim-before-place
-- idempotency: position_id is the PK, so a duplicate POSITION_OPENED
-- observation or a restart can never produce two demo orders for the same
-- position (INSERT OR IGNORE in repository.py::claim_demo_execution()).
CREATE TABLE IF NOT EXISTS demo_executions (
    position_id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    entry_client_order_id TEXT,
    entry_exchange_order_id TEXT,
    entry_quantity TEXT,
    sl_exchange_order_id TEXT,
    tp_exchange_order_id TEXT,
    exit_reason TEXT,
    exchange_fill_entry TEXT,
    exchange_fill_exit TEXT,
    last_error TEXT,
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
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
    _migrate_runs_add_instruments_scanned(conn)
    _migrate_candidates_add_reference_price(conn)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _migrate_runs_add_instruments_scanned(conn: sqlite3.Connection) -> None:
    """Fas 6 daily report (2026-08-29): runs.instruments_scanned lades till
    EFTER att riktiga produktionsdatabaser (data/crypto_trading.db) redan
    existerade med den gamla runs-strukturen. `CREATE TABLE IF NOT EXISTS`
    ovan gör INGENTING mot en redan existerande tabell - en explicit,
    idempotent `ALTER TABLE` krävs. `_SCHEMA` innehåller MEDVETET inte
    denna kolumn i sin egen `runs`-definition, så att både en helt ny
    databas och en redan existerande går via exakt samma kodväg här,
    istället för två divergerande sätt att få kolumnen. Kontrolleras via
    `PRAGMA table_info` (inte "IF NOT EXISTS" på `ALTER TABLE`, som inte
    stöds av alla SQLite-versioner) - säker att köra om vid varje
    anslutning, förstör aldrig befintliga rader (nya kolumnen blir NULL
    för dem, aldrig ett fel eller en gissning)."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    if "instruments_scanned" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN instruments_scanned INTEGER")


def _migrate_candidates_add_reference_price(conn: sqlite3.Connection) -> None:
    """Root-cause-fix (2026-09-02): candidates.reference_price - det
    faktiska referenspris (senaste ticker-pris vid evidens-tillfället) som
    Risk Agent behöver för att kunna svara med ett absolut, Decimal-
    parsbart suggested_stop_loss/suggested_target istället för en
    kvalitativ beskrivning (som alltid misslyckades parsningen i
    paper_trading/position_opening.py - 0/10 CONFIRMED-kandidater öppnade
    någonsin en position). Samma migreringsmönster och samma motivering
    som _migrate_runs_add_instruments_scanned() ovan - lades till EFTER att
    riktiga produktionsdatabaser redan existerade utan kolumnen."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(candidates)").fetchall()}
    if "reference_price" not in columns:
        conn.execute("ALTER TABLE candidates ADD COLUMN reference_price TEXT")
