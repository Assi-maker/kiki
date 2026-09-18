"""Isolation proof for the GODFATHER priority-boost overlay (2026-09-18):
`godfather_priority_heuristics` and `guardian_authority_heuristics` must be
mutually unreachable from each other's call sites - table separation (not
vocabulary disjointness) is the safety property that lets a priority-boost
heuristic share PRE_ENTRY_VETO's own factor vocabulary without ever being
able to influence a real Guardian Authority decision, and vice versa.

Grep-based, over real production source files - the same style this
codebase's other isolation suites (test_self_improvement_isolation.py,
test_authority_shadow_isolation.py) already use for a structural, not just
documented, guarantee."""

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CRYPTO_TRADING = _REPO_ROOT / "crypto_trading"

_GUARDIAN_SAFETY_KERNEL_FILES = [
    _CRYPTO_TRADING / "guardian" / "authority.py",
    _CRYPTO_TRADING / "guardian" / "tick.py",
]

_GODFATHER_STRATEGY_FILES = [
    _CRYPTO_TRADING / "godfather" / "priority_boost.py",
    _CRYPTO_TRADING / "screening" / "candidate_engine.py",
]


def test_guardian_safety_kernel_never_reads_the_priority_boost_table():
    for path in _GUARDIAN_SAFETY_KERNEL_FILES:
        text = path.read_text(encoding="utf-8")
        assert "find_godfather_priority_heuristics" not in text, (
            f"{path} must never read godfather_priority_heuristics - "
            "that table exists only for candidate ranking, never for a "
            "real Guardian Authority decision"
        )
        assert "godfather_priority_heuristics" not in text, (
            f"{path} must not even reference the godfather_priority_heuristics "
            "table name"
        )


def test_godfather_strategy_layer_never_reads_guardian_authority_heuristics_table():
    for path in _GODFATHER_STRATEGY_FILES:
        text = path.read_text(encoding="utf-8")
        assert "find_guardian_authority_heuristics" not in text, (
            f"{path} must never read guardian_authority_heuristics - that "
            "table is Guardian Authority's own safety-kernel-adjacent "
            "table, structurally isolated from GODFATHER's strategy layer"
        )


_FORBIDDEN_DECISION_CORE_NAMES = {
    "decide_pre_entry",
    "decide_open_position",
    "maybe_open_position_for_candidate",
}


def _imported_names(path: Path) -> set[str]:
    """Every name actually IMPORTED (not merely mentioned in a docstring or
    comment) by `path` - AST-based, so a module docstring that legitimately
    explains why these functions are frozen/untouched (as priority_boost.py's
    own does) can never trip this check the way a bare substring scan would."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.asname or alias.name for alias in node.names)
    return names


def test_guardian_authority_decision_core_functions_are_never_imported_by_godfather_package():
    """Belt-and-suspenders: even the pure decision functions themselves
    (decide_pre_entry/decide_open_position/maybe_open_position_for_candidate)
    must never be IMPORTED by the new godfather/ ranking code path - only the
    pure, side-effect-free helpers it legitimately reuses (_pre_entry_factors,
    evaluate_heuristics - itself reused here, but only ever against the
    SEPARATE godfather_priority_heuristics table, never against
    guardian_authority_heuristics)."""
    for path in _GODFATHER_STRATEGY_FILES:
        imported = _imported_names(path)
        overlap = imported & _FORBIDDEN_DECISION_CORE_NAMES
        assert not overlap, f"{path} imports forbidden decision-core name(s): {overlap}"


def test_only_two_files_read_godfather_priority_heuristics_at_all():
    """Confirms the read surface is exactly what the design claims: the
    ranking call site in candidate_engine.py, plus priority_boost.py's own
    pipeline (which reads it for the self-critique-style forward tracking
    and to build LLM context) - nothing else in the whole crypto_trading/
    package ever touches this table."""
    readers = []
    for path in _CRYPTO_TRADING.rglob("*.py"):
        if path.name == "repository.py":
            continue  # the CRUD method's own definition, not a "reader"
        text = path.read_text(encoding="utf-8")
        if "find_godfather_priority_heuristics" in text:
            readers.append(path)

    assert {p.name for p in readers} == {"priority_boost.py", "candidate_engine.py"}, (
        f"unexpected reader set for godfather_priority_heuristics: {readers}"
    )
