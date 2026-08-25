from __future__ import annotations


class ConfigError(Exception):
    """Kastas när konfiguration saknas eller inte validerar. Fail-fast vid start —
    aldrig en tyst default för ett SPEC-obligatoriskt värde."""
