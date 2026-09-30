"""GODFATHER with evidence as CONTEXT - shadow decisions and prediction errors.

Separate process (the evidence shadow service); never imported by the bot.
Reads the bot DB read-only and writes only the shadow DB.

For every relevant GODFATHER decision two (sometimes three) arms are asked:

- WITHOUT: the baseline - exactly what GODFATHER sees today;
- WITH: the same, plus `historical_evidence` (evidence/context.py, the
  newest snapshot with as_of <= the decision, for the classification made
  for the candidate's own decision time) and `self_critique`
  (evidence/self_critique.py, GODFATHER's own earlier predictions whose
  outcome was known at the decision);
- WITHOUT_REPLICATE on every AA_EVERY-th decision: the baseline asked again
  on the identical context - the AI's own noise.

Decisions:

- ENTRY - a CONFIRMED Gate entry that opened a position. Decision time =
  the Gate evaluation. The trade is taken whatever is said; GODFATHER states
  a stance (advisory) and its expectation: expected R, P(win), MFE, MAE.
- MANAGEMENT - a Guardian state transition on which the Guardian AI fires.
  GODFATHER recommends HOLD/WATCH/PROTECT/EXIT and states the expected
  final R if held.

Outcomes (`godfather_outcomes`) are resolved only after the position closed
and carry `known_at` = the close. The self-critique of a decision at T uses
only outcomes with known_at <= T. Nothing here is executed: no order, no
stop, no size, no Guardian state, no Safety Kernel, no cap.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.runner import AgentRunner
from crypto_trading.entry_research.evidence_ab_report import (
    _paper_pnl,
    _paper_risk,
    verified_outcomes,
)
from crypto_trading.evidence import store
from crypto_trading.evidence.context import build_evidence_context
from crypto_trading.evidence.self_critique import build_self_critique
from crypto_trading.evidence_shadow import service as sv
from crypto_trading.guardian.ai_context import build_ai_context
from crypto_trading.schemas.assessments import AssessmentBase

ENTRY_AGENT = "crypto-godfather-entry-shadow.md"
MGMT_AGENT = "crypto-godfather-management-shadow.md"
AA_EVERY = 4
DAILY_AI_CAP = 300  # live loop only; ~110 calls/day at the current volume
ROLES = ("news_sentiment", "technical", "bull_thesis", "forecast", "risk",
         "bear_adversarial", "qa")

SCHEMA = """
CREATE TABLE IF NOT EXISTS godfather_decisions (
    decision_id TEXT, arm TEXT, kind TEXT, position_id TEXT, candidate_id TEXT,
    decided_at TEXT, state TEXT, live INTEGER, signal_types TEXT, primary_status TEXT,
    evidence_available INTEGER, context_given TEXT, stance TEXT, recommendation TEXT,
    expected_r REAL, p_win REAL, expected_mfe_r REAL, expected_mae_r REAL,
    confidence REAL, reasoning TEXT, cost_usd REAL, evaluated_at TEXT, error TEXT,
    PRIMARY KEY (decision_id, arm)
);
CREATE TABLE IF NOT EXISTS godfather_outcomes (
    decision_id TEXT PRIMARY KEY, kind TEXT, position_id TEXT, decided_at TEXT,
    known_at TEXT, live INTEGER, risk_usdt REAL, actual_pnl_usdt REAL, actual_r REAL,
    at_decision_r REAL, hold_r REAL, mfe_r_after REAL, mae_r_after REAL,
    exit_reason TEXT, verified INTEGER, verified_r REAL, entry_verdict TEXT,
    management_verdict TEXT, classification TEXT, resolved_at TEXT
);
"""


class EntryView(AssessmentBase):
    stance: Literal["CONFIDENT", "NEUTRAL", "DOUBTFUL"]
    expected_r: float
    p_win: float
    expected_mfe_r: float
    expected_mae_r: float
    confidence: float
    reasoning: str


class ManagementView(AssessmentBase):
    recommendation: Literal["HOLD", "WATCH", "PROTECT", "EXIT"]
    expected_final_r: float
    p_win: float
    confidence: float
    reasoning: str


def open_db(sh: sqlite3.Connection) -> sqlite3.Connection:
    sh.executescript(SCHEMA)
    return sh


def _dt(x: str) -> datetime:
    return datetime.fromisoformat(x)


# ------------------------------------------------------------------ position facts


class Facts:
    """Everything about closed positions an outcome needs, loaded once.
    Each value is a fact fixed at the position's close (known_at)."""

    def __init__(
        self, bot: sqlite3.Connection, db_path, with_verified: bool = True,
        verified: dict | None = None,
    ) -> None:
        self.pos = {r["position_id"]: r for r in bot.execute("SELECT * FROM positions")}
        self.live = {
            r[0]
            for r in bot.execute(
                "SELECT position_id FROM live_executions WHERE exchange_fill_entry IS NOT NULL"
            )
        }
        self.path: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for pid, at, u in bot.execute(
            "SELECT position_id, observed_at, unrealized_pnl FROM guardian_observations"
            " ORDER BY observed_at"
        ):
            self.path[pid].append((at, float(u)))
        self.inv = {
            r[0]: (r[1], r[2], r[3])
            for r in bot.execute(
                "SELECT position_id, entry_verdict, management_verdict, classification"
                " FROM godfather_trade_investigations"
            )
        }
        if verified is not None:  # reuse: kline verification is the slow part
            self.verified = verified
        else:
            self.verified = verified_outcomes(db_path) if with_verified else {}

    def closed(self, pid: str) -> bool:
        p = self.pos.get(pid)
        return bool(p and p["status"] == "CLOSED" and p["simulated_fill_exit"] is not None
                    and p["closed_at"])

    def outcome(self, kind: str, decision_id: str, pid: str, decided_at: str,
                at_obs_usdt: float | None) -> dict | None:
        if not self.closed(pid):
            return None
        p = self.pos[pid]
        risk = _paper_risk(p)
        pnl = _paper_pnl(p)
        v = self.verified.get(pid, {})
        inv = self.inv.get(pid, (None, None, None))
        row = {
            "decision_id": decision_id, "kind": kind, "position_id": pid,
            "decided_at": decided_at, "known_at": p["closed_at"], "live": int(pid in self.live),
            "risk_usdt": risk, "actual_pnl_usdt": pnl, "exit_reason": p["exit_reason"],
            "verified": int(bool(v.get("verified"))), "verified_r": v.get("r"),
            "entry_verdict": inv[0], "management_verdict": inv[1], "classification": inv[2],
            "actual_r": None, "at_decision_r": None, "hold_r": None,
            "mfe_r_after": None, "mae_r_after": None,
        }
        if not risk:
            return row  # zero-size position: no exposure, not scorable
        at = (at_obs_usdt or 0.0) if kind == "MANAGEMENT" else 0.0
        after = [u for t, u in self.path.get(pid, []) if t >= decided_at] + [pnl]
        row.update(
            actual_r=pnl / risk,
            at_decision_r=at / risk,
            hold_r=(pnl - at) / risk,
            mfe_r_after=(max(after) - at) / risk,
            mae_r_after=(min(after) - at) / risk,
        )
        return row


# ------------------------------------------------------------------ decisions to make


def entry_decisions(bot: sqlite3.Connection, since: str) -> list[dict]:
    rows = bot.execute(
        "SELECT p.position_id, p.candidate_id, g.evaluated_at, g.outcome FROM positions p"
        " JOIN gate_evaluations g ON g.candidate_id = p.candidate_id"
        " WHERE g.evaluated_at > ? ORDER BY g.evaluated_at",
        (since,),
    ).fetchall()
    return [
        {"decision_id": f"ENTRY:{r[0]}", "kind": "ENTRY", "position_id": r[0],
         "candidate_id": r[1], "decided_at": r[2], "state": r[3], "obs": None}
        for r in rows
    ]


def management_decisions(bot: sqlite3.Connection, since: str) -> list[dict]:
    out = []
    for o, fires in sv.eligible_observations(bot, since, limit=10**7):
        if fires:
            out.append({"decision_id": f"MGMT:{o['observation_id']}", "kind": "MANAGEMENT",
                        "position_id": o["position_id"], "candidate_id": None,
                        "decided_at": o["observed_at"], "state": o["state"], "obs": o})
    return out


# ------------------------------------------------------------------ contexts


def _classification(sh: sqlite3.Connection, candidate_id: str | None) -> dict | None:
    r = sh.execute(
        "SELECT side, decision_time, signal_types, regimes FROM candidate_evidence"
        " WHERE candidate_id = ? AND error IS NULL",
        (candidate_id or "",),
    ).fetchone()
    if r is None:
        return None
    return {"side": r[0], "decided_for": _dt(r[1]), "signal_types": json.loads(r[2]),
            "regimes": json.loads(r[3])}


def entry_base(repo, pos: sqlite3.Row, gate_outcome: str) -> dict:
    try:
        cand = repo.get_candidate(pos["candidate_id"])
    except Exception:  # noqa: BLE001 - a corrupt candidate would try to write
        cand = None
    entry, sl, tgt = (float(pos["theoretical_entry"]), float(pos["stop_loss"]),
                      float(pos["target"]))
    risk_pct = abs(entry - sl) / entry * 100 if entry else None
    ctx: dict = {
        "decision": "ENTRY",
        "gate_outcome": gate_outcome,
        "instrument": pos["instrument"],
        "side": pos["direction"],
        "planned": {
            "entry": entry, "stop_loss": sl, "target": tgt,
            "risk_pct": round(risk_pct, 3) if risk_pct else None,
            "reward_risk": round(abs(tgt - entry) / abs(entry - sl), 2) if entry != sl else None,
        },
    }
    if cand is not None:
        for role in ROLES:
            a = getattr(cand, role, None)
            if a is not None:
                ctx[f"{role}_assessment"] = a.model_dump(mode="json")
    return ctx


def management_base(repo, o: sqlite3.Row, risk_usdt: float | None) -> dict:
    pos = repo.get_position(o["position_id"])
    try:
        cand = repo.get_candidate(pos.candidate_id) if pos else None
    except Exception:  # noqa: BLE001
        cand = None
    factors = (
        {k: Decimal(str(v)) for k, v in json.loads(o["factors"]).items()}
        if isinstance(o["factors"], str) else {}
    )
    ctx = build_ai_context(
        cand, factors, Decimal(str(o["decay_score"])), Decimal(str(o["progress_ratio"])),
        Decimal(str(o["unrealized_pnl"])), o["state"],
    )
    if risk_usdt:
        ctx["unrealized_r"] = round(float(o["unrealized_pnl"]) / risk_usdt, 3)
    return ctx


def history_rows(sh: sqlite3.Connection) -> list[dict]:
    """The WITH arm's own earlier decisions joined with their outcomes -
    what the self-critique may draw on (filtered by known_at later)."""
    out = []
    for r in sh.execute(
        "SELECT d.kind, d.state, d.signal_types, d.primary_status, d.recommendation,"
        " d.expected_r, d.p_win, o.known_at, o.actual_r, o.hold_r FROM godfather_decisions d"
        " JOIN godfather_outcomes o ON o.decision_id = d.decision_id"
        " WHERE d.arm = 'WITH' AND d.error IS NULL AND o.actual_r IS NOT NULL"
    ):
        out.append({"kind": r[0], "state": r[1], "signal_types": json.loads(r[2] or "[]"),
                    "primary_status": r[3], "recommendation": r[4], "expected_r": r[5],
                    "p_win": r[6], "known_at": _dt(r[7]), "actual_r": r[8], "hold_r": r[9]})
    return out


def prepare(d: dict, sh, repo, reader, facts: Facts, history: list[dict]) -> dict:
    """Both arms' contexts for one decision - built ONLY from what existed at
    decided_at (see module docstring)."""
    T = _dt(d["decided_at"])
    pos = facts.pos[d["position_id"]]
    cid = d["candidate_id"] or pos["candidate_id"]
    risk = _paper_risk(pos) or None
    if d["kind"] == "ENTRY":
        base = entry_base(repo, pos, d["state"])
    else:
        base = management_base(repo, d["obs"], risk)
    cls = _classification(sh, cid)
    if cls is not None:
        ev = build_evidence_context(reader, cls["signal_types"], cls["side"], cls["regimes"], T,
                                    classified_for=cls["decided_for"])
    else:
        ev = {"role": "CONTEXT_NOT_RULE", "available": False,
              "reason": "no evidence for this instrument (not in the research universe)"}
    critique = build_self_critique(
        history, T, d["kind"],
        signal_types=cls["signal_types"] if cls else None,
        primary_status=ev.get("primary_status"),
        state=d["state"] if d["kind"] == "MANAGEMENT" else None,
    )
    return {
        **d, "candidate_id": cid, "live": int(d["position_id"] in facts.live),
        "base": base, "with_extra": {"historical_evidence": ev, "self_critique": critique},
        "signal_types": cls["signal_types"] if cls else None,
        "primary_status": ev.get("primary_status"),
        "evidence_available": int(bool(ev.get("available"))),
        "at_obs": float(d["obs"]["unrealized_pnl"]) if d["obs"] is not None else None,
    }


def ask(runner: AgentRunner, agents: dict, p: dict, replicate: bool) -> dict:
    kind = p["kind"]
    agent, schema = agents[kind]
    arms = [("WITHOUT", p["base"]), ("WITH", {**p["base"], **p["with_extra"]})]
    if replicate:
        arms.append(("WITHOUT_REPLICATE", p["base"]))
    out = {}
    for arm, ctx in arms:
        try:
            a = runner.run(agent, ctx, schema)
            cost = float(getattr(runner, "last_call_cost_usd", Decimal("0")))
            out[arm] = (a if a.status == "ok" else None, cost,
                        None if a.status == "ok" else f"status {a.status}")
        except Exception as e:  # noqa: BLE001
            out[arm] = (None, 0.0, f"{type(e).__name__}: {e}"[:300])
    return {**p, "answers": out}


def write(sh: sqlite3.Connection, r: dict, now: datetime) -> None:
    for arm, (a, cost, err) in r["answers"].items():
        given = r["with_extra"] if arm == "WITH" else None
        sh.execute(
            "INSERT OR REPLACE INTO godfather_decisions VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["decision_id"], arm, r["kind"], r["position_id"], r["candidate_id"],
                r["decided_at"], r["state"], r["live"], json.dumps(r["signal_types"]),
                r["primary_status"], r["evidence_available"],
                json.dumps(given) if given else None,
                getattr(a, "stance", None), getattr(a, "recommendation", None),
                (a.expected_r if r["kind"] == "ENTRY" else a.expected_final_r) if a else None,
                a.p_win if a else None, getattr(a, "expected_mfe_r", None),
                getattr(a, "expected_mae_r", None), a.confidence if a else None,
                a.reasoning if a else None, cost, now.isoformat(), err,
            ),
        )


def resolve(sh: sqlite3.Connection, facts: Facts) -> int:
    """(Re)write the outcome of every decision whose position has closed."""
    n = 0
    now = datetime.now(UTC).isoformat()
    for did, kind, pid, at in sh.execute(
        "SELECT DISTINCT decision_id, kind, position_id, decided_at FROM godfather_decisions"
    ).fetchall():
        at_obs = None
        if kind == "MANAGEMENT":  # the unrealised P/L AT the observation
            at_obs = next((u for t, u in facts.path.get(pid, []) if t == at), None)
        o = facts.outcome(kind, did, pid, at, at_obs)
        if o is None:
            continue
        cols = list(o) + ["resolved_at"]
        sh.execute(
            f"INSERT OR REPLACE INTO godfather_outcomes ({','.join(cols)})"
            f" VALUES ({','.join('?' * len(cols))})",
            [*o.values(), now],
        )
        n += 1
    sh.commit()
    return n


# ------------------------------------------------------------------ drivers

HEARTBEAT = "logs/heartbeat.json"


def bot_ai_healthy(path: str = HEARTBEAT) -> bool:
    """The shadow shares the bot's API key. It spends nothing while the bot's
    own AI health is not OK (credit exhausted, outage): the bot has priority,
    and a failed shadow call is only a wasted row."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["ai"]["status"] == "OK"
    except Exception:  # noqa: BLE001 - unknown health = do not spend
        return False


def _failed(r: dict) -> bool:
    return any(a is None for a, _, _ in r["answers"].values())


def _agents() -> dict:
    return {"ENTRY": (load_agent_definition(ENTRY_AGENT), EntryView),
            "MANAGEMENT": (load_agent_definition(MGMT_AGENT), ManagementView)}


def run_decisions(decisions: list[dict], ctx: dict, workers: int, index_of: dict) -> int:
    """Prepare sequentially (sqlite), ask in parallel, write sequentially."""
    sh, repo, reader, facts = ctx["sh"], ctx["repo"], ctx["reader"], ctx["facts"]
    history = history_rows(sh)
    preps = [prepare(d, sh, repo, reader, facts, history) for d in decisions]
    agents = _agents()
    local = threading.local()

    def runner():
        if not hasattr(local, "r"):
            local.r = sv.build_runner()
        return local.r

    now = datetime.now(UTC)
    failed: set[str] = set()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [
            ex.submit(lambda p=p: ask(runner(), agents, p,
                                      index_of[p["decision_id"]] % AA_EVERY == 0))
            for p in preps
        ]
        for f in as_completed(futs):
            r = f.result()
            write(sh, r, now)
            if _failed(r):
                failed.add(r["decision_id"])
    sh.commit()
    ctx["last_failed"] = failed
    return len(preps)


def backfill(ctx: dict, since: str, workers: int = 6) -> dict:
    """All decisions since `since`, one UTC day at a time: the self-critique
    of a day's decisions can only draw on outcomes known before each
    decision, and those decisions were made (and written) on earlier days."""
    sh, bot = ctx["sh"], ctx["bot"]
    done = {r[0] for r in sh.execute(
        "SELECT decision_id FROM godfather_decisions WHERE arm = 'WITH' AND error IS NULL")}
    all_d = sorted(entry_decisions(bot, since) + management_decisions(bot, since),
                   key=lambda d: d["decided_at"])
    index_of = {}
    for kind in ("ENTRY", "MANAGEMENT"):  # A/A every 4th of each kind, in time order
        for i, d in enumerate(x for x in all_d if x["kind"] == kind):
            index_of[d["decision_id"]] = i
    todo = [d for d in all_d if d["decision_id"] not in done
            and d["position_id"] in ctx["facts"].pos]
    by_day: dict[str, list[dict]] = defaultdict(list)
    for d in todo:
        by_day[d["decided_at"][:10]].append(d)
    t0, n = time.time(), 0
    stopped = None
    for day in sorted(by_day):
        resolve(sh, ctx["facts"])  # outcomes known so far (known_at filters in the critique)
        n += run_decisions(by_day[day], ctx, workers, index_of)
        bad = len(ctx["last_failed"])
        print(f"  {day}: {len(by_day[day])} decisions, {bad} failed"
              f" ({time.time() - t0:.0f}s)", flush=True)
        if bad > len(by_day[day]) / 2:  # API down / credit exhausted: stop, resume later
            stopped = f"{day}: {bad}/{len(by_day[day])} failed - stopped, rerun to resume"
            break
    resolved = resolve(sh, ctx["facts"])
    return {"decisions_total": len(all_d), "evaluated_now": n, "already_done": len(done),
            "outcomes_resolved": resolved, "stopped": stopped,
            "seconds": round(time.time() - t0)}


def live_cycle(ctx: dict, now: datetime) -> dict:
    """Live loop step: new ENTRY / MANAGEMENT decisions (own daily cap),
    then outcomes for everything that closed since."""
    sh, bot = ctx["sh"], ctx["bot"]
    wm = sv._state(sh, "godfather_watermark", sv.BACKFILL_FROM)
    new = sorted(entry_decisions(bot, wm) + management_decisions(bot, wm),
                 key=lambda d: d["decided_at"])
    known = [d["position_id"] in ctx["facts"].pos for d in new]
    if not all(known):  # stop before a position the loaded facts do not have yet
        new = new[: known.index(False)]
    budget = DAILY_AI_CAP - sv._calls_today(sh, now)
    take = new[: max(0, budget // 3)] if bot_ai_healthy() else []
    if take:
        counts = {k: int(sv._state(sh, f"godfather_count:{k}", "0")) for k in
                  ("ENTRY", "MANAGEMENT")}
        index_of = {}
        for d in take:
            index_of[d["decision_id"]] = counts[d["kind"]]
            counts[d["kind"]] += 1
        run_decisions(take, ctx, 1, index_of)
        sv._count_calls(sh, now, sum(2 + (index_of[d["decision_id"]] % AA_EVERY == 0)
                                     for d in take))
        ok = [d for d in take if d["decision_id"] not in ctx["last_failed"]]
        first_bad = next((i for i, d in enumerate(take)
                          if d["decision_id"] in ctx["last_failed"]), None)
        done = take if first_bad is None else take[:first_bad]
        if done:  # never move the watermark past a failed decision - it is retried
            for k in ("ENTRY", "MANAGEMENT"):
                sv._set_state(sh, f"godfather_count:{k}", str(
                    int(sv._state(sh, f"godfather_count:{k}", "0"))
                    + sum(d["kind"] == k for d in done)))
            sv._set_state(sh, "godfather_watermark", done[-1]["decided_at"])
        take = ok
    return {"decisions": len(take), "resolved": resolve(sh, ctx["facts"])}


def make_ctx(settings, with_verified: bool = True) -> dict:
    sh = open_db(sv.open_shadow_db(settings.evidence.shadow_db))
    bot = sv._bot_ro(settings.db_path)
    return {
        "sh": sh, "bot": bot, "repo": sv.read_only_repository(settings.db_path),
        "reader": store.EvidenceReader(settings.evidence.evidence_db),
        "facts": Facts(bot, settings.db_path, with_verified),
    }


def refresh_facts(ctx: dict, settings, max_age: timedelta = timedelta(minutes=30)) -> None:
    """Positions, paths and verdicts every cycle (cheap); the kline
    verification of closed exits every `max_age`."""
    last = ctx.get("facts_at")
    if last is None or datetime.now(UTC) - last > max_age:
        ctx["facts"] = Facts(ctx["bot"], settings.db_path)
        ctx["facts_at"] = datetime.now(UTC)
    else:
        ctx["facts"] = Facts(ctx["bot"], settings.db_path, verified=ctx["facts"].verified)
