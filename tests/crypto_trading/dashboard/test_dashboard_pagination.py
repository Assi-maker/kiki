from datetime import UTC, datetime

from fastapi.testclient import TestClient

from crypto_trading.config.loader import get_settings
from crypto_trading.dashboard.api import create_app
from crypto_trading.storage.repository import SQLiteRepository

_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)

_PAGINATED_ENDPOINTS = ["/api/trade-history", "/api/forecast", "/api/system-health"]


def _client(tmp_path):
    db_path = tmp_path / "test.db"
    repo = SQLiteRepository(db_path)
    for i in range(10):
        repo.start_run(f"run-{i}", "discovery", _NOW)
        repo.complete_run(f"run-{i}", _NOW, "ok", [], instruments_scanned=i)
    app = create_app(lambda: SQLiteRepository(db_path), get_settings())
    return TestClient(app)


def test_negative_limit_is_rejected_not_treated_as_unlimited_by_sqlite(tmp_path):
    """SQLite tolkar ett negativt LIMIT som obegränsat - detta test bevisar
    att -1 avvisas av API:t innan det någonsin når SQL-lagret, aldrig att
    alla 10 rader returneras."""
    client = _client(tmp_path)
    for endpoint in _PAGINATED_ENDPOINTS:
        response = client.get(f"{endpoint}?limit=-1")
        assert response.status_code == 422, f"{endpoint} accepted limit=-1"


def test_zero_limit_is_rejected(tmp_path):
    client = _client(tmp_path)
    for endpoint in _PAGINATED_ENDPOINTS:
        response = client.get(f"{endpoint}?limit=0")
        assert response.status_code == 422, f"{endpoint} accepted limit=0"


def test_normal_limit_is_accepted_and_respected(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/system-health?limit=5")
    assert response.status_code == 200
    assert len(response.json()["recent_runs"]) == 5


def test_extremely_large_limit_is_rejected(tmp_path):
    """Konsekvent policy: ett värde över den hårda maxgränsen avvisas (422),
    klipps aldrig tyst - ingen dold cap-logik som skulle kunna divergera
    mellan endpoints."""
    client = _client(tmp_path)
    for endpoint in _PAGINATED_ENDPOINTS:
        response = client.get(f"{endpoint}?limit=1000000")
        assert response.status_code == 422, f"{endpoint} accepted limit=1000000"


def test_limit_at_exactly_the_max_boundary_is_accepted(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/system-health?limit=500")
    assert response.status_code == 200


def test_negative_offset_is_rejected(tmp_path):
    client = _client(tmp_path)
    for endpoint in ["/api/trade-history", "/api/forecast"]:
        response = client.get(f"{endpoint}?offset=-1")
        assert response.status_code == 422, f"{endpoint} accepted offset=-1"


def test_missing_limit_falls_back_to_default(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/system-health")
    assert response.status_code == 200
    assert len(response.json()["recent_runs"]) == 10  # default 50 covers all 10 seeded rows
