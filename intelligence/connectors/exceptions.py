class ConnectorError(Exception):
    """Basklass för alla connector-fel.
    Fångas alltid av event_pipeline — kraschar aldrig processen.
    """


class ConnectorConfigError(ConnectorError):
    """Saknad eller ogiltig konfiguration (t.ex. API-nyckel)."""


class ConnectorUnavailableError(ConnectorError):
    """Källan svarade inte inom timeout/retry-policy."""
