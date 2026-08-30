from fastapi.testclient import TestClient

from crypto_trading.config.loader import get_settings
from crypto_trading.dashboard.api import create_app
from crypto_trading.storage.repository import SQLiteRepository


def _client(tmp_path):
    db_path = tmp_path / "test.db"
    app = create_app(lambda: SQLiteRepository(db_path), get_settings())
    return TestClient(app)


def test_root_serves_html_frontend(tmp_path):
    client = _client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<html" in response.text
