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

-- BingX Live execution (2026-09-06): strictly additive third parallel
-- observer of an already-Gate-approved PAPER position (alongside PAPER's
-- own `positions` and BingX Demo's `demo_executions`) - never joined-into
-- or written-from position_opening.py/position_closing.py or
-- demo_execution.py, see
-- docs/superpowers/specs/2026-09-06-bingx-live-execution-design.md.
-- phase: CLAIMED -> ENTRY_SUBMITTED -> ACTIVE -> CLOSED / FAILED / SKIPPED
-- (SKIPPED: capacity/margin/exchange-minimum prevented this position from
-- ever getting a live order - a safe, expected outcome, not an error).
-- margin_usdt/notional_usdt/leverage are recorded per-row even though
-- currently constant (10/100/10) - audit trail if the fixed values ever
-- change. realized_fees_usdt/realized_funding_usdt are populated at close
-- from BingX's own income/commission data, NULL when not yet known/N/A.
CREATE TABLE IF NOT EXISTS live_executions (
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
    margin_usdt TEXT,
    notional_usdt TEXT,
    leverage TEXT,
    realized_fees_usdt TEXT,
    realized_funding_usdt TEXT,
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

-- LIVE Profit Protection (2026-09-13): one row per LIVE position, created
-- (claimed) the moment a PP attempt starts - never before. See
-- docs/superpowers/specs/2026-09-13-live-profit-protection-design.md
-- "Data model" for the exact status values and their meaning. Idempotency
-- gate is the position_id primary key itself (INSERT OR IGNORE) - unlike
-- live_executions' claim, no WHERE EXISTS positions race-guard is needed
-- here because the caller already verifies the live position itself before
-- ever calling the claim. old_sl_order_id/old_sl_price and
-- new_sl_order_id are unknown at claim time - populated by later updates
-- once the exchange state is observed.
CREATE TABLE IF NOT EXISTS live_profit_protection (
    position_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    threshold_pct TEXT NOT NULL,
    trigger_mark_price TEXT,
    breakeven_price TEXT,
    old_sl_order_id TEXT,
    old_sl_price TEXT,
    new_sl_client_order_id TEXT NOT NULL,
    new_sl_order_id TEXT,
    last_error TEXT,
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Position Guardian (2026-09-04): strictly append-only, shadow-mode-only
-- observer of an already-open PAPER position, see
-- docs/superpowers/specs/2026-09-04-position-guardian-design.md.
-- NEVER written from paper_trading/position_opening.py or
-- position_closing.py - Guardian only reads `positions`, never writes it.
CREATE TABLE IF NOT EXISTS guardian_observations (
    observation_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    state TEXT NOT NULL,
    decay_score TEXT NOT NULL,
    progress_ratio TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    factors TEXT NOT NULL,
    ai_reasoning TEXT,
    ai_cost_usd TEXT,
    run_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_guardian_observations_position
    ON guardian_observations(position_id, observed_at);

CREATE TRIGGER IF NOT EXISTS guardian_observations_no_update
BEFORE UPDATE ON guardian_observations
BEGIN
    SELECT RAISE(ABORT, 'guardian_observations is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS guardian_observations_no_delete
BEFORE DELETE ON guardian_observations
BEGIN
    SELECT RAISE(ABORT, 'guardian_observations is append-only: DELETE is not permitted');
END;

-- Profit Protection PAPER shadow experiment (2026-09-11) - strictly
-- additive shadow simulation of an already-open PAPER position, see
-- docs/superpowers/specs/2026-09-11-profit-protection-experiment-design.md.
-- NEVER joined-into or written-from position_opening.py/position_closing.py,
-- never read by Gate/Risk/Guardian/LIVE. One row per (position_id,
-- threshold_pct) - at most 2 rows per real position (FROZEN_THRESHOLDS_PCT
-- always has exactly two values). The one-time activation watermark for
-- this feature lives in schema_meta (key 'profit_protection_activated_at'),
-- same pattern as recovery_sweep_activated_at - deliberately no separate
-- table for that.
CREATE TABLE IF NOT EXISTS profit_protection_shadow_positions (
    shadow_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    threshold_pct TEXT NOT NULL,
    entry_price TEXT NOT NULL,
    original_stop_loss TEXT NOT NULL,
    target TEXT NOT NULL,
    threshold_price TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    status TEXT NOT NULL,
    threshold_reached INTEGER NOT NULL DEFAULT 0,
    threshold_reached_at TEXT,
    breakeven_stop_loss TEXT,
    mfe TEXT NOT NULL DEFAULT '0',
    mae TEXT NOT NULL DEFAULT '0',
    exit_reason TEXT,
    theoretical_exit TEXT,
    simulated_fill_exit TEXT,
    fees TEXT,
    funding TEXT,
    closed_at TEXT,
    shadow_realized_pnl TEXT,
    hypothetical_baseline_exit_reason TEXT,
    hypothetical_baseline_pnl TEXT,
    pnl_difference TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pp_shadow_position
    ON profit_protection_shadow_positions(position_id);
CREATE INDEX IF NOT EXISTS idx_pp_shadow_status
    ON profit_protection_shadow_positions(status);

-- Guardian Authority decisions (2026-09-14): decision-only memory for the
-- autonomous Guardian Authority extension, see
-- docs/superpowers/specs/2026-09-14-guardian-authority-design.md "Memory /
-- self-improvement". Only actual interventions (PRE_ENTRY_VETO, TIGHTEN_SL,
-- CLOSE_EARLY) get a row - the default outcomes (implicit APPROVE/
-- NO_ACTION) are never logged here, keeping this table a small decision log
-- layered on top of Guardian's own existing per-tick guardian_observations,
-- never a duplicate of it. position_id is NULL for a PRE_ENTRY_VETO (no
-- position exists yet). expected_outcome/expected_direction/confidence/
-- decided_at/reasoning are written once, at save time, and are NEVER
-- updated afterward (spec requirement 10) - resolve_guardian_authority_
-- decision() only ever sets the actual-outcome columns below plus
-- outcome_status/resolved_at, so later self-critique compares a genuinely
-- pre-registered expectation to the real outcome, never a hindsight-biased
-- one. decision_id is the PK (INSERT OR IGNORE claim-style insert, same
-- idempotency shape as live_profit_protection's position_id PK).
CREATE TABLE IF NOT EXISTS guardian_authority_decisions (
    decision_id TEXT PRIMARY KEY,
    position_id TEXT,
    candidate_id TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    reasoning TEXT NOT NULL,
    expected_outcome TEXT NOT NULL,
    expected_direction TEXT NOT NULL,
    confidence REAL,
    outcome_status TEXT NOT NULL DEFAULT 'PENDING',
    actual_exit_reason TEXT,
    actual_pnl_usdt TEXT,
    expectation_correct BOOLEAN,
    resolved_at TEXT,
    old_sl TEXT,
    new_sl TEXT,
    run_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_guardian_authority_decisions_position
    ON guardian_authority_decisions(position_id);
CREATE INDEX IF NOT EXISTS idx_guardian_authority_decisions_outcome_status
    ON guardian_authority_decisions(outcome_status);

-- Guardian Authority heuristics (2026-09-14): self-maintained heuristics
-- memory for the autonomous Guardian Authority extension, see
-- docs/superpowers/specs/2026-09-14-guardian-authority-design.md "Memory /
-- self-improvement". Unlike guardian_authority_decisions (which uses
-- INSERT OR IGNORE for immutable pre-decision expectations), heuristics
-- evolve and are overwritten via INSERT OR REPLACE - they are living rules,
-- continuously refined by self-critique feedback loops. The entire set is
-- read fresh on every decision (find_guardian_authority_heuristics),
-- giving the decision logic access to the system's current rule library.
CREATE TABLE IF NOT EXISTS guardian_authority_heuristics (
    heuristic_id TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    condition_json TEXT NOT NULL,
    adjustment REAL NOT NULL,
    confidence REAL NOT NULL,
    sample_size INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

-- Guardian Authority LIVE stop-loss tightening (2026-09-14): one row per
-- LIVE position, created (claimed) the moment a tightening attempt starts -
-- never before. Deliberately a SEPARATE table from live_profit_protection
-- with exactly the same shape and the same idempotency/state-machine
-- semantics (position_id primary key + INSERT OR IGNORE claim, fields
-- populated forward as exchange state is observed, one terminal status per
-- row). The separation is the whole point: LIVE Profit Protection and
-- Guardian Authority are two independent mechanisms that may both want to
-- move the SAME position's stop, and neither may ever consume, block or
-- overwrite the other's claim row - see
-- crypto_trading/guardian/authority_live.py's module docstring and its
-- "racing Profit Protection" tests. Column-by-column counterpart of
-- live_profit_protection, with ONE difference: `new_sl_price` (the
-- caller-supplied target, always known at claim time, hence NOT NULL)
-- replaces PP's computed `breakeven_price`/`threshold_pct`/
-- `trigger_mark_price` trio. old_sl_order_id/old_sl_price and
-- new_sl_order_id are unknown at claim time - populated by later updates
-- once the real exchange state is observed.
CREATE TABLE IF NOT EXISTS guardian_authority_live_sl_actions (
    position_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    new_sl_price TEXT NOT NULL,
    old_sl_order_id TEXT,
    old_sl_price TEXT,
    new_sl_client_order_id TEXT NOT NULL,
    new_sl_order_id TEXT,
    last_error TEXT,
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Guardian Authority shadow/observation mode (2026-09-15), tick-time half:
-- see docs/superpowers/specs/2026-09-15-guardian-authority-shadow-design.md.
-- Purely observational counterpart to guardian_authority_decisions - logs
-- GODFATHER's HYPOTHETICAL open-position decisions (TIGHTEN_SL/CLOSE_EARLY/
-- NO_ACTION), including hypothetical NO_ACTION, for a PAPER position,
-- WITHOUT ever reading from or writing to real position/order state. This
-- is what closes the cold-start deadlock discovered post-merge: real
-- Guardian Authority (authority_enabled) never logs NO_ACTION, so with zero
-- learned heuristics no data ever accumulates to learn from. Modeled
-- directly on profit_protection_shadow_positions (same shadow-table state-
-- machine shape: one row per real PAPER position, OBSERVING -> DECIDED ->
-- RESOLVED, or ABANDONED on orphan), using guardian_authority_decisions'
-- own field names for the decision-shaped columns so a reader who already
-- knows one table reads the other for free. shadow_id = position_id (1:1 -
-- unlike PP shadow's multi-threshold shadow_id shape, GODFATHER has no
-- parallel-threshold concept).
--
-- shadow_decision/decided_at/expected_outcome/expected_direction/
-- confidence/factors_json/proposed_new_sl are set ONCE, together, by
-- decide_guardian_authority_shadow() at the first tick the hypothetical
-- decision is not NO_ACTION (OBSERVING -> DECIDED, requirement-10-style
-- immutable-once-set discipline, same as guardian_authority_decisions'
-- own expected_outcome/expected_direction/confidence/decided_at/reasoning).
-- If a position closes while still OBSERVING (hypothetical NO_ACTION for
-- its entire life), resolve_guardian_authority_shadow_no_action() sets
-- those same decision fields (decision-only subset) retroactively, at
-- close time, straight to RESOLVED - so every row ends up with a decision
-- recorded, even the ones GODFATHER never would have logged for real.
--
-- last_factors_json is the ONE column updated on EVERY tick (alongside
-- mfe/mae) regardless of decision state - a fresh factors snapshot is
-- always available for resolve_guardian_authority_shadow_no_action's own
-- factors_json parameter to be populated from, even for a position that
-- never got a real hypothetical intervention. This is deliberately a
-- SEPARATE column from the immutable, decision-time factors_json above -
-- last_factors_json keeps changing after decide_guardian_authority_shadow
-- has already frozen factors_json.
CREATE TABLE IF NOT EXISTS guardian_authority_shadow_observations (
    shadow_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    status TEXT NOT NULL,
    shadow_decision TEXT,
    decided_at TEXT,
    expected_outcome TEXT,
    expected_direction TEXT,
    confidence REAL,
    factors_json TEXT,
    proposed_new_sl TEXT,
    mfe TEXT NOT NULL DEFAULT '0',
    mae TEXT NOT NULL DEFAULT '0',
    last_factors_json TEXT,
    actual_exit_reason TEXT,
    actual_pnl_usdt TEXT,
    actual_closed_at TEXT,
    expectation_correct BOOLEAN,
    prediction_error REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    run_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ga_shadow_position
    ON guardian_authority_shadow_observations(position_id);
CREATE INDEX IF NOT EXISTS idx_ga_shadow_status
    ON guardian_authority_shadow_observations(status);

-- Guardian Authority shadow/observation mode (2026-09-15), pre-entry half:
-- see docs/superpowers/specs/2026-09-15-guardian-authority-shadow-design.md
-- "guardian_authority_shadow_pre_entry_observations". Purely observational
-- counterpart of decide_pre_entry - logs what GODFATHER's pre-entry veto
-- WOULD have decided (APPROVE/PRE_ENTRY_VETO) for a CONFIRMED candidate,
-- WITHOUT ever blocking the real open (the real veto path is completely
-- separate and untouched by this table). Much simpler than the tick-time
-- table above: no per-tick concern, no state machine beyond a single
-- PENDING -> RESOLVED transition - pre-entry is evaluated exactly once, at
-- the same call site maybe_open_position_for_candidate already hooks, so
-- one INSERT at confirm time sets every decision-shaped column at once
-- (shadow_decision/expected_outcome/expected_direction/confidence/
-- factors_json - all immutable from then on, same requirement-10 spirit as
-- guardian_authority_decisions' own expectation columns).
--
-- Controller simplification (2026-09-15, superseding the original design
-- doc's `position_id` column + `link_guardian_authority_pre_entry_shadow_
-- to_position` method): position_id is always exactly candidate_id in this
-- codebase (position_opening.py: `position_id=candidate.candidate_id`), so
-- shadow_id simply IS candidate_id - which is also, by construction, what
-- the real position's position_id will be if one ever opens. No separate
-- position_id column exists; a later task resolves a shadow row by calling
-- repo.get_position(shadow_id) directly. find_pending_guardian_authority_
-- pre_entry_shadows() therefore returns every PENDING row unfiltered (no
-- "position_id IS NOT NULL" filter is needed or possible).
--
-- expectation_correct stays NULL forever for every row of this table (both
-- APPROVE and PRE_ENTRY_VETO) - matches Task 8's own real-path ruling that
-- PRE_ENTRY_VETO (and, symmetrically, APPROVE) have no counterfactual to
-- score: a veto never actually blocks the real open, so there is no
-- non-entry outcome to compare against. resolve_guardian_authority_pre_
-- entry_shadow() has no expectation_correct parameter and never writes to
-- this column - it exists purely for schema symmetry with the sibling
-- shadow/decisions tables and stays NULL by construction.
CREATE TABLE IF NOT EXISTS guardian_authority_shadow_pre_entry_observations (
    shadow_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    instrument TEXT NOT NULL,
    shadow_decision TEXT NOT NULL,
    expected_outcome TEXT NOT NULL,
    expected_direction TEXT NOT NULL,
    confidence REAL NOT NULL,
    factors_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    actual_exit_reason TEXT,
    actual_pnl_usdt TEXT,
    actual_closed_at TEXT,
    expectation_correct BOOLEAN,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    run_id TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ga_shadow_pre_entry_status
    ON guardian_authority_shadow_pre_entry_observations(status);
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
    _migrate_guardian_authority_decisions_add_intervention_applied(conn)
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
        _add_column_idempotent(conn, "ALTER TABLE runs ADD COLUMN instruments_scanned INTEGER")


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
        _add_column_idempotent(conn, "ALTER TABLE candidates ADD COLUMN reference_price TEXT")


def _migrate_guardian_authority_decisions_add_intervention_applied(conn: sqlite3.Connection) -> None:
    """Guardian Authority hardening fix I2 (2026-09-14):
    guardian_authority_decisions.intervention_applied was added AFTER
    Guardian Authority's own table-creation code (guardian_authority_
    decisions itself, see its CREATE TABLE IF NOT EXISTS block above) had
    already shipped to master and started running unconditionally on every
    app startup - independent of settings.guardian.authority_enabled - so
    real databases already have this table WITHOUT the column. Same
    migration pattern and same reasoning as
    _migrate_runs_add_instruments_scanned and
    _migrate_candidates_add_reference_price above: `CREATE TABLE IF NOT
    EXISTS` alone does nothing to an already-existing table, so an
    explicit, idempotent `ALTER TABLE` is required, guarded by `PRAGMA
    table_info` (not "ALTER TABLE ... IF NOT EXISTS", not supported by all
    SQLite versions) - safe to run on every connection, never destroys
    existing rows (the new column is simply NULL for them).

    Nullable, no default: NULL means "not yet determined" - relevant only
    during the brief same-tick window between a TIGHTEN_SL decision's
    initial save (intervention_applied=None, unknown) and its write-attempt
    outcome being known a few lines later in the same function call
    (crypto_trading/guardian/tick.py::process_one_position). Every row has
    a real True/False value by the time Task 9's self-critique
    (update_heuristics_from_resolved_decisions) ever reads it."""
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(guardian_authority_decisions)").fetchall()
    }
    if "intervention_applied" not in columns:
        _add_column_idempotent(
            conn,
            "ALTER TABLE guardian_authority_decisions ADD COLUMN intervention_applied BOOLEAN",
        )


def _add_column_idempotent(conn: sqlite3.Connection, alter_sql: str) -> None:
    """Closes the same check-then-act race as _set_wal_mode_with_retry above,
    for ADD COLUMN specifically: two connections can both see the column
    missing via PRAGMA table_info and both attempt the ALTER TABLE - the
    loser gets 'duplicate column name', not a real failure (the column now
    exists, which is the only postcondition this function promises)."""
    try:
        conn.execute(alter_sql)
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc):
            raise
