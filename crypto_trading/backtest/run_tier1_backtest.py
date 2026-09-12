from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.backtest.dataset import select_backtest_targets
from crypto_trading.backtest.replay_engine import replay_position
from crypto_trading.backtest.report import build_tier1_report
from crypto_trading.config.loader import Settings, get_settings
from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.storage.repository import Repository, SQLiteRepository


def run_tier1_backtest(
    source_repo: Repository, connector, settings: Settings,
    split_cutoff: datetime, output_dir: Path,
) -> dict:
    """Read-only against source_repo. Writes ONLY to two fresh, disposable
    backtest DB files under output_dir - never to data/crypto_trading.db.
    Every position is replayed into exactly ONE of train.db/test.db,
    decided once, before any replay happens, by
    target.opened_at < split_cutoff - physically separate databases, not
    a filter applied after the fact, so out-of-sample truly cannot leak
    into training results by construction (Global Constraints)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = output_dir / "historical_data_cache"
    train_repo = SQLiteRepository(output_dir / "train.db")
    test_repo = SQLiteRepository(output_dir / "test.db")
    run_id = new_run_id()

    targets = select_backtest_targets(source_repo)
    n_skipped = 0
    # Final whole-branch review, Important Fix 4: every exception during
    # replay_position lands in `n_positions_skipped_due_to_fetch_error`,
    # a name that specifically claims "the exchange was unavailable" - but
    # a genuine logic bug (a ValueError, a KeyError, an AssertionError)
    # is counted exactly the same way and is therefore indistinguishable
    # from routine network flakiness in the report. This list records what
    # actually went wrong per position so a real bug can't hide behind
    # that count's name.
    skipped_positions: list[dict] = []
    for target in targets:
        destination = train_repo if target.opened_at < split_cutoff else test_repo
        try:
            replay_position(target, connector, source_repo, destination, settings, cache_dir, run_id)
        except Exception as exc:
            log_event(
                run_id, event="replay_position_failed", position_id=target.position_id,
                instrument=target.instrument, error_type=type(exc).__name__, error=str(exc),
            )
            n_skipped += 1
            skipped_positions.append({
                "position_id": target.position_id,
                "instrument": target.instrument,
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
            continue

    report = build_tier1_report(train_repo, test_repo, source_repo, targets)
    report["split_cutoff"] = split_cutoff.isoformat()
    report["n_positions_total"] = len(targets)
    report["n_positions_train"] = sum(1 for t in targets if t.opened_at < split_cutoff)
    report["n_positions_test"] = sum(1 for t in targets if t.opened_at >= split_cutoff)
    # Kept as-is for backward compatibility with this plan's own report
    # consumers; `skipped_positions` is the field that says WHY.
    report["n_positions_skipped_due_to_fetch_error"] = n_skipped
    report["skipped_positions"] = skipped_positions

    (output_dir / "tier1_report.json").write_text(json.dumps(report, indent=2, default=str))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Profit Protection Tier 1 historical replay")
    parser.add_argument(
        "--split-cutoff", required=True,
        help="ISO datetime (UTC) - positions opened before this go to train, on/after go to test",
    )
    parser.add_argument("--output-dir", default="backtest_output/tier1")
    args = parser.parse_args()

    settings = get_settings()
    source_repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    connector = BingXMarketDataConnector(
        base_url=settings.pipeline.bingx_base_url, timeout_seconds=10.0,
        max_retries=settings.pipeline.bingx_max_retries,
        requests_per_second=settings.pipeline.bingx_requests_per_second,
        cache_ttl_seconds=settings.pipeline.bingx_cache_ttl_seconds,
    )
    split_cutoff = datetime.fromisoformat(args.split_cutoff)
    if split_cutoff.tzinfo is None:
        split_cutoff = split_cutoff.replace(tzinfo=UTC)

    report = run_tier1_backtest(source_repo, connector, settings, split_cutoff, Path(args.output_dir))
    print(json.dumps(
        {"n_positions_total": report["n_positions_total"],
         "n_positions_train": report["n_positions_train"],
         "n_positions_test": report["n_positions_test"],
         "n_positions_skipped_due_to_fetch_error": report["n_positions_skipped_due_to_fetch_error"],
         "baseline_parity_mismatches": len(report["baseline_parity_mismatches"])},
        indent=2,
    ))


if __name__ == "__main__":
    main()
