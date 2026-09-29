"""Trade funnel reconstruction (2026-09-29) - read-only.

    python -m crypto_trading.performance.funnel_report --since 2026-09-29T00:00Z [--until ...]

Cycle level: the DISCOVERY_FUNNEL event every discovery cycle records
(markets scanned -> eligible -> top-N -> quant shortlist -> cooldown skips ->
candidates created -> budget -> AI started/ok/failed -> Gate outcome by reason
-> paper positions). Candidate level: each candidate followed through
assessments, gate_evaluations, positions, safety_kernel_decisions and
live_executions to the stage where it stopped. No AI calls.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime

from crypto_trading.orchestrator import _ROLE_ORDER as _AI_ROLES

_FUNNEL_COUNTS = (
    "markets_scanned", "eligible", "top_n", "quant_shortlist", "data_invalid",
    "skipped_rejected_cooldown", "skipped_kernel_reject_cooldown", "candidates_created",
    "budget_limited", "ai_started", "ai_all_roles_ok", "ai_failed", "confirmed",
    "paper_positions_opened",
)


def cycle_funnels(conn: sqlite3.Connection, since: datetime, until: datetime) -> list[dict]:
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM events WHERE event_type = 'DISCOVERY_FUNNEL' AND occurred_at >= ? "
        "AND occurred_at < ? ORDER BY occurred_at", (since.isoformat(), until.isoformat()),
    )]


def _ai_state(statuses: dict[str, str]) -> str:
    if not statuses:
        return "NOT_STARTED"
    if all(statuses.get(r) == "ok" for r in _AI_ROLES):
        return "ALL_OK"
    return "FAILED:" + ",".join(r for r in _AI_ROLES if statuses.get(r) != "ok")


def candidate_paths(conn: sqlite3.Connection, since: datetime, until: datetime) -> list[dict]:
    conn.row_factory = sqlite3.Row
    out = []
    for c in conn.execute(
        "SELECT candidate_id, instrument, status, created_at FROM candidates WHERE created_at >= ? "
        "AND created_at < ? ORDER BY created_at", (since.isoformat(), until.isoformat()),
    ).fetchall():
        cid = c["candidate_id"]
        statuses = {r["field_name"]: json.loads(r["payload"]).get("status") for r in conn.execute(
            "SELECT field_name, payload FROM assessments WHERE candidate_id = ?", (cid,))}
        ai = _ai_state(statuses)
        g = conn.execute("SELECT outcome, detail_json FROM gate_evaluations WHERE candidate_id = ?",
                         (cid,)).fetchone()
        gate = g["outcome"] if g else None
        gate_reasons = json.loads(g["detail_json"]).get("reasons", []) if g else []
        pos = conn.execute("SELECT position_id FROM positions WHERE candidate_id = ?", (cid,)).fetchone()
        risk = execution = None
        if pos is not None:
            k = conn.execute("SELECT action, detail_json FROM safety_kernel_decisions WHERE position_id = ?",
                             (pos["position_id"],)).fetchone()
            if k is not None:
                reasons = json.loads(k["detail_json"]).get("reasons") or []
                risk = k["action"] + (":" + "+".join(reasons) if reasons else "")
            x = conn.execute("SELECT phase, exit_reason, last_error FROM live_executions WHERE position_id = ?",
                             (pos["position_id"],)).fetchone()
            if x is not None:
                detail = x["exit_reason"] or x["last_error"]
                execution = x["phase"] + (":" + detail if detail else "")
        out.append({
            "candidate_id": cid, "symbol": c["instrument"], "created_at": c["created_at"],
            "status": c["status"], "ai": ai, "gate": gate, "gate_reasons": gate_reasons,
            "position": pos["position_id"] if pos else None, "risk": risk, "execution": execution,
            "stopped_at": _stopped_at(c["status"], ai, gate, pos, risk, execution),
        })
    return out


def _stopped_at(status, ai, gate, pos, risk, execution) -> str:
    if status in ("BUDGET_LIMITED", "DATA_INVALID", "CANDIDATE", "UNDER_AI_ANALYSIS",
                  "ANALYSIS_INTERRUPTED"):
        return status
    if ai.startswith("FAILED"):
        return "AI_FAILED"
    if gate is not None and gate != "CONFIRMED":
        return f"GATE_{gate}"
    if pos is None:
        return "NO_POSITION"
    if execution is not None:
        phase = execution.split(":")[0]
        return {"CLOSED": "LIVE_CLOSED", "ACTIVE": "LIVE_OPEN"}.get(phase, f"EXEC_{phase}")
    if risk is not None and risk.startswith("REJECT"):
        return "RISK_REJECTED"
    return "NOT_EVALUATED_BY_LIVE"  # signal went stale / capacity full before the kernel ran


def summarize(conn: sqlite3.Connection, since: datetime, until: datetime) -> dict:
    cycles = cycle_funnels(conn, since, until)
    totals = {k: sum(c.get(k, 0) for c in cycles) for k in _FUNNEL_COUNTS}
    gate: Counter = Counter()
    for c in cycles:
        for outcome, reasons in c.get("gate", {}).items():
            for reason, n in reasons.items():
                gate[f"{outcome}:{reason}"] += n
    paths = candidate_paths(conn, since, until)
    return {
        "since": since.isoformat(), "until": until.isoformat(), "cycles": len(cycles), "totals": totals,
        "gate": dict(gate),
        "stopped_at": dict(Counter(p["stopped_at"] for p in paths)),
        "risk_rejections": dict(Counter(p["risk"].split(":", 1)[1] for p in paths
                                        if p["risk"] and p["risk"].startswith("REJECT:"))),
    }


def main() -> None:
    from crypto_trading.config.loader import get_settings

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", default=None)
    ap.add_argument("--candidates", action="store_true", help="also print every candidate's path")
    args = ap.parse_args()
    since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    until = datetime.fromisoformat(args.until.replace("Z", "+00:00")) if args.until else datetime.now(UTC)
    conn = sqlite3.connect(f"file:{get_settings().db_path}?mode=ro", uri=True)
    print(json.dumps(summarize(conn, since, until), indent=1))
    if args.candidates:
        for p in candidate_paths(conn, since, until):
            print(json.dumps(p))


if __name__ == "__main__":
    main()
