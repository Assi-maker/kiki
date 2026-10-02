"""SQLite state for the trend strategy (its own file, data/trend.db).

Separate from the intraday bot's DB on purpose: no shared instance lock,
no migrations of the old schema, nothing the old code paths can touch.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    mode TEXT, day TEXT, status TEXT, started_at TEXT, finished_at TEXT, detail TEXT,
    PRIMARY KEY(mode, day));
CREATE TABLE IF NOT EXISTS paper_state(
    id INTEGER PRIMARY KEY CHECK(id = 1), equity REAL, cash REAL, last_day TEXT,
    qty TEXT, last_price TEXT, last_ts INTEGER);
CREATE TABLE IF NOT EXISTS equity(
    mode TEXT, day TEXT, equity REAL, weights TEXT, prices TEXT, PRIMARY KEY(mode, day));
CREATE TABLE IF NOT EXISTS orders(
    mode TEXT, day TEXT, symbol TEXT, kind TEXT, side TEXT, qty REAL, price REAL,
    fee REAL, client_id TEXT, order_id TEXT, ts TEXT);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class TrendStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def run_status(self, mode: str, day: str) -> str | None:
        row = self.db.execute("SELECT status FROM runs WHERE mode=? AND day=?", (mode, day)).fetchone()
        return row[0] if row else None

    def start_run(self, mode: str, day: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?)", (mode, day, "STARTED", now_iso(), None, None)
        )
        self.db.commit()

    def finish_run(self, mode: str, day: str, status: str, detail: dict) -> None:
        self.db.execute(
            "UPDATE runs SET status=?, finished_at=?, detail=? WHERE mode=? AND day=?",
            (status, now_iso(), json.dumps(detail, default=str), mode, day),
        )
        self.db.commit()

    def last_runs(self, n: int = 10) -> list[tuple]:
        return self.db.execute(
            "SELECT mode, day, status, finished_at, detail FROM runs ORDER BY day DESC, mode LIMIT ?", (n,)
        ).fetchall()

    def record_order(self, mode: str, day: str, symbol: str, kind: str, side: str, qty: float, price: float,
                     fee: float, client_id: str | None, order_id: str | None) -> None:
        self.db.execute(
            "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (mode, day, symbol, kind, side, qty, price, fee, client_id, order_id, now_iso()),
        )
        self.db.commit()

    def record_equity(self, mode: str, day: str, equity: float, weights: dict, prices: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO equity VALUES(?,?,?,?,?)",
            (mode, day, equity, json.dumps(weights), json.dumps(prices)),
        )
        self.db.commit()

    def equity_curve(self, mode: str) -> list[tuple[str, float]]:
        return self.db.execute("SELECT day, equity FROM equity WHERE mode=? ORDER BY day", (mode,)).fetchall()

    def paper_state(self) -> dict | None:
        row = self.db.execute("SELECT equity, cash, last_day, qty, last_price, last_ts FROM paper_state").fetchone()
        if not row:
            return None
        return {"equity": row[0], "cash": row[1], "last_day": row[2], "qty": json.loads(row[3]),
                "last_price": json.loads(row[4]), "last_ts": row[5]}

    def save_paper_state(self, state: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO paper_state VALUES(1,?,?,?,?,?,?)",
            (state["equity"], state["cash"], state["last_day"], json.dumps(state["qty"]),
             json.dumps(state["last_price"]), state["last_ts"]),
        )
        self.db.commit()
