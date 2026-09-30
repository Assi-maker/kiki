"""Evidence shadow service - separate process, never part of the trading bot.

    python -m crypto_trading.evidence_shadow.service [--once]

Reads the bot database READ-ONLY and writes only its own database
(`data/evidence_shadow.db`). If this process crashes, hangs or is never
started, LIVE is unaffected. Nothing here can open, close, size, veto or
move anything.

Every cycle (60 s):
1. Classify new candidates at their decision time (created_at): signal
   types + regimes (classifier.py, Binance data cut at the decision time)
   and the historical evidence that existed then (snapshot as_of <= T).
2. Guardian A/B: for every Guardian state transition that invoked (or would
   invoke) the AI, ask the shadow agent for a recommendation WITHOUT and
   WITH historical evidence (and, for every 4th, a second WITHOUT call as an
   A/A noise control). Logged only; own daily AI-call cap, not counted in
   the bot's budget.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import traceback
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.agents.runner import AgentRunner, RealClaudeRunner
from crypto_trading.config.loader import get_settings
from crypto_trading.evidence import store
from crypto_trading.evidence_shadow import classifier as cl
from crypto_trading.guardian.ai_context import build_ai_context, should_invoke_ai
from crypto_trading.schemas.assessments import AssessmentBase
from crypto_trading.storage.repository import SQLiteRepository

BACKFILL_FROM = "2026-09-01T00:00:00+00:00"
DAILY_AI_CAP = 120
AA_EVERY = 4
AGENT_FILE = "crypto-guardian-evidence-shadow.md"

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidate_evidence (
    candidate_id TEXT PRIMARY KEY, symbol TEXT, side TEXT, decision_time TEXT,
    classified_at TEXT, last_bar_close TEXT, signal_types TEXT, regimes TEXT,
    evidence_as_of TEXT, evidence TEXT, primary_status TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS guardian_ab (
    observation_id TEXT PRIMARY KEY, position_id TEXT, candidate_id TEXT,
    observed_at TEXT, deterministic_state TEXT, evidence_available INTEGER,
    evidence_status TEXT, rec_without TEXT, conf_without REAL, rec_with TEXT,
    conf_with REAL, rec_without_replicate TEXT, changed INTEGER, cost_usd REAL,
    evaluated_at TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS shadow_state (key TEXT PRIMARY KEY, value TEXT);
"""


class ShadowRecommendation(AssessmentBase):
    recommendation: Literal["HOLD", "WATCH", "PROTECT", "EXIT"]
    confidence: float
    reasoning: str


# ------------------------------------------------------------------ storage


def open_shadow_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _state(conn: sqlite3.Connection, key: str, default: str) -> str:
    r = conn.execute("SELECT value FROM shadow_state WHERE key = ?", (key,)).fetchone()
    return r[0] if r else default


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO shadow_state VALUES (?, ?)", (key, value))
    conn.commit()


def _bot_ro(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def read_only_repository(db_path: Path) -> SQLiteRepository:
    """The bot's own read methods (get_position / get_candidate) on a
    mode=ro connection. SQLiteRepository.__init__ runs init_schema (a
    write), so it is bypassed on purpose: this process must never write to
    the bot database - any write attempt raises sqlite3.OperationalError."""
    repo = SQLiteRepository.__new__(SQLiteRepository)
    repo._conn = _bot_ro(db_path)
    return repo


# ------------------------------------------------------------------ 1. classification


class Classifier:
    """Per-symbol caches; every classification is still cut at its own T."""

    def __init__(self, universe: list[str], source: object | None = None) -> None:
        """`source` supplies fetch_symbol / fetch_universe_at (the public API
        by default; local_data.LocalSource reads the same Binance data from
        disk under the same cut-off contract)."""
        self.universe = universe
        self.source = source
        self._fetch = source.fetch_symbol if source is not None else cl.fetch_symbol
        self._fetch_universe = (
            source.fetch_universe_at if source is not None else cl.fetch_universe_at
        )
        self.cache: dict[str, cl.SymbolData] = {}
        self.cache_end: dict[str, int] = {}
        self.market: dict[int, dict] = {}

    def _data(self, sym: str, T: int) -> cl.SymbolData:
        end = self.cache_end.get(sym)
        if end is None or end > T or T - end > 2 * 86400:
            d = self._fetch(sym, T)
        else:  # append only what closed since the last fetch
            old = self.cache[sym]
            new = self._fetch(sym, T, history_s=T - end + 3600)
            bars = {b[0]: b for b in old.bars} | {b[0]: b for b in new.bars}
            fund = dict(old.funding) | dict(new.funding)
            oi = dict(old.oi) | dict(new.oi)
            keep = T - cl.HISTORY_S
            d = cl.SymbolData(
                [bars[k] for k in sorted(bars) if k >= keep],
                sorted(fund.items()),
                sorted((k, v) for k, v in oi.items() if k >= T - 3 * 86400),
            )
        self.cache[sym], self.cache_end[sym] = d, T
        return d

    def market_at(self, H: int) -> dict:
        if H not in self.market:
            btc = self._data(cl.REF, H)
            self.market[H] = cl.market_regime_at(H, btc, self._fetch_universe(self.universe, H))
        return self.market[H]

    def classify(self, symbol: str, side: str, T: int) -> cl.Classification:
        c = cl.classify_symbol(symbol, side, T, self._data(symbol, T))
        c.regimes = {**c.regimes, **self.market_at(T - T % cl.HOUR)}
        return c


def evidence_for(
    reader: store.EvidenceReader | None, c: cl.Classification, when: datetime
) -> tuple[str | None, dict, str | None]:
    """Evidence known at `when` for every signal type of the classification.
    NO_EVENT is matched with the random BASELINE."""
    if reader is None:
        return None, {}, None
    out, as_of, statuses = {}, None, []
    for typ in c.signal_types:
        pkg = reader.lookup("BASELINE" if typ == "NO_EVENT" else typ, c.side, when, c.regimes)
        ctx = store.evidence_context(pkg)
        out[typ] = ctx
        if pkg is not None:
            as_of = pkg.snapshot["as_of"]
            statuses.append(ctx.get("status"))
    order = list(store_statuses())
    primary = (
        min(statuses, key=lambda s: order.index(s) if s in order else 99) if statuses else None
    )
    return as_of, out, primary


def store_statuses() -> tuple[str, ...]:
    # most cautionary first: the primary status is the worst applicable one
    return (
        "NEGATIVE_OOS",
        "NO_EDGE",
        "INSUFFICIENT_DATA",
        "POSITIVE_UNCONFIRMED",
        "VALIDATED_EDGE",
    )


def classify_new_candidates(
    bot: sqlite3.Connection,
    sh: sqlite3.Connection,
    clf: Classifier,
    reader: store.EvidenceReader | None,
    limit: int = 50,
) -> int:
    wm = _state(sh, "candidates_watermark", BACKFILL_FROM)
    rows = bot.execute(
        # candidates that never reach an AI/Gate decision (budget-limited,
        # invalid data) have no outcome to evaluate - skipped to save API time
        "SELECT candidate_id, instrument, created_at FROM candidates WHERE created_at > ?"
        " AND status NOT IN ('BUDGET_LIMITED', 'DATA_INVALID')"
        " ORDER BY created_at LIMIT ?",
        (wm, limit),
    ).fetchall()
    for r in rows:
        T_dt = datetime.fromisoformat(r["created_at"])
        T = int(T_dt.timestamp())
        sym = r["instrument"].replace("-", "")
        side = "LONG"  # the bot opens LONG only (paper_trading/position_opening.py)
        try:
            c = clf.classify(sym, side, T)
            as_of, ev, primary = evidence_for(reader, c, T_dt)
            err = c.error
        except Exception as e:  # noqa: BLE001 - one bad candidate never stops the loop
            c = cl.Classification(sym, side, T, None)
            as_of, ev, primary, err = None, {}, None, f"{type(e).__name__}: {e}"[:300]
        sh.execute(
            "INSERT OR REPLACE INTO candidate_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r["candidate_id"],
                sym,
                side,
                T_dt.isoformat(),
                datetime.now(UTC).isoformat(),
                datetime.fromtimestamp(c.last_bar_close, UTC).isoformat()
                if c.last_bar_close
                else None,
                json.dumps(c.signal_types),
                json.dumps(c.regimes),
                as_of,
                json.dumps(ev),
                primary,
                err,
            ),
        )
        _set_state(sh, "candidates_watermark", r["created_at"])
    sh.commit()
    return len(rows)


# ------------------------------------------------------------------ 2. Guardian A/B


def _calls_today(sh: sqlite3.Connection, now: datetime) -> int:
    day = now.date().isoformat()
    return int(_state(sh, f"ai_calls:{day}", "0"))


def _count_calls(sh: sqlite3.Connection, now: datetime, n: int) -> None:
    day = now.date().isoformat()
    _set_state(sh, f"ai_calls:{day}", str(_calls_today(sh, now) + n))


def eligible_observations(
    bot: sqlite3.Connection, since: str, limit: int = 500
) -> list[tuple[sqlite3.Row, bool]]:
    """Guardian observations after `since` on which the Guardian AI fires
    (should_invoke_ai: a transition into or between non-HOLD states)."""
    obs = bot.execute(
        "SELECT * FROM guardian_observations WHERE observed_at > ? ORDER BY observed_at LIMIT ?",
        (since, limit),
    ).fetchall()
    out = []
    for o in obs:
        prev = bot.execute(
            "SELECT state FROM guardian_observations WHERE position_id = ? AND observed_at < ?"
            " ORDER BY observed_at DESC LIMIT 1",
            (o["position_id"], o["observed_at"]),
        ).fetchone()
        out.append((o, should_invoke_ai(dict(prev) if prev else None, o["state"])))
    return out


def prepare_ab(
    sh: sqlite3.Connection,
    repo: SQLiteRepository,
    reader: store.EvidenceReader | None,
    o: sqlite3.Row,
) -> dict:
    """Everything the two arms read, as known at the observation: the
    Guardian AI context (the bot's own builder) and the evidence of the
    candidate's classification at its decision time, looked up in the
    newest snapshot with as_of <= the observation."""
    pos = repo.get_position(o["position_id"])
    try:  # a corrupt candidate makes get_candidate try to WRITE an event -> ro error
        cand = repo.get_candidate(pos.candidate_id) if pos else None
    except Exception:  # noqa: BLE001
        cand = None
    factors = (
        {k: Decimal(str(v)) for k, v in json.loads(o["factors"]).items()}
        if isinstance(o["factors"], str)
        else {}
    )
    base = build_ai_context(
        cand,
        factors,
        Decimal(str(o["decay_score"])),
        Decimal(str(o["progress_ratio"])),
        Decimal(str(o["unrealized_pnl"])),
        o["state"],
    )
    observed = datetime.fromisoformat(o["observed_at"])
    ev_ctx, ev_status = None, None
    row = sh.execute(
        "SELECT signal_types, regimes, side, decision_time FROM"
        " candidate_evidence WHERE candidate_id = ?",
        (pos.candidate_id if pos else "",),
    ).fetchone()
    if row and reader is not None and datetime.fromisoformat(row[3]) <= observed:
        c = cl.Classification(
            pos.instrument.replace("-", ""),
            row[2],
            0,
            None,
            json.loads(row[0]),
            json.loads(row[1]),
        )
        _, ev, ev_status = evidence_for(reader, c, observed)  # snapshot as_of <= observed
        ev_ctx = ev
    return {
        "o": o,
        "candidate_id": pos.candidate_id if pos else None,
        "base": base,
        "ev_ctx": ev_ctx,
        "ev_status": ev_status,
    }


def ask_ab(runner: AgentRunner, agent: object, prep: dict, replicate: bool) -> dict:
    """The AI calls of one A/B row: WITHOUT, WITH evidence, and (A/A noise
    control) a second WITHOUT on the identical context. Thread-safe: it only
    reads `prep` and calls the runner."""
    base, ev_ctx = prep["base"], prep["ev_ctx"]
    err = None
    cost = Decimal("0")
    a = b = a2 = None
    try:
        a = runner.run(agent, base, ShadowRecommendation)
        cost += getattr(runner, "last_call_cost_usd", Decimal("0"))
        b = runner.run(
            agent,
            {**base, "historical_evidence": ev_ctx or {"available": False}},
            ShadowRecommendation,
        )
        cost += getattr(runner, "last_call_cost_usd", Decimal("0"))
        if replicate:
            a2 = runner.run(agent, base, ShadowRecommendation)
            cost += getattr(runner, "last_call_cost_usd", Decimal("0"))
    except Exception as e:  # noqa: BLE001
        a = b = a2 = None
        err = f"{type(e).__name__}: {e}"[:300]
    return {**prep, "a": a, "b": b, "a2": a2, "err": err, "cost": cost}


def write_ab(sh: sqlite3.Connection, r: dict, now: datetime) -> None:
    def rec(x):
        return x.recommendation if x is not None and x.status == "ok" else None

    def conf(x):
        return x.confidence if x is not None and x.status == "ok" else None

    o, a, b = r["o"], r["a"], r["b"]
    sh.execute(
        "INSERT OR REPLACE INTO guardian_ab VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            o["observation_id"],
            o["position_id"],
            r["candidate_id"],
            o["observed_at"],
            o["state"],
            int(r["ev_ctx"] is not None),
            r["ev_status"],
            rec(a),
            conf(a),
            rec(b),
            conf(b),
            rec(r["a2"]),
            int(rec(a) is not None and rec(b) is not None and rec(a) != rec(b)),
            float(r["cost"]),
            now.isoformat(),
            r["err"],
        ),
    )


def guardian_ab(
    bot: sqlite3.Connection,
    sh: sqlite3.Connection,
    repo: SQLiteRepository,
    runner: AgentRunner,
    reader: store.EvidenceReader | None,
    limit: int = 10,
) -> int:
    now = datetime.now(UTC)
    wm = _state(sh, "guardian_watermark", BACKFILL_FROM)
    agent = load_agent_definition(AGENT_FILE)
    done = 0
    for o, fires in eligible_observations(bot, wm):
        if not fires:
            _set_state(sh, "guardian_watermark", o["observed_at"])
            continue
        n_calls = 3 if done % AA_EVERY == 0 else 2
        if done >= limit or _calls_today(sh, now) + n_calls > DAILY_AI_CAP:
            break  # resume here next cycle / next day
        r = ask_ab(runner, agent, prepare_ab(sh, repo, reader, o), n_calls == 3)
        _count_calls(sh, now, n_calls)
        write_ab(sh, r, now)
        _set_state(sh, "guardian_watermark", o["observed_at"])
        done += 1
    sh.commit()
    return done


# ------------------------------------------------------------------ main loop


def build_runner() -> AgentRunner:
    return RealClaudeRunner(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        model=os.environ.get("CRYPTO_TRADING_GUARDIAN_MODEL", "claude-haiku-4-5"),
        timeout_seconds=float(os.environ.get("CRYPTO_TRADING_AGENT_TIMEOUT_SECONDS", "60")),
        max_retries=1,
    )


def universe_from_archive() -> list[str]:
    p = Path("data/entry_research/archive.db")
    if not p.exists():
        return []
    c = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    syms = [
        s
        for (s,) in c.execute(
            "SELECT symbol FROM klines5m WHERE t >= (SELECT MAX(t) - 86400000 FROM klines5m)"
            " GROUP BY symbol HAVING MAX(h) > MIN(l)"
        )
    ]  # listed and not a dead flat contract
    c.close()
    return syms


def main() -> None:
    load_dotenv(Path(".env"), override=False)
    settings = get_settings()
    sh = open_shadow_db(settings.evidence.shadow_db)
    bot = _bot_ro(settings.db_path)
    repo = read_only_repository(settings.db_path)
    ev_path = Path(settings.evidence.evidence_db)
    reader = store.EvidenceReader(ev_path) if ev_path.exists() else None
    from crypto_trading.evidence_shadow.local_data import LocalSource

    src = LocalSource()  # the research files on disk + an incremental gap store
    clf = Classifier(universe_from_archive(), src)
    runner = build_runner()
    once = "--once" in sys.argv
    print(f"evidence shadow started, universe {len(clf.universe)}", flush=True)
    while True:
        n1 = 0
        try:
            wm = _state(sh, "candidates_watermark", BACKFILL_FROM)
            if bot.execute(
                "SELECT 1 FROM candidates WHERE created_at > ? LIMIT 1", (wm,)
            ).fetchone():
                src.fill_gaps(int(time.time()))  # data up to now covers every pending T
            n1 = classify_new_candidates(bot, sh, clf, reader)
            n2 = guardian_ab(bot, sh, repo, runner, reader)
            print(f"{datetime.now(UTC).isoformat()} classified {n1}, guardian A/B {n2}", flush=True)
        except Exception:  # noqa: BLE001 - the shadow loop never dies on one error
            traceback.print_exc()
        if once:
            break
        time.sleep(5 if n1 == 50 else 60)


if __name__ == "__main__":
    main()
