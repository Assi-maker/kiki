import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _imported_top_level_modules(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_crypto_trading_never_imports_intelligence():
    crypto_trading_files = (_REPO_ROOT / "crypto_trading").rglob("*.py")
    offenders = []
    for py_file in crypto_trading_files:
        if "intelligence" in _imported_top_level_modules(py_file):
            offenders.append(str(py_file))
    assert offenders == [], f"crypto_trading files importing intelligence: {offenders}"


def test_intelligence_never_imports_crypto_trading():
    intelligence_files = (_REPO_ROOT / "intelligence").rglob("*.py")
    offenders = []
    for py_file in intelligence_files:
        if "crypto_trading" in _imported_top_level_modules(py_file):
            offenders.append(str(py_file))
    assert offenders == [], f"intelligence files importing crypto_trading: {offenders}"


def test_crypto_trading_has_no_broker_account_or_order_code():
    """SPEC_CRYPTO.md §1/§19 (2026-09-04 amendment): the absolute ban on
    broker/order code narrowed to "no LIVE broker account", with one
    explicit, reviewed exception for BingX Demo (VST)-only execution -
    never the live account (see
    docs/superpowers/specs/2026-09-04-bingx-demo-execution-design.md). Only
    the files that implement/wire that exception may contain these terms;
    everywhere else in crypto_trading/ must stay exactly as clean as
    before."""
    forbidden_terms = ("account_balance", "place_order", "broker_credential", "api_secret")
    allowed_files = {
        _REPO_ROOT / "crypto_trading" / "connectors" / "bingx_demo_trading.py",
        _REPO_ROOT / "crypto_trading" / "run.py",
    }
    crypto_trading_files = (_REPO_ROOT / "crypto_trading").rglob("*.py")
    offenders = []
    for py_file in crypto_trading_files:
        if py_file in allowed_files:
            continue
        content = py_file.read_text(encoding="utf-8").lower()
        for term in forbidden_terms:
            if term in content:
                offenders.append((str(py_file), term))
    assert offenders == [], f"forbidden broker/order terms found: {offenders}"
