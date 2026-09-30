"""A/B report: does historical evidence improve GODFATHER's decisions? (research only)

    python -m crypto_trading.entry_research.evidence_ab_report

Reads the bot DB and the shadow DB READ-ONLY. Nothing here changes a
decision; it measures whether evidence WOULD have helped, and says so only
when the effect exceeds the AI's own noise.

Guardian A/B (table guardian_ab, one row per Guardian state transition):
- changed rate: recommendation WITHOUT vs WITH evidence differ;
- A/A noise: WITHOUT vs a second WITHOUT call (every 4th row) - the rate at
  which the AI changes its mind with NO new information. Only the part of
  the changed rate above this is attributable to evidence;
- counterfactual P/L per arm: EXIT = realise the unrealised P/L at the
  observation; any other recommendation = the position's actual later
  outcome (PROTECT/WATCH are treated as holding - a limit of this design);
- MFE / MAE after the observation (from later Guardian observations);
- false EXIT (exit recommended, holding would have gained) and missed EXIT
  (no exit recommended, holding lost), per arm.

Candidate evidence (table candidate_evidence):
- per primary evidence status: candidates, CONFIRMED, LIVE, closed paper
  trades, mean / total realised P/L, win rate;
- counterfactual only: how LIVE frequency and P/L WOULD have changed had
  NEGATIVE_OOS candidates been skipped. This is reported, never applied.
"""

from __future__ import annotations

import json
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

from crypto_trading.config.loader import get_settings
from crypto_trading.entry_research.stats import cluster_t, t_sf


def _ro(path: str | Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _paper_pnl(pos: sqlite3.Row) -> float:
    """paper_trading.execution.compute_pnl: `size` is the NOTIONAL in USDT,
    so P/L = size x price return - fees - funding (the same units as the
    Guardian's unrealised P/L)."""
    entry = float(pos["simulated_fill_entry"])
    return (
        float(pos["size"]) * (float(pos["simulated_fill_exit"]) - entry) / entry
        - float(pos["fees"] or 0)
        - float(pos["funding"] or 0)
    )


def _paper_risk(pos: sqlite3.Row) -> float:
    """Planned risk in USDT: notional x distance to the stop the position
    was opened with. positions.stop_loss moves only through a Guardian
    Authority TIGHTEN_SL, which is always recorded in
    guardian_authority_decisions (empty as of 2026-09-30), so the stored
    stop is the planned one."""
    entry = float(pos["simulated_fill_entry"])
    return float(pos["size"]) * abs(entry - float(pos["stop_loss"])) / entry


def _pnl_after(bot: sqlite3.Connection, position_id: str, observed_at: str) -> dict | None:
    """Unrealised P/L at the observation, final P/L, and MFE/MAE after it,
    from the position's own later Guardian observations and its close."""
    rows = bot.execute(
        "SELECT observed_at, unrealized_pnl FROM guardian_observations WHERE position_id = ?"
        " AND observed_at >= ? ORDER BY observed_at",
        (position_id, observed_at),
    ).fetchall()
    pos = bot.execute("SELECT * FROM positions WHERE position_id = ?", (position_id,)).fetchone()
    if not rows or pos is None or pos["status"] != "CLOSED" or pos["simulated_fill_exit"] is None:
        return None
    at_obs = float(rows[0]["unrealized_pnl"])
    final, risk = _paper_pnl(pos), _paper_risk(pos)
    if not risk:
        return None  # zero-size position: no exposure, no outcome to measure
    path = [float(r["unrealized_pnl"]) for r in rows] + [final]
    return {
        "at_obs": at_obs,
        "final": final,
        "mfe_after": max(path) - at_obs,
        "mae_after": min(path) - at_obs,
        "risk_usdt": risk,
    }


def verified_outcomes(db_path: str | Path) -> dict[str, dict]:
    """Per closed position: was the booked paper exit confirmed on exchange
    candles (godfather.book: MATCH), and the R of the ACTUAL position
    against the planned risk when that outcome is usable evidence. 39 of
    the early paper exits were booked after an outage at the wrong price
    (Fas 2A.1), so every outcome-based number is also reported on verified
    positions only."""
    from crypto_trading.godfather.book import load_book
    from crypto_trading.storage.repository import SQLiteRepository

    # the bot's own read methods on a mode=ro connection (__init__ would
    # run init_schema, a write, so it is bypassed)
    repo = SQLiteRepository.__new__(SQLiteRepository)
    repo._conn = _ro(db_path)
    try:
        book = load_book(repo, risk_limits=get_settings().risk_limits)
    except Exception as e:  # noqa: BLE001 - the report still runs without it
        print(f"verified outcomes unavailable: {type(e).__name__}: {e}", file=sys.stderr)
        return {}
    return {
        t.position.position_id: {
            "verdict": t.kline_verdict,
            "verified": t.kline_verdict == "MATCH",
            "r": float(t.outcome_r) if t.outcome_r is not None else None,
        }
        for t in book
    }


def _wilson(k: int, n: int) -> list[float] | None:
    if not n:
        return None
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(c - h, 3), round(c + h, 3)]


def _binom_two_sided(b: int, c: int) -> float:
    """Exact McNemar p for b vs c discordant pairs."""
    from math import comb

    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(k + 1)) / 2**n
    return round(min(1.0, 2 * tail), 4)


# Pre-registered: no conclusion about evidence is drawn below these sizes.
MIN_AB_WITH_OUTCOME = 100
MIN_AA = 30
MIN_CLOSED_TRADES = 100


def _mean(xs: list[float]) -> float | None:
    return round(st.mean(xs), 4) if xs else None


def _rate(xs: list[bool]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def _cluster_ci(values: list[float], clusters: list) -> dict | None:
    """Mean with a 95 % CI clustered per position (several decisions on the
    same position are not independent) and a two-sided p."""
    if len(values) < 2:
        return None
    t, g = cluster_t(values, [str(c) for c in clusters])
    m = st.mean(values)
    se = abs(m / t) if t else None
    p = 2 * t_sf(abs(t), g - 1) if g > 1 and t else 1.0
    return {
        "mean": round(m, 4),
        "ci95": [round(m - 1.96 * se, 4), round(m + 1.96 * se, 4)] if se else None,
        "p_two_sided": round(p, 4),
        "clusters": g,
    }


def guardian_section(
    bot: sqlite3.Connection, sh: sqlite3.Connection, verified: dict | None = None
) -> dict:
    verified = verified or {}
    rows = sh.execute("SELECT * FROM guardian_ab WHERE error IS NULL").fetchall()
    both = [r for r in rows if r["rec_without"] and r["rec_with"]]
    aa = [r for r in both if r["rec_without_replicate"]]
    out: dict = {
        "evaluated": len(rows),
        "with_both_answers": len(both),
        "evidence_available": sum(r["evidence_available"] for r in both),
        "changed_rate": round(sum(r["changed"] for r in both) / len(both), 3) if both else None,
        "aa_noise_rate": round(
            sum(r["rec_without"] != r["rec_without_replicate"] for r in aa) / len(aa), 3
        )
        if aa
        else None,
        "aa_n": len(aa),
    }
    if out["changed_rate"] is not None and out["aa_noise_rate"] is not None:
        out["changed_above_noise"] = round(out["changed_rate"] - out["aa_noise_rate"], 3)
    out["changed_rate_ci95"] = _wilson(sum(r["changed"] for r in both), len(both))
    n_noise = sum(r["rec_without"] != r["rec_without_replicate"] for r in aa)
    out["aa_noise_rate_ci95"] = _wilson(n_noise, len(aa))
    # Paired on the A/A rows (same context, same moment): does adding
    # evidence change the answer more often than simply asking again?
    b_ = sum(r["rec_with"] != r["rec_without"] and r["rec_without_replicate"] == r["rec_without"]
             for r in aa)
    c_ = sum(r["rec_with"] == r["rec_without"] and r["rec_without_replicate"] != r["rec_without"]
             for r in aa)
    out["evidence_vs_replicate_mcnemar"] = {
        "only_evidence_changed": b_,
        "only_replicate_changed": c_,
        "p_two_sided": _binom_two_sided(b_, c_),
    }
    out["recommendation_mix"] = {
        arm: {k: sum(r[col] == k for r in both) for k in ("HOLD", "WATCH", "PROTECT", "EXIT")}
        for arm, col in (("without", "rec_without"), ("with", "rec_with"))
    }
    arms: dict = {"without": defaultdict(list), "with": defaultdict(list)}
    paired: dict = defaultdict(list)  # per observation: with - without
    for r in both:
        o = _pnl_after(bot, r["position_id"], r["observed_at"])
        if o is None:
            continue
        hold_gain = o["final"] - o["at_obs"]
        cf = {}
        for arm, rec in (("without", r["rec_without"]), ("with", r["rec_with"])):
            a = arms[arm]
            cf[arm] = o["at_obs"] if rec == "EXIT" else o["final"]
            a["cf_usdt"].append(cf[arm])
            a["cf_r"].append(cf[arm] / o["risk_usdt"])
            a["false_exit"].append(rec == "EXIT" and hold_gain > 0)
            a["missed_exit"].append(rec != "EXIT" and hold_gain < 0)
            a["exit_share"].append(rec == "EXIT")
            if rec == "EXIT":  # upside given away by exiting
                a["mfe_after_exit"].append(o["mfe_after"])
            else:  # drawdown sat through by holding
                a["mae_after_hold"].append(o["mae_after"])
        paired["verified"].append(bool(verified.get(r["position_id"], {}).get("verified")))
        paired["pos"].append(r["position_id"])
        paired["d_usdt"].append(cf["with"] - cf["without"])
        paired["d_r"].append((cf["with"] - cf["without"]) / o["risk_usdt"])
        wrong = {arm: (arms[arm]["false_exit"][-1] or arms[arm]["missed_exit"][-1]) for arm in arms}
        paired["only_without_wrong"].append(wrong["without"] and not wrong["with"])
        paired["only_with_wrong"].append(wrong["with"] and not wrong["without"])
    for arm, a in arms.items():
        n = len(a["cf_usdt"])
        out[arm] = {
            "n_with_outcome": n,
            "expectancy_usdt": _mean(a["cf_usdt"]),
            "expectancy_r": _mean(a["cf_r"]),
            "total_usdt": round(sum(a["cf_usdt"]), 2) if n else None,
            "false_exit_rate": _rate(a["false_exit"]),
            "missed_exit_rate": _rate(a["missed_exit"]),
            "exit_recommendation_share": _rate(a["exit_share"]),
            "mean_mfe_after_exit_usdt": _mean(a["mfe_after_exit"]),
            "mean_mae_after_hold_usdt": _mean(a["mae_after_hold"]),
        }
    n = len(paired["d_usdt"])
    out["with_minus_without"] = {
        "n": n,
        "positions": len(set(paired["pos"])),
        "expectancy_usdt": _cluster_ci(paired["d_usdt"], paired["pos"]),
        "expectancy_r": _cluster_ci(paired["d_r"], paired["pos"]),
        # discordant errors: the arm that is wrong ALONE more often is worse
        "only_without_wrong": sum(paired["only_without_wrong"]),
        "only_with_wrong": sum(paired["only_with_wrong"]),
    }
    keep = [i for i, v in enumerate(paired["verified"]) if v]
    out["with_minus_without_verified_exits_only"] = {
        "n": len(keep),
        "positions": len({paired["pos"][i] for i in keep}),
        "expectancy_usdt": _cluster_ci([paired["d_usdt"][i] for i in keep],
                                       [paired["pos"][i] for i in keep]),
        "expectancy_r": _cluster_ci([paired["d_r"][i] for i in keep],
                                    [paired["pos"][i] for i in keep]),
    }
    out["sufficient_data"] = {
        "required": {"ab_rows_with_outcome": MIN_AB_WITH_OUTCOME, "aa_pairs": MIN_AA},
        "have": {"ab_rows_with_outcome": n, "aa_pairs": len(aa)},
        "met": n >= MIN_AB_WITH_OUTCOME and len(aa) >= MIN_AA,
    }
    return out


def candidate_section(
    bot: sqlite3.Connection, sh: sqlite3.Connection, verified: dict | None = None
) -> dict:
    verified = verified or {}
    ev = {r["candidate_id"]: r for r in sh.execute("SELECT * FROM candidate_evidence")}
    groups: dict = defaultdict(lambda: defaultdict(list))
    live_ids = {
        r[0]
        for r in bot.execute(
            "SELECT position_id FROM live_executions WHERE exchange_fill_entry IS NOT NULL"
        )
    }
    for cid, e in ev.items():
        c = bot.execute("SELECT status FROM candidates WHERE candidate_id = ?", (cid,)).fetchone()
        if c is None:
            continue
        status = e["primary_status"] or ("CLASSIFY_ERROR" if e["error"] else "NO_EVIDENCE")
        g = groups[status]
        g["n"].append(1)
        g["confirmed"].append(c["status"] == "CONFIRMED")
        pos = bot.execute("SELECT * FROM positions WHERE candidate_id = ?", (cid,)).fetchone()
        g["live"].append(bool(pos and pos["position_id"] in live_ids))
        if (
            pos
            and pos["status"] == "CLOSED"
            and pos["simulated_fill_exit"] is not None
            and _paper_risk(pos)  # zero-size: no exposure, not a trade outcome
        ):
            pnl, risk = _paper_pnl(pos), _paper_risk(pos)
            g["pnl"].append(pnl)
            g["r"].append(pnl / risk)
            g["day"].append(pos["opened_at"][:10])
            v = verified.get(pos["position_id"], {})
            if v.get("verified") and v.get("r") is not None:
                g["r_verified"].append(v["r"])
                g["day_verified"].append(pos["opened_at"][:10])
    out: dict = {}
    for status, g in sorted(groups.items()):
        p = g["pnl"]
        out[status] = {
            "candidates": len(g["n"]),
            "confirmed": sum(g["confirmed"]),
            "live": sum(g["live"]),
            "closed_paper_trades": len(p),
            "expectancy_usdt": _mean(p),
            "expectancy_r": _cluster_ci(g["r"], g["day"]) if len(p) >= 2 else None,
            "total_pnl_usdt": round(sum(p), 2) if p else None,
            "win_rate": _rate([x > 0 for x in p]),
            "verified_trades": len(g["r_verified"]),
            "expectancy_r_verified": _cluster_ci(g["r_verified"], g["day_verified"])
            if len(g["r_verified"]) >= 2
            else None,
        }
    # The evidence status as a (hypothetical) warning label on closed trades:
    # a NEGATIVE_OOS trade that WON is a false alarm; a non-negative one that
    # LOST is a missed warning.
    neg_r = groups.get("NEGATIVE_OOS", {}).get("r", [])
    rest_r = [x for s_, g in groups.items() if s_ != "NEGATIVE_OOS" for x in g.get("r", [])]
    all_r = neg_r + rest_r
    out["_evidence_label_quality"] = {
        "false_alarm_rate": _rate([x > 0 for x in neg_r]),
        "missed_warning_rate": _rate([x <= 0 for x in rest_r]),
        "expectancy_r_negative_oos": _mean(neg_r),
        "expectancy_r_other": _mean(rest_r),
    }
    total_live = sum(v["live"] for v in out.values() if isinstance(v, dict) and "live" in v)
    neg = out.get("NEGATIVE_OOS", {})
    out["_counterfactual_if_negative_oos_were_skipped_NOT_APPLIED"] = {
        "live_trades_removed": neg.get("live", 0),
        "live_frequency_change_share": round(-neg.get("live", 0) / total_live, 3)
        if total_live
        else None,
        "paper_trades_removed": len(neg_r),
        "paper_pnl_removed_usdt": neg.get("total_pnl_usdt"),
        "expectancy_r_all": _mean(all_r),
        "expectancy_r_if_filtered": _mean(rest_r),
    }
    neg_v = groups.get("NEGATIVE_OOS", {}).get("r_verified", [])
    rest_v = [
        x for s_, g in groups.items() if s_ != "NEGATIVE_OOS" for x in g.get("r_verified", [])
    ]
    out["_counterfactual_verified_exits_only_NOT_APPLIED"] = {
        "trades": len(neg_v) + len(rest_v),
        "negative_oos_trades": len(neg_v),
        "expectancy_r_negative_oos": _mean(neg_v),
        "expectancy_r_other": _mean(rest_v),
        "expectancy_r_all": _mean(neg_v + rest_v),
        "expectancy_r_if_filtered": _mean(rest_v),
    }
    out["_sufficient_data"] = {
        "required_closed_trades": MIN_CLOSED_TRADES,
        "have": len(all_r),
        "met": len(all_r) >= MIN_CLOSED_TRADES,
    }
    return out


def main() -> None:
    s = get_settings()
    bot, sh = _ro(s.db_path), _ro(s.evidence.shadow_db)
    verified = verified_outcomes(s.db_path)
    report = {
        "verified_positions": sum(v["verified"] for v in verified.values()),
        "guardian_ab": guardian_section(bot, sh, verified),
        "candidates": candidate_section(bot, sh, verified),
    }
    print(json.dumps(report, indent=1))
    Path("data/entry_research/evidence_ab_report.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
