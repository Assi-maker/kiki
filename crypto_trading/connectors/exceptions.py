from __future__ import annotations


class ConnectorError(Exception):
    """Basklass för alla connector-fel i crypto_trading/."""


class ConnectorUnavailableError(ConnectorError):
    """BingX svarade inte (timeout/nätverksfel), gav ett icke-2xx-svar efter
    uttömd retry, eller ett API-nivå-fel (code != 0). Aldrig en gissning -
    anroparen ska klassa den underliggande candidate:n som DATA_INVALID
    nedströms (SPEC §8.2), inte krascha."""
