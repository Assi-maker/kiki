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
import json
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
    """Checklist item 2 (exclusion half, STRUCTURAL layer): the plan's
    Global Constraint list (`position_opening.py`, `position_sizing.py`,
    `gate/`, `screening/`, `risk_limits.yaml`, `live_execution.yaml`) must
    never appear in the hardcoded scan universe - confirmed once, by hand,
    via `git diff --stat a09b0ab..HEAD` at authoring time (none of these
    paths appears in that diff at all), and pinned here so a future edit to
    PRODUCTION_FILES/ALL_TOUCHED_PY_FILES that accidentally adds one of
    them is caught immediately, without needing to re-run git to notice.

    IMPORTANT SCOPE NOTE: this test only proves something about THIS
    FILE'S OWN CONSTANTS - it can only fail if a human hand-edits
    PRODUCTION_FILES/ALL_TOUCHED_PY_FILES to (re-)add one of these paths;
    it says nothing about whether `risk_limits.yaml`/`live_execution.yaml`
    have actually been left untouched in the live checked-out repo. That
    durable, content-level proof is what the two hash tests below
    (`test_risk_limits_yaml_is_byte_identical_to_recorded_hash`,
    `test_live_execution_yaml_hard_limit_fields_are_byte_identical_to_
    recorded_hash`) provide, using the exact same zero-git-history-
    dependency discipline as item 1's frozen-function hashes. This test
    remains useful only as a narrower, faster sanity check on the scan-
    universe constants themselves (existence on disk, no forbidden
    directory prefix)."""
    for forbidden in FORBIDDEN_TOUCHED_PATHS:
        assert forbidden not in PRODUCTION_FILES
        assert forbidden not in ALL_TOUCHED_PY_FILES
        assert (REPO_ROOT / forbidden).is_file(), f"{forbidden} unexpectedly missing from disk"
    for path in ALL_TOUCHED_PY_FILES:
        for prefix in FORBIDDEN_TOUCHED_DIR_PREFIXES:
            assert not path.startswith(prefix), f"{path} is under forbidden prefix {prefix}"


# ---------------------------------------------------------------------------
# Item 2 (exclusion half, CONTENT layer - code review fix, Important #1):
# risk_limits.yaml and live_execution.yaml's own hard-limit field VALUES
# must be byte-identical/value-identical to their state at authoring time -
# not merely "not in our own file list" (the test above), which is a
# tautology about a constant this same file defines and proves nothing
# about the live repository. Same mechanism, same zero-git-history-
# dependency discipline as item 1's 8 frozen-function hashes.
# ---------------------------------------------------------------------------

_RISK_LIMITS_YAML_PATH = "crypto_trading/config/risk_limits.yaml"
_LIVE_EXECUTION_YAML_PATH = "crypto_trading/config/live_execution.yaml"
_LIVE_EXECUTION_HARD_LIMIT_FIELDS = ("leverage", "margin_per_trade_usdt", "max_concurrent_positions")

# Recorded once (2026-09-17, authoring/fix time), from the live checked-out
# files - never from git history. The LIVE hard-limit hash was re-recorded
# 2026-09-26 for an explicit user decision (margin_per_trade_usdt 10 -> 100;
# leverage 10 and max_concurrent_positions 4 unchanged).
_EXPECTED_RISK_LIMITS_YAML_SHA256 = (
    "88806ef2f5d74b2f5a494eb3a70a0d98d4b1ce8b66c91387a427b65652a2112b"
)
_EXPECTED_LIVE_EXECUTION_HARD_LIMITS_SHA256 = (
    "9c0f3227ed40a402aaa535be230e05d4186a23d9d2846fb40232d91a2584b0a3"
)


def _live_execution_hard_limits_canonical_json() -> str:
    """Deterministic (sorted-keys) JSON of ONLY live_execution.yaml's 3
    named hard-limit fields - not the whole file, since the Global
    Constraint specifically names "live_execution.yaml's hard limits
    (leverage, margin, max_concurrent_positions)", not the file's every
    byte (which also carries comments/other, non-frozen fields this plan
    has no constraint against). Hashing the canonical JSON of just the 3
    values (not the raw YAML text) means a value's own type/formatting is
    what is protected, immune to an unrelated reflow of surrounding
    comments elsewhere in the same file."""
    with (REPO_ROOT / _LIVE_EXECUTION_YAML_PATH).open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    fields = {name: raw[name] for name in _LIVE_EXECUTION_HARD_LIMIT_FIELDS}
    return json.dumps(fields, sort_keys=True)


def test_risk_limits_yaml_is_byte_identical_to_recorded_hash():
    """Global Constraint (verbatim): never modify
    crypto_trading/config/risk_limits.yaml. Whole-file SHA-256 (the entire
    file is in scope for this constraint, unlike live_execution.yaml which
    only names 3 specific fields) against a hash recorded at authoring
    time from the live file - a real edit to this file, accidental or
    malicious, fails this test; the exclusion-from-our-own-constant test
    above cannot detect that at all."""
    content = (REPO_ROOT / _RISK_LIMITS_YAML_PATH).read_text(encoding="utf-8")
    assert _sha256(content) == _EXPECTED_RISK_LIMITS_YAML_SHA256, (
        "crypto_trading/config/risk_limits.yaml changed since its hash was "
        "recorded - Global Constraint violation"
    )


def test_live_execution_yaml_hard_limit_fields_are_byte_identical_to_recorded_hash():
    """Global Constraint (verbatim): never modify live_execution.yaml's
    hard limits (leverage, margin, max_concurrent_positions). SHA-256 of
    the 3 fields' own canonical values (not the whole file - see
    `_live_execution_hard_limits_canonical_json`'s own docstring) against a
    hash recorded at authoring time from the live file."""
    canonical = _live_execution_hard_limits_canonical_json()
    assert _sha256(canonical) == _EXPECTED_LIVE_EXECUTION_HARD_LIMITS_SHA256, (
        "live_execution.yaml's hard-limit fields changed since their hash "
        "was recorded - Global Constraint violation"
    )


def test_risk_limits_and_live_execution_hash_checks_genuinely_detect_a_real_change():
    """Deliberate-break confirmation for both hash checks above, same
    shape as item 1's own `test_byte_identical_check_genuinely_detects_a_
    real_change_in_a_function`: mutates each real, live value and confirms
    the hash changes AND no longer matches the recorded expected hash."""
    real_risk_limits = (REPO_ROOT / _RISK_LIMITS_YAML_PATH).read_text(encoding="utf-8")
    mutated_risk_limits = real_risk_limits + "\n# tampered\n"
    assert _sha256(mutated_risk_limits) != _sha256(real_risk_limits)
    assert _sha256(mutated_risk_limits) != _EXPECTED_RISK_LIMITS_YAML_SHA256

    real_canonical = _live_execution_hard_limits_canonical_json()
    mutated_fields = json.loads(real_canonical)
    mutated_fields["leverage"] = mutated_fields["leverage"] + 1
    mutated_canonical = json.dumps(mutated_fields, sort_keys=True)
    assert _sha256(mutated_canonical) != _sha256(real_canonical)
    assert _sha256(mutated_canonical) != _EXPECTED_LIVE_EXECUTION_HARD_LIMITS_SHA256


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
SELF_IMPROVEMENT_PATH = "crypto_trading/guardian/self_improvement.py"
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


def _source_with_decorators(source: str, node) -> str:
    """The node's own source segment WITH its decorator lines prepended.

    `ast.get_source_segment` on a FunctionDef starts at the `def` line and
    excludes every decorator above it (a decorator is a separate child node,
    `node.decorator_list`), so a hash of that segment alone would not change
    if someone added, removed or edited a decorator on a frozen function -
    and a decorator can change what the function DOES without touching a
    single byte inside it. All 8 frozen functions/methods have zero
    decorators today, so including them changes none of the recorded hashes;
    this is purely about what a future change would be caught by. (Code
    review fix, 2026-09-17 fix wave, Task 8 item 1 hardening.)"""
    decorators = [
        ast.get_source_segment(source, decorator) or "" for decorator in node.decorator_list
    ]
    segment = ast.get_source_segment(source, node)
    assert segment is not None
    return "".join(f"@{decorator}\n" for decorator in decorators) + segment


def _current_function_source(path: str, function_name: str) -> str:
    """`function_name`'s CURRENT top-level (or nested, but here always
    top-level) source segment, read straight from the live file on disk
    (never `git show` - see module docstring, lesson #2), decorators
    included (see `_source_with_decorators`)."""
    source = _read(path)
    tree = ast.parse(source, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return _source_with_decorators(source, node)
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
                    return _source_with_decorators(source, child)
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


def test_the_frozen_source_hash_covers_decorators_too():
    """Deliberate-break confirmation for `_source_with_decorators` (Task 8
    item 1 hardening, 2026-09-17 fix wave): a decorator added to a frozen
    function must change its recorded hash. Proven on a synthesized pair
    rather than by touching a real frozen function - the extraction logic is
    the thing under test, and it is the SAME helper both hash tests above
    use.

    Also pins the reason the 8 recorded hashes did not have to change when
    this hardening landed: none of the frozen functions has a decorator, so
    the prepended prefix is empty for every one of them."""
    undecorated = "def f(x):\n    return x\n"
    decorated = "@some_decorator\ndef f(x):\n    return x\n"

    def _extract(source: str) -> str:
        tree = ast.parse(source)
        return _source_with_decorators(source, tree.body[0])

    assert _extract(undecorated) == "def f(x):\n    return x"
    assert _extract(decorated).startswith("@some_decorator\n")
    assert _sha256(_extract(decorated)) != _sha256(_extract(undecorated))

    # ...and the real frozen functions/methods genuinely carry no decorator,
    # which is why every recorded hash above is unaffected by this change.
    authority_source = _read(AUTHORITY_PATH)
    authority_tree = ast.parse(authority_source, filename=AUTHORITY_PATH)
    for node in ast.walk(authority_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in (
            _PURE_DECISION_FUNCTIONS
        ):
            assert node.decorator_list == [], f"{node.name} unexpectedly has a decorator"


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


def test_authority_py_frozen_functions_have_zero_deletions_in_the_whole_diff():
    """Independent, coarser-grained corroboration of the 6 authority.py hash
    checks above, NARROWED (2026, TAKE_PROFIT addition) to the 6 frozen
    functions' own line ranges rather than the whole file.

    This test originally asserted `git diff --numstat` for the WHOLE FILE
    showed zero deletions since a09b0ab - true for Guardian Authority Live
    Autonomy's own Tasks 1-7 (which genuinely never touched anything else in
    authority.py either), but only ever a coarser PROXY for the actual
    Global Constraint, which is specifically about the 6 named frozen
    functions, not about every line in the file forever. A later, separate,
    independently-reviewed change (adding the TAKE_PROFIT decision type)
    legitimately edits a non-frozen function in this same file
    (`resolve_pending_decisions`, to treat TAKE_PROFIT the same way it
    already treats CLOSE_EARLY) without touching any of the 6 frozen
    functions at all - exactly the kind of routine, narrow, non-frozen
    change this file was never meant to forbid. The corroboration is
    therefore tightened to directly check what it always meant to protect,
    mirroring `test_frozen_repository_methods_line_ranges_were_never_
    touched_by_this_diff` below's own already-established, more precise
    line-range-scoped pattern: no line THIS WHOLE HISTORICAL DIFF ever
    deleted falls inside any of the 6 frozen functions' own AST line range.
    The 6 SHA-256 hash checks above remain the primary, byte-exact proof;
    this is still a second, independent, git-history-based corroboration of
    the same guarantee, not a weakening of it."""
    out = subprocess.run(
        ["git", "diff", "-U0", f"{BASE_SHA}..HEAD", "--", AUTHORITY_PATH],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    deleted_line_numbers: set[int] = set()
    old_lineno = None
    for line in out.splitlines():
        if line.startswith("@@"):
            import re

            match = re.match(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", line)
            assert match is not None
            old_lineno = int(match.group(1))
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            assert old_lineno is not None
            deleted_line_numbers.add(old_lineno)
            old_lineno += 1
        elif line.startswith("+"):
            continue
        elif old_lineno is not None:
            old_lineno += 1

    # The 6 frozen functions' line ranges as they exist in the OLD (base)
    # revision - a deletion is only meaningful relative to the file version
    # it deleted FROM, so this reads authority.py AS OF BASE_SHA, never the
    # live checked-out file (which is what every other AST scan in this file
    # reads, deliberately - see module docstring, lesson #2. This is the one
    # narrow, explained exception: git-history-dependent by construction,
    # since a diff's old-side line numbers are only meaningful against the
    # old-side file, and this repository's history back to a09b0ab is not
    # expected to become unreachable).
    old_source = subprocess.run(
        ["git", "show", f"{BASE_SHA}:{AUTHORITY_PATH}"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    old_tree = ast.parse(old_source, filename=AUTHORITY_PATH)
    frozen_ranges: list[tuple[int, int]] = []
    for node in ast.walk(old_tree):
        is_frozen_fn = (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in _PURE_DECISION_FUNCTIONS
        )
        if is_frozen_fn:
            frozen_ranges.append((node.lineno, node.end_lineno or node.lineno))
    assert len(frozen_ranges) == len(_PURE_DECISION_FUNCTIONS), (
        "expected to find all 6 frozen functions in the base revision of authority.py"
    )

    violations = {
        ln for ln in deleted_line_numbers
        if any(start <= ln <= end for start, end in frozen_ranges)
    }
    assert violations == set(), (
        f"authority.py has deleted line(s) {violations} inside a frozen function's own range"
    )


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


# ---------------------------------------------------------------------------
# Item 2 (code review fix, Important #2): the AST Import/ImportFrom scan
# above cannot see a DYNAMIC import - `importlib.import_module(
# "crypto_trading.paper_trading.position_sizing")` or `__import__(...)`
# produces no Import/ImportFrom node at all, and would sail through
# undetected. The reference file's own equivalent coverage (test_
# authority_shadow_isolation.py:368-384) pairs its AST scan with a line-
# restricted TEXTUAL scan for this exact reason. A bare textual/substring
# scan is not safe to reuse verbatim here: this plan's own self_
# improvement.py module docstring legitimately narrates, in prose, "this
# module never imports position_sizing.py" - a raw substring scan across
# the whole file (or even restricted to added lines, since that docstring
# line IS an added line in a wholly new file) would flag its own
# documentation of the guarantee as a violation of it.
#
# Fix: stay AST-based, but target the SPECIFIC call shapes a dynamic-import
# escape hatch actually takes (import_module(...)/__import__(...) with a
# string-literal argument, or getattr(module, "compute_position_size")) -
# never a bare "does this string constant contain the substring" scan over
# every ast.Constant in the file, which would re-introduce the exact same
# docstring false positive a module docstring IS an ast.Constant string at
# the AST level, indistinguishable from any other string literal without
# this position-in-a-Call restriction.
# ---------------------------------------------------------------------------

_DYNAMIC_IMPORT_CALL_NAMES = ("import_module", "__import__")
_FORBIDDEN_COMPUTE_POSITION_SIZE_ATTR = "compute_position_size"


def _dynamic_import_escape_hatch_violations(path: str) -> list[str]:
    """Every `import_module(...)`/`__import__(...)` call whose argument is
    a string-literal naming the forbidden position_sizing module (exact
    match or a submodule of it), and every `getattr(obj, "compute_position_
    size")` call - the two concrete shapes that let code reach the
    forbidden module/function WITHOUT ever producing an
    Import/ImportFrom AST node. Restricted to actual Call-argument string
    literals, never a bare scan of every string constant in the file, so a
    docstring/comment mentioning either name in prose is never matched."""
    offenders: list[str] = []
    tree = ast.parse(_read(path), filename=path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        string_args = [
            arg.value
            for arg in (*node.args, *(kw.value for kw in node.keywords))
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        if name in _DYNAMIC_IMPORT_CALL_NAMES:
            for value in string_args:
                if value == _FORBIDDEN_POSITION_SIZING_MODULE or value.startswith(
                    _FORBIDDEN_POSITION_SIZING_MODULE + "."
                ):
                    offenders.append(f"{path}:{node.lineno} dynamic-imports {value!r} via {name}()")
        elif name == "getattr":
            for value in string_args:
                if value == _FORBIDDEN_COMPUTE_POSITION_SIZE_ATTR:
                    offenders.append(
                        f"{path}:{node.lineno} getattr(..., {value!r}) escape hatch"
                    )
    return offenders


def test_no_production_file_dynamically_imports_position_sizing():
    """Global Constraint (verbatim): never import position_sizing.py -
    covering the dynamic-import escape hatch the static AST import scan
    above cannot see (see module section above)."""
    offenders: list[str] = []
    for path in PRODUCTION_FILES:
        offenders.extend(_dynamic_import_escape_hatch_violations(path))
    assert offenders == [], f"forbidden dynamic position_sizing reference(s): {offenders}"


def test_dynamic_import_escape_hatch_scan_genuinely_catches_both_shapes():
    """Deliberate-break confirmation for both shapes the scan above
    catches - `importlib.import_module(...)` and `getattr(..., "compute_
    position_size")` - and confirms it does NOT flag an unrelated dynamic
    import of a different, unforbidden module (proving this is a targeted
    match, not an over-broad 'any dynamic import at all' scan that would
    also flag legitimate dynamic imports elsewhere in this codebase)."""
    fake_source = (
        "import importlib\n"
        "\n"
        "def f(other_module):\n"
        "    mod = importlib.import_module('crypto_trading.paper_trading.position_sizing')\n"
        "    fn = getattr(mod, 'compute_position_size')\n"
        "    unrelated = importlib.import_module('crypto_trading.guardian.authority')\n"
        "    return fn, unrelated\n"
    )
    tree = ast.parse(fake_source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        string_args = [
            arg.value
            for arg in (*node.args, *(kw.value for kw in node.keywords))
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        if name in _DYNAMIC_IMPORT_CALL_NAMES:
            for value in string_args:
                if value == _FORBIDDEN_POSITION_SIZING_MODULE or value.startswith(
                    _FORBIDDEN_POSITION_SIZING_MODULE + "."
                ):
                    offenders.append(f"import_module:{value}")
        elif name == "getattr":
            for value in string_args:
                if value == _FORBIDDEN_COMPUTE_POSITION_SIZE_ATTR:
                    offenders.append(f"getattr:{value}")
    assert offenders == [
        f"import_module:{_FORBIDDEN_POSITION_SIZING_MODULE}",
        f"getattr:{_FORBIDDEN_COMPUTE_POSITION_SIZE_ATTR}",
    ]


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
    every PRODUCTION file it touched, must not contain the literal
    substring "set_leverage(" - the opening parenthesis is what
    distinguishes an actual call/reference-as-callable from a bare
    docstring mention of the identifier (e.g. self_improvement.py's own
    module docstring: "never references `set_leverage`" - no parenthesis
    follows, so this pattern does not match it). Confirmed by hand at
    authoring time that "set_leverage(" does not occur anywhere in any
    production file this plan touches, added or not.

    Deliberately scoped to PRODUCTION_FILES, not the broader
    ALL_TOUCHED_PY_FILES: this isolation test's OWN source necessarily
    contains the literal substring "set_leverage(" many times over (as
    synthetic test fixture strings and in this very docstring/assertion
    text), and a raw substring scan - unlike the AST-based scans elsewhere
    in this file, which correctly see a string literal as an
    `ast.Constant`, never a `Call` node - cannot tell "the text
    'set_leverage(' appears in this line" apart from "this line is a Call
    to set_leverage". PRODUCTION_FILES has no such self-reference problem
    and is the actual scope item 2's Global Constraint cares about."""
    offenders: list[str] = []
    for path in PRODUCTION_FILES:
        added = _added_line_numbers(path)
        if not added:
            continue
        for lineno, line in enumerate(_read(path).splitlines(), start=1):
            if lineno in added and "set_leverage(" in line:
                offenders.append(f"{path}:{lineno}")
    assert offenders == [], f"textual set_leverage( reference(s) found: {offenders}"


# ---------------------------------------------------------------------------
# Item 3: upsert_guardian_authority_heuristic has EXACTLY 4 call sites in
# the whole codebase - the original (pre-this-plan) Task 9 self-critique
# call site (authority.py::update_heuristics_from_resolved_decisions), this
# plan's Task 5 promotion call site (self_improvement.py::
# _write_llm_heuristic, called from promote_validated_heuristic_candidates),
# this plan's Task 6 demotion call site (self_improvement.py::
# track_and_demote_underperforming_heuristics), and the 4th, added by the
# 2026-09-17 final-review fix wave (review finding I4): self_improvement.py::
# _zero_orphan_llm_heuristic, called from _reconcile_orphan_llm_heuristics,
# which silences a live `ga-llm:*` row that no PROMOTED candidate references
# (a crash between promotion's own two writes). It is a ZEROING write only -
# adjustment=0.0, confidence=0.0 - in the same category as the demotion site
# above: it can silence a rule, never strengthen one. A 5th call site
# anywhere - in this plan's diff or any future one - would mean an
# unaccounted write path into the real guardian_authority_heuristics table
# exists, which the plan's own Global Constraint forbids outright.
# ---------------------------------------------------------------------------

_UPSERT_HEURISTIC_FN = "upsert_guardian_authority_heuristic"

# Named explicitly, one entry per sanctioned call site, so a 5th appearing
# anywhere is structurally impossible to miss: the count check below must
# equal exactly `len(_SANCTIONED_UPSERT_CALL_SITES)`.
_SANCTIONED_UPSERT_CALL_SITES = (
    ("crypto_trading/guardian/authority.py", "update_heuristics_from_resolved_decisions"),
    ("crypto_trading/guardian/self_improvement.py", "_write_llm_heuristic"),
    ("crypto_trading/guardian/self_improvement.py", "track_and_demote_underperforming_heuristics"),
    ("crypto_trading/guardian/self_improvement.py", "_zero_orphan_llm_heuristic"),
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


def test_upsert_guardian_authority_heuristic_has_exactly_four_call_sites_in_the_whole_codebase():
    """Checklist item 3. Enumerates every Call node named
    upsert_guardian_authority_heuristic across the entire crypto_trading/
    production package and asserts the set of (path, enclosing function)
    pairs is EXACTLY the 4 named, sanctioned sites - not merely 'count ==
    4' (which a 5th call site replacing one of the 4 could satisfy by
    accident), and not merely 'each of the 4 exists' (which would not
    catch a 5th)."""
    sites = _production_call_sites(_UPSERT_HEURISTIC_FN)
    observed = sorted({(path, enclosing) for path, _lineno, enclosing in sites})
    expected = sorted(_SANCTIONED_UPSERT_CALL_SITES)
    assert observed == expected, (
        f"expected exactly the {len(expected)} sanctioned "
        f"upsert_guardian_authority_heuristic call sites {expected}, found {observed}"
    )
    assert len(sites) == len(_SANCTIONED_UPSERT_CALL_SITES), (
        f"expected exactly {len(_SANCTIONED_UPSERT_CALL_SITES)} call sites total, "
        f"found {len(sites)}: {sites}"
    )


def test_upsert_call_site_scan_genuinely_catches_an_extra_synthesized_call_site():
    """Deliberate-break confirmation: synthesizes a fake extra call site in
    a throwaway file inside crypto_trading/, re-runs the exact same scan
    logic against the real repo PLUS that synthesized file, and confirms it
    is caught as one site too many - proving the count/identity check above
    is not vacuously true just because today's real codebase happens to have
    exactly the sanctioned set. Cleans up the throwaway file itself in a
    `finally` block."""
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
        assert observed != expected, "scanner failed to detect the synthesized extra call site"
        assert len(sites) == len(_SANCTIONED_UPSERT_CALL_SITES) + 1
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


def _package_keyword_write_sites(field_name: str) -> list[tuple[str, int, str]]:
    """(path, lineno, enclosing_function) for every `field_name=...` keyword
    write across the WHOLE production crypto_trading/ package - the same live
    filesystem walk `_production_call_sites` (item 3) already uses, for the
    same reason: this check must catch a writer ANYWHERE, not only in this
    plan's own hardcoded touched-file list.

    Code review fix (2026-09-17 fix wave, Task 8 item 1): the write-site
    check below used to scan only PRODUCTION_FILES while its sibling
    upsert_guardian_authority_heuristic check already scanned the whole
    package - an asymmetry that meant a third writer added in any file
    outside that hardcoded list would not have been caught at all. Test
    files are deliberately excluded, exactly as in `_production_call_sites`:
    tests legitimately pass this field when seeding fixture decision rows."""
    sites: list[tuple[str, int, str]] = []
    for path in sorted((REPO_ROOT / "crypto_trading").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(REPO_ROOT).as_posix()
        for lineno, enclosing in _keyword_write_sites(relative, field_name):
            sites.append((relative, lineno, enclosing))
    return sites


def test_matched_heuristic_ids_json_is_written_only_at_the_two_orchestration_call_sites():
    """Checklist item 5 (write half). Across the WHOLE production
    crypto_trading/ package, every keyword-argument write of
    matched_heuristic_ids_json must be inside exactly the 2 named
    orchestration FUNCTIONS - never inside decide_open_position/
    decide_pre_entry (already proven separately above), and never inside any
    function other than these 2.

    3, not 2, raw CALL SITES (2026, TAKE_PROFIT addition): `process_one_
    position` now saves a decision (and this field) for THREE decision
    types instead of two - TIGHTEN_SL/CLOSE_EARLY's pre-existing save plus
    TAKE_PROFIT's new one, added alongside them following the exact same
    orchestration-layer "recover matched_ids via a second, duplicate,
    side-effect-free evaluate_heuristics call" pattern - both still inside
    the SAME single sanctioned function, so the set of DISTINCT (path,
    enclosing_function) pairs this test actually cares about is unchanged."""
    observed = _package_keyword_write_sites(_MATCHED_IDS_FIELD)

    observed_pairs = sorted({(path, enclosing) for path, _lineno, enclosing in observed})
    expected_pairs = sorted(_MATCHED_IDS_ORCHESTRATION_WRITE_SITES)
    assert observed_pairs == expected_pairs, (
        f"expected matched_heuristic_ids_json write sites {expected_pairs}, "
        f"found {observed_pairs}"
    )
    assert len(observed) == 3, (
        f"expected exactly 3 write call sites, found {len(observed)}: {observed}"
    )


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


def test_matched_ids_write_scan_catches_a_third_site_outside_the_hardcoded_file_list():
    """Deliberate-break confirmation for the WIDENING itself (2026-09-17 fix
    wave), not just for the keyword-matching logic: the synthesized third
    writer is placed in a REAL throwaway file inside crypto_trading/ that is
    deliberately NOT in PRODUCTION_FILES. The old, narrower scan would have
    sailed straight past it; the whole-package walk catches it. Same
    scratch-file discipline (and `finally` cleanup) as item 3's own
    equivalent test."""
    scratch_path = (
        REPO_ROOT / "crypto_trading" / "_scratch_isolation_test_third_matched_ids_write.py"
    )
    relative = scratch_path.relative_to(REPO_ROOT).as_posix()
    assert relative not in PRODUCTION_FILES, "scratch file must be outside the hardcoded list"
    assert not scratch_path.exists(), "scratch file collision - aborting synthesized-violation test"
    try:
        scratch_path.write_text(
            "def rogue_orchestrator(repo):\n"
            "    repo.save_guardian_authority_decision(matched_heuristic_ids_json='[]')\n",
            encoding="utf-8",
        )
        observed = _package_keyword_write_sites(_MATCHED_IDS_FIELD)
        observed_pairs = sorted({(path, enclosing) for path, _lineno, enclosing in observed})
        assert (relative, "rogue_orchestrator") in observed_pairs
        assert observed_pairs != sorted(_MATCHED_IDS_ORCHESTRATION_WRITE_SITES)
    finally:
        scratch_path.unlink(missing_ok=True)


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

    # POSITIVE assertion (code review fix, 2026-09-17 fix wave, Task 8 item
    # 2): every check above is an ABSENCE check, so all of them would pass
    # vacuously if the field were renamed repo-wide and every reference
    # silently vanished - including the forward-attribution read this whole
    # tracking mechanism depends on. Confirm the field really IS read where
    # it is supposed to be, in the extracted source of the two real reading
    # functions: `_forward_tighten_sl_stats` (Task 6's own attribution read,
    # the load-bearing one) and `_real_decision_context` (Task 3's
    # prompt-building read).
    for reader in ("_forward_tighten_sl_stats", "_real_decision_context"):
        reader_source = _current_function_source(SELF_IMPROVEMENT_PATH, reader)
        assert _MATCHED_IDS_FIELD in reader_source, (
            f"{reader} no longer reads {_MATCHED_IDS_FIELD} - forward "
            "attribution would be silently broken, and every absence check "
            "in this test would still pass"
        )


# ---------------------------------------------------------------------------
# Item 6 (pre-activation gate, 2026-09-15 to 2026-09-17): this suite was
# written and passed BEFORE Task 9 flipped authority_enabled, proving the
# whole pipeline really was inert until that specific, deliberate commit.
# ACTIVATED 2026-09-17 - see guardian.yaml's own activation comment for the
# full grep/AST-provable safety guarantees restated at that commit. The
# bare Pydantic default (GuardianConfig.authority_enabled) still correctly
# defaults False - only this deployment's own guardian.yaml now overrides
# it - so test_authority_enabled_defaults_false_in_settings_loader below is
# unchanged and still green; the other two are inverted to their
# now-permanent post-activation state.
# ---------------------------------------------------------------------------


def test_authority_enabled_is_activated_in_guardian_yaml():
    """Inverted 2026-09-17 (was test_authority_enabled_defaults_false_in_
    guardian_yaml, the pre-activation gate - see the section comment above).
    Guards against a future accidental revert: the running guardian.yaml
    must keep saying `true`, not silently drift back to inert."""
    with (REPO_ROOT / "crypto_trading/config/guardian.yaml").open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    assert raw["authority_enabled"] is True


def test_authority_enabled_defaults_false_in_settings_loader():
    from crypto_trading.config.loader import GuardianConfig

    assert GuardianConfig().authority_enabled is False


def test_authority_enabled_is_activated_via_get_settings():
    """Inverted 2026-09-17 (was test_authority_enabled_defaults_false_via_
    get_settings). Broader corroboration of the test above: the field as
    actually resolved through the real settings-loading path
    (`get_settings`), not just the raw YAML - catching a hypothetical
    loader-layer regression that silently ignores or overrides the YAML's
    now-activated value while the file on disk still correctly says
    `true`."""
    from crypto_trading.config.loader import get_settings

    assert get_settings().guardian.authority_enabled is True


_SELF_IMPROVEMENT_TICK_FN = "run_godfather_self_improvement_tick"


def _authority_enabled_gated_call_linenos(path: str, function_name: str) -> set[int]:
    """Line numbers of every Call to `function_name`, anywhere in `path`,
    that sits inside an `ast.If` block whose OWN test expression mentions
    `authority_enabled` - the same test-source-substring check the
    original (weaker) version of this test used, now used only to build a
    per-file allow-set rather than to answer "does at least one exist"."""
    source = _read(path)
    tree = ast.parse(source, filename=path)
    gated: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test_source = ast.get_source_segment(source, node.test) or ""
            if "authority_enabled" not in test_source:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and _call_name(inner) == function_name:
                    gated.add(inner.lineno)
    return gated


def test_run_godfather_self_improvement_tick_wiring_is_gated_by_authority_enabled():
    """Checklist item 6 corroboration (code review fix, Important #3):
    corroborates that the flag genuinely gates the WHOLE pipeline - i.e.
    the pre-activation state really is "this whole pipeline is a no-op
    today", not just "one config field happens to read False" and not just
    "at least one call site happens to be gated, and there might be others
    that are not".

    The original version of this test set `found_gated_call = True` on the
    FIRST `ast.If` matching (test mentions authority_enabled) AND (body
    contains the call) anywhere in discovery_loop.py, which proves
    EXISTENCE of a gated call site but nothing about EXCLUSIVITY: a second,
    UNGATED call site added later - in discovery_loop.py or any other
    production file - would leave that version green. Fixed by reusing
    item 3's own `_production_call_sites` (already proven, via its own
    deliberate-break test, to enumerate every Call to a named function
    across the WHOLE crypto_trading/ package) to find EVERY real call site
    of `run_godfather_self_improvement_tick` anywhere in the codebase, then
    asserting every single one's line number is in its own file's
    `_authority_enabled_gated_call_linenos` set. Zero call sites anywhere
    is accepted (vacuously fine - nothing to gate); one ungated call site
    among N is a failure."""
    all_sites = _production_call_sites(_SELF_IMPROVEMENT_TICK_FN)
    assert all_sites, "expected at least one real call site of run_godfather_self_improvement_tick"

    ungated: list[str] = []
    gated_cache: dict[str, set[int]] = {}
    for path, lineno, enclosing in all_sites:
        if path not in gated_cache:
            gated_cache[path] = _authority_enabled_gated_call_linenos(path, _SELF_IMPROVEMENT_TICK_FN)
        if lineno not in gated_cache[path]:
            ungated.append(f"{path}:{lineno} in {enclosing}")
    assert ungated == [], f"ungated call site(s) of {_SELF_IMPROVEMENT_TICK_FN}: {ungated}"


def test_authority_enabled_gating_check_genuinely_catches_an_ungated_call_site():
    """Deliberate-break confirmation, same real-scratch-file discipline as
    item 3's own `test_upsert_call_site_scan_genuinely_catches_a_fourth_
    synthesized_call_site`: writes a REAL throwaway `.py` file under
    `crypto_trading/` containing an UNGATED call to `run_godfather_self_
    improvement_tick`, re-runs the exact same call-site-plus-gating scan
    against the real repo plus that file, and confirms the ungated site is
    caught - proving the exclusivity check above is not vacuously true
    just because today's real codebase happens to gate its one real call
    site correctly. Cleans up the scratch file in a `finally` block."""
    scratch_path = (
        REPO_ROOT / "crypto_trading" / "_scratch_isolation_test_ungated_self_improvement_call.py"
    )
    assert not scratch_path.exists(), "scratch file collision - aborting synthesized-violation test"
    try:
        scratch_path.write_text(
            "def rogue_caller(repo, runner, settings, run_id, now):\n"
            "    run_godfather_self_improvement_tick(repo, runner, settings, run_id, now)\n",
            encoding="utf-8",
        )
        all_sites = _production_call_sites(_SELF_IMPROVEMENT_TICK_FN)
        ungated = []
        for path, lineno, enclosing in all_sites:
            gated = _authority_enabled_gated_call_linenos(path, _SELF_IMPROVEMENT_TICK_FN)
            if lineno not in gated:
                ungated.append(f"{path}:{lineno} in {enclosing}")
        assert ungated, "scanner failed to detect the synthesized ungated call site"
    finally:
        scratch_path.unlink(missing_ok=True)


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
    these files at any point). Covers both the static AST Import/ImportFrom
    scan and the dynamic-import escape-hatch scan (code review fix,
    Important #2)."""
    offenders: list[str] = []
    for path in ALL_TOUCHED_PY_FILES:
        offenders.extend(_position_sizing_import_violations(path))
        offenders.extend(_dynamic_import_escape_hatch_violations(path))
    assert offenders == [], f"forbidden position_sizing import(s) anywhere in the diff: {offenders}"
