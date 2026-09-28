"""Entry Quality Layer - SHADOW ONLY (2026-09-28).

Classifies every discovered candidate STRONG / ACCEPTABLE / WEAK / REJECT from
the FROZEN registry (config/entry_quality_registry.json) and logs why, which
features were used, the historical evidence behind the matched patterns, and -
once its 6 h window has passed - what actually happened.

It never gates, sizes, blocks or touches anything: it holds only the public
read-only market-data connector and writes only entry_quality_shadow. No
LIVE code imports it. Promotion to LIVE would need >= 30 forward observations
per class and an explicit human decision - nothing here does that.

Classification (evidence level is logged next to the class):
  REJECT     - matches a FAILURE_PATTERN or FAILURE_HYPOTHESIS
  STRONG     - matches a validated EDGE (none exist in the registry today)
  ACCEPTABLE - matches a WEAK_EDGE or HYPOTHESIS pattern
  WEAK       - matches nothing: the baseline, which is negative expectancy
A candidate that matches both a failure and an edge pattern is REJECT: the
Safety-first reading of conflicting hypotheses.
"""
from __future__ import annotations

import statistics as st
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from crypto_trading.entry_research import dataset, experience, patterns, registry
from crypto_trading.entry_research.klines import KlineCache
from crypto_trading.entry_research.stats import summarize
from crypto_trading.logging import log_event, new_run_id

READY_AFTER = timedelta(minutes=dataset.PRIMARY_LATENCY_MIN + dataset.HORIZON_MIN + 15)
KLINE_CACHE = Path(__file__).resolve().parents[2] / "data" / "entry_research" / "klines.db"
FAILURE = {"FAILURE_PATTERN", "FAILURE_HYPOTHESIS"}
EVIDENCE_LEVEL = {"EDGE": "VALIDATED", "WEAK_EDGE": "WEAK", "HYPOTHESIS": "HYPOTHESIS",
                  "FAILURE_PATTERN": "VALIDATED", "FAILURE_HYPOTHESIS": "HYPOTHESIS"}


def _cond(c: list) -> tuple:
    f, op, lo, hi = c
    return (f, op, lo, hi)


def classify(feat: dict, reg: dict) -> dict:
    matched = [p for p in reg["patterns"] if all(patterns.holds(_cond(c), feat) for c in p["conditions"])]
    fail = [p for p in matched if p["class"] in FAILURE]
    edge = [p for p in matched if p["class"] == "EDGE"]
    acceptable = [p for p in matched if p["class"] in ("WEAK_EDGE", "HYPOTHESIS")]
    if fail:
        eq, basis = "REJECT", fail
    elif edge:
        eq, basis = "STRONG", edge
    elif acceptable:
        eq, basis = "ACCEPTABLE", acceptable
    else:
        eq, basis = "WEAK", []
    used = sorted({c[0] for p in reg["patterns"] for c in p["conditions"]})
    return {
        "eq_class": eq,
        "evidence_level": min((EVIDENCE_LEVEL[p["class"]] for p in basis), default="BASELINE",
                              key=["VALIDATED", "WEAK", "HYPOTHESIS", "BASELINE"].index),
        "why": [f"{p['class']}: {p['label']}" for p in basis] or ["no registry pattern matched (baseline)"],
        "matched_ids": [p["id"] for p in matched],
        "evidence": {p["id"]: p["evidence"] for p in basis},
        "features_used": {f: feat.get(f) for f in used},
    }


def evaluate(row: dict, reg: dict, frozen_at: datetime, independent: bool) -> dict:
    result = classify(row["feat"], reg)
    std = row["outcomes"].get("primary")
    return {
        "candidate_id": row["candidate_id"], "symbol": row["symbol"], "t0": row["t0"].isoformat(),
        "cohort": "FORWARD_OOS" if row["t0"] >= frozen_at else "HISTORICAL_IN_SAMPLE",
        "registry_version": reg["version"], "independent": independent,
        "pipeline_cohort": row["cohort"], **result,
        "features": row["feat"], "outcome": std, "fast_outcome": row["outcomes"].get("fast"),
        "fwd": row.get("fwd"), "own_bracket": row.get("own"), "live": row.get("live"),
        "experience": experience.chain_record(row),
    }


def run_tick(repo, db_path: str, cache: KlineCache, reg: dict, now: datetime, limit: int = 200) -> int:
    run_id = new_run_id()
    repo.start_run(run_id, "entry_quality_shadow", now)
    frozen_at = datetime.fromisoformat(reg["frozen_at"])
    ids = repo.find_candidates_without_entry_quality(now - READY_AFTER, limit)
    done, errors = 0, []
    for row in dataset.load_rows(db_path, ids) if ids else []:
        try:
            independent = not repo.had_candidate_on_symbol_within(
                row["symbol"], row["t0"] - timedelta(hours=dataset.INDEPENDENCE_HOURS), row["t0"],
                exclude=row["candidate_id"])
            dataset.enrich(row, cache)
            repo.save_entry_quality(evaluate(row, reg, frozen_at, independent), now)
            done += 1
        except Exception as exc:  # noqa: BLE001 - one candidate never stops the batch
            errors.append(f"{row['candidate_id']}: {type(exc).__name__}: {exc}")
    log_event(run_id, event="entry_quality_shadow_tick", classified=done, errors=len(errors))
    repo.complete_run(run_id, datetime.now(UTC), "ok" if not errors else "partial_error", errors[:20])
    return done


def run_forever(repo, connector, db_path: str, interval_seconds: int = 1800) -> None:
    from crypto_trading.godfather import entry_patterns

    cache = KlineCache(KLINE_CACHE, connector)
    reg = registry.load()
    last_learn = None
    while True:
        now = datetime.now(UTC)
        try:
            while run_tick(repo, db_path, cache, reg, now) >= 200:  # historical backfill in chunks
                pass
        except Exception as exc:  # noqa: BLE001
            log_event("entry_quality", event="entry_quality_tick_failed", error_type=type(exc).__name__,
                      error=str(exc)[:300])
        if last_learn is None or now - last_learn >= timedelta(hours=24):
            try:
                entry_patterns.learn(repo, now)
            except Exception as exc:  # noqa: BLE001
                log_event("entry_quality", event="godfather_entry_patterns_failed",
                          error_type=type(exc).__name__, error=str(exc)[:300])
            last_learn = now
        time.sleep(interval_seconds)


# ------------------------------------------------------------------ report

def forward_report(records: list[dict]) -> dict:
    """Per class: forward OOS vs historical, independent candidates only."""
    out = {}
    for cohort in ("HISTORICAL_IN_SAMPLE", "FORWARD_OOS"):
        per = {}
        for eq in ("STRONG", "ACCEPTABLE", "WEAK", "REJECT"):
            recs = [{**r["outcome"], "t0": datetime.fromisoformat(r["t0"])} for r in records
                    if r["cohort"] == cohort and r["eq_class"] == eq and r["independent"] and r.get("outcome")]
            per[eq] = summarize(recs) if recs else {"n": 0}
        out[cohort] = per
    fwd = [r for r in records if r["cohort"] == "FORWARD_OOS" and r["independent"] and r.get("outcome")]
    rej = [r["outcome"]["r"] for r in fwd if r["eq_class"] == "REJECT"]
    kept = [r["outcome"]["r"] for r in fwd if r["eq_class"] != "REJECT"]
    out["forward_reject_vs_rest"] = {
        "n_reject": len(rej), "n_rest": len(kept),
        "mean_reject": st.mean(rej) if rej else None, "mean_rest": st.mean(kept) if kept else None,
        "enough_for_review": len(rej) >= 30 and len(kept) >= 30,
    }
    return out


def main() -> None:
    import argparse
    import json

    from crypto_trading.config.loader import get_settings
    from crypto_trading.storage.repository import SQLiteRepository

    ap = argparse.ArgumentParser(description="Entry Quality Layer shadow report (read-only)")
    ap.parse_args()
    repo = SQLiteRepository(get_settings().db_path)
    print(json.dumps(forward_report(repo.list_entry_quality()), indent=1, default=str))


if __name__ == "__main__":
    main()
