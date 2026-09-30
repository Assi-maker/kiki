"""Historical Evidence Layer - schema and READ-ONLY lookup.

The evidence database (`data/historical_evidence.db`) is written only by the
offline research builder (crypto_trading/entry_research/evidence_builder.py).
The bot side opens it with `mode=ro`, so nothing here can write to it, and
this module imports nothing that can trade, size, move a stop or close a
position (enforced by tests/crypto_trading/evidence/).

Temporal contract: every lookup takes the DECISION time and reads only the
newest snapshot whose `as_of` is <= that time. A snapshot with a given
`as_of` contains only outcomes that had fully EXITED before `as_of`, so a
decision can never see an outcome that was not yet known.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DEFAULT_PATH = Path("data/historical_evidence.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,            -- ISO UTC; only outcomes with exit < as_of
    built_at TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    data_start TEXT NOT NULL,
    symbols INTEGER NOT NULL,
    survivorship_note TEXT NOT NULL,
    selection_sha TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_cell (
    snapshot_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    side TEXT NOT NULL,
    regime_dim TEXT NOT NULL,
    regime_value TEXT NOT NULL,
    exit_model TEXT NOT NULL,
    period TEXT NOT NULL,
    n INTEGER NOT NULL,
    trades_per_day REAL,
    mean_r REAL,
    se_r REAL,
    ci_low REAL,
    ci_high REAL,
    p_pos REAL,
    win_rate REAL,
    mfe_r REAL,
    mae_r REAL,
    hold_min REAL,
    net_usdt_per_trade REAL,
    baseline_mean_r REAL,
    diff_vs_baseline REAL,
    p_diff REAL,
    PRIMARY KEY (snapshot_id, signal_type, side, regime_dim, regime_value, exit_model, period)
);
CREATE TABLE IF NOT EXISTS evidence_verdict (
    snapshot_id TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    side TEXT NOT NULL,
    regime_dim TEXT NOT NULL,
    regime_value TEXT NOT NULL,
    oos_status TEXT NOT NULL,
    strength TEXT NOT NULL,
    vs_baseline TEXT NOT NULL,
    train_only_positive INTEGER NOT NULL,
    protocol_accepted INTEGER NOT NULL,
    headline TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, signal_type, side, regime_dim, regime_value)
);
"""

EXIT_MODELS = ("FIXED", "TIME15m", "TIME30m", "TIME1h", "TIME2h", "TIME4h")
UNAVAILABLE_HORIZONS = ("5m",)  # not in the current research data - reported as n/a


@dataclass(frozen=True)
class EvidencePackage:
    """Immutable evidence for ONE (signal type, side) at ONE decision time."""

    decision_time: str
    snapshot: dict
    signal_type: str
    side: str
    overall: dict  # verdict + per-period FIXED stats for regime ALL/ALL
    horizons: dict  # exit model -> OOS stats (regime ALL/ALL)
    regimes: dict = field(default_factory=dict)  # "dim=value" -> verdict + OOS stats
    unavailable_horizons: tuple = UNAVAILABLE_HORIZONS


class EvidenceReader:
    """Read-only access. Opening a missing file fails loudly instead of
    silently creating an empty database."""

    def __init__(self, path: Path | str = DEFAULT_PATH) -> None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(p)
        self._conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    def snapshot_for(self, decision_time: datetime) -> dict | None:
        """Newest snapshot with as_of <= decision_time, or None."""
        if decision_time.tzinfo is None:
            raise ValueError("decision_time must be timezone-aware (UTC)")
        row = self._conn.execute(
            "SELECT * FROM evidence_snapshot WHERE as_of <= ? ORDER BY as_of DESC LIMIT 1",
            (decision_time.isoformat(),),
        ).fetchone()
        return dict(row) if row else None

    def _cells(self, sid: str, typ: str, side: str, dim: str, val: str) -> dict:
        out: dict = {}
        for r in self._conn.execute(
            "SELECT * FROM evidence_cell WHERE snapshot_id=? AND signal_type=? AND side=?"
            " AND regime_dim=? AND regime_value=?",
            (sid, typ, side, dim, val),
        ):
            out.setdefault(r["exit_model"], {})[r["period"]] = {
                k: r[k]
                for k in r.keys()  # noqa: SIM118 - sqlite3.Row
                if k
                not in (
                    "snapshot_id",
                    "signal_type",
                    "side",
                    "regime_dim",
                    "regime_value",
                    "exit_model",
                    "period",
                )
            }
        return out

    def _verdict(self, sid: str, typ: str, side: str, dim: str, val: str) -> dict | None:
        r = self._conn.execute(
            "SELECT oos_status, strength, vs_baseline, train_only_positive, protocol_accepted,"
            " headline FROM evidence_verdict WHERE snapshot_id=? AND signal_type=? AND side=?"
            " AND regime_dim=? AND regime_value=?",
            (sid, typ, side, dim, val),
        ).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["train_only_positive"] = bool(d["train_only_positive"])
        d["protocol_accepted"] = bool(d["protocol_accepted"])
        return d

    def lookup(
        self,
        signal_type: str,
        side: str,
        decision_time: datetime,
        regimes: dict[str, str] | None = None,
    ) -> EvidencePackage | None:
        """Evidence known at `decision_time` for this signal type / side and,
        optionally, the regime values the candidate had AT that time."""
        snap = self.snapshot_for(decision_time)
        if snap is None:
            return None
        sid = snap["snapshot_id"]
        cells = self._cells(sid, signal_type, side, "ALL", "ALL")
        overall = {
            "verdict": self._verdict(sid, signal_type, side, "ALL", "ALL"),
            "periods": cells.get("FIXED", {}),
        }
        horizons = {m: cells.get(m, {}).get("OOS") for m in EXIT_MODELS}
        reg_out = {}
        for dim, val in (regimes or {}).items():
            if val is None:
                continue
            c = self._cells(sid, signal_type, side, dim, val)
            reg_out[f"{dim}={val}"] = {
                "verdict": self._verdict(sid, signal_type, side, dim, val),
                "oos": c.get("FIXED", {}).get("OOS"),
            }
        return EvidencePackage(
            decision_time=decision_time.isoformat(),
            snapshot={
                k: snap[k]
                for k in ("snapshot_id", "as_of", "survivorship_note", "selection_sha", "symbols")
            },
            signal_type=signal_type,
            side=side,
            overall=overall,
            horizons=horizons,
            regimes=reg_out,
        )


def evidence_context(pkg: EvidencePackage | None) -> dict:
    """Compact, JSON-safe context for an AI role (GODFATHER / Guardian).
    Interpretation material only - it carries no instruction and no action."""
    if pkg is None:
        return {"available": False, "reason": "no evidence snapshot existed at decision time"}
    v = pkg.overall.get("verdict") or {}
    return {
        "available": True,
        "evidence_as_of": pkg.snapshot["as_of"],
        "signal": f"{pkg.signal_type} {pkg.side}",
        "status": v.get("oos_status"),
        "headline": v.get("headline"),
        "train_only_positive_warning": v.get("train_only_positive"),
        "horizons_oos_mean_r": {
            m: (round(s["mean_r"], 4) if s and s.get("mean_r") is not None else None)
            for m, s in pkg.horizons.items()
        },
        "unavailable_horizons": list(pkg.unavailable_horizons),
        "regimes": {k: (x["verdict"] or {}).get("headline") for k, x in pkg.regimes.items()},
        "caveats": [
            pkg.snapshot["survivorship_note"],
            "Evidence is context, not a rule: it never opens, closes, sizes or vetoes.",
        ],
    }
