"""P5 trend strategy CLI.

    python -m crypto_trading.trend run       # one idempotent daily rebalance (mode from config)
    python -m crypto_trading.trend status    # config, last runs, equity curve, live account (read-only)
    python -m crypto_trading.trend signal    # today's target weights, no trading, no state

Scheduled hourly by Task Scheduler (crypto_trading_trend); every run after
the first one of a UTC day is a no-op, so a sleeping PC catches up later.
Exit codes: 0 ok / already done, 1 failed (logged + stored in runs).
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from crypto_trading.config.loader import get_settings, is_safe_mode
from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
from crypto_trading.trend.engine import closed_day, daily_closes, load_config, run_once
from crypto_trading.trend.signal import target_weights
from crypto_trading.trend.store import TrendStore

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "trend.db"
LOG_PATH = ROOT / "logs" / "trend.log"


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stdout))
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _market() -> BingXMarketDataConnector:
    settings = get_settings()
    return BingXMarketDataConnector(
        base_url=settings.pipeline.bingx_base_url,
        timeout_seconds=15.0,
        max_retries=settings.pipeline.bingx_max_retries,
        requests_per_second=settings.pipeline.bingx_requests_per_second,
        cache_ttl_seconds=0.0,
    )


def _trader():
    from crypto_trading.run import build_live_trading_connector_from_env

    return build_live_trading_connector_from_env()


def cmd_run() -> int:
    log = logging.getLogger("crypto_trading.trend")
    cfg = load_config()
    market = _market()  # get_settings() has loaded .env from here on
    trader = _trader() if cfg.mode == "LIVE" else None
    store = TrendStore(DB_PATH)
    try:
        result = run_once(cfg, market, store, datetime.now(UTC), trader=trader, safe_mode=is_safe_mode())
    except Exception:
        log.exception("trend run failed")
        return 1
    finally:
        store.close()
    log.info("trend run %s %s %s weights=%s equity=%s trades=%s %s", result.mode, result.day, result.status,
             {k: round(v, 4) for k, v in result.weights.items()}, result.equity, result.trades, result.detail)
    return 0


def cmd_signal() -> int:
    cfg = load_config()
    market = _market()
    now = datetime.now(UTC)
    closes = {s: daily_closes(market, s, int(now.timestamp() * 1000))[0] for s in cfg.assets}
    w = target_weights(closes, cfg)
    print(f"closed day {closed_day(now)}  mode {cfg.mode}")
    for s in cfg.assets:
        print(f"  {s:10s} close {closes[s][-1]:>12.2f}  weight {w[s]:.3f}")
    print(f"  gross exposure {sum(w.values()):.3f} x equity")
    return 0


def cmd_status() -> int:
    get_settings()  # loads .env, so SAFE_MODE and the credentials are visible below
    cfg = load_config()
    store = TrendStore(DB_PATH)
    print(f"mode {cfg.mode}  assets {cfg.assets}  lookbacks {cfg.lookbacks_days}  safe_mode {is_safe_mode()}")
    for row in store.last_runs(10):
        print("  run", row[:4], (row[4] or "")[:160])
    for mode in ("PAPER", "LIVE"):
        curve = store.equity_curve(mode)
        if curve:
            first, last = curve[0][1], curve[-1][1]
            print(f"  {mode} equity {first:.2f} -> {last:.2f} ({last / first - 1:+.2%}) over {len(curve)} days")
    store.close()
    trader = _trader()
    if trader is not None:
        bal = trader.get_balance()
        print(f"  LIVE account equity {bal.get('equity')} available {bal.get('availableMargin')}")
        for p in trader.get_all_positions():
            print(f"  LIVE position {p.get('symbol')} {p.get('positionSide')} {p.get('positionAmt')}")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "run":
        _setup_logging()
        return cmd_run()
    if cmd == "signal":
        return cmd_signal()
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
