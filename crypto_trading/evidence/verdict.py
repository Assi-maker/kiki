"""Historical Evidence Layer - pure verdict rules (design:
docs/superpowers/specs/2026-09-30-historical-evidence-layer-design.md).

Zero I/O. Turns per-period statistics of one (signal type, side, regime)
into an honest label. The rules are fixed in advance and deliberately make
it hard to call anything profitable:

- the status is decided on OUT-OF-SAMPLE data only (VALID + TEST + HOLDOUT);
  TRAIN can never raise a status - a TRAIN-only positive is a WARNING flag;
- a positive OOS interval is still only POSITIVE_UNCONFIRMED unless the
  pre-registered protocol (frozen TRAIN -> VALID selection that then passed
  TEST and HOLDOUT) accepted exactly this cell: ~1 600 cells were tested,
  so a few positive intervals are expected by chance;
- VALIDATED_EDGE therefore requires `protocol_accepted=True`.
"""

from __future__ import annotations

MIN_OOS_N = 100
STATUSES = (
    "INSUFFICIENT_DATA",
    "NEGATIVE_OOS",
    "NO_EDGE",
    "POSITIVE_UNCONFIRMED",
    "VALIDATED_EDGE",
)


def oos_status(oos: dict | None, protocol_accepted: bool) -> str:
    if not oos or oos.get("n", 0) < MIN_OOS_N or oos.get("ci_low") is None:
        return "INSUFFICIENT_DATA"
    if oos["ci_high"] < 0:
        return "NEGATIVE_OOS"
    if oos["ci_low"] <= 0:
        return "NO_EDGE"
    return "VALIDATED_EDGE" if protocol_accepted else "POSITIVE_UNCONFIRMED"


def strength(oos: dict | None) -> str:
    if not oos or oos.get("ci_low") is None:
        return "LOW"
    width = oos["ci_high"] - oos["ci_low"]
    if oos["n"] >= 1000 and width <= 0.10:
        return "HIGH"
    if oos["n"] >= 300 and width <= 0.25:
        return "MEDIUM"
    return "LOW"


def vs_baseline(oos: dict | None) -> str:
    if not oos or oos.get("p_diff") is None or oos.get("diff_vs_baseline") is None:
        return "UNKNOWN"
    if oos["p_diff"] >= 0.05:
        return "NOT_DIFFERENT"
    return "BETTER" if oos["diff_vs_baseline"] > 0 else "WORSE"


def train_only_positive(train: dict | None, oos: dict | None) -> bool:
    return bool(
        train
        and train.get("n", 0) >= MIN_OOS_N
        and train.get("mean_r", 0) > 0
        and not (oos and oos.get("n", 0) >= MIN_OOS_N and oos.get("mean_r", 0) > 0)
    )


def headline(status: str, oos: dict | None, cmp: str, strength_: str) -> str:
    if not oos or not oos.get("n"):
        return f"{status}: no out-of-sample observations"
    base = {
        "BETTER": "better than random",
        "WORSE": "worse than random",
        "NOT_DIFFERENT": "not different from random",
        "UNKNOWN": "no random-baseline comparison",
    }[cmp]
    ci = f" [{oos['ci_low']:+.2f}, {oos['ci_high']:+.2f}]" if oos.get("ci_low") is not None else ""
    return (
        f"{status}: OOS {oos['mean_r']:+.3f} R{ci} after costs, n {oos['n']}, "
        f"{base}, {strength_} certainty"
    )


def verdict(periods: dict[str, dict], protocol_accepted: bool = False) -> dict:
    """`periods` maps TRAIN / VALID / TEST / HOLDOUT / OOS -> cell stats."""
    oos = periods.get("OOS")
    status = oos_status(oos, protocol_accepted)
    cmp = vs_baseline(oos)
    strength_ = strength(oos)
    return {
        "oos_status": status,
        "strength": strength_,
        "vs_baseline": cmp,
        "train_only_positive": train_only_positive(periods.get("TRAIN"), oos),
        "protocol_accepted": protocol_accepted,
        "headline": headline(status, oos, cmp, strength_),
    }
