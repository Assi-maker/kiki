"""Discovery-side LIVE capacity + capital gate (2026-09-19, AI-cost
optimization).

Answers ONE question before a discovery tick spends anything (market fetch,
Haiku prescreen, 7-role Sonnet analysis): "how many new LIVE positions could
actually be opened right now?" - from real free slots (reconciled against the
exchange) AND real BingX available margin. The answer becomes an analysis
budget (candidate cap), so credits go where a LIVE position can result.

What this deliberately is NOT:
- Not the final safety check. process_pending_positions() in live_execution.py
  still re-checks capacity, margin, symbol-duplication, signal TTL and
  exchange minimums immediately before EACH order - balance/exchange state can
  change between analysis and execution, and this coarse pre-analysis budget
  never replaces that. Nothing here places, sizes or cancels an order.
- Not a strategy change. It only caps how many already-qualified candidates
  are sent to full AI analysis; Gate, Risk, sizing, leverage, SL/TP and the
  GODFATHER safety rules are untouched.

Capital arithmetic mirrors the execution gate exactly: one position needs
availableMargin >= margin_per_trade + margin_safety_buffer, so n positions
need n * margin_per_trade + buffer (the buffer is a one-off threshold, never
part of an order's size - same rule as LiveExecutionConfig documents).

Debounce: only SUPPRESSED decisions are cached, for `cooldown_seconds`. A
suppression is safe to repeat (it spends nothing); a permissive decision
authorises spending money and therefore always rests on a fresh
reconciliation + balance. Once the cooldown lapses the next evaluation
re-checks the exchange, so discovery resumes on its own when capital or a
slot returns."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from crypto_trading.config.loader import LiveExecutionConfig
from crypto_trading.logging import log_event
from crypto_trading.paper_trading.live_execution import reconcile_active_executions
from crypto_trading.storage.repository import Repository

SUPPRESSED_CAPACITY = "capacity"
SUPPRESSED_CAPITAL = "capital"
SUPPRESSED_CHECK_FAILED = "check_failed"


@dataclass(frozen=True)
class LiveDiscoveryDecision:
    """`max_candidates`: None = no LIVE-derived cap (the normal
    max_candidates_per_discovery_run applies); 0 = do not discover at all;
    N > 0 = at most N candidates may go to full AI analysis this tick."""

    suppressed_reason: str | None
    max_candidates: int | None
    active_count: int | None = None
    free_slots: int | None = None
    affordable_slots: int | None = None
    usable_slots: int | None = None
    available_margin: str | None = None
    from_cache: bool = False


def affordable_live_slots(available_margin: Decimal, cfg: LiveExecutionConfig) -> int:
    """How many new LIVE positions the available margin can fund, under the
    same margin + safety-buffer rule the pre-order gate applies."""
    spendable = available_margin - cfg.margin_safety_buffer_usdt
    if spendable < cfg.margin_per_trade_usdt:
        return 0
    return int(spendable // cfg.margin_per_trade_usdt)


class LiveDiscoveryGate:
    def __init__(self, cooldown_seconds: int = 0):
        self._cooldown_seconds = cooldown_seconds
        self._cached: LiveDiscoveryDecision | None = None
        self._cached_at: datetime | None = None

    def evaluate(
        self,
        repo: Repository,
        live_connector: object,
        market_data_connector: object,
        cfg: LiveExecutionConfig,
        run_id: str,
        now: datetime,
    ) -> LiveDiscoveryDecision:
        """Never raises: any failure (exchange down, unreadable balance
        payload) is a fail-closed suppression, never a permissive guess."""
        if (
            self._cached is not None
            and self._cached_at is not None
            and (now - self._cached_at).total_seconds() < self._cooldown_seconds
        ):
            return replace(self._cached, from_cache=True)

        decision = self._compute(repo, live_connector, market_data_connector, cfg, run_id, now)
        if decision.suppressed_reason is not None:
            self._cached, self._cached_at = decision, now
        else:
            self._cached, self._cached_at = None, None
        return decision

    @staticmethod
    def _compute(
        repo: Repository,
        live_connector: object,
        market_data_connector: object,
        cfg: LiveExecutionConfig,
        run_id: str,
        now: datetime,
    ) -> LiveDiscoveryDecision:
        try:
            active_count = reconcile_active_executions(
                repo, live_connector, market_data_connector, run_id, now
            )
            free_slots = cfg.max_concurrent_positions - active_count
            if free_slots <= 0:
                return LiveDiscoveryDecision(
                    SUPPRESSED_CAPACITY, 0, active_count=active_count, free_slots=0
                )

            balance = live_connector.get_balance()
            available = Decimal(str(balance.get("availableMargin", "0")))
            affordable = affordable_live_slots(available, cfg)
            usable = min(free_slots, affordable)
            common = dict(
                active_count=active_count, free_slots=free_slots,
                affordable_slots=affordable, usable_slots=usable,
                available_margin=str(available),
            )
            if usable <= 0:
                return LiveDiscoveryDecision(SUPPRESSED_CAPITAL, 0, **common)
            if usable >= cfg.max_concurrent_positions:
                # Every slot is free AND affordable: nothing constrains
                # LIVE, so the normal discovery budget applies.
                return LiveDiscoveryDecision(None, None, **common)
            return LiveDiscoveryDecision(
                None, usable + cfg.discovery_candidate_buffer, **common
            )
        except Exception as exc:
            log_event(
                run_id, event="live_discovery_gate_check_failed",
                error_type=type(exc).__name__, error=str(exc),
            )
            return LiveDiscoveryDecision(SUPPRESSED_CHECK_FAILED, 0)
