"""GODFATHER self-critique - its own past prediction errors, as context.

For a decision at time T, summarises GODFATHER's EARLIER decisions whose
outcome was already known at T (`known_at <= T`): how far its expected R
was from the realised R (bias, absolute error), how well its win
probability was calibrated, and - for position management - how often
each recommendation turned out right. Overall, and for the decisions that
look like this one (same signal type / evidence status / Guardian state).

It is feedback to weigh, never a filter: the output carries the same
CONTEXT_NOT_RULE role as the evidence and no action field. Pure: rows in,
dict out; the caller loads the rows (evidence_shadow/godfather_shadow.py).
"""

from __future__ import annotations

import statistics as st
from collections.abc import Callable
from datetime import datetime

MIN_N = 5  # below this a subset is shown as "too few" - no numbers to over-read

SELF_CRITIQUE_NOTICE = (
    "Your own track record on decisions whose outcome is already known. "
    "Context for calibration, NOT A RULE: it never blocks a trade or an "
    "action. Small samples are noisy."
)


def _stats(rows: list[dict]) -> dict:
    if len(rows) < MIN_N:
        return {"n": len(rows), "too_few": True}
    exp = [r["expected_r"] for r in rows if r.get("expected_r") is not None]
    act = [r["actual_r"] for r in rows if r.get("expected_r") is not None]
    out: dict = {"n": len(rows)}
    if exp:
        errs = [a - e for a, e in zip(act, exp, strict=True)]
        out["mean_expected_r"] = round(st.mean(exp), 3)
        out["mean_actual_r"] = round(st.mean(act), 3)
        out["bias_actual_minus_expected_r"] = round(st.mean(errs), 3)
        out["mean_abs_error_r"] = round(st.mean(abs(x) for x in errs), 3)
    pw = [(r["p_win"], r["actual_r"] > 0) for r in rows if r.get("p_win") is not None]
    if pw:
        out["mean_p_win"] = round(st.mean(p for p, _ in pw), 3)
        out["realised_win_rate"] = round(sum(w for _, w in pw) / len(pw), 3)
        out["brier"] = round(st.mean((p - w) ** 2 for p, w in pw), 3)
    rec = [r for r in rows if r.get("recommendation")]
    if rec:
        by: dict = {}
        for r in rec:
            by.setdefault(r["recommendation"], []).append(r)
        out["recommendations"] = {
            k: {
                "n": len(v),
                # EXIT was right when holding on lost; any other call was
                # right when holding on did not lose
                "right_rate": round(
                    sum((r["hold_r"] < 0) == (k == "EXIT") for r in v) / len(v), 3
                )
                if all(r.get("hold_r") is not None for r in v)
                else None,
            }
            for k, v in by.items()
        }
    return out


def build_self_critique(
    history: list[dict],
    decision_time: datetime,
    kind: str,
    signal_types: list[str] | None = None,
    primary_status: str | None = None,
    state: str | None = None,
) -> dict:
    """`history`: earlier decisions of this kind with their outcome, each a
    dict with known_at (datetime), kind, expected_r, actual_r and
    optionally p_win, recommendation, hold_r, signal_types, primary_status,
    state. Rows whose outcome was not known at `decision_time` are dropped
    here - whatever the caller passes."""
    known = [
        r for r in history
        if r["kind"] == kind and r["known_at"] <= decision_time and r.get("actual_r") is not None
    ]
    subsets: dict[str, Callable[[dict], bool]] = {}
    if signal_types:
        s = set(signal_types)
        subsets["same_signal_type"] = lambda r: bool(s & set(r.get("signal_types") or []))
    if primary_status:
        subsets["same_evidence_status"] = lambda r: r.get("primary_status") == primary_status
    if state:
        subsets["same_guardian_state"] = lambda r: r.get("state") == state
    return {
        "role": "CONTEXT_NOT_RULE",
        "notice": SELF_CRITIQUE_NOTICE,
        "outcomes_known_before": decision_time.isoformat(),
        "all_decisions": _stats(known),
        **{name: _stats([r for r in known if f(r)]) for name, f in subsets.items()},
    }
