"""Isolation proof for the GODFATHER Intelligence Layer (2026-09-25).

The safety argument for shipping this layer enabled - while every other
GODFATHER surface ships inert - is structural, not procedural:

  * nothing in the intelligence layer can reach an order primitive, a
    sizing function, the safety kernel's own heuristics tables, or any
    write path into `positions`; and
  * nothing in the live trading path reads any of the seven
    `godfather_*` intelligence tables.

Together those mean a bug anywhere in this layer can produce a wrong
REPORT and nothing else. Both halves are proven here by grep and AST over
the real production sources, the same style the codebase's existing
isolation suites (`test_priority_boost_isolation.py`,
`test_self_improvement_isolation.py`, `test_authority_shadow_isolation.py`)
already use.
"""

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CRYPTO_TRADING = _REPO_ROOT / "crypto_trading"

_INTELLIGENCE_MODULES = [
    _CRYPTO_TRADING / "godfather" / "auditor.py",
    _CRYPTO_TRADING / "godfather" / "counterfactual.py",
    _CRYPTO_TRADING / "godfather" / "entry_quality.py",
    _CRYPTO_TRADING / "godfather" / "experience.py",
    _CRYPTO_TRADING / "godfather" / "features.py",
    _CRYPTO_TRADING / "godfather" / "investigator.py",
    _CRYPTO_TRADING / "godfather" / "objective.py",
    _CRYPTO_TRADING / "godfather" / "path.py",
    _CRYPTO_TRADING / "godfather" / "pipeline.py",
    _CRYPTO_TRADING / "godfather" / "prediction_error.py",
    _CRYPTO_TRADING / "godfather" / "report.py",
    _CRYPTO_TRADING / "godfather" / "stats.py",
    _CRYPTO_TRADING / "godfather" / "thesis.py",
    _CRYPTO_TRADING / "godfather_loop.py",
]

# The seven tables this layer owns. Nothing in the live trading path may
# name any of them.
_INTELLIGENCE_TABLES = [
    "godfather_trade_investigations",
    "godfather_decision_audits",
    "godfather_counterfactuals",
    "godfather_experience_patterns",
    "godfather_prediction_errors",
    "godfather_position_thesis",
    "godfather_entry_quality",
]

# Everything that can move real money, or that decides whether money
# moves. `storage/` is deliberately absent: the repository is the shared
# persistence layer and necessarily names every table in the schema.
_LIVE_TRADING_PATH = [
    _CRYPTO_TRADING / "gate" / "risk_signal_gate.py",
    _CRYPTO_TRADING / "gate" / "qa_gate.py",
    _CRYPTO_TRADING / "screening" / "candidate_engine.py",
    _CRYPTO_TRADING / "screening" / "eligibility_filter.py",
    _CRYPTO_TRADING / "screening" / "quant_screener.py",
    _CRYPTO_TRADING / "guardian" / "authority.py",
    _CRYPTO_TRADING / "guardian" / "authority_live.py",
    _CRYPTO_TRADING / "guardian" / "tick.py",
    _CRYPTO_TRADING / "guardian" / "self_improvement.py",
    _CRYPTO_TRADING / "godfather" / "priority_boost.py",
    _CRYPTO_TRADING / "paper_trading" / "position_opening.py",
    _CRYPTO_TRADING / "paper_trading" / "position_closing.py",
    _CRYPTO_TRADING / "paper_trading" / "position_sizing.py",
    _CRYPTO_TRADING / "paper_trading" / "monitoring.py",
    _CRYPTO_TRADING / "paper_trading" / "live_execution.py",
    _CRYPTO_TRADING / "paper_trading" / "live_profit_protection.py",
    _CRYPTO_TRADING / "paper_trading" / "demo_execution.py",
]

# Names that place, cancel or modify a real order, change leverage, size a
# position, or mutate a real position row.
_FORBIDDEN_EXECUTION_NAMES = {
    "place_order",
    "place_stop_loss_order",
    "place_take_profit_order",
    "cancel_order",
    "close_position",
    "set_leverage",
    "compute_position_size",
    "open_position_for_candidate",
    "maybe_open_position_for_candidate",
    "tighten_position_stop_loss",
    "close_position_with_event",
    "close_position_for_live_exit",
    "create_position_with_event",
    "upsert_guardian_authority_heuristic",
    "upsert_godfather_priority_heuristic",
    "save_guardian_authority_decision",
    "promote_guardian_authority_heuristic_candidate",
    "promote_godfather_priority_heuristic_candidate",
}

_FORBIDDEN_IMPORT_PREFIXES = (
    "crypto_trading.connectors",
    "crypto_trading.paper_trading.position_opening",
    "crypto_trading.paper_trading.position_closing",
    "crypto_trading.paper_trading.position_sizing",
    "crypto_trading.paper_trading.live_execution",
    "crypto_trading.paper_trading.demo_execution",
    "crypto_trading.paper_trading.live_profit_protection",
    "crypto_trading.guardian.authority",
    "crypto_trading.guardian.authority_live",
    "crypto_trading.gate",
)


def _called_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_every_intelligence_module_exists_and_is_covered_by_this_suite():
    """A new module added to the package without being listed here would
    otherwise be silently unguarded."""
    listed = {path.name for path in _INTELLIGENCE_MODULES}
    on_disk = {
        path.name
        for path in (_CRYPTO_TRADING / "godfather").glob("*.py")
        if path.name not in ("__init__.py", "priority_boost.py")
    }

    assert on_disk <= listed, f"unguarded intelligence modules: {sorted(on_disk - listed)}"


def test_the_intelligence_layer_calls_no_execution_primitive():
    for path in _INTELLIGENCE_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        forbidden = _called_names(tree) & _FORBIDDEN_EXECUTION_NAMES
        assert not forbidden, (
            f"{path.name} calls {sorted(forbidden)} - the intelligence layer analyses "
            "trades, it never places, sizes, closes or authorises one"
        )


def test_the_intelligence_layer_imports_no_execution_or_connector_module():
    for path in _INTELLIGENCE_MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module in _imported_modules(tree):
            assert not module.startswith(_FORBIDDEN_IMPORT_PREFIXES), (
                f"{path.name} imports {module} - the intelligence layer must not be "
                "able to reach an exchange connector, the Gate, or an execution module"
            )


def test_the_intelligence_layer_never_writes_the_safety_kernels_heuristics():
    """`guardian_authority_heuristics` is the ONLY table the live decision
    core reads. If nothing here can write it, nothing here can change a
    live decision - regardless of what it concludes."""
    for path in _INTELLIGENCE_MODULES:
        text = path.read_text(encoding="utf-8")
        assert "upsert_guardian_authority_heuristic" not in text, path.name
        assert "upsert_godfather_priority_heuristic" not in text, path.name


def test_the_live_trading_path_never_reads_an_intelligence_table():
    for path in _LIVE_TRADING_PATH:
        text = path.read_text(encoding="utf-8")
        for table in _INTELLIGENCE_TABLES:
            assert table not in text, (
                f"{path.name} references {table} - no trading decision may depend on "
                "the intelligence layer in this phase"
            )


def test_the_live_trading_path_never_calls_an_intelligence_repository_method():
    intelligence_readers = {
        "find_godfather_trade_investigations",
        "get_godfather_trade_investigation",
        "find_godfather_decision_audits",
        "get_godfather_decision_audit",
        "find_godfather_counterfactuals",
        "find_godfather_counterfactuals_for_position",
        "find_godfather_experience_patterns",
        "find_godfather_prediction_errors",
        "find_godfather_position_thesis_for_position",
        "find_latest_godfather_position_thesis",
        "get_godfather_entry_quality",
        "find_godfather_entry_quality_assessments",
    }
    for path in _LIVE_TRADING_PATH:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        forbidden = _called_names(tree) & intelligence_readers
        assert not forbidden, f"{path.name} calls {sorted(forbidden)}"


def test_the_live_trading_path_never_imports_an_intelligence_module():
    intelligence_imports = tuple(
        f"crypto_trading.godfather.{path.stem}"
        for path in _INTELLIGENCE_MODULES
        if path.parent.name == "godfather"
    ) + ("crypto_trading.godfather_loop",)
    for path in _LIVE_TRADING_PATH:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for module in _imported_modules(tree):
            assert not module.startswith(intelligence_imports), (
                f"{path.name} imports {module}"
            )


def test_the_enforcement_flags_are_read_by_nothing_yet():
    """Both enforcement surfaces ship inert, and "inert" is checked rather
    than promised: no module anywhere reads the flags, so no code path
    exists that flipping one could activate by accident."""
    readers = []
    for path in _CRYPTO_TRADING.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "entry_quality_enforcement_enabled" in text or "thesis_enforcement_enabled" in text:
            readers.append(path.name)

    assert readers == ["loader.py"], (
        f"enforcement flags are read by {readers} - they must be declared in config "
        "only until a separate, explicitly approved activation"
    )


def test_every_thesis_and_entry_quality_row_is_written_as_advisory():
    """Grep-level guard on the two writers: a future edit that starts
    persisting enforced=True has to change this test too."""
    pipeline = (_CRYPTO_TRADING / "godfather" / "pipeline.py").read_text(encoding="utf-8")
    entry_quality = (_CRYPTO_TRADING / "godfather" / "entry_quality.py").read_text(
        encoding="utf-8"
    )

    assert "enforced=False" in pipeline
    assert "enforced=True" not in pipeline
    assert "enforced=False" in entry_quality
    assert "enforced=True" not in entry_quality
