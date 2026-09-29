"""Entry Quality Layer (2026-09-28): SHADOW ONLY. It classifies and logs;
nothing in it can block, size, open, change or close a trade."""
import ast
import pathlib
from datetime import UTC, datetime, timedelta

from crypto_trading.entry_research import quality
from crypto_trading.entry_research.klines import KlineCache
from crypto_trading.godfather import entry_patterns
from crypto_trading.storage.repository import SQLiteRepository
from tests.crypto_trading.test_discovery_wiring import _persisted_candidate_in_status

T0 = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)  # the fixture candidate's created_at
ROOT = pathlib.Path(__file__).resolve().parents[3]

REG = {
    "version": "test", "frozen_at": "2026-09-28T00:00:00+00:00",
    "patterns": [
        {"id": "fail-00", "class": "FAILURE_HYPOTHESIS", "label": "chg_1h<=0.26",
         "conditions": [["chg_1h", "LOW", 0.26, 0.94]], "evidence": {"train": {"n": 71}}},
        {"id": "edge-01", "class": "HYPOTHESIS", "label": "ret_1h>0.64",
         "conditions": [["ret_1h", "HIGH", 0.04, 0.64]], "evidence": {"train": {"n": 31}}},
        {"id": "edge-02", "class": "EDGE", "label": "volz_30m>3",
         "conditions": [["volz_30m", "HIGH", 1.0, 3.0]], "evidence": {"train": {"n": 40}}},
    ],
}


def test_failure_match_is_reject_even_when_an_edge_also_matches():
    out = quality.classify({"chg_1h": 0.1, "ret_1h": 1.0, "volz_30m": 5}, REG)
    assert out["eq_class"] == "REJECT"
    assert out["evidence_level"] == "HYPOTHESIS"
    assert set(out["matched_ids"]) == {"fail-00", "edge-01", "edge-02"}
    assert "fail-00" in out["evidence"]


def test_classes_and_the_baseline():
    assert quality.classify({"chg_1h": 0.5, "volz_30m": 5}, REG)["eq_class"] == "STRONG"
    assert quality.classify({"chg_1h": 0.5, "ret_1h": 1.0}, REG)["eq_class"] == "ACCEPTABLE"
    weak = quality.classify({"chg_1h": 0.5, "ret_1h": 0.1}, REG)
    assert weak["eq_class"] == "WEAK" and weak["evidence_level"] == "BASELINE"
    # a missing feature never matches (no silent default)
    assert quality.classify({}, REG)["eq_class"] == "WEAK"


def test_the_shipped_registry_contains_no_validated_edge_and_is_frozen():
    from crypto_trading.entry_research import registry

    reg = registry.load()
    assert reg["frozen_at"] and reg["version"]
    assert not [p for p in reg["patterns"] if p["class"] == "EDGE"]
    assert all(p["conditions"] for p in reg["patterns"])


class _Market:
    def __init__(self):
        self.calls = []

    def get_klines(self, symbol, interval, limit=100, start_time_ms=None, end_time_ms=None):
        self.calls.append(symbol)
        out, t = [], start_time_ms
        while t <= end_time_ms and len(out) < limit:
            m = datetime.fromtimestamp(t / 1000, UTC)
            p = 100.0 + (m - T0).total_seconds() / 3600 * (0.2 if m >= T0 else 0.0) + (m.minute % 7) * 0.05
            out.append({"time": t, "open": str(p), "high": str(p + 0.3), "low": str(p - 0.3),
                        "close": str(p), "volume": "1"})
            t += 60_000
        return out


def test_tick_classifies_a_ready_candidate_once_and_writes_only_its_table(tmp_path):
    db = tmp_path / "t.db"
    repo = SQLiteRepository(db)
    _persisted_candidate_in_status(repo, "CANDIDATE")
    cache = KlineCache(tmp_path / "k.db", _Market())
    before = {t: repo._conn.execute(f"SELECT count(*) FROM [{t}]").fetchone()[0]
              for (t,) in repo._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    now = T0 + timedelta(hours=8)
    assert quality.run_tick(repo, str(db), cache, REG, now) == 1
    rec = repo.list_entry_quality()[0]
    assert rec["cohort"] == "HISTORICAL_IN_SAMPLE"  # before frozen_at
    assert rec["eq_class"] in {"STRONG", "ACCEPTABLE", "WEAK", "REJECT"}
    assert rec["why"] and rec["features_used"] and rec["outcome"] is not None
    assert rec["experience"]["actual"]["std_r"] == rec["outcome"]["r"]
    assert quality.run_tick(repo, str(db), cache, REG, now) == 0

    after = {t: repo._conn.execute(f"SELECT count(*) FROM [{t}]").fetchone()[0] for t in before}
    changed = {t for t in before if before[t] != after[t]}
    assert changed <= {"entry_quality_shadow", "runs"}


def test_a_candidate_whose_window_has_not_passed_waits(tmp_path):
    db = tmp_path / "t.db"
    repo = SQLiteRepository(db)
    _persisted_candidate_in_status(repo, "CANDIDATE")
    cache = KlineCache(tmp_path / "k.db", _Market())
    assert quality.run_tick(repo, str(db), cache, REG, T0 + timedelta(hours=3)) == 0


def test_forward_cohort_is_only_after_the_freeze():
    row = {"candidate_id": "c", "symbol": "X", "t0": datetime(2026, 9, 29, tzinfo=UTC), "feat": {},
           "outcomes": {"primary": None, "fast": None}, "cohort": "NOT_ANALYSED"}
    frozen = datetime.fromisoformat(REG["frozen_at"])
    assert quality.evaluate(row, REG, frozen, True)["cohort"] == "FORWARD_OOS"
    row["t0"] = datetime(2026, 9, 27, tzinfo=UTC)
    assert quality.evaluate(row, REG, frozen, True)["cohort"] == "HISTORICAL_IN_SAMPLE"


def test_godfather_does_not_categorise_a_small_sample():
    assert entry_patterns.derive([{"independent": True, "t0": "2026-09-01T00:00:00+00:00",
                                   "features": {}, "outcome": {"r": 1.0}}] * 50) is None


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def test_entry_research_never_touches_trading_code():
    forbidden = ("live_execution", "paper_trading", "safety_kernel", "gate", "guardian", "bingx_trade",
                 "demo_execution", "orchestrator", "state_machine")
    files = list((ROOT / "crypto_trading" / "entry_research").glob("*.py")) + [
        ROOT / "crypto_trading" / "godfather" / "entry_patterns.py"]
    for f in files:
        for mod in _imports(f):
            if (f.name, mod) in _ALLOWED_PURE_IMPORTS:
                continue
            assert not any(part in mod for part in forbidden), f"{f.name} imports {mod}"


# The counterfactual risk-policy replay (2026-09-29) evaluates policy A with
# the Safety Kernel's OWN arithmetic, so "policy A" is provably the LIVE
# kernel. It may import that module only - and only because the module is
# pure (locked by the test below).
_ALLOWED_PURE_IMPORTS = {
    ("risk_policy.py", "crypto_trading.safety_kernel"),
    ("pre_ai_calibration.py", "crypto_trading.safety_kernel"),  # ground truth = the kernel itself
}


def test_the_safety_kernel_stays_a_pure_module():
    """No connector, storage, execution or network import in the kernel - so
    importing it from a read-only replay can never reach the exchange."""
    impure = ("connectors", "storage", "live_execution", "paper_trading", "httpx", "requests",
              "sqlite3", "orchestrator", "guardian", "godfather")
    for mod in _imports(ROOT / "crypto_trading" / "safety_kernel.py"):
        assert not any(part in mod for part in impure), f"safety_kernel imports {mod}"


def test_no_trading_path_reads_the_entry_quality_layer():
    trading = ["live_execution_loop.py", "orchestrator.py", "safety_kernel.py", "discovery_loop.py",
               "guardian_loop.py", "demo_execution_loop.py"]
    for name in trading:
        text = (ROOT / "crypto_trading" / name).read_text(encoding="utf-8")
        assert "entry_research" not in text and "entry_quality" not in text, name
    for f in (ROOT / "crypto_trading" / "gate").rglob("*.py"):
        assert "entry_research" not in f.read_text(encoding="utf-8")
