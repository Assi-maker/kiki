from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

from crypto_trading.config.exceptions import ConfigError

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "crypto_trading" / "config"


class PipelineConfig(BaseModel):
    discovery_interval_minutes: int = Field(gt=0)
    monitoring_interval_seconds: int = Field(gt=0)
    top_n: int = Field(gt=0)
    cooldown_minutes: int = Field(gt=0)
    max_data_age_seconds: dict[str, int]
    min_sample_size_for_calibration: int = Field(gt=0)
    calibration_preliminary_sample_size: int = Field(gt=0)
    sqlite_busy_timeout_ms: int = Field(gt=0)


class RiskLimitsConfig(BaseModel):
    starting_capital_usdt: Decimal = Field(gt=0)
    risk_per_trade_pct: Decimal = Field(gt=0, le=1)
    max_concurrent_positions: int = Field(gt=0)
    max_total_exposure_pct: Decimal = Field(gt=0, le=1)
    spread_pct: Decimal = Field(ge=0)
    slippage_pct: Decimal = Field(ge=0)
    fee_pct: Decimal = Field(ge=0)


class BudgetLimitsConfig(BaseModel):
    max_candidates_per_discovery_run: int = Field(gt=0)
    max_ai_calls_per_discovery_run: int = Field(gt=0)
    max_ai_calls_per_day: int = Field(gt=0)
    warning_threshold_pct: Decimal = Field(gt=0, le=1)


class Settings(BaseModel):
    db_path: Path
    pipeline: PipelineConfig
    risk_limits: RiskLimitsConfig
    budget_limits: BudgetLimitsConfig


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
    )
