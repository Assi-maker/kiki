"""Shared test isolation for crypto_trading."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_safe_mode_from_the_machine_env(monkeypatch):
    """The real .env carries CRYPTO_TRADING_SAFE_MODE=1 as a hard breaker
    while LIVE is stopped (2026-10-01). Any test that calls get_settings()
    runs load_dotenv() on that real file and leaves the variable in
    os.environ, which would silently disarm every later test's flags. Each
    test therefore starts without it; tests that exercise SAFE_MODE set it
    themselves."""
    monkeypatch.delenv("CRYPTO_TRADING_SAFE_MODE", raising=False)
