import ast
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from crypto_trading.config.loader import get_settings
from crypto_trading.dashboard.api import create_app
from crypto_trading.storage.repository import Repository

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DASHBOARD_DIR = _REPO_ROOT / "crypto_trading" / "dashboard"

# Samtliga skrivande metoder på Repository-protokollet (Fas 0-6). Dashboarden
# (Fas 7) får ALDRIG anropa någon av dessa - AC1/Read-only-AC.
_REPOSITORY_WRITE_METHODS = [
    "create_candidate_with_event",
    "transition_candidate_with_event",
    "save_assessment",
    "save_gate_decision",
    "create_position_with_event",
    "close_position_with_event",
    "start_run",
    "complete_run",
    "record_ai_call_event",
    "save_forecast_record",
    "record_telegram_event",
]


def _imported_top_level_modules(py_file: Path) -> set[str]:
    """Samma AST-baserade importgranskning som
    tests/crypto_trading/test_no_intelligence_coupling.py."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_no_mutating_routes_exist():
    """AC1: samtliga routes i dashboard/api.py är GET/HEAD - inget
    skrivvägs-API existerar. Route-introspektion, inte manuell granskning."""
    app = create_app(lambda: MagicMock(spec=Repository), get_settings())

    offenders = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods is None:
            continue
        if not methods <= {"GET", "HEAD"}:
            offenders.append((getattr(route, "path", route), methods))

    assert offenders == [], f"mutating routes found: {offenders}"


def test_dashboard_default_host_is_localhost():
    """Säkerhets-AC: default bind är 127.0.0.1."""
    settings = get_settings()
    assert settings.dashboard.host == "127.0.0.1"


def test_dashboard_package_never_imports_network_or_secret_modules():
    """Read-only-AC: dashboard/-koden gör aldrig ett BingX-, Claude- eller
    Telegram-anrop - verifierat mekaniskt via en importgranskning, samma
    princip som test_no_intelligence_coupling.py."""
    forbidden_modules = {"httpx", "anthropic"}
    forbidden_submodule_prefixes = (
        "crypto_trading.connectors",
        "crypto_trading.notify.telegram",
        "crypto_trading.agents.runner",
    )
    offenders = []
    for py_file in _DASHBOARD_DIR.rglob("*.py"):
        imported = _imported_top_level_modules(py_file)
        if imported & forbidden_modules:
            offenders.append((str(py_file), imported & forbidden_modules))
        text = py_file.read_text(encoding="utf-8")
        for prefix in forbidden_submodule_prefixes:
            if prefix in text:
                offenders.append((str(py_file), prefix))

    assert offenders == [], f"forbidden imports found in dashboard/: {offenders}"


def test_create_app_never_touches_repository_write_methods():
    """Read-only-AC: varken create_app() själv eller en GET /api/health-
    request får anropa någon skrivande Repository-metod."""
    repo = MagicMock(spec=Repository)
    settings = get_settings()

    app = create_app(lambda: repo, settings)
    client = TestClient(app)
    response = client.get("/api/health")

    assert response.status_code == 200
    for method_name in _REPOSITORY_WRITE_METHODS:
        getattr(repo, method_name).assert_not_called()
