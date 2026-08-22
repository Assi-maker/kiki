from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    reliability_score REAL NOT NULL,
    url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    category TEXT NOT NULL,
    metric TEXT NOT NULL,
    baseline REAL NOT NULL,
    deviation REAL NOT NULL,
    description TEXT NOT NULL,
    raw_ref TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    time_horizon TEXT NOT NULL,
    liquidity TEXT NOT NULL,
    status TEXT NOT NULL,
    score REAL,
    score_breakdown TEXT
);

CREATE TABLE IF NOT EXISTS assessments (
    opportunity_id TEXT NOT NULL,
    field_name TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (opportunity_id, field_name)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT NOT NULL,
    event_id TEXT,
    opportunity_id TEXT,
    agent_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    errors TEXT,
    latency_ms REAL
);
"""


def get_connection(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()
