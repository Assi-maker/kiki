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


def _ro(path: str | Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


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
    size = float(pos["size"])
    final = (
        (float(pos["simulated_fill_exit"]) - float(pos["simulated_fill_entry"])) * size
        - float(pos["fees"] or 0)
        - float(pos["funding"] or 0)
    )
    path = [float(r["unrealized_pnl"]) for r in rows] + [final]
    return {
        "at_obs": at_obs,
        "final": final,
        "mfe_after": max(path) - at_obs,
        "mae_after": min(path) - at_obs,
    }


def guardian_section(bot: sqlite3.Connection, sh: sqlite3.Connection) -> dict:
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
    arms: dict = {"without": defaultdict(list), "with": defaultdict(list)}
    for r in both:
        o = _pnl_after(bot, r["position_id"], r["observed_at"])
        if o is None:
            continue
        hold_gain = o["final"] - o["at_obs"]
        for arm, rec in (("without", r["rec_without"]), ("with", r["rec_with"])):
            a = arms[arm]
            a["counterfactual_pnl"].append(o["at_obs"] if rec == "EXIT" else o["final"])
            a["false_exit"].append(rec == "EXIT" and hold_gain > 0)
            a["missed_exit"].append(rec != "EXIT" and hold_gain < 0)
            a["mfe_after"].append(o["mfe_after"])
            a["mae_after"].append(o["mae_after"])
            a["exit_share"].append(rec == "EXIT")
    for arm, a in arms.items():
        n = len(a["counterfactual_pnl"])
        out[arm] = {
            "n_with_outcome": n,
            "mean_counterfactual_pnl_usdt": round(st.mean(a["counterfactual_pnl"]), 3)
            if n
            else None,
            "total_counterfactual_pnl_usdt": round(sum(a["counterfactual_pnl"]), 2) if n else None,
            "false_exit_rate": round(sum(a["false_exit"]) / n, 3) if n else None,
            "missed_exit_rate": round(sum(a["missed_exit"]) / n, 3) if n else None,
            "exit_recommendation_share": round(sum(a["exit_share"]) / n, 3) if n else None,
            "mean_mfe_after_usdt": round(st.mean(a["mfe_after"]), 3) if n else None,
            "mean_mae_after_usdt": round(st.mean(a["mae_after"]), 3) if n else None,
        }
    return out


def candidate_section(bot: sqlite3.Connection, sh: sqlite3.Connection) -> dict:
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
        if pos and pos["status"] == "CLOSED" and pos["simulated_fill_exit"] is not None:
            pnl = (
                (float(pos["simulated_fill_exit"]) - float(pos["simulated_fill_entry"]))
                * float(pos["size"])
                - float(pos["fees"] or 0)
                - float(pos["funding"] or 0)
            )
            g["pnl"].append(pnl)
    out = {}
    for status, g in sorted(groups.items()):
        p = g["pnl"]
        out[status] = {
            "candidates": len(g["n"]),
            "confirmed": sum(g["confirmed"]),
            "live": sum(g["live"]),
            "closed_paper_trades": len(p),
            "mean_pnl_usdt": round(st.mean(p), 3) if p else None,
            "total_pnl_usdt": round(sum(p), 2) if p else None,
            "win_rate": round(sum(x > 0 for x in p) / len(p), 3) if p else None,
        }
    neg = out.get("NEGATIVE_OOS", {})
    total_live = sum(v["live"] for v in out.values())
    out["_counterfactual_if_negative_oos_were_skipped_NOT_APPLIED"] = {
        "live_trades_removed": neg.get("live", 0),
        "live_frequency_change_share": round(-neg.get("live", 0) / total_live, 3)
        if total_live
        else None,
        "paper_pnl_removed_usdt": neg.get("total_pnl_usdt"),
    }
    return out


def main() -> None:
    s = get_settings()
    bot, sh = _ro(s.db_path), _ro(s.evidence.shadow_db)
    report = {"guardian_ab": guardian_section(bot, sh), "candidates": candidate_section(bot, sh)}
    print(json.dumps(report, indent=1))
    Path("data/entry_research/evidence_ab_report.json").write_text(
        json.dumps(report, indent=1), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
