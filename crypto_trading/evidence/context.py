"""GODFATHER evidence context - what history says, as known at the decision.

One builder for every AI reader (the Guardian AI explanation behind the
bot's flag, and the GODFATHER shadow decisions): for each signal type the
candidate had at its decision time, the evidence of the newest snapshot
with as_of <= the decision time, in the fields a decision-maker needs:

    signal type, side, the regime the candidate was in, OOS expectancy in R
    after costs with its 95 % interval, sample size, win rate, MFE / MAE,
    comparison with random entries, status, certainty - and a plain notice
    that this is CONTEXT, NOT A RULE.

Pure and read-only: it reads an `EvidenceReader` (mode=ro) and returns a
JSON-safe dict. It has no action field, imports no decision module, and
cannot open, close, size, veto or move anything (test_godfather_evidence.py
pins this by AST).
"""

from __future__ import annotations

from datetime import datetime

from crypto_trading.evidence import store

CONTEXT_NOTICE = (
    "Historical evidence is CONTEXT, NOT A RULE. It describes how similar "
    "signals did in the past on data known at this decision. It never "
    "rejects a trade, sizes it, moves a stop or overrides Guardian or the "
    "Safety Kernel. Weigh it against the current information; a single "
    "position can differ from the historical average."
)

# The horizon the research's main verdict uses (fixed stop / target / time
# limit, like the bot's own exits) - the others are shown as horizons.
PRIMARY_MODEL = "FIXED"


def _r(x: object, digits: int = 3) -> float | None:
    return round(float(x), digits) if isinstance(x, (int, float)) else None


def _cell(c: dict | None) -> dict | None:
    if not c:
        return None
    return {
        "n": c.get("n"),
        "expectancy_r_after_costs": _r(c.get("mean_r")),
        "ci95_r": [_r(c.get("ci_low")), _r(c.get("ci_high"))],
        "win_rate": _r(c.get("win_rate")),
        "mfe_r": _r(c.get("mfe_r")),
        "mae_r": _r(c.get("mae_r")),
        "hold_minutes": _r(c.get("hold_min"), 0),
        "random_baseline_r": _r(c.get("baseline_mean_r")),
        "diff_vs_random_r": _r(c.get("diff_vs_baseline")),
    }


def signal_evidence(pkg: store.EvidencePackage | None) -> dict:
    """One signal type's evidence in decision-maker terms."""
    if pkg is None:
        return {"available": False, "reason": "no evidence snapshot existed at decision time"}
    v = pkg.overall.get("verdict") or {}
    oos = (pkg.overall.get("periods") or {}).get("OOS")
    regimes = {}
    for key, x in pkg.regimes.items():
        rv = x.get("verdict") or {}
        regimes[key] = {
            "status": rv.get("oos_status"),
            "certainty": rv.get("strength"),
            "oos": _cell(x.get("oos")),
        }
    return {
        "available": True,
        "signal_type": pkg.signal_type,
        "side": pkg.side,
        "status": v.get("oos_status"),
        "certainty": v.get("strength"),
        "vs_random_baseline": v.get("vs_baseline"),
        "train_only_positive_warning": bool(v.get("train_only_positive")),
        "validated_by_protocol": bool(v.get("protocol_accepted")),
        "headline": v.get("headline"),
        "oos": _cell(oos),
        "oos_by_horizon_r": {
            m: _r(s.get("mean_r")) if s else None for m, s in pkg.horizons.items()
        },
        "in_regime_at_decision": regimes,
    }


def build_evidence_context(
    reader: store.EvidenceReader | None,
    signal_types: list[str],
    side: str,
    regimes: dict[str, str | None],
    decision_time: datetime,
    classified_for: datetime | None = None,
) -> dict:
    """The context for one decision. `classified_for` is the decision time
    the classification was made for; a classification made for a LATER time
    is refused (it could describe the future)."""
    if classified_for is not None and classified_for > decision_time:
        return {
            "role": "CONTEXT_NOT_RULE",
            "available": False,
            "reason": "classification is later than this decision - not used",
        }
    if reader is None or not signal_types:
        return {
            "role": "CONTEXT_NOT_RULE",
            "available": False,
            "reason": "no classification or no evidence store",
        }
    signals, as_of, statuses = [], None, []
    for typ in signal_types:
        pkg = reader.lookup(
            "BASELINE" if typ == "NO_EVENT" else typ, side, decision_time, regimes
        )
        e = signal_evidence(pkg)
        if typ == "NO_EVENT":
            e["note"] = "no research event fired - compared with random entries"
        signals.append(e)
        if pkg is not None:
            as_of = pkg.snapshot["as_of"]
            statuses.append(e["status"])
    order = ("NEGATIVE_OOS", "NO_EDGE", "INSUFFICIENT_DATA", "POSITIVE_UNCONFIRMED",
             "VALIDATED_EDGE")
    primary = min(
        (s for s in statuses if s), key=lambda s: order.index(s) if s in order else 99,
        default=None,
    )
    return {
        "role": "CONTEXT_NOT_RULE",
        "notice": CONTEXT_NOTICE,
        "available": as_of is not None,
        "evidence_as_of": as_of,
        "side": side,
        "regime_at_decision": {k: v for k, v in regimes.items() if v is not None},
        "primary_status": primary,
        "signals": signals,
    }
