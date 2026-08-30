from fastapi.testclient import TestClient

from crypto_trading.config.loader import get_settings
from crypto_trading.dashboard.api import create_app
from crypto_trading.storage.repository import SQLiteRepository

_FORBIDDEN_NUMERIC_KEYS = {
    "win_rate",
    "expectancy",
    "pnl",
    "cumulative_pnl",
    "drawdown",
    "profit_factor",
    "trade_count",
}


def _client(tmp_path):
    db_path = tmp_path / "test.db"
    app = create_app(lambda: SQLiteRepository(db_path), get_settings())
    return TestClient(app)


def test_performance_returns_not_available_yet_with_no_computed_numbers(tmp_path):
    client = _client(tmp_path)

    response = client.get("/api/performance")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "not_available_yet"
    assert not (_FORBIDDEN_NUMERIC_KEYS & body.keys())
