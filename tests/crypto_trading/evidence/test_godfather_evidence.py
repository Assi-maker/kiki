"""GODFATHER with historical evidence as CONTEXT (2026-09-30).

Pins: temporal isolation (no later classification, snapshot, outcome or
observation reaches a decision), context-not-rule (no action field
anywhere, no decision module imports it), no write path to the bot
database, the baseline arm is exactly today's context, Safety Kernel /
Guardian authority / LIVE execution / sizing untouched and the % caps
still log-only."""

import ast
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from crypto_trading.evidence import context as ec
from crypto_trading.evidence import self_critique as sc
from crypto_trading.evidence import store
from crypto_trading.evidence_shadow import godfather_shadow as gf

ROOT = Path(__file__).resolve().parents[3]
T = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
ACTION_WORDS = {"action", "reject", "veto", "size", "leverage", "stop_loss_new", "decision_rule",
                "block", "skip", "execute"}


def _keys(x) -> set[str]:
    if isinstance(x, dict):
        return set(x) | set().union(*(_keys(v) for v in x.values())) if x else set()
    if isinstance(x, list):
        return set().union(*(_keys(v) for v in x)) if x else set()
    return set()


class _Reader:
    """An EvidenceReader stand-in that records the time it was asked for."""

    def __init__(self):
        self.asked: list[datetime] = []

    def lookup(self, typ, side, when, regimes):
        self.asked.append(when)
        cell = {"n": 500, "mean_r": -0.1, "ci_low": -0.2, "ci_high": 0.0, "win_rate": 0.4,
                "mfe_r": 1.2, "mae_r": -0.8, "hold_min": 600, "baseline_mean_r": -0.12,
                "diff_vs_baseline": 0.02}
        return store.EvidencePackage(
            decision_time=when.isoformat(),
            snapshot={"snapshot_id": "s", "as_of": (when - timedelta(days=3)).isoformat(),
                      "survivorship_note": "n", "selection_sha": "x", "symbols": 1},
            signal_type=typ, side=side,
            overall={"verdict": {"oos_status": "NEGATIVE_OOS", "strength": "MEDIUM",
                                 "vs_baseline": "NOT_DIFFERENT", "train_only_positive": 0,
                                 "protocol_accepted": 0, "headline": "h"},
                     "periods": {"OOS": cell}},
            horizons={"FIXED": cell},
            regimes={"mkt_trend=bull": {"verdict": {"oos_status": "NO_EDGE", "strength": "LOW"},
                                        "oos": cell}},
        )


# ------------------------------------------------------------------ evidence context


def test_context_carries_every_requested_field_and_is_labelled_context_not_rule():
    c = ec.build_evidence_context(_Reader(), ["BRK_4H"], "LONG", {"mkt_trend": "bull"}, T)
    assert c["role"] == "CONTEXT_NOT_RULE" and "NOT A RULE" in c["notice"]
    s = c["signals"][0]
    assert s["signal_type"] == "BRK_4H" and s["side"] == "LONG"
    assert s["status"] == "NEGATIVE_OOS" and s["certainty"] == "MEDIUM"
    oos = s["oos"]
    for k in ("n", "expectancy_r_after_costs", "ci95_r", "win_rate", "mfe_r", "mae_r",
              "random_baseline_r", "diff_vs_random_r"):
        assert oos[k] is not None, k
    assert s["in_regime_at_decision"]["mkt_trend=bull"]["status"] == "NO_EDGE"
    assert c["regime_at_decision"] == {"mkt_trend": "bull"}
    assert not (_keys(c) & ACTION_WORDS)


def test_context_asks_the_store_for_the_decision_time_only():
    r = _Reader()
    ec.build_evidence_context(r, ["BRK_4H", "NO_EVENT"], "LONG", {}, T)
    assert r.asked == [T, T]


def test_a_classification_made_for_a_later_time_is_refused():
    r = _Reader()
    c = ec.build_evidence_context(r, ["BRK_4H"], "LONG", {}, T,
                                  classified_for=T + timedelta(seconds=1))
    assert c["available"] is False and r.asked == []


# ------------------------------------------------------------------ self-critique


def _hist(n, known_at, **kw):
    base = {"kind": "ENTRY", "expected_r": 0.3, "actual_r": -0.5, "p_win": 0.6,
            "signal_types": ["BRK_4H"], "primary_status": "NEGATIVE_OOS", "known_at": known_at}
    return [{**base, **kw} for _ in range(n)]


def test_self_critique_uses_only_outcomes_known_before_the_decision():
    rows = _hist(6, T - timedelta(hours=1)) + _hist(50, T + timedelta(seconds=1), actual_r=9.0)
    c = sc.build_self_critique(rows, T, "ENTRY", ["BRK_4H"], "NEGATIVE_OOS")
    a = c["all_decisions"]
    assert a["n"] == 6 and a["mean_actual_r"] == -0.5
    assert a["bias_actual_minus_expected_r"] == -0.8
    assert c["same_signal_type"]["n"] == 6 and c["role"] == "CONTEXT_NOT_RULE"
    assert not (_keys(c) & ACTION_WORDS)


def test_self_critique_hides_numbers_below_the_minimum_sample():
    c = sc.build_self_critique(_hist(4, T - timedelta(days=1)), T, "ENTRY")
    assert c["all_decisions"] == {"n": 4, "too_few": True}


def test_self_critique_scores_recommendations_by_what_holding_did():
    rows = (_hist(5, T - timedelta(days=1), kind="MANAGEMENT", recommendation="EXIT", hold_r=-1)
            + _hist(5, T - timedelta(days=1), kind="MANAGEMENT", recommendation="HOLD",
                    hold_r=-1))
    rec = sc.build_self_critique(rows, T, "MANAGEMENT")["all_decisions"]["recommendations"]
    assert rec["EXIT"]["right_rate"] == 1.0 and rec["HOLD"]["right_rate"] == 0.0


# ------------------------------------------------------------------ shadow decisions


def _bot(tmp_path: Path) -> Path:
    p = tmp_path / "bot.db"
    c = sqlite3.connect(p)
    c.executescript(
        "CREATE TABLE positions (position_id TEXT, candidate_id TEXT, instrument TEXT,"
        " direction TEXT, status TEXT, theoretical_entry TEXT, simulated_fill_entry TEXT,"
        " stop_loss TEXT, target TEXT, size TEXT, opened_at TEXT, simulated_fill_exit TEXT,"
        " exit_reason TEXT, fees TEXT, funding TEXT, closed_at TEXT);"
        "CREATE TABLE live_executions (position_id TEXT, exchange_fill_entry TEXT);"
        "CREATE TABLE guardian_observations (observation_id TEXT, position_id TEXT,"
        " observed_at TEXT, state TEXT, decay_score TEXT, progress_ratio TEXT,"
        " unrealized_pnl TEXT, factors TEXT);"
        "CREATE TABLE godfather_trade_investigations (position_id TEXT, entry_verdict TEXT,"
        " management_verdict TEXT, classification TEXT);"
        "CREATE TABLE gate_evaluations (candidate_id TEXT, evaluated_at TEXT, outcome TEXT);"
    )
    # notional 100 USDT, entry 10, stop 9 -> planned risk 10 USDT = 1 R
    c.execute("INSERT INTO positions VALUES ('p1','c1','X-USDT','LONG','CLOSED','10','10','9',"
              "'12','100',?, '11','target','0','0',?)",
              ((T - timedelta(hours=1)).isoformat(), (T + timedelta(hours=5)).isoformat()))
    for i, (st_, u) in enumerate([("HOLD", 5), ("WATCH", -3), ("WATCH", 20), ("PROTECT", 8)]):
        at = (T + timedelta(hours=i)).isoformat()
        c.execute("INSERT INTO guardian_observations VALUES (?,?,?,?,?,?,?,?)",
                  (f"p1:{at}", "p1", at, st_, "0.5", "0.2", str(u), "{}"))
    c.execute("INSERT INTO gate_evaluations VALUES ('c1', ?, 'CONFIRMED')",
              ((T - timedelta(hours=1)).isoformat(),))
    c.commit()
    c.close()
    return p


def _ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def test_decisions_facts_and_outcomes_work_on_a_read_only_bot_db(tmp_path):
    bot = _ro(_bot(tmp_path))  # any write would raise sqlite3.OperationalError
    ents = gf.entry_decisions(bot, "2026-01-01")
    mg = gf.management_decisions(bot, "2026-01-01")
    assert [d["kind"] for d in ents] == ["ENTRY"]
    assert [d["state"] for d in mg] == ["WATCH", "PROTECT"]  # AI fires on transitions only
    facts = gf.Facts(bot, tmp_path / "bot.db", with_verified=False)
    o = facts.outcome("MANAGEMENT", "d", "p1", mg[0]["decided_at"], -3.0)
    assert o["known_at"] == (T + timedelta(hours=5)).isoformat()
    assert o["risk_usdt"] == pytest.approx(10.0) and o["actual_r"] == pytest.approx(1.0)
    assert o["at_decision_r"] == pytest.approx(-0.3) and o["hold_r"] == pytest.approx(1.3)
    # MFE/MAE after the decision: the HOLD tick before it (+5) is not part of it
    assert o["mfe_r_after"] == pytest.approx(2.3) and o["mae_r_after"] == pytest.approx(0.0)
    with pytest.raises(sqlite3.OperationalError):
        bot.execute("INSERT INTO positions (position_id) VALUES ('x')")


def test_zero_size_positions_are_not_scored(tmp_path):
    p = _bot(tmp_path)
    c = sqlite3.connect(p)
    c.execute("UPDATE positions SET size = '0'")
    c.commit()
    c.close()
    facts = gf.Facts(_ro(p), p, with_verified=False)
    o = facts.outcome("ENTRY", "d", "p1", T.isoformat(), None)
    assert o["actual_r"] is None and o["risk_usdt"] == 0


def test_open_positions_have_no_outcome(tmp_path):
    p = _bot(tmp_path)
    c = sqlite3.connect(p)
    c.execute("UPDATE positions SET status = 'OPEN_POSITION', closed_at = NULL")
    c.commit()
    c.close()
    facts = gf.Facts(_ro(p), p, with_verified=False)
    assert facts.outcome("ENTRY", "d", "p1", T.isoformat(), None) is None


class _Repo:
    def get_candidate(self, _):
        return None

    def get_position(self, _):
        class P:
            candidate_id, instrument = "c1", "X-USDT"
        return P()


def _shadow(tmp_path, decided_for: datetime) -> sqlite3.Connection:
    from crypto_trading.evidence_shadow import service as sv

    sh = gf.open_db(sv.open_shadow_db(str(tmp_path / "sh.db")))
    sh.execute("INSERT INTO candidate_evidence (candidate_id, side, decision_time, signal_types,"
               " regimes) VALUES ('c1','LONG',?,'[\"BRK_4H\"]','{}')", (decided_for.isoformat(),))
    return sh


@pytest.mark.parametrize("kind", ["ENTRY", "MANAGEMENT"])
def test_baseline_arm_is_todays_context_and_with_arm_only_adds_context(tmp_path, kind):
    bot = _ro(_bot(tmp_path))
    facts = gf.Facts(bot, tmp_path / "bot.db", with_verified=False)
    d = (gf.entry_decisions(bot, "2026-01-01") if kind == "ENTRY"
         else gf.management_decisions(bot, "2026-01-01"))[0]
    sh = _shadow(tmp_path, T - timedelta(hours=2))
    p = gf.prepare(d, sh, _Repo(), _Reader(), facts, [])
    assert "historical_evidence" not in p["base"] and "self_critique" not in p["base"]
    assert set(p["with_extra"]) == {"historical_evidence", "self_critique"}
    assert p["with_extra"]["historical_evidence"]["available"] is True
    assert not (_keys(p["with_extra"]) & ACTION_WORDS)


def test_a_candidate_classified_after_the_decision_gets_no_evidence(tmp_path):
    bot = _ro(_bot(tmp_path))
    facts = gf.Facts(bot, tmp_path / "bot.db", with_verified=False)
    d = gf.entry_decisions(bot, "2026-01-01")[0]  # decided at T - 1 h
    r = _Reader()
    p = gf.prepare(d, _shadow(tmp_path, T), _Repo(), r, facts, [])
    assert p["with_extra"]["historical_evidence"]["available"] is False and r.asked == []


def test_the_self_critique_of_a_decision_ignores_outcomes_known_later(tmp_path):
    bot = _ro(_bot(tmp_path))
    facts = gf.Facts(bot, tmp_path / "bot.db", with_verified=False)
    d = gf.management_decisions(bot, "2026-01-01")[0]  # decided at T + 1 h
    later = _hist(9, T + timedelta(hours=2), kind="MANAGEMENT")
    earlier = _hist(5, T, kind="MANAGEMENT")
    p = gf.prepare(d, _shadow(tmp_path, T - timedelta(hours=2)), _Repo(), _Reader(), facts,
                   later + earlier)
    assert p["with_extra"]["self_critique"]["all_decisions"]["n"] == 5


# ------------------------------------------------------------------ isolation


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    return names


FORBIDDEN = ("crypto_trading.safety_kernel", "crypto_trading.live_execution",
             "crypto_trading.connectors", "crypto_trading.paper_trading.position_opening",
             "crypto_trading.paper_trading.position_closing", "crypto_trading.position_sizing",
             "crypto_trading.guardian.authority", "crypto_trading.gate")


@pytest.mark.parametrize("rel", ["crypto_trading/evidence/context.py",
                                 "crypto_trading/evidence/self_critique.py",
                                 "crypto_trading/evidence/bridge.py",
                                 "crypto_trading/evidence_shadow/godfather_shadow.py"])
def test_evidence_modules_reach_no_execution_sizing_gate_or_authority_code(rel):
    names = _imports(ROOT / rel)
    assert not [n for n in names if n.startswith(FORBIDDEN)], rel


def test_no_trading_module_imports_the_new_context_or_critique():
    for p in (ROOT / "crypto_trading").rglob("*.py"):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith(("crypto_trading/evidence/", "crypto_trading/evidence_shadow/",
                           "crypto_trading/entry_research/")):
            continue
        bad = [n for n in _imports(p) if n.startswith(("crypto_trading.evidence.context",
                                                        "crypto_trading.evidence.self_critique"))]
        assert not bad, rel


def test_godfather_shadow_writes_only_its_own_tables():
    src = (ROOT / "crypto_trading/evidence_shadow/godfather_shadow.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            up = n.value.upper()
            for verb in ("INSERT", "UPDATE ", "DELETE "):
                if verb in up:
                    assert "GODFATHER_DECISIONS" in up or "GODFATHER_OUTCOMES" in up, n.value


def test_production_flags_evidence_off_and_caps_log_only():
    ev = yaml.safe_load((ROOT / "crypto_trading/config/evidence.yaml").read_text(encoding="utf-8"))
    assert ev["context_enabled"] is False
    sk = yaml.safe_load(
        (ROOT / "crypto_trading/config/safety_kernel.yaml").read_text(encoding="utf-8")
    )
    assert sk["risk_caps_enforced"] is False
    assert sk["max_group_risk_pct"] == "0.05"


def test_shadow_spends_nothing_while_the_bots_ai_is_unhealthy(tmp_path, monkeypatch):
    """Shared API key: when the bot's AI health is not OK (e.g. credit
    exhausted) the shadow makes no call and does not move its watermark."""
    hb = tmp_path / "heartbeat.json"
    hb.write_text('{"ai": {"status": "FAILING"}}')
    monkeypatch.setattr(gf, "HEARTBEAT", str(hb))
    assert gf.bot_ai_healthy(str(hb)) is False
    assert gf.bot_ai_healthy(str(tmp_path / "missing.json")) is False
    bot = _ro(_bot(tmp_path))
    ctx = {"sh": _shadow(tmp_path, T), "bot": bot,
           "facts": gf.Facts(bot, tmp_path / "bot.db", with_verified=False)}
    monkeypatch.setattr(gf, "bot_ai_healthy", lambda path=None: False)
    monkeypatch.setattr(gf, "run_decisions", lambda *a, **k: pytest.fail("AI called"))
    out = gf.live_cycle(ctx, T + timedelta(days=1))
    assert out["decisions"] == 0
    from crypto_trading.evidence_shadow import service as sv

    assert sv._state(ctx["sh"], "godfather_watermark", "none") == "none"
