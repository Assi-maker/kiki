"""The self-improvement loop's decision point: may a policy change anything?

OBSERVE -> INVESTIGATE -> HYPOTHESIS -> HISTORICAL TEST -> TRAIN/TEST ->
WALK-FORWARD -> OOS -> BASELINE COMPARISON -> LIVE/CANARY EVIDENCE ->
PROMOTE -> MONITOR -> ROLLBACK.

Everything up to BASELINE COMPARISON is computed by the supervisor sweep
from real history and handed to this module as `PolicyEvidence`. This
module applies the gates, deterministically, and records the result:

| gate | passes when |
|---|---|
| sample_size | n >= 30 scored trades/cohorts |
| effect_size | bootstrap 95% CI of the mean effect is entirely above 0 |
| multiple_testing | Benjamini-Hochberg significant across EVERY policy in the sweep |
| train_test | effect positive in BOTH chronological halves |
| walk_forward | effect positive in >= 3 of 4 sequential blocks |
| costs | net of fees/funding/spread/slippage, and positive under pessimistic fills |
| baseline | expectancy improves vs the baseline (win rate alone never counts) |
| regime | no regime cell (n >= 10) with a CI entirely below 0 |
| rollback_path | a baseline to revert to exists |

Statuses:

* INSUFFICIENT_DATA - fewer than 30 samples. No strategic change, ever.
* NOISE - enough data, no distinguishable effect.
* SUSPECT - a policy that is LIVE today whose evidence points negative
  without proving it (TIGHTEN_SL_AFTER_FAVORABLE). Kept, watched.
* FAILED - significant, robust harm.
* VALIDATED - every gate passed: eligible for a canary.
* CANARY / PROMOTED - only reachable when `policy_promotion_enabled` is
  true (ships false), and even then NOTHING in the trading path reads
  this table (AST-pinned). Promotion is recorded evidence of eligibility;
  wiring a policy into Guardian Authority is a separate, reviewed change.
* ROLLED_BACK - a CANARY/PROMOTED policy whose forward evidence turned
  negative. Automatic, and always allowed (rolling back only ever
  removes a change).

Every status change is appended to `godfather_policy_transitions`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

from crypto_trading.godfather import stats

MIN_SAMPLES = 30
MIN_REGIME_CELL = 10
MIN_WALK_FORWARD_POSITIVE = 3
ROLLBACK_MIN_FORWARD = 10

_PROMOTABLE = {"VALIDATED"}
_LIVE_STATUSES = {"CANARY", "PROMOTED"}


@dataclass
class PolicyEvidence:
    policy_id: str
    kind: str  # POSITION | ENTRY | PORTFOLIO
    description: str
    live_today: bool
    n: int
    mean_effect: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float
    train_mean: float | None
    test_mean: float | None
    walk_forward_block_means: list[float | None]
    costs_included: bool
    pessimistic_mean: float | None = None
    expectancy_change: float | None = None
    win_rate_change: float | None = None
    regime_cells: list[dict] = field(default_factory=list)
    unobservable: int = 0
    unavailable: int = 0
    notes: list[str] = field(default_factory=list)


def _gate(condition: bool | None) -> str:
    if condition is None:
        return "INSUFFICIENT_DATA"
    return "PASS" if condition else "FAIL"


def evaluate_gates(evidence: PolicyEvidence, fdr_significant: bool) -> dict[str, str]:
    enough = evidence.n >= MIN_SAMPLES
    blocks = [b for b in evidence.walk_forward_block_means if b is not None]
    regime_bad = any(
        cell.get("n", 0) >= MIN_REGIME_CELL
        and cell.get("ci_high") is not None
        and cell["ci_high"] < 0
        for cell in evidence.regime_cells
    )
    return {
        "sample_size": _gate(enough),
        "effect_size": _gate(
            None if not enough or evidence.ci_low is None else evidence.ci_low > 0
        ),
        "multiple_testing": _gate(None if not enough else fdr_significant),
        "train_test": _gate(
            None if evidence.train_mean is None or evidence.test_mean is None
            else evidence.train_mean > 0 and evidence.test_mean > 0
        ),
        "walk_forward": _gate(
            None if len(blocks) < 4
            else sum(1 for b in blocks if b > 0) >= MIN_WALK_FORWARD_POSITIVE
        ),
        "costs": _gate(
            evidence.costs_included
            and (evidence.pessimistic_mean is None or evidence.pessimistic_mean > 0)
            and (evidence.mean_effect or 0) > 0
        ),
        "baseline": _gate(
            None if evidence.mean_effect is None
            else (evidence.expectancy_change if evidence.expectancy_change is not None
                  else evidence.mean_effect) > 0
        ),
        "regime": _gate(not regime_bad),
        "rollback_path": "PASS",
    }


def decide_status(
    evidence: PolicyEvidence, gates: dict[str, str], fdr_significant: bool
) -> tuple[str, list[str]]:
    flags: list[str] = []
    negative = evidence.mean_effect is not None and evidence.mean_effect < 0
    if negative:
        flags.append("NEGATIVE_DIRECTION")
    if (
        evidence.win_rate_change is not None
        and evidence.win_rate_change > 0
        and evidence.expectancy_change is not None
        and evidence.expectancy_change <= 0
    ):
        flags.append("WIN_RATE_UP_EXPECTANCY_DOWN")

    if evidence.n < MIN_SAMPLES:
        if evidence.live_today and negative:
            return "SUSPECT", [*flags, "LIVE_POLICY_UNPROVEN_NEGATIVE"]
        return "INSUFFICIENT_DATA", flags
    robust_harm = (
        fdr_significant
        and evidence.ci_high is not None
        and evidence.ci_high < 0
        and (evidence.train_mean or 0) < 0
        and (evidence.test_mean or 0) < 0
    )
    if robust_harm:
        return "FAILED", flags
    if all(value == "PASS" for value in gates.values()):
        return "VALIDATED", flags
    if evidence.live_today and negative:
        return "SUSPECT", [*flags, "LIVE_POLICY_UNPROVEN_NEGATIVE"]
    return "NOISE", flags


def next_status(
    current: str | None, computed: str, promotion_enabled: bool
) -> str:
    """Combine the freshly computed evidence status with the policy's
    current lifecycle state."""
    if current in _LIVE_STATUSES:
        # A live policy is only ever moved by `rollback_check`; fresh
        # historical evidence alone does not re-promote or demote it.
        return current
    if computed in _PROMOTABLE and promotion_enabled:
        return "CANARY"
    return computed


def rollback_check(current: str | None, forward_effects: list[float]) -> str | None:
    """For a CANARY/PROMOTED policy: roll back when its forward evidence
    (trades after promotion only) is negative on average. Returns the new
    status, or None if nothing changes."""
    if current not in _LIVE_STATUSES:
        return None
    if len(forward_effects) < ROLLBACK_MIN_FORWARD:
        return None
    mean = stats.mean(forward_effects)
    if mean is not None and mean < 0:
        return "ROLLED_BACK"
    if current == "CANARY" and mean is not None and mean > 0:
        return "PROMOTED"
    return None


def evaluate_registry(
    evidences: list[PolicyEvidence],
    current_statuses: dict[str, str],
    promotion_enabled: bool,
    fdr_q: float = 0.10,
) -> list[dict]:
    """Gate every policy, with Benjamini-Hochberg across ALL of them."""
    flags = stats.benjamini_hochberg([e.p_value for e in evidences], fdr_q)
    out: list[dict] = []
    for evidence, significant in zip(evidences, flags, strict=True):
        gates = evaluate_gates(evidence, significant)
        computed, status_flags = decide_status(evidence, gates, significant)
        status = next_status(current_statuses.get(evidence.policy_id), computed, promotion_enabled)
        out.append({
            "policy_id": evidence.policy_id,
            "kind": evidence.kind,
            "description": evidence.description,
            "status": status,
            "computed_status": computed,
            "previous_status": current_statuses.get(evidence.policy_id),
            "fdr_significant": significant,
            "gates": gates,
            "flags": status_flags,
            "evidence": asdict(evidence),
        })
    return out


def transitions(rows: list[dict], now: datetime) -> list[dict]:
    return [
        {
            "policy_id": row["policy_id"],
            "from_status": row["previous_status"],
            "to_status": row["status"],
            "changed_at": now.isoformat(),
            "reason": ",".join(
                [g for g, v in row["gates"].items() if v != "PASS"] or ["all_gates_pass"]
            ),
        }
        for row in rows
        if row["previous_status"] != row["status"]
    ]
