from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator

from crypto_trading.config.exceptions import ConfigError

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "crypto_trading" / "config"

_REQUIRED_DATA_TYPES = {"ticker", "kline", "funding_rate", "open_interest", "contracts"}


class PipelineConfig(BaseModel):
    discovery_interval_minutes: int = Field(gt=0)
    monitoring_interval_seconds: int = Field(gt=0)
    top_n: int = Field(gt=0)
    cooldown_minutes: int = Field(gt=0)
    max_data_age_seconds: dict[str, int]
    min_sample_size_for_calibration: int = Field(gt=0)
    calibration_preliminary_sample_size: int = Field(gt=0)
    sqlite_busy_timeout_ms: int = Field(gt=0)
    required_fields: dict[str, list[str]]
    screener_timeframes: list[str]
    bingx_base_url: str
    bingx_requests_per_second: float = Field(gt=0)
    bingx_cache_ttl_seconds: float = Field(ge=0)
    bingx_max_retries: int = Field(gt=0)
    kline_consistency_tolerance_pct: Decimal = Field(gt=0, le=1)
    eligibility_min_quote_volume_24h_usdt: Decimal = Field(gt=0)
    eligibility_max_spread_pct: Decimal = Field(gt=0, le=1)
    screener_lookback_periods: int = Field(gt=1)
    screener_price_volatility_threshold_pct: Decimal = Field(gt=0)
    screener_rsi_period: int = Field(gt=1)
    screener_rsi_overbought_threshold: Decimal = Field(gt=0, le=100)
    screener_volume_zscore_threshold: Decimal = Field(gt=0)
    screener_funding_rate_threshold_pct: Decimal = Field(gt=0)
    screener_funding_history_limit: int = Field(gt=1)
    evidence_change_threshold_for_reanalysis: Decimal = Field(ge=0)
    news_rss_base_url: str = "https://www.coindesk.com/arc/outboundfeeds/rss/"
    fear_greed_base_url: str = "https://api.alternative.me/fng/"

    @field_validator("max_data_age_seconds")
    @classmethod
    def max_data_age_seconds_covers_all_data_types(cls, v: dict[str, int]) -> dict[str, int]:
        missing = _REQUIRED_DATA_TYPES - v.keys()
        if missing:
            raise ValueError(f"max_data_age_seconds missing required keys: {missing}")
        return v

    @field_validator("required_fields")
    @classmethod
    def required_fields_covers_all_data_types(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        missing = _REQUIRED_DATA_TYPES - v.keys()
        if missing:
            raise ValueError(f"required_fields missing required keys: {missing}")
        return v


class RiskLimitsConfig(BaseModel):
    starting_capital_usdt: Decimal = Field(gt=0)
    risk_per_trade_pct: Decimal = Field(gt=0, le=1)
    max_concurrent_positions: int = Field(gt=0)
    max_total_exposure_pct: Decimal = Field(gt=0, le=1)
    spread_pct: Decimal = Field(ge=0)
    slippage_pct: Decimal = Field(ge=0)
    fee_pct: Decimal = Field(ge=0)
    max_position_hold_hours: int = Field(gt=0)


class BudgetLimitsConfig(BaseModel):
    max_candidates_per_discovery_run: int = Field(gt=0)
    max_ai_calls_per_discovery_run: int = Field(gt=0)
    max_ai_calls_per_day: int = Field(gt=0)
    warning_threshold_pct: Decimal = Field(gt=0, le=1)
    # Kostnadsbudget (2026-09-03): hård dollar-baserad daglig gräns, oberoende
    # av max_ai_calls_per_day (som räknar ANROP, inte $ - ett prishopp eller
    # kontexttillväxt per anrop skulle annars inte fångas). UTC-dygn som
    # budgetperiod, exakt samma _utc_day_start()-mönster som
    # max_ai_calls_per_day redan använder i orchestrator.py. $10.00/dag
    # rekommenderat 2026-09-03 utifrån verklig observerad kostnad (~$1/cykel
    # med 7-10 fullanalyserade kandidater, 500-anropstaket implicerar redan
    # ett tak på ~$9.30/dag i dagens prismix) - se conversation/incident-
    # anteckningar samma datum för den fulla analysen.
    max_daily_ai_cost_usd: Decimal = Field(gt=0, default=Decimal("10.00"))
    # Kostnadsoptimering (2026-09-02): billig förscreening (t.ex. Haiku 4.5)
    # på ett urval av den redan budget-godkända, redan rankade kandidat-
    # poolen, INNAN den dyra fulla 7-rollskedjan (Sonnet 5). Se
    # screening/candidate_engine.py::apply_opportunity_screening().
    # opportunity_screening_enforce=False (default): screeningen körs och
    # loggas för utvärdering men ändrar INTE vilka kandidater som går
    # vidare - måste sättas till True manuellt, med verklig evidens, innan
    # den faktiskt filtrerar bort kandidater.
    max_candidates_for_ai_prescreen: int = Field(gt=0, default=5)
    max_candidates_for_full_analysis: int = Field(gt=0, default=2)
    opportunity_screening_enforce: bool = False


class DetectiveConfig(BaseModel):
    # Detective (Post-Trade Analyst, 2026-09-04): analyserar EFTERHAND redan
    # stängda PAPER-trades i batchar av `batch_size` - aldrig en per trade
    # (kostnadskontroll, explicit användarkrav: "inte ett dyrt Sonnet-anrop
    # efter varje enskild trade"). Se crypto_trading/detective/batch.py.
    batch_size: int = Field(gt=0, default=10)
    check_interval_seconds: int = Field(gt=0, default=300)
    # Minsta totala antal stängda trades innan Detective får inkludera en
    # WIN-vs-LOSS-jämförelse i sitt underlag (explicit användarkrav: "jag
    # vill också att den jämför WIN vs LOSS när tillräckligt många trades
    # finns"). Under denna gräns får den fortfarande observera enskilda
    # trades, bara utan den historiska jämförelsen.
    min_history_for_win_loss_comparison: int = Field(gt=0, default=20)


class DemoExecutionConfig(BaseModel):
    # BingX Demo (VST) execution tunables only - whether the thread runs at
    # all is the CRYPTO_TRADING_DEMO_EXECUTION_ENABLED env-var arm flag
    # (is_demo_execution_enabled() below), same opt-in pattern already used
    # for the dashboard/Telegram threads in run.py.
    check_interval_seconds: int = Field(gt=0, default=30)
    claim_stale_after_seconds: int = Field(gt=0, default=30)
    max_retries: int = Field(gt=0, default=3)


class NotifyConfig(BaseModel):
    notification_level: Literal["important", "decisions", "debug"]
    notify_interval_seconds: int = Field(gt=0)


class DashboardConfig(BaseModel):
    host: str
    port: int = Field(gt=0, le=65535)


class Settings(BaseModel):
    db_path: Path
    pipeline: PipelineConfig
    risk_limits: RiskLimitsConfig
    budget_limits: BudgetLimitsConfig
    notify: NotifyConfig
    dashboard: DashboardConfig
    detective: DetectiveConfig = Field(default_factory=DetectiveConfig)
    demo_execution: DemoExecutionConfig = Field(default_factory=DemoExecutionConfig)


def _load_yaml_model(path: Path, model: type[BaseModel]) -> BaseModel:
    if not path.exists():
        raise ConfigError(f"config file missing: {path}")
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {path}: {exc}") from exc


def get_settings() -> Settings:
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    db_path_override = os.environ.get("CRYPTO_TRADING_DB_PATH_OVERRIDE")
    db_path = (
        Path(db_path_override) if db_path_override else _PROJECT_ROOT / "data" / "crypto_trading.db"
    )
    return Settings(
        db_path=db_path,
        pipeline=_load_yaml_model(_CONFIG_DIR / "pipeline.yaml", PipelineConfig),
        risk_limits=_load_yaml_model(_CONFIG_DIR / "risk_limits.yaml", RiskLimitsConfig),
        budget_limits=_load_yaml_model(_CONFIG_DIR / "budget_limits.yaml", BudgetLimitsConfig),
        notify=_load_yaml_model(_CONFIG_DIR / "notify.yaml", NotifyConfig),
        dashboard=_load_yaml_model(_CONFIG_DIR / "dashboard.yaml", DashboardConfig),
        detective=_load_yaml_model(_CONFIG_DIR / "detective.yaml", DetectiveConfig),
        demo_execution=_load_yaml_model(_CONFIG_DIR / "demo_execution.yaml", DemoExecutionConfig),
    )


def is_demo_execution_enabled() -> bool:
    """Opt-in arm flag for the BingX Demo execution thread - same pattern as
    run.py's existing build_dashboard_app_from_env()/build_notifier_from_env()
    checks (plain os.environ.get(), no load_dotenv() of its own - callers
    always run this after get_settings() has already loaded .env into
    os.environ once for the whole process). Deliberately an env var, not a
    YAML setting: matches how the other optional threads (dashboard,
    Telegram) are gated in this codebase, and keeps "should this thread run
    at all" a deploy-time decision, not a checked-in default."""
    return bool(os.environ.get("CRYPTO_TRADING_DEMO_EXECUTION_ENABLED"))
