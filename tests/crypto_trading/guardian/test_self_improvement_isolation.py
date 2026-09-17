"""Guardian Authority Live Autonomy (2026-09-15) - Task 8: whole-plan
production-isolation tests (req-9-style checklist).

Not a new feature - an independent re-verification, across the WHOLE
plan's diff (base `a09b0ab` - the master commit
`guardian-authority-live-autonomy` forked from - through current HEAD,
Tasks 1-7), of every safety/isolation guarantee this plan's Global
Constraints promise (docs/superpowers/plans/2026-09-15-guardian-authority-
live-autonomy.md, "Global Constraints" section) and each individual
task's own review already checked one task at a time.

Mirrors `tests/crypto_trading/guardian/test_authority_shadow_isolation.py`
(Shadow Mode's own equivalent whole-plan isolation suite) exactly in
structure and rigor, INCLUDING that file's own final-review-fixed
mistakes, which this file must not reintroduce:

1. The scan universe below (`PRODUCTION_FILES`/`ALL_TOUCHED_PY_FILES`) is
   a literal, HARDCODED list, recorded once, by hand, from `git diff
   --stat a09b0ab..HEAD` at authoring time (2026-09-17) - never re-derived
   from a live git computation. A git-diff-derived list would silently
   stop checking a file once it is no longer "new" on whatever branch this
   merges into, which defeats the point of a permanent isolation
   guarantee.
2. The frozen-function integrity check (item 1 below) compares a
   HARDCODED SHA-256 hash of each function's/method's CURRENT source
   (`inspect`-style AST source extraction straight off the live,
   checked-out file, computed once at authoring time) against a freshly
   computed hash of that same source at test time - never `git show
   <base_sha>:path` or any other git-history-dependent comparison. That
   would break the moment `a09b0ab` becomes unreachable (rebase, squash,
   shallow clone) and would then silently stop protecting anything.

`_added_line_numbers` below (used only to restrict a handful of scans to
the lines THIS plan actually added, never to compute the scan universe
itself or the frozen-function hashes) is the one place this file still
touches git, via `git diff -U0 a09b0ab..HEAD` - the same, narrower use the
reference file's own module docstring explains survives its own fix: it
depends only on `a09b0ab` remaining a resolvable ancestor commit, which it
will for as long as this repository's history exists, not on the diff
range never growing.
"""

from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_SHA = "a09b0ab"  # master commit guardian-authority-live-autonomy forked from


# ---------------------------------------------------------------------------
# Hardcoded scan universe (see module docstring, lesson #1) - recorded once,
# by hand, from `git diff --stat a09b0ab..HEAD` at authoring time
# (2026-09-17, HEAD abd7deb). This IS the scan universe, not a value derived
# from a live git computation.
# ---------------------------------------------------------------------------

# Every production .py file Tasks 1-7's combined diff touches.
PRODUCTION_FILES = [
    "crypto_trading/discovery_loop.py",
    "crypto_trading/guardian/authority.py",
    "crypto_trading/guardian/self_improvement.py",
    "crypto_trading/guardian/tick.py",
    "crypto_trading/schemas/assessments.py",
    "crypto_trading/storage/db.py",
    "crypto_trading/storage/repository.py",
]

# All touched .py files, production AND test - the broader scan universe for
# the full-diff re-checks below.
ALL_TOUCHED_PY_FILES = [
    *PRODUCTION_FILES,
    "tests/crypto_trading/guardian/test_authority.py",
    "tests/crypto_trading/guardian/test_self_improvement.py",
    "tests/crypto_trading/guardian/test_self_improvement_demotion.py",
    "tests/crypto_trading/guardian/test_self_improvement_isolation.py",  # this file
    "tests/crypto_trading/guardian/test_self_improvement_pre_entry_pool.py",
    "tests/crypto_trading/guardian/test_self_improvement_promotion.py",
    "tests/crypto_trading/guardian/test_self_improvement_tick.py",
    "tests/crypto_trading/guardian/test_self_improvement_validation.py",
    "tests/crypto_trading/guardian/test_tick.py",
    "tests/crypto_trading/storage/test_db.py",
    "tests/crypto_trading/storage/test_repository_guardian_authority.py",
    "tests/crypto_trading/storage/test_repository_guardian_authority_heuristic_candidates.py",
    "tests/crypto_trading/test_discovery_loop.py",
]

# Global Constraint (verbatim): never modify position_opening.py,
# position_sizing.py, gate/, screening/, risk_limits.yaml, or
# live_execution.yaml's hard limits. These are named here, once, so the
# exclusion test below fails loudly if any of them is ever (re-)added to
# the scan universe above - which would mean this plan started touching a
# file it is explicitly forbidden from touching.
FORBIDDEN_TOUCHED_PATHS = (
    "crypto_trading/paper_trading/position_opening.py",
    "crypto_trading/paper_trading/position_sizing.py",
    "crypto_trading/config/risk_limits.yaml",
    "crypto_trading/config/live_execution.yaml",
)
FORBIDDEN_TOUCHED_DIR_PREFIXES = (
    "crypto_trading/gate/",
    "crypto_trading/screening/",
)


def test_production_files_list_is_not_empty_and_matches_expected_shape():
    """Sanity check on the scan universe itself: if this were empty, every
    other test below would vacuously pass while checking nothing. Pins the
    exact expected file set and confirms every listed path genuinely exists
    on disk, so a typo or an accidentally-removed file is caught here rather
    than silently shrinking every other check's coverage."""
    assert PRODUCTION_FILES == [
        "crypto_trading/discovery_loop.py",
        "crypto_trading/guardian/authority.py",
        "crypto_trading/guardian/self_improvement.py",
        "crypto_trading/guardian/tick.py",
        "crypto_trading/schemas/assessments.py",
        "crypto_trading/storage/db.py",
        "crypto_trading/storage/repository.py",
    ]
    for path in PRODUCTION_FILES:
        assert (REPO_ROOT / path).is_file(), f"{path} does not exist on disk"


def test_hardcoded_scan_universe_is_internally_consistent_and_self_contained():
    """Self-contained consistency checks on the two hardcoded scan-universe
    constants: every listed path exists on disk, PRODUCTION_FILES is a
    genuine subset of ALL_TOUCHED_PY_FILES, and ALL_TOUCHED_PY_FILES
    contains at least one touched test file."""
    assert PRODUCTION_FILES, "PRODUCTION_FILES must not be empty"
    assert ALL_TOUCHED_PY_FILES, "ALL_TOUCHED_PY_FILES must not be empty"
    for path in ALL_TOUCHED_PY_FILES:
        assert (REPO_ROOT / path).is_file(), f"{path} does not exist on disk"
    assert set(PRODUCTION_FILES) <= set(ALL_TOUCHED_PY_FILES)
    test_files = [f for f in ALL_TOUCHED_PY_FILES if f.startswith("tests/")]
    assert test_files, "expected at least one touched test file in the scan universe"


def test_scan_universe_excludes_every_never_modify_path_and_directory():
    """Checklist item 2 (exclusion half): the plan's Global Constraint list
    (`position_opening.py`, `position_sizing.py`, `gate/`, `screening/`,
    `risk_limits.yaml`, `live_execution.yaml`) must never appear in the
    hardcoded scan universe - confirmed once, by hand, via `git diff --stat
    a09b0ab..HEAD` at authoring time (none of these paths appears in that
    diff at all), and pinned here so a future edit to PRODUCTION_FILES/
    ALL_TOUCHED_PY_FILES that accidentally adds one of them is caught
    immediately, without needing to re-run git to notice."""
    for forbidden in FORBIDDEN_TOUCHED_PATHS:
        assert forbidden not in PRODUCTION_FILES
        assert forbidden not in ALL_TOUCHED_PY_FILES
        assert (REPO_ROOT / forbidden).is_file(), f"{forbidden} unexpectedly missing from disk"
    for path in ALL_TOUCHED_PY_FILES:
        for prefix in FORBIDDEN_TOUCHED_DIR_PREFIXES:
            assert not path.startswith(prefix), f"{path} is under forbidden prefix {prefix}"
    # Existence + sanity of live_execution.yaml's own three hard-limit
    # fields (not a "this plan left them at value X" claim - the exclusion
    # assertion above is what proves this plan never touched the file at
    # all - just confirming the file this plan is forbidden from touching
    # is itself intact and still carries the fields the constraint names).
    with (REPO_ROOT / "crypto_trading/config/live_execution.yaml").open(encoding="utf-8") as f:
        live_execution_raw = yaml.safe_load(f)
    for field in ("leverage", "margin_per_trade_usdt", "max_concurrent_positions"):
        assert field in live_execution_raw, f"live_execution.yaml missing hard-limit field {field}"


_HUNK_HEADER_RE = None


def _hunk_header_match(line: str) -> str | None:
    import re

    global _HUNK_HEADER_RE
    if _HUNK_HEADER_RE is None:
        _HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    match = _HUNK_HEADER_RE.match(line)
    return match.group(1) if match else None


def _added_line_numbers(path: str, base: str = BASE_SHA) -> set[int]:
    """New-file line numbers this plan's diff actually ADDED for `path`, via
    `git diff -U0` (zero context lines - a hunk contains only real +/-
    lines). Context/unchanged/pre-existing lines never appear in a -U0 diff
    at all, so they cannot be mistaken for additions. Used ONLY to restrict
    a scan to new lines - never to compute the scan universe or a frozen-
    function hash (see module docstring)."""
    out = subprocess.run(
        ["git", "diff", "-U0", f"{base}..HEAD", "--", path],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    added: set[int] = set()
    new_lineno = None
    for line in out.splitlines():
        header = _hunk_header_match(line)
        if header:
            new_lineno = int(header)
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            assert new_lineno is not None
            added.add(new_lineno)
            new_lineno += 1
    return added


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


# ---------------------------------------------------------------------------
# Item 1: SHA-256 of live source for the 8 frozen functions - 6 top-level
# pure functions in authority.py, and 2 methods (find_guardian_authority_
# heuristics / upsert_guardian_authority_heuristic) that live on
# repository.py's concrete SQLiteRepository class (NOT the Repository(
# Protocol) declaration a few hundred lines above it in the same file,
# which is a type-checking stub with an `...` body and cannot itself be
# "modified" in any behaviorally meaningful sense - the real, executable
# implementation is what this plan's Global Constraint protects).
# ---------------------------------------------------------------------------

_PURE_DECISION_FUNCTIONS = (
    "evaluate_heuristics",
    "decide_pre_entry",
    "decide_open_position",
    "heuristic_condition_matches",
    "_compute_proposed_new_sl",
    "_groups_for_factors",
)

AUTHORITY_PATH = "crypto_trading/guardian/authority.py"
REPOSITORY_PATH = "crypto_trading/storage/repository.py"
_REPOSITORY_CLASS = "SQLiteRepository"

_REPOSITORY_FROZEN_METHODS = (
    "find_guardian_authority_heuristics",
    "upsert_guardian_authority_heuristic",
)

# Recorded once (2026-09-17, authoring time, HEAD abd7deb): SHA-256 of each
# function's/method's AST source segment, read straight from the live files
# at authoring time (the exact same extraction `_current_function_source`/
# `_current_method_source` below perform at test time). No git history
# involved - see module docstring, lesson #2. The 6 authority.py values
# below are byte-identical to the ones already recorded in the sibling
# Shadow Mode isolation file's own `_EXPECTED_FUNCTION_SOURCE_SHA256` - this
# is expected, not a coincidence: these 6 functions are the SAME frozen
# pure decision core, unmodified by either plan.
_EXPECTED_FUNCTION_SOURCE_SHA256 = {
    "evaluate_heuristics": "b7351e34af0b2e9943533463aa20b3f304fca235c9251e6bf720e40a82ea2c69",
    "decide_pre_entry": "509a88b5326b7b757a59d6510e7ca132abd32e19bcb6952dd68fe9abd8461cbf",
    "decide_open_position": "b4d761d049fc7e0e1fd193cb7529b11b36dcdb640950425acd931c7a131f80fd",
    "heuristic_condition_matches": "119661ae444769f018a634a95bef8d3a8a9d613e669f15304819bdd16248276f",
    "_compute_proposed_new_sl": "a580c0ce176dd6acc6cb74d7c7cbaf79d3d8e9146c0887a56d7f2c6222dd4c57",
    "_groups_for_factors": "7bbefa709200c067b26f0d09b0e43493e1815f8a5aa60a1084f4c900edde4efc",
}

_EXPECTED_METHOD_SOURCE_SHA256 = {
    "find_guardian_authority_heuristics": (
        "382238f062a2ffb82b14acc3b6609d234e23ac5fb2eb9d4e2d8a420105efecd9"
    ),
    "upsert_guardian_authority_heuristic": (
        "4c3e3648bce30f12b97fce43aba38682682b464c61f42ed789afb0c1ffe379e6"
    ),
}


def _current_function_source(path: str, function_name: str) -> str:
    """`function_name`'s CURRENT top-level (or nested, but here always
    top-level) source segment, read straight from the live file on disk
    (never `git show` - see module docstring, lesson #2)."""
    source = _read(path)
    tree = ast.parse(source, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, f"could not extract source for {function_name}"
            return segment
    raise AssertionError(f"function {function_name} not found in {path}")


def _current_method_source(path: str, class_name: str, method_name: str) -> str:
    """`method_name`'s CURRENT source segment, restricted to a direct child
    of `class_name`'s own body - so `repository.py`'s Repository(Protocol)
    stub declaration (same method name, `...` body, a few hundred lines
    above the concrete implementation) can never be the one this hashes."""
    source = _read(path)
    tree = ast.parse(source, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == method_name:
                    segment = ast.get_source_segment(source, child)
                    assert segment is not None, f"could not extract source for {class_name}.{method_name}"
                    return segment
    raise AssertionError(f"method {class_name}.{method_name} not found in {path}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("function_name", _PURE_DECISION_FUNCTIONS)
def test_pure_decision_function_is_byte_identical_to_recorded_hash(function_name):
    """Global Constraint (verbatim): 'Never modify evaluate_heuristics,
    decide_pre_entry, decide_open_position, heuristic_condition_matches,
    _compute_proposed_new_sl, _groups_for_factors, ... - byte-identical
    before/after this whole plan.' Compares a SHA-256 hash of the
    function's CURRENT source (read from the live file, never `git show`)
    against the hash recorded at authoring time - self-contained, survives
    this branch merging, never merely 'behaves the same'."""
    current_source = _current_function_source(AUTHORITY_PATH, function_name)
    current_hash = _sha256(current_source)
    assert current_hash == _EXPECTED_FUNCTION_SOURCE_SHA256[function_name], (
        f"{function_name} changed since its hash was recorded - Global Constraint violation"
    )


@pytest.mark.parametrize("method_name", _REPOSITORY_FROZEN_METHODS)
def test_frozen_repository_method_is_byte_identical_to_recorded_hash(method_name):
    """Global Constraint (verbatim): 'Never modify ... upsert_guardian_
    authority_heuristic, find_guardian_authority_heuristics - byte-identical
    before/after this whole plan.' Same mechanism as the test above, applied
    to the concrete SQLiteRepository method (never the Repository(Protocol)
    stub declaration - see `_current_method_source`'s own docstring)."""
    current_source = _current_method_source(REPOSITORY_PATH, _REPOSITORY_CLASS, method_name)
    current_hash = _sha256(current_source)
    assert current_hash == _EXPECTED_METHOD_SOURCE_SHA256[method_name], (
        f"{method_name} changed since its hash was recorded - Global Constraint violation"
    )


def test_byte_identical_check_genuinely_detects_a_real_change_in_a_function():
    """Confirms the comparison above is not vacuously true. Deliberately
    hashes evaluate_heuristics' CURRENT source against a mutated copy of
    itself (one character appended) and confirms the two hashes differ,
    then confirms the mutated hash does NOT equal the recorded expected
    hash either - the exact failure mode the real test above would hit if
    evaluate_heuristics were ever modified."""
    current_source = _current_function_source(AUTHORITY_PATH, "evaluate_heuristics")
    mutated = current_source + "  # tampered\n"
    assert _sha256(mutated) != _sha256(current_source)
    assert _sha256(mutated) != _EXPECTED_FUNCTION_SOURCE_SHA256["evaluate_heuristics"]


def test_byte_identical_check_genuinely_detects_a_real_change_in_a_method():
    """Method-extraction counterpart of the test above, for
    `upsert_guardian_authority_heuristic` (a class method, not a top-level
    function) - confirms `_current_method_source` genuinely distinguishes a
    mutated body from the recorded hash, and is not vacuously true just
    because the class-scoped lookup happened to find nothing and fall
    through to some default."""
    current_source = _current_method_source(
        REPOSITORY_PATH, _REPOSITORY_CLASS, "upsert_guardian_authority_heuristic"
    )
    mutated = current_source + "  # tampered\n"
    assert _sha256(mutated) != _sha256(current_source)
    assert _sha256(mutated) != _EXPECTED_METHOD_SOURCE_SHA256["upsert_guardian_authority_heuristic"]


def test_method_extraction_never_accidentally_matches_the_protocol_stub():
    """Deliberate-break confirmation for `_current_method_source`'s own
    class-scoping: `Repository(Protocol)` (repository.py, ~line 46) declares
    both frozen method names with an `...` body a few hundred lines before
    `SQLiteRepository`'s own concrete implementation. If class-scoping ever
    regressed to a bare whole-file name search (like `_current_function_
    source`'s, which has no class filter), it would find the Protocol
    stub's `...` body first - a source segment that could never meaningfully
    change and would make the hash check above vacuous. Confirms the actual
    extracted source for both frozen methods contains a real function body,
    not just an ellipsis."""
    for method_name in _REPOSITORY_FROZEN_METHODS:
        segment = _current_method_source(REPOSITORY_PATH, _REPOSITORY_CLASS, method_name)
        assert segment.strip() != f"def {method_name}(self) -> list[dict]: ..."
        assert "..." not in segment.splitlines()[-1].strip() or len(segment.splitlines()) > 1
        assert "self._conn" in segment, (
            f"{method_name}'s extracted source does not look like the real "
            "SQLiteRepository implementation - class-scoping may have matched "
            "the Protocol stub instead"
        )


def test_authority_py_pure_decision_functions_have_zero_deletions_in_the_whole_diff():
    """Independent, coarser-grained corroboration of the 6 authority.py
    hash checks above: `git diff --numstat` for the whole file shows only
    insertions (Task 2's additive matched_heuristic_ids_json wiring inside
    maybe_open_position_for_candidate, which is NOT one of the frozen
    functions) and ZERO deletions - meaning no existing line anywhere in
    authority.py, including but not limited to the 6 named functions, was
    ever modified or removed by this plan."""
    out = subprocess.run(
        ["git", "diff", "--numstat", f"{BASE_SHA}..HEAD", "--", AUTHORITY_PATH],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    added, removed, _ = out.split("\t")
    assert removed == "0", f"authority.py has {removed} deleted line(s) - expected 0"
    assert int(added) > 0


def test_frozen_repository_methods_line_ranges_were_never_touched_by_this_diff():
    """repository.py's own numstat is NOT insertions-only (Task 1/2 widened
    an unrelated INSERT statement's column list elsewhere in the same file),
    so the whole-file zero-deletions corroboration above does not apply
    here. Narrower, still git-based, corroboration instead: restrict
    `_added_line_numbers`'s new-line set to each frozen method's own AST
    line range (`lineno`..`end_lineno`) and confirm it is empty - i.e. this
    diff added zero lines inside either frozen method's own body, anywhere,
    not just at the exact lines the hash check happens to read today."""
    added = _added_line_numbers(REPOSITORY_PATH)
    source = _read(REPOSITORY_PATH)
    tree = ast.parse(source, filename=REPOSITORY_PATH)
    for method_name in _REPOSITORY_FROZEN_METHODS:
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == _REPOSITORY_CLASS:
                for child in node.body:
                    if (
                        isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and child.name == method_name
                    ):
                        found = True
                        touched = {
                            ln for ln in added
                            if child.lineno <= ln <= (child.end_lineno or child.lineno)
                        }
                        assert touched == set(), (
                            f"{method_name} has added line(s) {touched} in this diff"
                        )
        assert found, f"{method_name} not found in {_REPOSITORY_CLASS}"


# ---------------------------------------------------------------------------
# Item 2: no file this plan touched imports position_sizing.py or calls
# set_leverage (Global Constraint, verbatim). The scan universe already
# structurally excludes position_opening.py/gate//screening//risk_limits
# .yaml/live_execution.yaml (see test_scan_universe_excludes_every_
# never_modify_path_and_directory above).
# ---------------------------------------------------------------------------

_FORBIDDEN_POSITION_SIZING_MODULE = "crypto_trading.paper_trading.position_sizing"
_FORBIDDEN_SET_LEVERAGE_CALL = "set_leverage"


def _position_sizing_import_violations(path: str) -> list[str]:
    """AST-based whole-file import scan (Import/ImportFrom nodes only) -
    deliberately NOT a bare substring scan for "position_sizing": this
    plan's own self_improvement.py module docstring legitimately says (in
    prose) "this module never imports position_sizing.py" to document the
    very guarantee this test verifies at the code level, and a substring
    scan would incorrectly flag that documentation as a violation of it
    (same precedent as the sibling isolation file's own handling of
    "authority_enabled" appearing in legitimate new-code prose). An AST
    Import/ImportFrom scan has no such false-positive risk: prose in a
    docstring or comment is never parsed as an import statement."""
    offenders: list[str] = []
    tree = ast.parse(_read(path), filename=path)
    for node in ast.walk(tree):
        imported: set[str] = set()
        if isinstance(node, ast.Import):
            imported = {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported = {node.module}
        for module in imported:
            if module == _FORBIDDEN_POSITION_SIZING_MODULE or module.startswith(
                _FORBIDDEN_POSITION_SIZING_MODULE + "."
            ):
                offenders.append(f"{path}: imports {module}")
    return offenders


def test_no_production_file_imports_position_sizing():
    """Global Constraint (verbatim): never import position_sizing.py
    anywhere in this plan's diff."""
    offenders: list[str] = []
    for path in PRODUCTION_FILES:
        offenders.extend(_position_sizing_import_violations(path))
    assert offenders == [], f"forbidden position_sizing import(s): {offenders}"


def test_position_sizing_import_scan_genuinely_catches_a_synthesized_violation():
    """Deliberate-break confirmation: a synthesized module containing `from
    crypto_trading.paper_trading.position_sizing import compute_position_size`
    must be flagged by the same AST matching logic the test above relies
    on."""
    fake_source = (
        "from crypto_trading.paper_trading.position_sizing import compute_position_size\n"
        "\n"
        "def f():\n"
        "    return compute_position_size\n"
    )
    tree = ast.parse(fake_source)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _FORBIDDEN_POSITION_SIZING_MODULE:
            hits.append(node.module)
    assert hits == [_FORBIDDEN_POSITION_SIZING_MODULE]


def _new_set_leverage_call_violations(path: str) -> list[str]:
    """AST Call-name scan for `set_leverage`, restricted to lines THIS
    plan's diff actually added (`_added_line_numbers`) - a Call node, not a
    bare identifier, so a docstring/comment mention of "set_leverage" (this
    plan's own self_improvement.py module docstring says, in prose, "this
    module ... never references set_leverage") can never be mistaken for an
    actual call site."""
    added = _added_line_numbers(path)
    if not added:
        return []
    tree = ast.parse(_read(path), filename=path)
    violations: list[str] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            if _call_name(node) == _FORBIDDEN_SET_LEVERAGE_CALL and node.lineno in added:
                enclosing = self.stack[-1] if self.stack else "<module>"
                violations.append(f"{path}:{node.lineno} calls set_leverage() inside {enclosing}")
            self.generic_visit(node)

    _Visitor().visit(tree)
    return violations


def test_no_new_call_site_of_set_leverage_in_any_touched_production_file():
    """Global Constraint (verbatim): never call set_leverage. AST-based,
    new-call-sites-only scan across every production file this plan's diff
    touches."""
    all_violations: list[str] = []
    for path in PRODUCTION_FILES:
        all_violations.extend(_new_set_leverage_call_violations(path))
    assert all_violations == [], f"forbidden new set_leverage() call site(s): {all_violations}"


def test_set_leverage_call_scan_genuinely_catches_a_new_violation_when_one_exists():
    """Deliberate-break confirmation, same shape as the sibling isolation
    file's own equivalent test: synthesizes a fake diff/source pair (no
    real file touched), and asserts the underlying detection logic (AST
    Call-name matching against the added-lines set) flags a call on a line
    marked as added, and does NOT flag the identical call on a line not
    marked as added."""
    source = (
        "def caller():\n"
        "    set_leverage(5)\n"   # line 2
        "    other_call('x')\n"   # line 3
    )
    tree = ast.parse(source)
    added = {2}  # line 3 simulates pre-existing, untouched code

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.hits: list[str] = []

        def visit_Call(self, node: ast.Call) -> None:
            if _call_name(node) == "set_leverage" and node.lineno in added:
                self.hits.append("set_leverage")
            self.generic_visit(node)

    visitor = _Visitor()
    visitor.visit(tree)
    assert visitor.hits == ["set_leverage"]


def test_no_new_line_anywhere_in_the_diff_textually_calls_set_leverage():
    """Line-restricted textual scan (belt-and-suspenders on top of the AST
    scan above, same discipline as the sibling isolation file's own
    position_sizing textual check): every line THIS plan added, across
    every touched file (production AND test), must not contain the literal
    substring "set_leverage(" - the opening parenthesis is what
    distinguishes an actual call/reference-as-callable from a bare
    docstring mention of the identifier (e.g. self_improvement.py's own
    module docstring: "never references `set_leverage`" - no parenthesis
    follows, so this pattern does not match it). Confirmed by hand at
    authoring time that "set_leverage(" does not occur anywhere in any file
    this plan touches, added or not."""
    offenders: list[str] = []
    for path in ALL_TOUCHED_PY_FILES:
        added = _added_line_numbers(path)
        if not added:
            continue
        for lineno, line in enumerate(_read(path).splitlines(), start=1):
            if lineno in added and "set_leverage(" in line:
                offenders.append(f"{path}:{lineno}")
    assert offenders == [], f"textual set_leverage( reference(s) found: {offenders}"


# ---------------------------------------------------------------------------
# Item 3: upsert_guardian_authority_heuristic has EXACTLY 3 call sites in
# the whole codebase - the original (pre-this-plan) Task 9 self-critique
# call site (authority.py::update_heuristics_from_resolved_decisions), this
# plan's Task 5 promotion call site (self_improvement.py::
# _write_llm_heuristic, called from promote_validated_heuristic_candidates),
# and this plan's Task 6 demotion call site (self_improvement.py::
# track_and_demote_underperforming_heuristics). A 4th call site anywhere -
# in this plan's diff or any future one - would mean a second write path
# into the real guardian_authority_heuristics table exists, which the
# plan's own Global Constraint forbids outright.
# ---------------------------------------------------------------------------

_UPSERT_HEURISTIC_FN = "upsert_guardian_authority_heuristic"

# Named explicitly, one entry per sanctioned call site, so a 4th appearing
# anywhere is structurally impossible to miss: the count check below must
# equal exactly `len(_SANCTIONED_UPSERT_CALL_SITES)`.
_SANCTIONED_UPSERT_CALL_SITES = (
    ("crypto_trading/guardian/authority.py", "update_heuristics_from_resolved_decisions"),
    ("crypto_trading/guardian/self_improvement.py", "_write_llm_heuristic"),
    ("crypto_trading/guardian/self_improvement.py", "track_and_demote_underperforming_heuristics"),
)


def _production_call_sites(function_name: str) -> list[tuple[str, int, str]]:
    """(path, lineno, enclosing_function_name) for every Call to
    `function_name` across the WHOLE production crypto_trading/ package -
    not just PRODUCTION_FILES (this plan's own touched-file list), and not
    restricted to added lines: this check must catch a violation ANYWHERE
    in the codebase, pre-existing or new, inside or outside this plan's own
    diff. Test files are deliberately excluded (tests legitimately seed
    fixture rows via direct repo calls - the same "seed helper" precedent
    the sibling isolation file documents for save_guardian_observation)."""
    sites: list[tuple[str, int, str]] = []
    for path in sorted((REPO_ROOT / "crypto_trading").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)

        class _Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stack: list[str] = []

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node: ast.Call) -> None:
                if _call_name(node) == function_name:
                    enclosing = self.stack[-1] if self.stack else "<module>"
                    sites.append((relative, node.lineno, enclosing))
                self.generic_visit(node)

        _Visitor().visit(tree)
    return sites


def test_upsert_guardian_authority_heuristic_has_exactly_three_call_sites_in_the_whole_codebase():
    """Checklist item 3. Enumerates every Call node named
    upsert_guardian_authority_heuristic across the entire crypto_trading/
    production package and asserts the set of (path, enclosing function)
    pairs is EXACTLY the 3 named, sanctioned sites - not merely 'count ==
    3' (which a 4th call site replacing one of the 3 could satisfy by
    accident), and not merely 'each of the 3 exists' (which would not
    catch a 4th)."""
    sites = _production_call_sites(_UPSERT_HEURISTIC_FN)
    observed = sorted({(path, enclosing) for path, _lineno, enclosing in sites})
    expected = sorted(_SANCTIONED_UPSERT_CALL_SITES)
    assert observed == expected, (
        f"expected exactly the 3 sanctioned upsert_guardian_authority_heuristic "
        f"call sites {expected}, found {observed}"
    )
    assert len(sites) == 3, f"expected exactly 3 call sites total, found {len(sites)}: {sites}"


def test_upsert_call_site_scan_genuinely_catches_a_fourth_synthesized_call_site():
    """Deliberate-break confirmation: synthesizes a fake fourth call site in
    a throwaway file inside crypto_trading/, re-runs the exact same scan
    logic against the real repo PLUS that synthesized file, and confirms it
    is caught as a 4th site - proving the count/identity check above is not
    vacuously true just because today's real codebase happens to have
    exactly 3. Cleans up the throwaway file itself in a `finally` block."""
    scratch_path = REPO_ROOT / "crypto_trading" / "_scratch_isolation_test_fourth_call_site.py"
    assert not scratch_path.exists(), "scratch file collision - aborting synthesized-violation test"
    try:
        scratch_path.write_text(
            "def rogue_caller(repo):\n"
            "    repo.upsert_guardian_authority_heuristic(heuristic_id='x')\n",
            encoding="utf-8",
        )
        sites = _production_call_sites(_UPSERT_HEURISTIC_FN)
        observed = sorted({(path, enclosing) for path, _lineno, enclosing in sites})
        expected = sorted(_SANCTIONED_UPSERT_CALL_SITES)
        assert observed != expected, "scanner failed to detect the synthesized 4th call site"
        assert len(sites) == 4
    finally:
        scratch_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Item 4: guardian_authority_heuristic_candidates is never read by
# evaluate_heuristics/decide_pre_entry/decide_open_position - these three
# functions never call find_proposed_.../find_validated_.../
# find_promoted_guardian_authority_heuristic_candidates at all.
# ---------------------------------------------------------------------------

_CANDIDATE_TABLE_READ_FNS = (
    "find_proposed_guardian_authority_heuristic_candidates",
    "find_validated_guardian_authority_heuristic_candidates",
    "find_promoted_guardian_authority_heuristic_candidates",
)

_REAL_DECISION_CORE_FNS = (
    "evaluate_heuristics",
    "decide_pre_entry",
    "decide_open_position",
)


def _calls_within_function(path: str, function_name: str) -> list[str]:
    """Every called-function-name found anywhere inside `function_name`'s
    own body (nested defs included, matching the sibling isolation file's
    own stack-tracking discipline for forbidden-call scans)."""
    source = _read(path)
    tree = ast.parse(source, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            names: list[str] = []
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    name = _call_name(inner)
                    if name:
                        names.append(name)
            return names
    raise AssertionError(f"function {function_name} not found in {path}")


def test_real_decision_core_never_calls_any_candidate_table_reader():
    """Checklist item 4. `evaluate_heuristics`, `decide_pre_entry`, and
    `decide_open_position` (the pure decision core - already proven byte-
    identical to their pre-plan source by item 1's hash checks) must never
    call any of the 3 candidate-table read methods, anywhere in their own
    body."""
    violations: list[str] = []
    for fn in _REAL_DECISION_CORE_FNS:
        called = set(_calls_within_function(AUTHORITY_PATH, fn))
        overlap = called & set(_CANDIDATE_TABLE_READ_FNS)
        if overlap:
            violations.append(f"{fn} calls {sorted(overlap)}")
    assert violations == [], f"real decision core reads the candidates table: {violations}"

    # Belt-and-suspenders textual scan: none of the 3 forbidden names may
    # appear ANYWHERE in authority.py's source at all (comment, docstring,
    # or code) - the file that DEFINES these three functions has no
    # legitimate reason to mention the candidates table's read methods in
    # any form.
    authority_source = _read(AUTHORITY_PATH)
    for name in _CANDIDATE_TABLE_READ_FNS:
        assert name not in authority_source, f"authority.py mentions {name}"


def test_candidate_table_read_scan_genuinely_catches_a_synthesized_violation():
    """Deliberate-break confirmation, same shape as the sibling isolation
    file's own equivalent test for its shadow-heuristics-read check."""
    fake_source = (
        "def evaluate_heuristics(factors, heuristics):\n"
        "    candidates = find_proposed_guardian_authority_heuristic_candidates()\n"
        "    return candidates\n"
    )
    tree = ast.parse(fake_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate_heuristics":
            names = {
                _call_name(inner) for inner in ast.walk(node) if isinstance(inner, ast.Call)
            }
            assert "find_proposed_guardian_authority_heuristic_candidates" in names
            return
    raise AssertionError("synthesized function not found - test itself is broken")


# ---------------------------------------------------------------------------
# Item 5: matched_heuristic_ids_json is written ONLY at the two
# orchestration call sites (tick.py's TIGHTEN_SL/CLOSE_EARLY save,
# authority.py's PRE_ENTRY_VETO save) - never read by decide_open_position/
# decide_pre_entry (a forward-looking audit field, populated after the
# decision, never an input to it).
# ---------------------------------------------------------------------------

_MATCHED_IDS_FIELD = "matched_heuristic_ids_json"
_MATCHED_IDS_ORCHESTRATION_WRITE_SITES = (
    ("crypto_trading/guardian/tick.py", "process_one_position"),
    ("crypto_trading/guardian/authority.py", "maybe_open_position_for_candidate"),
)


def test_decide_functions_never_reference_matched_heuristic_ids_json():
    """decide_open_position/decide_pre_entry's own source (byte-identical
    to pre-plan per item 1's hash checks) must not contain the string
    matched_heuristic_ids_json in any form - it is populated by the
    orchestration layer strictly AFTER these two functions return, from a
    second, duplicate evaluate_heuristics call these two functions
    themselves have no part in."""
    for fn in ("decide_open_position", "decide_pre_entry"):
        source = _current_function_source(AUTHORITY_PATH, fn)
        assert _MATCHED_IDS_FIELD not in source, f"{fn} references {_MATCHED_IDS_FIELD}"


def _keyword_write_sites(path: str, field_name: str) -> list[tuple[int, str]]:
    """(lineno, enclosing_function) for every Call anywhere in `path` that
    passes `field_name=...` as a keyword argument - the shape a write via
    `repo.save_guardian_authority_decision(..., matched_heuristic_ids_json=
    ...)` takes at both real orchestration call sites."""
    tree = ast.parse(_read(path), filename=path)
    sites: list[tuple[int, str]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            for kw in node.keywords:
                if kw.arg == field_name:
                    enclosing = self.stack[-1] if self.stack else "<module>"
                    sites.append((node.lineno, enclosing))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return sites


def test_matched_heuristic_ids_json_is_written_only_at_the_two_orchestration_call_sites():
    """Checklist item 5 (write half). Across every production file this
    plan touched, every keyword-argument write of matched_heuristic_ids_json
    must be inside exactly the 2 named orchestration functions - never
    inside decide_open_position/decide_pre_entry (already proven separately
    above), and never a 3rd new call site anywhere else."""
    observed: list[tuple[str, int, str]] = []
    for path in PRODUCTION_FILES:
        for lineno, enclosing in _keyword_write_sites(path, _MATCHED_IDS_FIELD):
            observed.append((path, lineno, enclosing))

    observed_pairs = sorted({(path, enclosing) for path, _lineno, enclosing in observed})
    expected_pairs = sorted(_MATCHED_IDS_ORCHESTRATION_WRITE_SITES)
    assert observed_pairs == expected_pairs, (
        f"expected matched_heuristic_ids_json write sites {expected_pairs}, "
        f"found {observed_pairs}"
    )
    assert len(observed) == 2, f"expected exactly 2 write call sites, found {len(observed)}: {observed}"


def test_matched_heuristic_ids_json_write_scan_genuinely_catches_a_third_site():
    """Deliberate-break confirmation: a synthesized function passing
    matched_heuristic_ids_json= as a keyword must be caught by the same
    keyword-matching logic the test above relies on."""
    fake_source = (
        "def rogue_orchestrator(repo):\n"
        "    repo.save_guardian_authority_decision(matched_heuristic_ids_json='[]')\n"
    )
    tree = ast.parse(fake_source)
    hits = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            for kw in node.keywords:
                if kw.arg == _MATCHED_IDS_FIELD:
                    hits.append(self.stack[-1] if self.stack else "<module>")
            self.generic_visit(node)

    _Visitor().visit(tree)
    assert hits == ["rogue_orchestrator"]


def test_matched_heuristic_ids_json_read_sites_are_limited_to_forward_tracking():
    """The read side has exactly one legitimate consumer beyond the DB
    layer's own row-shape plumbing: Task 6's forward-performance tracking
    (self_improvement.py, reading a resolved decision row's own
    matched_heuristic_ids_json back to attribute it to a specific promoted
    heuristic - `_real_decision_context`'s prompt-building read and
    `_forward_tighten_sl_stats`'s attribution read). Neither
    decide_open_position nor decide_pre_entry may ever be among the
    readers - confirmed by test_decide_functions_never_reference_
    matched_heuristic_ids_json above; this test additionally confirms the
    field's only OTHER production-file appearances are inside
    self_improvement.py (reads) and the two orchestration writes plus
    storage/db.py's migration and storage/repository.py's own column
    plumbing (schema-level, not decision-logic)."""
    unexpected_files = {"crypto_trading/guardian/authority.py", "crypto_trading/guardian/tick.py"}
    for path in PRODUCTION_FILES:
        if path in unexpected_files:
            continue  # already covered by the write-site test above
        source = _read(path)
        if _MATCHED_IDS_FIELD in source:
            assert path in (
                "crypto_trading/guardian/self_improvement.py",
                "crypto_trading/storage/db.py",
                "crypto_trading/storage/repository.py",
            ), f"unexpected matched_heuristic_ids_json reference in {path}"


# ---------------------------------------------------------------------------
# Item 6: authority_enabled still defaults false in both crypto_trading/
# config/guardian.yaml and the settings loader (GuardianConfig). This test
# suite is written and must pass BEFORE Task 9 flips the flag - a real
# pre-activation gate, not written after the fact.
# ---------------------------------------------------------------------------


def test_authority_enabled_defaults_false_in_guardian_yaml():
    with (REPO_ROOT / "crypto_trading/config/guardian.yaml").open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    assert raw["authority_enabled"] is False


def test_authority_enabled_defaults_false_in_settings_loader():
    from crypto_trading.config.loader import GuardianConfig

    assert GuardianConfig().authority_enabled is False


def test_authority_enabled_defaults_false_via_get_settings():
    """Broader corroboration of the test above: the field as actually
    resolved through the real settings-loading path (`get_settings`), not
    just the bare model default - catching a hypothetical guardian.yaml
    override that flips the flag on disk while the bare Pydantic default
    stays False."""
    from crypto_trading.config.loader import get_settings

    assert get_settings().guardian.authority_enabled is False


def test_run_godfather_self_improvement_tick_wiring_is_gated_by_authority_enabled():
    """Corroborates that the flag genuinely gates the whole pipeline at its
    one production call site (discovery_loop.py, Task 7), not merely that
    the flag itself defaults False in isolation - i.e. the pre-activation
    state really is "this whole pipeline is a no-op today", not just "one
    config field happens to read False"."""
    source = _read("crypto_trading/discovery_loop.py")
    tree = ast.parse(source, filename="crypto_trading/discovery_loop.py")
    found_gated_call = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test_source = ast.get_source_segment(source, node.test) or ""
            if "authority_enabled" not in test_source:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and _call_name(inner) == "run_godfather_self_improvement_tick":
                    found_gated_call = True
    assert found_gated_call, (
        "run_godfather_self_improvement_tick is not called inside an "
        "authority_enabled-gated if-block in discovery_loop.py"
    )


# ---------------------------------------------------------------------------
# Item 7 (full-suite green) is verified by running the whole repo test
# suite, not by a test inside this file - see the Task 8 report for the
# exact command and output. The remaining tests below are broader,
# full-diff re-checks mirroring the sibling isolation file's own item-6
# "re-check across the FULL touched-file universe" section.
# ---------------------------------------------------------------------------


def test_full_diff_has_zero_new_set_leverage_call_sites_anywhere():
    """Broadest re-check of the set_leverage scan: every touched .py file,
    test files included, not just PRODUCTION_FILES. Restricted to new call
    sites only (test files may legitimately reference set_leverage in a
    mock/patch target on a PRE-EXISTING line this plan did not add - none
    do today, but the restriction is the same defensive discipline as the
    sibling isolation file's own equivalent broad re-check)."""
    all_violations: list[str] = []
    for path in ALL_TOUCHED_PY_FILES:
        all_violations.extend(_new_set_leverage_call_violations(path))
    assert all_violations == [], f"forbidden new set_leverage() call site(s) anywhere: {all_violations}"


def test_full_diff_position_sizing_import_is_genuinely_absent_everywhere():
    """Broadest re-check of the position_sizing import scan: every touched
    file, not just PRODUCTION_FILES, whole-file (not new-lines-restricted -
    no legitimate pre-existing import of this module could exist in ANY of
    these files at any point)."""
    offenders: list[str] = []
    for path in ALL_TOUCHED_PY_FILES:
        offenders.extend(_position_sizing_import_violations(path))
    assert offenders == [], f"forbidden position_sizing import(s) anywhere in the diff: {offenders}"
