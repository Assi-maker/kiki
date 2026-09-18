"""Guardian Authority Shadow/Observation Mode (2026-09-15) - Task 10:
whole-plan production-isolation tests (req-9-style checklist).

This is the plan's own final code task: not a new feature, but an
independent re-verification, across the WHOLE plan's diff (base `eb19335`
- the master commit this branch forked from - through current HEAD), of
every safety/isolation guarantee the plan's Global Constraints promise
(docs/superpowers/plans/2026-09-15-guardian-authority-shadow.md, "Global
Constraints" section) and each individual task's own review already
checked one task at a time.

Same discipline as the ORIGINAL Guardian Authority plan's own Task 10
(tests/crypto_trading/guardian/test_authority.py's "Task 10: Production
isolation / Global Constraints (req-9 checklist)" section, and
test_authority_live.py's "18. Production isolation / Global Constraints"
section): AST-based scanning where practical (import statements, function
calls), plain source-text scanning otherwise (config field names, string
literals) - not reinvented, reused.

The one addition this file makes over that precedent's *_source() helpers:
several files this plan TOUCHED (as opposed to created) already contained,
BEFORE this plan, a legitimate, already-reviewed reference to one of the
very names this file must treat as forbidden in NEW code - e.g.
`crypto_trading/paper_trading/recovery_sweep.py` already called the real
`open_position_for_candidate()` directly (pre-existing, its own sanctioned
call site, untouched by this plan) before this plan ever added a line to
that file. A naive whole-file scan would flag that pre-existing, already-
reviewed call as a false positive. So instead of scanning whole files,
the checks below scan only the NEW-FILE LINE NUMBERS this plan's own diff
(against base `eb19335`) actually added - via `git diff -U0`, which with
zero context lines reports exactly (and only) the added/removed lines per
hunk - cross-referenced against each added line's AST node. A pre-existing
call, however forbidden-looking, sitting on a line this plan never
touched, is correctly excluded; a genuinely NEW call, even one added deep
inside an old function, is correctly caught. (Newly created files, where
every line is "added" by construction, reduce to an ordinary whole-file
scan under this same mechanism - no special-casing needed.)

**Final whole-branch review Important #4 fix (2026-09-15, HEAD 7561929):**
the ORIGINAL version of this file derived its scan universe (the file lists
below) via a live `git diff --name-only eb19335..HEAD` at collection time,
and checked the 6 pure-decision functions' byte-identity via `git show
eb19335:<path>`. Both break the moment this branch merges: the diff range
`eb19335..HEAD` keeps growing to include every subsequent unrelated commit
on the target branch, which (a) breaks the pinned production-file-list
equality assert as soon as anything else touches any of the same paths,
and (b) starts "policing" brand new, unrelated future code as if it were
part of this plan. Fixed by severing both mechanisms from git history
entirely: `PRODUCTION_FILES`/`ALL_TOUCHED_PY_FILES` below are now the
literal, hardcoded scan universe itself (recorded once, by hand, from that
same `git diff --name-only eb19335..HEAD` at authoring time - not merely a
sanity-check constant compared against a live git computation anymore),
and the 6 functions' byte-identity check now compares a hardcoded SHA-256
hash of each function's AST source (also recorded once, from the live file,
at authoring time) against a freshly-computed hash of that function's
CURRENT source read straight off disk - never `git show`. Both tripwires
are now fully self-contained: they detect a real future violation without
depending on `eb19335`, or any other specific commit, still existing or
resolving cleanly. (`_added_line_numbers` below, and every item 1/2/5/6
check built on it, is UNCHANGED by this fix - it restricts each of these
same hardcoded, now-fixed-forever file paths to only the lines THIS plan
added, which remains valid indefinitely since `eb19335` stays a permanent,
resolvable ancestor commit in this repository's history.)
"""

from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_SHA = "eb19335"  # the master commit this plan's branch forked from


# ---------------------------------------------------------------------------
# Explicit, hardcoded scan universe (see module docstring's Important #4
# fix note) - recorded once, by hand, from `git diff --name-only
# eb19335..HEAD` at authoring time (2026-09-15, HEAD 7561929). This IS the
# scan universe now, not a value derived from a live git computation - so
# it can never silently grow to include a future unrelated commit's files
# once this branch merges.
# ---------------------------------------------------------------------------

# Item 6 / task brief: "Files this plan touched" - every production .py
# file this plan's diff touches, used as the scan universe for items 1, 2,
# 4 and 5 below.
PRODUCTION_FILES = [
    "crypto_trading/config/loader.py",
    "crypto_trading/guardian/authority.py",
    "crypto_trading/monitoring_loop.py",
    "crypto_trading/paper_trading/guardian_authority_shadow.py",
    "crypto_trading/paper_trading/recovery_sweep.py",
    "crypto_trading/paper_trading/replay.py",
    "crypto_trading/performance/guardian_authority_shadow_report.py",
    "crypto_trading/storage/db.py",
    "crypto_trading/storage/repository.py",
]

# All touched .py files, production AND test - the scan universe for item
# 6's broader, unrestricted re-check.
ALL_TOUCHED_PY_FILES = [
    *PRODUCTION_FILES,
    "tests/crypto_trading/config/test_loader.py",
    "tests/crypto_trading/guardian/test_authority.py",
    "tests/crypto_trading/guardian/test_authority_shadow_isolation.py",
    "tests/crypto_trading/paper_trading/test_guardian_authority_shadow.py",
    "tests/crypto_trading/paper_trading/test_recovery_sweep.py",
    "tests/crypto_trading/paper_trading/test_replay.py",
    "tests/crypto_trading/performance/test_guardian_authority_shadow_report.py",
    "tests/crypto_trading/storage/test_repository_guardian_authority_pre_entry_shadow.py",
    "tests/crypto_trading/storage/test_repository_guardian_authority_shadow.py",
    "tests/crypto_trading/test_monitoring_loop.py",
]


def test_production_files_list_is_not_empty_and_matches_expected_shape():
    """Sanity check on the scan universe itself: if this were empty, every
    other test below would vacuously pass while checking nothing. Pins the
    exact expected file set (see module docstring's Important #4 fix note -
    this is now the hardcoded scan universe itself, not a value re-derived
    from git) and confirms every listed path genuinely exists on disk, so a
    typo or an accidentally-removed file is caught here rather than
    silently shrinking every other check's coverage."""
    assert PRODUCTION_FILES == [
        "crypto_trading/config/loader.py",
        "crypto_trading/guardian/authority.py",
        "crypto_trading/monitoring_loop.py",
        "crypto_trading/paper_trading/guardian_authority_shadow.py",
        "crypto_trading/paper_trading/recovery_sweep.py",
        "crypto_trading/paper_trading/replay.py",
        "crypto_trading/performance/guardian_authority_shadow_report.py",
        "crypto_trading/storage/db.py",
        "crypto_trading/storage/repository.py",
    ]
    assert (REPO_ROOT / "crypto_trading/config/guardian.yaml").is_file()
    for path in PRODUCTION_FILES:
        assert (REPO_ROOT / path).is_file(), f"{path} does not exist on disk"


def test_hardcoded_scan_universe_is_internally_consistent_and_self_contained():
    """Replaces the OLD `git diff --stat`-based independent reconfirmation
    (removed per the module docstring's Important #4 fix - there is no
    longer a live git computation to reconfirm against). Self-contained
    consistency checks on the two hardcoded scan-universe constants
    instead: every listed path exists on disk, PRODUCTION_FILES is a
    genuine subset of ALL_TOUCHED_PY_FILES, and ALL_TOUCHED_PY_FILES
    contains at least one touched test file - so item 6's broader scan
    below actually has something beyond PRODUCTION_FILES to scan."""
    assert PRODUCTION_FILES, "PRODUCTION_FILES must not be empty"
    assert ALL_TOUCHED_PY_FILES, "ALL_TOUCHED_PY_FILES must not be empty"
    for path in ALL_TOUCHED_PY_FILES:
        assert (REPO_ROOT / path).is_file(), f"{path} does not exist on disk"
    assert set(PRODUCTION_FILES) <= set(ALL_TOUCHED_PY_FILES)
    test_files = [f for f in ALL_TOUCHED_PY_FILES if f.startswith("tests/")]
    assert test_files, "expected at least one touched test file in the scan universe"


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _added_line_numbers(path: str, base: str = BASE_SHA) -> set[int]:
    """New-file line numbers this plan's diff actually ADDED for `path`,
    via `git diff -U0` (zero context lines - a hunk contains only real
    +/- lines, nothing else). Context/unchanged/pre-existing lines never
    appear in a -U0 diff at all, so they cannot be mistaken for additions."""
    out = subprocess.run(
        ["git", "diff", "-U0", f"{base}..HEAD", "--", path],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    added: set[int] = set()
    new_lineno = None
    for line in out.splitlines():
        header = _HUNK_HEADER.match(line)
        if header:
            new_lineno = int(header.group(1))
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            assert new_lineno is not None
            added.add(new_lineno)
            new_lineno += 1
        # '-' lines belong to the OLD file and do not consume new_lineno.
    return added


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Item 1 (+ half of item 6): no file this plan touched calls
# open_position_for_candidate, tighten_position_stop_loss,
# apply_live_sl_tightening, place_stop_loss_order, cancel_order,
# set_leverage, or save_guardian_observation - as a NEW call site - except
# the one pre-existing, already-reviewed open_position_for_candidate call
# inside maybe_open_position_for_candidate itself (authority.py, which
# this plan never modified).
# ---------------------------------------------------------------------------

# save_guardian_observation is included at full zero-tolerance (not just
# its state="EXIT" flavor): none of the NEW code this plan adds has any
# legitimate reason to call it at all (it only ever writes to the brand
# new, separate shadow tables) - see guardian_authority_shadow.py's own
# module docstring ("Its only writes anywhere are the
# guardian_authority_shadow_observations repository methods"). Forbidding
# every call is a strictly stronger, and therefore still faithful, version
# of "forbid the state='EXIT' one".
FORBIDDEN_CALL_NAMES = (
    "open_position_for_candidate",
    "tighten_position_stop_loss",
    "apply_live_sl_tightening",
    "place_stop_loss_order",
    "cancel_order",
    "set_leverage",
    "save_guardian_observation",
)

# The ONE sanctioned exception in the whole plan: open_position_for_candidate
# may be called from inside maybe_open_position_for_candidate (authority.py)
# - but only because that call site is byte-identical / pre-existing (item 3
# proves this separately), which also means it can never appear as an ADDED
# line in the first place. Kept here anyway, explicitly, as defense in depth
# - not load-bearing today, but if a future refactor ever moved/retyped that
# line, this exception (and not a blanket pass) is the only thing that
# should ever let it through.
SANCTIONED_CALL_SITES = {
    ("crypto_trading/guardian/authority.py", "open_position_for_candidate"): "maybe_open_position_for_candidate",
}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _new_forbidden_call_violations(path: str) -> list[str]:
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
            name = _call_name(node)
            if name in FORBIDDEN_CALL_NAMES and node.lineno in added:
                enclosing = self.stack[-1] if self.stack else "<module>"
                sanctioned = SANCTIONED_CALL_SITES.get((path, name))
                if sanctioned != enclosing:
                    violations.append(f"{path}:{node.lineno} calls {name}() inside {enclosing}")
            self.generic_visit(node)

    _Visitor().visit(tree)
    return violations


def test_no_new_call_site_of_any_forbidden_production_function_in_touched_files():
    """Global Constraint (verbatim): 'Never call
    open_position_for_candidate, tighten_position_stop_loss,
    apply_live_sl_tightening, place_stop_loss_order, cancel_order,
    set_leverage, or repo.save_guardian_observation(..., state="EXIT")
    from any file this plan creates or touches.' Scanned as NEW call sites
    only (see module docstring) across every production .py file this
    plan's diff touches."""
    all_violations: list[str] = []
    for path in PRODUCTION_FILES:
        all_violations.extend(_new_forbidden_call_violations(path))
    assert all_violations == [], f"forbidden NEW call site(s) found: {all_violations}"


def test_forbidden_call_scan_genuinely_catches_a_new_violation_when_one_exists():
    """Confirms the scanner in the test above is not vacuously true (e.g.
    a base-SHA/path bug that makes _added_line_numbers always return an
    empty set, silently passing regardless of content). Synthesizes a tiny
    fake diff/source pair, entirely in memory - no real file is touched -
    and asserts the underlying detection logic (AST Call-name matching
    against the added-lines set) flags a call on a line marked as added,
    and does NOT flag the identical call on a line NOT marked as added
    (proving both the positive and negative cases work)."""
    source = (
        "def caller():\n"
        "    set_leverage(5)\n"      # line 2
        "    cancel_order('x')\n"    # line 3
    )
    tree = ast.parse(source)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    call_linenos = {_call_name(c): c.lineno for c in calls}
    assert call_linenos["set_leverage"] == 2
    assert call_linenos["cancel_order"] == 3

    # Line 2 marked added -> must be flagged. Line 3 NOT marked added
    # (simulating pre-existing code) -> must NOT be flagged.
    added = {2}

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []
            self.hits: list[str] = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            name = _call_name(node)
            if name in FORBIDDEN_CALL_NAMES and node.lineno in added:
                self.hits.append(name)
            self.generic_visit(node)

    visitor = _Visitor()
    visitor.visit(tree)
    assert visitor.hits == ["set_leverage"], (
        "scanner failed to catch a genuine new violation, or over/under-flagged"
    )


# ---------------------------------------------------------------------------
# Item 2: no file this plan touched imports
# crypto_trading.paper_trading.position_sizing.
# ---------------------------------------------------------------------------


def test_no_touched_file_imports_position_sizing():
    """Global Constraint (verbatim): never import
    crypto_trading/paper_trading/position_sizing.py. AST-based import scan
    (Import and ImportFrom) across every production .py file this plan's
    diff touches - whole-file, not new-lines-only, since this specific
    import has no legitimate reason to exist in ANY of these files at any
    point (unlike the forbidden calls above, several of which DO have a
    pre-existing sanctioned call site elsewhere in the same touched file)."""
    forbidden_module = "crypto_trading.paper_trading.position_sizing"
    offenders: list[str] = []
    for path in PRODUCTION_FILES:
        tree = ast.parse(_read(path), filename=path)
        for node in ast.walk(tree):
            imported: set[str] = set()
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = {node.module}
            for m in imported:
                if m == forbidden_module or m.startswith(forbidden_module + "."):
                    offenders.append(f"{path}: imports {m}")
    assert offenders == [], f"forbidden position_sizing import(s): {offenders}"

    # Belt-and-suspenders textual scan, same precedent as test_authority.py's
    # own test_neither_module_imports_or_calls_position_sizing: also catch a
    # dynamic-import escape hatch (importlib) or a bare mention of the one
    # function this codebase's sizing logic exposes. Restricted to NEW lines
    # only (unlike test_authority.py's whole-file version): at least one
    # touched file here (config/loader.py) has a pre-existing, unrelated,
    # already-reviewed comment mentioning position_sizing.py in a docstring
    # for a different config field - a whole-file substring scan would
    # false-positive on that pre-existing prose.
    for path in PRODUCTION_FILES:
        added = _added_line_numbers(path)
        if not added:
            continue
        for lineno, line in enumerate(_read(path).splitlines(), start=1):
            if lineno not in added:
                continue
            assert "position_sizing" not in line, f"{path}:{lineno} references position_sizing"
            assert "compute_position_size" not in line, f"{path}:{lineno} references compute_position_size"


# ---------------------------------------------------------------------------
# Item 3: evaluate_heuristics, decide_pre_entry, decide_open_position,
# heuristic_condition_matches, _compute_proposed_new_sl, _groups_for_factors
# in authority.py are byte-identical between eb19335 and HEAD.
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

# Recorded once (2026-09-15, HEAD 7561929, final whole-branch review's
# Important #4 fix - see module docstring): SHA-256 of each function's AST
# source segment, read straight from the live authority.py at authoring
# time (the exact same extraction `_current_function_source` below performs
# at test time). Replaces the OLD `git show eb19335:<path>` byte-identity
# comparison, which depended on `eb19335` remaining resolvable and the diff
# range never growing - this hash comparison depends on neither: it detects
# a future accidental modification to any of these 6 functions purely from
# the function's own current source, with no git history involved at all.
_EXPECTED_FUNCTION_SOURCE_SHA256 = {
    "evaluate_heuristics": "b7351e34af0b2e9943533463aa20b3f304fca235c9251e6bf720e40a82ea2c69",
    "decide_pre_entry": "509a88b5326b7b757a59d6510e7ca132abd32e19bcb6952dd68fe9abd8461cbf",
    "decide_open_position": "b4d761d049fc7e0e1fd193cb7529b11b36dcdb640950425acd931c7a131f80fd",
    "heuristic_condition_matches": "119661ae444769f018a634a95bef8d3a8a9d613e669f15304819bdd16248276f",
    "_compute_proposed_new_sl": "a580c0ce176dd6acc6cb74d7c7cbaf79d3d8e9146c0887a56d7f2c6222dd4c57",
    "_groups_for_factors": "7bbefa709200c067b26f0d09b0e43493e1815f8a5aa60a1084f4c900edde4efc",
}


def _current_function_source(path: str, function_name: str) -> str:
    """`function_name`'s CURRENT source segment, read straight from the
    live file on disk via `_read` (never `git show` - see module
    docstring's Important #4 fix note)."""
    source = _read(path)
    tree = ast.parse(source, filename=path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, f"could not extract source for {function_name}"
            return segment
    raise AssertionError(f"function {function_name} not found in {path}")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("function_name", _PURE_DECISION_FUNCTIONS)
def test_pure_decision_function_is_byte_identical_to_base(function_name):
    """Global Constraint (verbatim): 'Never modify
    crypto_trading/guardian/authority.py's pure decision engine
    (decide_pre_entry, decide_open_position, evaluate_heuristics,
    heuristic_condition_matches, _compute_proposed_new_sl) - reuse
    unmodified, same import, same call signature.' _groups_for_factors is
    the plan's own Task 8/9 addition to that same "never touch" set (it is
    reused unmodified by the shadow self-critique function, exactly as
    the pure decision functions are reused unmodified by the shadow
    observation functions). Compares a SHA-256 hash of the function's
    CURRENT source (read from the live file, never `git show`) against the
    hash recorded at authoring time (see _EXPECTED_FUNCTION_SOURCE_SHA256
    above) - self-contained, survives this branch merging, never merely
    'behaves the same'."""
    current_source = _current_function_source(AUTHORITY_PATH, function_name)
    current_hash = _sha256(current_source)
    assert current_hash == _EXPECTED_FUNCTION_SOURCE_SHA256[function_name], (
        f"{function_name} changed since its hash was recorded - Global Constraint violation"
    )


def test_byte_identical_check_genuinely_detects_a_real_change():
    """Confirms the comparison in the test above is not vacuously true
    (e.g. two equal empty strings, or a hash constant that matches
    anything). Deliberately hashes evaluate_heuristics' CURRENT source
    against a mutated copy of itself (one character appended) and confirms
    the two hashes differ - i.e. the mechanism can actually tell two
    different function bodies apart - then confirms the mutated hash does
    NOT equal the recorded expected hash either, the exact failure mode the
    real test above would hit if evaluate_heuristics were ever modified."""
    current_source = _current_function_source(AUTHORITY_PATH, "evaluate_heuristics")
    mutated = current_source + "  # tampered\n"
    assert _sha256(mutated) != _sha256(current_source)
    assert _sha256(mutated) != _EXPECTED_FUNCTION_SOURCE_SHA256["evaluate_heuristics"]


def test_authority_py_frozen_functions_have_zero_deletions_since_eb19335():
    """Independent, coarser-grained corroboration of the six function-level
    byte-identical checks above, NARROWED (2026, TAKE_PROFIT addition) to
    the 6 frozen functions' own line ranges rather than the whole file.

    This test originally asserted `git diff --numstat` for the WHOLE FILE
    showed zero deletions since eb19335 - true for Shadow/Observation
    Mode's own plan (which genuinely only ever added
    maybe_record_pre_entry_shadow/resolve_pending_pre_entry_shadows as a
    single new block), but only ever a coarser PROXY for the actual Global
    Constraint, which is specifically about the 6 named frozen functions,
    not about every line in the file forever. A later, separate,
    independently-reviewed change (adding the TAKE_PROFIT decision type)
    legitimately edits a non-frozen function in this same file
    (`resolve_pending_decisions`, to treat TAKE_PROFIT the same way it
    already treats CLOSE_EARLY) without touching any of the 6 frozen
    functions at all. The corroboration is therefore tightened to directly
    check what it always meant to protect: no line THIS WHOLE HISTORICAL
    DIFF ever deleted falls inside any of the 6 frozen functions' own AST
    line range (as that range existed at eb19335). The 6 SHA-256 hash
    checks above remain the primary, byte-exact proof; this is still a
    second, independent, git-history-based corroboration of the same
    guarantee, not a weakening of it. Mirrors the identical fix already
    applied to test_self_improvement_isolation.py's own equivalent,
    a09b0ab-anchored check."""
    out = subprocess.run(
        ["git", "diff", "-U0", f"{BASE_SHA}..HEAD", "--", AUTHORITY_PATH],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    deleted_line_numbers: set[int] = set()
    old_lineno = None
    for line in out.splitlines():
        if line.startswith("@@"):
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


# ---------------------------------------------------------------------------
# Item 4: find_guardian_authority_shadow_heuristics /
# upsert_guardian_authority_shadow_heuristic are called ONLY from Task 8's
# update_shadow_heuristics_from_resolved_shadow_observations - never from
# evaluate_heuristics/decide_pre_entry/decide_open_position, and never
# anywhere else in authority.py.
# ---------------------------------------------------------------------------

SHADOW_HEURISTICS_WRITE_FN = "upsert_guardian_authority_shadow_heuristic"
SHADOW_HEURISTICS_READ_FN = "find_guardian_authority_shadow_heuristics"
SHADOW_SELF_CRITIQUE_FN = "update_shadow_heuristics_from_resolved_shadow_observations"
SHADOW_MODULE_PATH = "crypto_trading/paper_trading/guardian_authority_shadow.py"
SHADOW_REPORT_PATH = "crypto_trading/performance/guardian_authority_shadow_report.py"


def _call_sites(path: str, function_name: str) -> list[tuple[int, str]]:
    """(lineno, enclosing_function_name) for every Call to `function_name`
    in `path`'s CURRENT source - whole-file (not new-lines-restricted):
    both guardian_authority_shadow_heuristics repository methods are
    brand-new (Task 2's own new table), so there is no pre-existing,
    pre-plan call site anywhere that a whole-file scan could mistake for
    a new one - every real call site in the whole plan IS new by
    construction."""
    tree = ast.parse(_read(path), filename=path)
    sites: list[tuple[int, str]] = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            if _call_name(node) == function_name:
                enclosing = self.stack[-1] if self.stack else "<module>"
                sites.append((node.lineno, enclosing))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return sites


def test_shadow_heuristics_write_is_called_only_from_the_self_critique_function():
    """upsert_guardian_authority_shadow_heuristic's only caller anywhere in
    guardian_authority_shadow.py must be
    update_shadow_heuristics_from_resolved_shadow_observations (Task 8)."""
    sites = _call_sites(SHADOW_MODULE_PATH, SHADOW_HEURISTICS_WRITE_FN)
    assert sites, "expected at least one call site (the Task 8 write itself)"
    offenders = [(lineno, fn) for lineno, fn in sites if fn != SHADOW_SELF_CRITIQUE_FN]
    assert offenders == [], f"{SHADOW_HEURISTICS_WRITE_FN} called outside {SHADOW_SELF_CRITIQUE_FN}: {offenders}"


def test_shadow_heuristics_write_has_zero_call_sites_outside_its_own_module():
    """No other production file this plan touched calls
    upsert_guardian_authority_shadow_heuristic at all - not authority.py,
    not the report, not monitoring_loop.py."""
    offenders = []
    for path in PRODUCTION_FILES:
        if path in (SHADOW_MODULE_PATH, "crypto_trading/storage/repository.py"):
            continue  # repository.py only DEFINES the method, never calls it
        for lineno, fn in _call_sites(path, SHADOW_HEURISTICS_WRITE_FN):
            offenders.append(f"{path}:{lineno} in {fn}")
    assert offenders == [], f"unexpected write call site(s): {offenders}"


def test_shadow_heuristics_read_never_called_from_authority_py_at_all():
    """The plan's own Global Constraint (verbatim): 'evaluate_heuristics/
    decide_pre_entry/decide_open_position must never be given a reason to
    read [guardian_authority_shadow_heuristics] - no shared read path, no
    flag-based branch in a shared function.' Strongest form of this check:
    authority.py (the file that DEFINES those three functions) must not
    reference find_guardian_authority_shadow_heuristics AT ALL, in any
    function - not just those three by name."""
    sites = _call_sites(AUTHORITY_PATH, SHADOW_HEURISTICS_READ_FN)
    assert sites == [], (
        f"authority.py must never call {SHADOW_HEURISTICS_READ_FN}: {sites}"
    )
    # Belt-and-suspenders textual scan: the identifier must not appear in
    # authority.py's source at all, in any form (comment, docstring, or
    # code) - it has no legitimate reason to be mentioned there whatsoever.
    assert SHADOW_HEURISTICS_READ_FN not in _read(AUTHORITY_PATH)


def test_shadow_heuristics_read_call_sites_are_limited_to_the_shadow_report():
    """The read side has one legitimate, sanctioned consumer this plan
    adds beyond Task 8's own module: Task 9's read-only report
    (build_report, guardian_authority_shadow_report.py), which surfaces
    `active_shadow_heuristics_count`. Every production call site of
    find_guardian_authority_shadow_heuristics, across every file this plan
    touched, must be inside that report module - and, per the test above,
    specifically never inside authority.py."""
    offenders = []
    found_in_report = False
    for path in PRODUCTION_FILES:
        if path == "crypto_trading/storage/repository.py":
            continue  # only DEFINES the method
        sites = _call_sites(path, SHADOW_HEURISTICS_READ_FN)
        if not sites:
            continue
        if path == SHADOW_REPORT_PATH:
            found_in_report = True
            continue
        offenders.extend(f"{path}:{lineno} in {fn}" for lineno, fn in sites)
    assert offenders == [], f"unexpected read call site(s) outside the shadow report: {offenders}"
    assert found_in_report, "expected the shadow report to read the shadow heuristics count"


def test_shadow_heuristics_checks_genuinely_catch_a_call_from_evaluate_heuristics():
    """Deliberate-break confirmation (this is one of the highest-stakes
    checks in this file): synthesizes a fake module where
    evaluate_heuristics calls find_guardian_authority_shadow_heuristics,
    and confirms the SAME call-site-collection logic used by the tests
    above would flag it as a violation - i.e. these tests are not
    vacuously true against the real authority.py just because it happens
    to contain zero references today."""
    fake_source = (
        "def evaluate_heuristics(factors, heuristics):\n"
        "    shadow = find_guardian_authority_shadow_heuristics()\n"
        "    return shadow\n"
    )
    tree = ast.parse(fake_source)
    sites = []

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            if _call_name(node) == SHADOW_HEURISTICS_READ_FN:
                sites.append(self.stack[-1] if self.stack else "<module>")
            self.generic_visit(node)

    _Visitor().visit(tree)
    assert sites == ["evaluate_heuristics"], "scanner failed to catch a synthesized real violation"


# ---------------------------------------------------------------------------
# Item 5: authority_shadow_enabled defaults false (GuardianConfig +
# guardian.yaml); authority_enabled is never referenced, read, or modified
# by any NEW code this plan added.
# ---------------------------------------------------------------------------


def test_authority_shadow_enabled_defaults_false_in_guardian_config():
    from crypto_trading.config.loader import GuardianConfig

    assert GuardianConfig().authority_shadow_enabled is False


def test_authority_shadow_enabled_defaults_false_in_guardian_yaml():
    import yaml

    with (REPO_ROOT / "crypto_trading/config/guardian.yaml").open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    assert raw["authority_shadow_enabled"] is False


def test_authority_enabled_field_declaration_is_unchanged_pre_existing_code():
    """The real flag's own declaration/default must be untouched by this
    plan - not merely 'still False today by coincidence', but genuinely
    absent from this plan's own diff (i.e. not even re-typed identically).
    Cross-checked against _added_line_numbers so this fails loudly if a
    future change ever re-writes that declaration line, even to the exact
    same text."""
    added = _added_line_numbers("crypto_trading/config/loader.py")
    source = _read("crypto_trading/config/loader.py")
    tree = ast.parse(source, filename="crypto_trading/config/loader.py")
    declared_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "authority_enabled"
    ]
    assert declared_lines, "authority_enabled field declaration not found at all"
    offenders = [ln for ln in declared_lines if ln in added]
    assert offenders == [], f"authority_enabled field declaration was touched: lines {offenders}"

    from crypto_trading.config.loader import GuardianConfig

    assert GuardianConfig().authority_enabled is False


def test_authority_enabled_yaml_key_is_unchanged_pre_existing_code():
    added_text_lines = subprocess.run(
        ["git", "diff", "-U0", f"{BASE_SHA}..HEAD", "--", "crypto_trading/config/guardian.yaml"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    added_content = [
        line[1:] for line in added_text_lines
        if line.startswith("+") and not line.startswith("+++")
    ]
    offenders = [
        line for line in added_content
        if re.match(r"^\s*authority_enabled\s*:", line)
    ]
    assert offenders == [], f"authority_enabled yaml key was (re-)added: {offenders}"


def _authority_enabled_reference_violations(path: str) -> list[str]:
    added = _added_line_numbers(path)
    if not added:
        return []
    tree = ast.parse(_read(path), filename=path)
    violations: list[str] = []
    for node in ast.walk(tree):
        lineno = getattr(node, "lineno", None)
        if lineno is None or lineno not in added:
            continue
        if isinstance(node, ast.Attribute) and node.attr == "authority_enabled":
            violations.append(f"{path}:{lineno} attribute .authority_enabled")
        elif isinstance(node, ast.Name) and node.id == "authority_enabled":
            violations.append(f"{path}:{lineno} name authority_enabled")
        elif isinstance(node, ast.keyword) and node.arg == "authority_enabled":
            violations.append(f"{path}:{lineno} keyword authority_enabled=")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "authority_enabled":
            violations.append(f"{path}:{lineno} field declaration authority_enabled")
    return violations


def test_authority_enabled_is_never_read_written_or_gated_by_new_code():
    """Global Constraint (verbatim): 'authority_enabled stays untouched,
    false, throughout. This plan's new flag (authority_shadow_enabled)
    never gates anything authority_enabled also gates, and vice versa.'
    NEW-code-only AST scan (Attribute/Name/keyword-argument/field-
    declaration references) across every production .py file this plan's
    diff touches. Deliberately does NOT do a bare substring scan for
    "authority_enabled" - unlike set_leverage or position_sizing, this
    identifier legitimately appears in PROSE this plan's own new docstrings
    and comments add (e.g. guardian_authority_shadow.py's module docstring,
    explaining that this feature is "completely independent of
    authority_enabled") to document the very independence this test
    verifies at the code level - a substring scan would incorrectly flag
    the documentation of the guarantee as a violation of it (same
    precedent as test_authority.py's own handling of the bare word
    "leverage" in a legitimate docstring)."""
    all_violations: list[str] = []
    for path in PRODUCTION_FILES:
        all_violations.extend(_authority_enabled_reference_violations(path))
    assert all_violations == [], f"new authority_enabled reference(s): {all_violations}"


def test_authority_enabled_scan_genuinely_catches_a_new_reference():
    """Deliberate-break confirmation: a synthesized 'added line' containing
    `settings.guardian.authority_enabled` must be caught by the same
    Attribute-matching logic the test above relies on."""
    source = (
        "def f(settings):\n"
        "    if settings.guardian.authority_enabled:\n"  # line 2
        "        return True\n"
        "    return False\n"
    )
    tree = ast.parse(source)
    hits = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "authority_enabled"
    ]
    assert hits == [2]


# ---------------------------------------------------------------------------
# Item 6: confirm the 6 forbidden-call patterns are genuinely absent as new
# call sites across the FULL hardcoded scan universe - every touched file,
# production AND test, not just PRODUCTION_FILES (see module docstring's
# Important #4 fix note: the file set itself is no longer re-derived from a
# live `git diff --stat`, but the per-file NEW-lines-only restriction below
# still uses `_added_line_numbers`, which remains valid indefinitely - see
# that note for why).
# ---------------------------------------------------------------------------


def test_full_diff_has_zero_new_call_sites_of_the_six_forbidden_functions_anywhere():
    """Broadest possible re-check for the 6 real forbidden-call-name
    patterns (excluding save_guardian_observation, which test files
    legitimately call via local seed helpers with state="EXIT" to CONSTRUCT
    a pre-existing real observation row as test fixture data, precisely in
    order to prove the shadow code path leaves it alone - see e.g.
    test_guardian_authority_shadow.py's own
    test_tick_never_writes_to_positions_or_real_guardian_authority_
    decisions_tables; that is the isolation guarantee being exercised, not
    a violation of it) - scanned across EVERY touched .py file in the whole
    diff, test files included, not just PRODUCTION_FILES. Restricted to new
    call sites only (see module docstring) for the same reason as item 1's
    own scan: some touched test files legitimately reference these names
    in mocks/patches on PRE-EXISTING lines this plan did not add."""
    production_only_names = tuple(n for n in FORBIDDEN_CALL_NAMES if n != "save_guardian_observation")
    all_violations: list[str] = []
    for path in ALL_TOUCHED_PY_FILES:
        added = _added_line_numbers(path)
        if not added:
            continue
        tree = ast.parse(_read(path), filename=path)

        class _Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stack: list[str] = []

            def visit_FunctionDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            def visit_Call(self, node):
                name = _call_name(node)
                if name in production_only_names and node.lineno in added:
                    enclosing = self.stack[-1] if self.stack else "<module>"
                    sanctioned = SANCTIONED_CALL_SITES.get((path, name))
                    if sanctioned != enclosing:
                        all_violations.append(f"{path}:{node.lineno} calls {name}() inside {enclosing}")
                self.generic_visit(node)

        _Visitor().visit(tree)
    assert all_violations == [], f"forbidden NEW call site(s) anywhere in the diff: {all_violations}"


def test_full_diff_position_sizing_import_is_genuinely_absent_everywhere():
    """Same broadening as the test above, but for item 2's import check:
    re-scan every touched file (not just PRODUCTION_FILES) for a
    position_sizing import, whole-file (not new-lines-restricted, for the
    same reason as test_no_touched_file_imports_position_sizing: no
    legitimate pre-existing reference could exist in ANY of these files)."""
    forbidden_module = "crypto_trading.paper_trading.position_sizing"
    offenders: list[str] = []
    for path in ALL_TOUCHED_PY_FILES:
        tree = ast.parse(_read(path), filename=path)
        for node in ast.walk(tree):
            imported: set[str] = set()
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = {node.module}
            for m in imported:
                if m == forbidden_module or m.startswith(forbidden_module + "."):
                    offenders.append(f"{path}: imports {m}")
    assert offenders == [], f"forbidden position_sizing import(s) anywhere in the diff: {offenders}"
