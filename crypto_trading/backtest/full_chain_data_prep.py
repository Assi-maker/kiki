"""One-time real historical data preparation for the full-chain replay
(`full_chain_replay.py`). Separated into its own module/CLI (rather than
folded into the replay driver itself) because it is the ONE step here that
makes real network calls (BingX market data - free, no Anthropic cost) and
is naturally run once, ahead of one or more replay passes that then read
the resulting on-disk cache."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.backtest.full_chain_replay import (
    HISTORICAL_UNIVERSE_SIZE,
    prefetch_historical_dataset,
    select_historical_universe,
)
from crypto_trading.config.loader import get_settings
from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector


def prepare(
    start: datetime, end: datetime, cache_dir: Path, universe_size: int = HISTORICAL_UNIVERSE_SIZE
):
    settings = get_settings()
    connector = BingXMarketDataConnector(
        base_url=settings.pipeline.bingx_base_url, timeout_seconds=10.0,
        max_retries=settings.pipeline.bingx_max_retries,
        requests_per_second=settings.pipeline.bingx_requests_per_second,
        cache_ttl_seconds=settings.pipeline.bingx_cache_ttl_seconds,
    )
    universe, contracts_raw = select_historical_universe(connector, settings, size=universe_size)
    dataset = prefetch_historical_dataset(
        connector, universe, contracts_raw, settings, start, end, cache_dir
    )

    manifest = {
        "universe": universe,
        "universe_size": universe_size,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "n_kline_series": len(dataset.klines),
        "n_klines_total": sum(len(v) for v in dataset.klines.values()),
        "n_funding_series": len(dataset.funding),
        "n_funding_total": sum(len(v) for v in dataset.funding.values()),
    }
    (cache_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return universe, contracts_raw, dataset, manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prefetch real historical BingX data for the full-chain replay"
    )
    parser.add_argument("--start", required=True, help="ISO datetime (UTC)")
    parser.add_argument("--end", required=True, help="ISO datetime (UTC)")
    parser.add_argument("--cache-dir", default="backtest_output/full_chain_cache")
    parser.add_argument("--universe-size", type=int, default=HISTORICAL_UNIVERSE_SIZE)
    args = parser.parse_args()

    def _parse(s: str) -> datetime:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    universe, _contracts, _dataset, manifest = prepare(
        _parse(args.start), _parse(args.end), cache_dir, args.universe_size
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
