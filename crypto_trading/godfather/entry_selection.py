"""Entry selection: TAKE / WAIT / REJECT relative to the competition.

The question the existing Entry Quality score answers is absolute: "is
this signal good?" The question that decides profitability when the Gate
confirms several signals at once is relative: "is this signal good
enough compared with the other signals available RIGHT NOW?" In the real
history 36 discovery runs confirmed between 2 and 8 signals at the same
moment, and the system took nearly all of them (144 of 169 confirmed).

This module ranks each confirmed signal inside its cohort - the other
signals confirmed in the same discovery run, which are exactly the
alternatives that existed at that moment and nothing later - and gives:

* REJECT - Entry Quality's own absolute REJECT (a known failure pattern
  or a quality score below its floor). Unchanged.
* TAKE (stored as `TRADE`, the existing schema value) - not rejected and
  in the better half of its cohort by quality score.
* WAIT - not rejected, but a better signal was available at the same time.

**No lookahead.** The quality score is the existing
`entry_quality.assess_entry_quality`, fed Experience Memory AS IT STOOD
before the signal: patterns built only from trades that had closed
before the start of the signal's UTC day. Replaying last week never uses
a trade that closed yesterday.

**Can only subtract.** TAKE never overrides a Gate NO_TRADE; the only
signals scored are ones the Gate already CONFIRMED. The verdict is
advisory (`enforced=False`) until the policy registry validates it.

The absolute score cut points in `entry_quality.py` are NOT calibrated and
this module does not calibrate them; it uses the score only to rank
within a cohort, and whether that ranking pays is measured in
`evaluate_selection`, on real outcomes, chronologically split.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from crypto_trading.godfather import stats

_ZERO = Decimal("0")


@dataclass
class EntrySignal:
    candidate_id: str
    instrument: str
    discovery_run_id: str
    decided_at: datetime
    quality_score: float
    absolute_verdict: str
    candidate_score: float
    theme: str
    position_id: str | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    realized_pnl: Decimal | None = None
    regime: str = "unknown"
    reason_codes: list[str] = field(default_factory=list)
    cohort_size: int = 1
    cohort_rank: int = 1
    selection_verdict: str = "TRADE"
    portfolio_verdict: str = "KEEP"
    # The EntryQualityAssessment this signal was scored with (persisted by
    # the supervisor); not used by any evaluation here.
    assessment: Any = None

    @property
    def scored(self) -> bool:
        return self.realized_pnl is not None


def assign_selection(signals: list[EntrySignal]) -> None:
    """Rank inside each discovery-run cohort and set `selection_verdict`.
    Ties break on the Gate's own candidate score, then id, so the ranking
    is deterministic."""
    cohorts: dict[str, list[EntrySignal]] = {}
    for signal in signals:
        cohorts.setdefault(signal.discovery_run_id, []).append(signal)
    for members in cohorts.values():
        members.sort(key=lambda s: (-s.quality_score, -s.candidate_score, s.candidate_id))
        take = max(1, math.ceil(len(members) / 2))
        for rank, signal in enumerate(members, start=1):
            signal.cohort_size = len(members)
            signal.cohort_rank = rank
            if signal.absolute_verdict == "REJECT":
                signal.selection_verdict = "REJECT"
            elif rank <= take:
                signal.selection_verdict = "TRADE"
            else:
                signal.selection_verdict = "WAIT"


def _floats(values: list[Decimal]) -> list[float]:
    return [float(v) for v in values]


def _group(signals: list[EntrySignal]) -> dict:
    pnls = [s.realized_pnl for s in signals if s.realized_pnl is not None]
    n = len(pnls)
    return {
        "n": n,
        "total_pnl_usdt": str(sum(pnls, _ZERO)),
        "mean_pnl_usdt": float(sum(pnls, _ZERO) / n) if n else None,
        "win_rate": (sum(1 for p in pnls if p > _ZERO) / n) if n else None,
    }


def two_group_test(a: list[Decimal], b: list[Decimal]) -> dict:
    fa, fb = _floats(a), _floats(b)
    ci = stats.bootstrap_diff_ci(fa, fb)
    return {
        "n_a": len(a),
        "n_b": len(b),
        "mean_diff_usdt": (sum(fa) / len(fa) - sum(fb) / len(fb)) if fa and fb else None,
        "ci_low_usdt": ci.lower if ci else None,
        "ci_high_usdt": ci.upper if ci else None,
        "p_value": stats.permutation_diff_p_value(fa, fb),
    }


def within_cohort_effects(signals: list[EntrySignal]) -> list[tuple[datetime, float]]:
    """Per cohort with at least one scored signal on EACH side of the
    TAKE line: mean real P/L of its TAKE signals minus mean of its
    non-TAKE ones. This is the purest form of the relative question -
    same moment, same market, only the ranking differs."""
    cohorts: dict[str, list[EntrySignal]] = {}
    for signal in signals:
        if signal.scored:
            cohorts.setdefault(signal.discovery_run_id, []).append(signal)
    effects: list[tuple[datetime, float]] = []
    for members in cohorts.values():
        top = [float(s.realized_pnl) for s in members if s.selection_verdict == "TRADE"]
        rest = [float(s.realized_pnl) for s in members if s.selection_verdict != "TRADE"]
        if top and rest:
            effects.append((
                min(s.decided_at for s in members),
                sum(top) / len(top) - sum(rest) / len(rest),
            ))
    effects.sort(key=lambda item: item[0])
    return effects


def evaluate_selection(signals: list[EntrySignal], cut: datetime) -> dict:
    """What the selection rule would have done to the REAL book.

    Only signals that were actually opened (with exposure and a known
    P/L) can be scored; confirmed-but-never-opened signals are counted
    separately as UNAVAILABLE outcomes rather than assumed flat.
    """
    scored = [s for s in signals if s.scored]
    take = [s for s in scored if s.selection_verdict == "TRADE"]
    rest = [s for s in scored if s.selection_verdict != "TRADE"]
    effects = within_cohort_effects(signals)
    effect_values = [value for _moment, value in effects]
    ci = stats.bootstrap_mean_ci(effect_values)
    train = [v for moment, v in effects if moment < cut]
    test = [v for moment, v in effects if moment >= cut]
    blocks = stats.sequential_blocks(effect_values, 4)
    return {
        "confirmed_signals": len(signals),
        "scored_signals": len(scored),
        "unavailable_outcomes": len(signals) - len(scored),
        "by_verdict": {
            verdict: _group([s for s in scored if s.selection_verdict == verdict])
            for verdict in ("TRADE", "WAIT", "REJECT")
        },
        "take_vs_rest": two_group_test(
            [s.realized_pnl for s in take], [s.realized_pnl for s in rest]
        ),
        "within_cohort": {
            "cohorts": len(effect_values),
            "mean_effect_usdt": stats.mean(effect_values),
            "ci_low_usdt": ci.lower if ci else None,
            "ci_high_usdt": ci.upper if ci else None,
            "p_value": stats.sign_flip_p_value(effect_values),
            "train_mean_usdt": stats.mean(train),
            "train_n": len(train),
            "test_mean_usdt": stats.mean(test),
            "test_n": len(test),
            "walk_forward_block_means": [stats.mean(block) for block in blocks],
            "values": effect_values,
            "moments": [moment.isoformat() for moment, _v in effects],
        },
        "book_if_only_take_usdt": str(sum((s.realized_pnl for s in take), _ZERO)),
        "book_actual_usdt": str(sum((s.realized_pnl for s in scored), _ZERO)),
        "trades_avoided": len(rest),
    }
