"""Real-AI historical replay (2026-09-18, GODFATHER LIVE-readiness
verification, phase 2 of 2 - see full_chain_mock_verification.py for phase
1, the free wiring proof this phase builds on).

REVISED 2026-09-18 (real-budget correction): the user's actual available
Anthropic credit is $5.00 total, not the $80 this script originally
targeted. A first run under the old $40/pass ceiling was killed after
spending $1.69 real USD across 105 real AI calls before the correction
landed - that partial run's DB is preserved at
`backtest_output/full_chain_real_ai/baseline_INTERRUPTED_partial_1.69usd.db`
as a legitimate (if incomplete) real-AI data point, not deleted.

Given $5 total, with $1.69 already spent, a full baseline-vs-GODFATHER
real-money A/B (which would need ~2x a single pass's cost) is not
achievable - running two starved passes would make BOTH less informative
than one adequately-covered pass. Per the user's explicit instruction ("Om
$5 inte räcker för två kompletta 3-veckorspass ska du inte försöka fejka
ett komplett resultat... separera tydligt vad som är komplett från vad som
är ofullständigt"), this script now runs exactly ONE real-AI pass -
GODFATHER-enabled (authority_enabled=True, priority_boost_enabled=True) -
since that is the configuration that actually exercises the thing this
whole exercise is about (the self-improvement pipeline), chronologically
from the start of the same real 21-day window, using real AI exactly where
production would, until the remaining budget is exhausted. The free,
already-complete mock-AI pass (`full_chain_mock_verification.py`) remains
the source of full-21-day, both-configuration MECHANICAL/statistical
pipeline verification; this real-AI pass is a genuine-decision-quality and
real-cost-tracking proof for whatever portion of the window it reaches
before stopping - not a claim of full-window coverage.

Cost control (explicit, corrected user requirement): hard absolute ceiling
$5.00 total (across the killed run + this one, combined), practical stop
at $4.90 for safety margin. `_ALREADY_SPENT_USD = Decimal("1.69")` (the
killed run's real, verified spend, read back from its own preserved DB) is
subtracted up front, so THIS run's own internal ceiling is set so that
(already_spent + this_run's_spend) triggers the stop at exactly $4.90
combined - reusing the EXISTING `sum_ai_cost_since` ledger via
`full_chain_replay.py::historical_replay_budget_exhausted` (no parallel
cost-tracking system, no new ledger). No auto-reload, no additional credit
purchase of any kind - if the ceiling is hit, the run stops, saves
everything already written to its DB, and reports the exact simulated
timestamp it reached; it never fabricates further "results" past that
point.

Cost-efficiency: `budget_limits.yaml`'s real, deployed
`opportunity_screening_enforce` is `false` in production (shadow mode - the
cheap screener call still happens and is logged, but does not actually
reduce how many candidates reach the expensive 7-agent chain). For THIS
budget-constrained real-AI pass only, `opportunity_screening_enforce` is
overridden to `true` - this makes the screener's own cost-optimization
purpose (announced in its own docstring) actually take effect, spending the
scarce real budget on the `max_candidates_for_full_analysis` (2, unchanged)
most promising candidates per tick rather than every prescreened one -
directly serving the user's explicit "prioritera de mest informativa
historiska situationerna... slösa inte krediten på onödiga AI-anrop"
instruction. This is a deliberate, disclosed deviation from the real
deployed config for this one cost-constrained verification run, not a
production config change (production's own `budget_limits.yaml` is
untouched by this script)."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from crypto_trading.agents.runner import RealClaudeRunner
from crypto_trading.backtest.full_chain_data_prep import prepare
from crypto_trading.backtest.full_chain_replay import (
    HistoricalDataSource,
    run_full_chain_historical_replay,
)
from crypto_trading.config.exceptions import ConfigError
from crypto_trading.config.loader import get_settings
from crypto_trading.logging import new_run_id
from crypto_trading.storage.repository import SQLiteRepository

_START = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
_END = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
_CACHE_DIR = Path("backtest_output/full_chain_cache")
_OUTPUT_DIR = Path("backtest_output/full_chain_real_ai")

# Real, verified spend from the killed first attempt (see module docstring) -
# read back from its own preserved DB, not asserted from memory.
_ALREADY_SPENT_USD = Decimal("1.69")
_ABSOLUTE_HARD_CEILING_USD = Decimal("5.00")
_PRACTICAL_STOP_TOTAL_USD = Decimal("4.90")  # combined, across the killed run + this one
_HEADROOM_FRACTION = 0.98  # historical_replay_budget_exhausted's own check multiplies by this

# This run's own internal ceiling, sized so (already_spent + this_run_spend)
# trips the stop at exactly _PRACTICAL_STOP_TOTAL_USD, not a cent more by
# design (small overshoot of one in-flight batch of calls beyond the check
# point is possible, as with any check-then-act gate - see
# historical_replay_budget_exhausted's own docstring - but bounded well
# under the $5.00 absolute ceiling given observed ~$0.11-0.30 per-candidate
# increments from the killed run's own real data).
_THIS_RUN_BUDGET_USD = (_PRACTICAL_STOP_TOTAL_USD - _ALREADY_SPENT_USD) / Decimal(
    str(_HEADROOM_FRACTION)
)

assert _PRACTICAL_STOP_TOTAL_USD <= _ABSOLUTE_HARD_CEILING_USD
assert _ALREADY_SPENT_USD < _PRACTICAL_STOP_TOTAL_USD

_MAIN_MODEL = os.environ.get("CRYPTO_TRADING_CLAUDE_MODEL", "claude-sonnet-5")
_SCREENER_MODEL = os.environ.get("CRYPTO_TRADING_SCREENER_MODEL", "claude-haiku-4-5")
_AGENT_TIMEOUT_SECONDS = float(os.environ.get("CRYPTO_TRADING_AGENT_TIMEOUT_SECONDS", "60"))
_AGENT_MAX_RETRIES = int(os.environ.get("CRYPTO_TRADING_AGENT_MAX_RETRIES", "3"))


def _build_runner(model: str, api_key: str) -> RealClaudeRunner:
    return RealClaudeRunner(
        api_key=api_key,
        model=model,
        timeout_seconds=_AGENT_TIMEOUT_SECONDS,
        max_retries=_AGENT_MAX_RETRIES,
    )


def _cost_events(db_path: Path) -> list[dict]:
    """Every real AI call this pass made, with its real cost and which
    historical decision it was for - read back from the existing,
    unmodified `AI_CALL_MADE` event rows (Repository.record_ai_call_event's
    own write path - no new tracking table). Opens its own short-lived,
    read-only connection to the already-closed-out backtest DB file rather
    than reaching into a live Repository instance's private connection."""
    uri = db_path.resolve().as_uri()
    conn = sqlite3.connect(f"{uri}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT occurred_at, aggregate_type, aggregate_id, payload FROM events "
            "WHERE event_type = 'AI_CALL_MADE' ORDER BY occurred_at"
        ).fetchall()
    finally:
        conn.close()

    events = []
    for row in rows:
        payload = json.loads(row["payload"])
        events.append(
            {
                "occurred_at": row["occurred_at"],
                "aggregate_type": row["aggregate_type"],
                "aggregate_id": row["aggregate_id"],
                "role": payload.get("role"),
                "status": payload.get("status"),
                "cost_usd": payload.get("cost_usd"),
            }
        )
    return events


def _run_pass(label: str, api_key: str) -> dict:
    settings = get_settings()
    settings = settings.model_copy(
        update={
            "guardian": settings.guardian.model_copy(update={"authority_enabled": True}),
            "godfather": settings.godfather.model_copy(update={"priority_boost_enabled": True}),
            "budget_limits": settings.budget_limits.model_copy(
                update={"opportunity_screening_enforce": True}
            ),
        }
    )

    universe, _contracts_raw, dataset, manifest = prepare(_START, _END, _CACHE_DIR)

    source = HistoricalDataSource(dataset, universe)
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db_path = _OUTPUT_DIR / f"{label}.db"
    if db_path.exists():
        raise SystemExit(
            f"refusing to reuse an existing db: {db_path} - remove it first for a fresh run"
        )
    repo = SQLiteRepository(db_path)

    run_id = new_run_id()
    runner = _build_runner(_MAIN_MODEL, api_key)
    screener_runner = _build_runner(_SCREENER_MODEL, api_key)

    print(
        f"=== starting pass {label!r} (GODFATHER-enabled, "
        f"opportunity_screening_enforce=True, this-run budget=${_THIS_RUN_BUDGET_USD}, "
        f"already spent (prior killed run)=${_ALREADY_SPENT_USD}, "
        f"combined practical stop=${_PRACTICAL_STOP_TOTAL_USD}, "
        f"absolute hard ceiling=${_ABSOLUTE_HARD_CEILING_USD}) ===",
        flush=True,
    )

    result = run_full_chain_historical_replay(
        repo, runner, settings, source, _START, _END, run_id,
        screener_runner=screener_runner,
        total_ai_budget_usd=_THIS_RUN_BUDGET_USD,
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

    cost_events = _cost_events(db_path)
    total_cost = sum(
        (Decimal(str(e["cost_usd"])) for e in cost_events if e["cost_usd"]), Decimal("0")
    )
    by_role_count: dict[str, int] = {}
    by_role_cost: dict[str, Decimal] = {}
    for e in cost_events:
        role = e["role"] or "unknown"
        by_role_count[role] = by_role_count.get(role, 0) + 1
        by_role_cost[role] = by_role_cost.get(role, Decimal("0")) + Decimal(str(e["cost_usd"] or 0))

    combined_spend = _ALREADY_SPENT_USD + total_cost

    summary = {
        "label": label,
        "driver_result": result,
        "n_positions_total": len(all_positions),
        "n_positions_open_at_end": n_open,
        "n_positions_closed_by_exit_reason": by_exit_reason,
        "n_ai_calls_total": len(cost_events),
        "this_run_ai_cost_usd": str(total_cost),
        "already_spent_prior_killed_run_usd": str(_ALREADY_SPENT_USD),
        "combined_ai_cost_usd": str(combined_spend),
        "absolute_hard_ceiling_usd": str(_ABSOLUTE_HARD_CEILING_USD),
        "practical_stop_usd": str(_PRACTICAL_STOP_TOTAL_USD),
        "cost_per_trade_usd": (
            str(total_cost / len(all_positions)) if all_positions else None
        ),
        "n_ai_calls_by_role": by_role_count,
        "cost_usd_by_role": {k: str(v) for k, v in by_role_cost.items()},
        "db_path": str(db_path),
        "universe_manifest": manifest,
    }
    (_OUTPUT_DIR / f"{label}_summary.json").write_text(
        json.dumps(summary, indent=2, default=str)
    )
    print(json.dumps(summary, indent=2, default=str), flush=True)
    return summary


def main() -> None:
    settings = get_settings()  # triggers config/loader.py's load_dotenv() as a side effect
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ConfigError("ANTHROPIC_API_KEY saknas - kan inte starta real-AI-replay")
    del settings  # only needed above to trigger dotenv loading

    summary = _run_pass("godfather", api_key=api_key)
    print("=== DONE ===")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
