"""Bot-side, read-only bridge to the Historical Evidence Layer.

Used ONLY to add interpretation material to AI contexts (Guardian's
explanation, the GODFATHER strategist). Behind `settings.evidence
.context_enabled` (config/evidence.yaml, default false): when the flag is
off every function returns None immediately and nothing is opened.

Never raises (a missing or locked evidence file must never disturb the
trading tick that hosts the call) and never returns anything that a
decision path could act on - the value is a plain dict of text/numbers.

Temporal contract: the candidate's classification is used only if it was
made for a decision time <= the requested time, and the evidence snapshot
is the newest with as_of <= the requested time (store.EvidenceReader).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from crypto_trading.evidence import store


def _enabled(settings) -> bool:
    ev = getattr(settings, "evidence", None)
    return bool(ev is not None and ev.context_enabled)


def evidence_for_candidate(
    settings, candidate_id: str | None, decision_time: datetime
) -> dict | None:
    if not _enabled(settings) or not candidate_id:
        return None
    try:
        sh = sqlite3.connect(
            f"file:{Path(settings.evidence.shadow_db).as_posix()}?mode=ro", uri=True
        )
        try:
            row = sh.execute(
                "SELECT side, decision_time, signal_types, regimes FROM candidate_evidence"
                " WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        finally:
            sh.close()
        if row is None or datetime.fromisoformat(row[1]) > decision_time:
            return {"available": False, "reason": "candidate not classified before this decision"}
        side, types, regimes = row[0], json.loads(row[2]), json.loads(row[3])
        reader = store.EvidenceReader(settings.evidence.evidence_db)
        try:
            signals = {}
            for typ in types:
                pkg = reader.lookup(
                    "BASELINE" if typ == "NO_EVENT" else typ, side, decision_time, regimes
                )
                signals[typ] = store.evidence_context(pkg)
        finally:
            reader.close()
        return {"available": True, "classified_at_decision": row[1], "signals": signals}
    except Exception:  # noqa: BLE001 - fail-safe: evidence is optional context
        return None


def evidence_overview(settings, decision_time: datetime) -> dict | None:
    """Headline per signal type (all regimes) known at decision_time - for
    the GODFATHER strategist's context."""
    if not _enabled(settings):
        return None
    try:
        reader = store.EvidenceReader(settings.evidence.evidence_db)
        try:
            return reader.overview(decision_time)
        finally:
            reader.close()
    except Exception:  # noqa: BLE001
        return None
