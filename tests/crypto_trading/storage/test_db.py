import json
import sqlite3
from decimal import Decimal

import pytest

from crypto_trading.storage.db import SCHEMA_VERSION, get_connection


def test_get_connection_enables_wal_mode(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_get_connection_sets_busy_timeout(tmp_path):
    conn = get_connection(tmp_path / "test.db", busy_timeout_ms=1234)
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 1234


def test_init_schema_is_idempotent(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO candidates "
        "(candidate_id, idempotency_key, instrument, discovery_run_id, evidence_hash, "
        "status, evidence_record, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("c1", "k1", "BTCUSDT", "run-1", "hash-1", "CANDIDATE", "{}", "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    # anropa init_schema igen (som en ny get_connection skulle göra)
    # - ska inte kasta eller ta bort data
    from crypto_trading.storage.db import init_schema

    init_schema(conn)
    row = conn.execute("SELECT candidate_id FROM candidates WHERE candidate_id = 'c1'").fetchone()
    assert row is not None


def test_runs_table_has_instruments_scanned_column_on_a_fresh_database(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "instruments_scanned" in columns


def test_migration_adds_instruments_scanned_column_to_a_pre_existing_database_without_it(
    tmp_path,
):
    """Fas 6 daily report (2026-08-29): runs-tabellen fick kolumnen
    instruments_scanned efter att riktiga produktionsdatabaser (data/
    crypto_trading.db) redan existerade med den GAMLA runs-strukturen.
    CREATE TABLE IF NOT EXISTS gör INGENTING mot en redan existerande
    tabell - detta test simulerar exakt den situationen: en databas skapad
    med det gamla schemat (utan kolumnen), sedan öppnad igen via
    get_connection() (som en riktig omstart av processen skulle göra),
    och bevisar att migreringen lägger till kolumnen utan att förstöra
    befintliga rader."""
    db_path = tmp_path / "pre_existing.db"

    # Simulerar en riktig, redan existerande produktionsdatabas skapad
    # INNAN instruments_scanned fanns - bygger bara den gamla runs-formen,
    # inte via _SCHEMA (som redan inkluderar kolumnen).
    old_conn = sqlite3.connect(db_path)
    old_conn.execute(
        "CREATE TABLE runs (run_id TEXT NOT NULL, run_type TEXT NOT NULL, "
        "started_at TEXT, completed_at TEXT, status TEXT, errors TEXT)"
    )
    old_conn.execute(
        "INSERT INTO runs (run_id, run_type, started_at, status) "
        "VALUES ('old-run-1', 'discovery', '2026-08-01T00:00:00+00:00', 'ok')"
    )
    old_conn.commit()
    old_conn.close()

    # En riktig omstart: get_connection() öppnar samma fil igen.
    conn = get_connection(db_path)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "instruments_scanned" in columns

    # Den gamla raden finns kvar, oförstörd, med NULL i den nya kolumnen.
    row = conn.execute("SELECT * FROM runs WHERE run_id = 'old-run-1'").fetchone()
    assert row is not None
    assert row["status"] == "ok"
    assert row["instruments_scanned"] is None


def test_migration_is_idempotent_across_repeated_connections(tmp_path):
    db_path = tmp_path / "test.db"
    get_connection(db_path)
    conn2 = get_connection(db_path)  # andra anslutningen ska inte krascha på ALTER TABLE igen
    columns = {row["name"] for row in conn2.execute("PRAGMA table_info(runs)").fetchall()}
    assert "instruments_scanned" in columns


def test_schema_version_is_recorded(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    assert row[0] == str(SCHEMA_VERSION)


def test_events_table_rejects_update(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id, occurred_at, "
        "run_id, schema_version, payload) VALUES (?,?,?,?,?,?,?,?)",
        ("e1", "CANDIDATE_CREATED", "candidate", "c1", "2026-01-01", "run-1", 1, "{}"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE events SET event_type = 'X' WHERE event_id = 'e1'")


def test_events_table_rejects_delete(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id, occurred_at, "
        "run_id, schema_version, payload) VALUES (?,?,?,?,?,?,?,?)",
        ("e2", "CANDIDATE_CREATED", "candidate", "c1", "2026-01-01", "run-1", 1, "{}"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM events WHERE event_id = 'e2'")


def test_events_seq_is_monotonically_increasing(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    for i in range(3):
        conn.execute(
            "INSERT INTO events (event_id, event_type, aggregate_type, aggregate_id, occurred_at, "
            "run_id, schema_version, payload) VALUES (?,?,?,?,?,?,?,?)",
            (f"e{i}", "X", "candidate", "c1", "2026-01-01", "run-1", 1, "{}"),
        )
    conn.commit()
    rows = conn.execute("SELECT seq FROM events ORDER BY seq").fetchall()
    seqs = [r[0] for r in rows]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 3


_DECIMAL_ROUNDTRIP_VALUES = [
    Decimal("0.1"),
    Decimal("1.234567890123456789"),  # 18 decimaler - fler signifikanta siffror än float64 (~15-17)
    Decimal("9876543210.21"),  # miljonklass
]


@pytest.mark.parametrize("value", _DECIMAL_ROUNDTRIP_VALUES)
def test_decimal_sqlite_text_roundtrip_is_exact(tmp_path, value):
    """Låser konventionen Decimal -> str -> SQLite TEXT -> str -> Decimal.
    Ingen positions-repository byggs för detta - bara den råa konventionen
    testas direkt mot en TEXT-kolumn som redan finns i schemat (positions.size),
    utan att dra in någon Phase 4-funktionalitet."""
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO positions "
        "(position_id, candidate_id, instrument, direction, status, theoretical_entry, "
        "simulated_fill_entry, stop_loss, target, size, fill_model_version, opened_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "pos-decimal-test",
            "cand-1",
            "BTCUSDT",
            "LONG",
            "OPEN_POSITION",
            "50000",
            "50000",
            "49000",
            "53000",
            str(value),  # canonical: alltid str(Decimal), aldrig float(...)
            "v1",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()

    row = conn.execute(
        "SELECT size FROM positions WHERE position_id = 'pos-decimal-test'"
    ).fetchone()
    stored_text = row["size"]
    assert isinstance(stored_text, str)
    reconstructed = Decimal(stored_text)

    assert reconstructed == value
    assert stored_text == str(value)  # ingen precisionsförlust i strängformen


def test_decimal_high_precision_value_would_lose_precision_via_float_but_not_via_str():
    """Bevisar konkret VARFÖR float aldrig får användas i serialiseringsvägen:
    ett värde med fler signifikanta siffror än float64 klarar av tappar
    precision om det passerar via float, men inte via str(Decimal)."""
    value = Decimal("1.234567890123456789")

    lost_via_float = Decimal(str(float(value)))
    assert lost_via_float != value  # bevisar att float-vägen FAKTISKT tappar precision

    preserved_via_str = Decimal(str(value))
    assert preserved_via_str == value  # str-vägen (den vi faktiskt använder) tappar ingenting


@pytest.mark.parametrize("value", _DECIMAL_ROUNDTRIP_VALUES)
def test_decimal_json_roundtrip_is_exact_never_via_float(value):
    """Låser samma konvention för JSON-payloads (t.ex. events.payload):
    Decimal -> str -> json.dumps -> json.loads -> Decimal, aldrig via float."""
    payload = {"amount": str(value)}
    serialized = json.dumps(payload, default=str)
    deserialized = json.loads(serialized)
    reconstructed = Decimal(deserialized["amount"])

    assert reconstructed == value
    assert isinstance(deserialized["amount"], str)  # aldrig ett JSON-tal/float
