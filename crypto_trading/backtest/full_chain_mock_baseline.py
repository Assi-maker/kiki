"""Companion to full_chain_mock_verification.py: the SAME free (zero
Anthropic cost), MockAgentRunner-driven pass across the SAME real, cached
21-day historical window/universe, but with GODFATHER fully disabled
(guardian.authority_enabled=False, godfather.priority_boost_enabled=False)
- the "bot as it existed before GODFATHER" baseline. Added 2026-09-18 after
discovering neither existing mock pass nor the real-AI pass gave a genuine
baseline-vs-GODFATHER comparison: the original mock-verification pass ran
GODFATHER-enabled only, and the real-AI phase's own baseline attempt was
interrupted by a budget-ceiling correction (see full_chain_real_ai_replay.py's
module docstring) before it produced a comparable dataset.

This gives a complete, real-market-data, structurally faithful 21-day
baseline-vs-GODFATHER comparison at zero additional dollar cost (same
reused historical data cache, no new market-data or AI calls) - the AI
OPINION CONTENT is synthetic/neutral in both mock passes (same fixed
fixtures - see full_chain_mock_verification.py's own docstring for why
that's fine for a mechanical/statistical-machinery verification, not a
decision-quality one), but the deterministic/statistical machinery
(screening, ranking, Gate, PRE_ENTRY_VETO, TIGHTEN_SL/CLOSE_EARLY/
TAKE_PROFIT, SL/TP/time-limit exits, Profit Protection, and - critically for
this comparison - whether GODFATHER's self-improvement pipeline being on at
all changes anything mechanically) is completely real."""

from __future__ import annotations

import json
from pathlib import Path

from crypto_trading.agents.runner import MockAgentRunner
from crypto_trading.backtest.full_chain_data_prep import prepare
from crypto_trading.backtest.full_chain_mock_verification import (
    _CACHE_DIR,
    _END,
    _START,
    _mock_fixtures,
    _ReferencePriceAwareRunner,
)
from crypto_trading.backtest.full_chain_replay import (
    HistoricalDataSource,
    run_full_chain_historical_replay,
)
from crypto_trading.config.loader import get_settings
from crypto_trading.logging import new_run_id
from crypto_trading.storage.repository import SQLiteRepository

_OUTPUT_DIR = Path("backtest_output/full_chain_mock_baseline")


def main() -> None:
    settings = get_settings()
    settings = settings.model_copy(
        update={
            "guardian": settings.guardian.model_copy(update={"authority_enabled": False}),
            "godfather": settings.godfather.model_copy(update={"priority_boost_enabled": False}),
        }
    )

    universe, _contracts_raw, dataset, manifest = prepare(_START, _END, _CACHE_DIR)
    print("universe manifest:", json.dumps(manifest, indent=2))

    source = HistoricalDataSource(dataset, universe)
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db_path = _OUTPUT_DIR / "mock_baseline.db"
    if db_path.exists():
        raise SystemExit(
            f"refusing to reuse an existing db: {db_path} - remove it first for a fresh run"
        )
    repo = SQLiteRepository(db_path)

    run_id = new_run_id()
    runner = _ReferencePriceAwareRunner(MockAgentRunner(fixtures=_mock_fixtures(run_id, _START)))

    result = run_full_chain_historical_replay(
        repo, runner, settings, source, _START, _END, run_id, screener_runner=runner,
    )

    all_positions = repo.find_all_positions(limit=100000)
    by_exit_reason: dict[str, int] = {}
    n_open = 0
    for position in all_positions:
        if position.status == "OPEN_POSITION":
            n_open += 1
        else:
            by_exit_reason[position.exit_reason or "unknown"] = (
                by_exit_reason.get(position.exit_reason or "unknown", 0) + 1
            )

    summary = {
        "driver_result": result,
        "n_positions_total": len(all_positions),
        "n_positions_open_at_end": n_open,
        "n_positions_closed_by_exit_reason": by_exit_reason,
        "db_path": str(db_path),
    }
    (_OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
