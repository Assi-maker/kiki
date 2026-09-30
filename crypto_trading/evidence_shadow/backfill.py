"""Evidence A/B backfill from existing history (shadow only, one-off).

    python -m crypto_trading.evidence_shadow.backfill [--since ISO] [--workers N]

The live shadow loop only saw what happened after it started. Everything
the experiment needs already exists for every closed position since
2026-09-01, each piece stamped with the time it became known:

- candidates.created_at (decision time) + Binance 5m bars / funding / OI on
  disk (local_data.LocalSource) -> the classification AT that time, with the
  classifier's own cut-offs (closed bars <= T, funding settled <= T,
  OI <= T);
- evidence snapshots -> the newest one with as_of <= the decision /
  observation (store.EvidenceReader.lookup);
- guardian_observations -> the deterministic state, factors, decay,
  progress and unrealised P/L AT the tick. The Guardian AI context is built
  from these only (the bot's own build_ai_context) - no later tick, no
  close, no outcome is read before the recommendation is logged.

So the A/B rows are the same rows the live loop would have written had it
been running then. The AI arms are asked fresh (the shadow agent's answer
cannot be recovered from the bot's historical AI text, which explains an
already decided state and does not recommend). Nothing is written to the
bot database; the running bot is not touched.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from crypto_trading.agents.loader import load_agent_definition
from crypto_trading.config.loader import get_settings
from crypto_trading.evidence import store
from crypto_trading.evidence_shadow import service as sv
from crypto_trading.evidence_shadow.local_data import LocalSource

SINCE = "2026-09-01T00:00:00+00:00"


def _arg(name: str, default: str) -> str:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def classify_all(bot, sh, clf, reader, since: str) -> dict:
    """Re-classify every candidate since `since` from local data. Rows the
    API-based live loop already wrote are compared first (same source,
    same code: they must agree)."""
    before = {
        r[0]: (json.loads(r[1] or "[]"), json.loads(r[2] or "{}"))
        for r in sh.execute(
            "SELECT candidate_id, signal_types, regimes FROM candidate_evidence WHERE error IS NULL"
        )
    }
    sv._set_state(sh, "candidates_watermark", since)
    t0, n = time.time(), 0
    while True:
        k = sv.classify_new_candidates(bot, sh, clf, reader, limit=100)
        n += k
        if k:
            print(f"  classified {n} ({time.time() - t0:.0f}s)", flush=True)
        if k < 100:
            break
    after = {
        r[0]: (json.loads(r[1] or "[]"), json.loads(r[2] or "{}"))
        for r in sh.execute("SELECT candidate_id, signal_types, regimes FROM candidate_evidence")
    }
    common = [c for c in before if c in after]
    same_sig = sum(sorted(before[c][0]) == sorted(after[c][0]) for c in common)
    same_reg = sum(before[c][1] == after[c][1] for c in common)
    diffs = [
        {"candidate_id": c, "api": before[c], "local": after[c]}
        for c in common
        if before[c] != after[c]
    ]
    return {
        "classified": n,
        "seconds": round(time.time() - t0),
        "api_fallback_symbol_fetches": getattr(clf.source, "api_fallbacks", None),
        "api_vs_local_compared": len(common),
        "same_signal_types": same_sig,
        "same_regimes": same_reg,
        "differences": diffs[:10],
    }


def guardian_all(bot, sh, repo, reader, since: str, workers: int) -> dict:
    agent = load_agent_definition(sv.AGENT_FILE)
    done = {
        r[0]
        for r in sh.execute(
            "SELECT observation_id FROM guardian_ab WHERE error IS NULL"
            " AND rec_without IS NOT NULL AND rec_with IS NOT NULL"
        )
    }
    fired = [o for o, fires in sv.eligible_observations(bot, since, limit=10**7) if fires]
    # A/A replicate on every AA_EVERY-th eligible observation in time order -
    # fixed before any answer is seen, independent of the outcome
    todo = [(i, o) for i, o in enumerate(fired) if o["observation_id"] not in done]
    preps = [(i, sv.prepare_ab(sh, repo, reader, o)) for i, o in todo]  # sqlite: main thread
    local = threading.local()

    def runner():
        if not hasattr(local, "r"):
            local.r = sv.build_runner()
        return local.r

    t0, n, calls = time.time(), 0, 0
    now = datetime.now(UTC)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [
            ex.submit(lambda p=p, i=i: sv.ask_ab(runner(), agent, p, i % sv.AA_EVERY == 0))
            for i, p in preps
        ]
        for f in as_completed(futs):
            r = f.result()
            sv.write_ab(sh, r, now)
            n += 1
            calls += 2 + (r["a2"] is not None)
            if n % 25 == 0:
                sh.commit()
                print(f"  A/B {n}/{len(preps)} ({time.time() - t0:.0f}s)", flush=True)
    sh.commit()
    if fired:
        sv._set_state(sh, "guardian_watermark", fired[-1]["observed_at"])
    sv._set_state(
        sh, "backfill_ai_calls", str(calls + int(sv._state(sh, "backfill_ai_calls", "0")))
    )
    return {
        "eligible_transitions": len(fired),
        "already_done": len(fired) - len(todo),
        "evaluated_now": n,
        "ai_calls_now": calls,
        "seconds": round(time.time() - t0),
    }


def main() -> None:
    load_dotenv(Path(".env"), override=False)
    since = _arg("--since", SINCE)
    workers = int(_arg("--workers", "6"))
    s = get_settings()
    sh = sv.open_shadow_db(s.evidence.shadow_db)
    bot = sv._bot_ro(s.db_path)
    repo = sv.read_only_repository(s.db_path)
    reader = store.EvidenceReader(Path(s.evidence.evidence_db))
    src = LocalSource()
    t0 = time.time()
    gap_rows = src.fill_gaps(int(time.time()))
    print(f"gap fill: {gap_rows} rows ({time.time() - t0:.0f}s)", flush=True)
    clf = sv.Classifier(sv.universe_from_archive(), src)
    out = {"since": since, "gap_rows_added": gap_rows}
    if "--skip-classify" not in sys.argv:
        out["classification"] = classify_all(bot, sh, clf, reader, since)
        print(json.dumps(out["classification"], indent=1), flush=True)
    if "--godfather" in sys.argv:
        from crypto_trading.evidence_shadow import godfather_shadow as gf

        gctx = gf.make_ctx(s)
        out["godfather"] = gf.backfill(gctx, since, workers)
        gf.refresh_facts(gctx, s)
        gctx["sh"].commit()
        sv._set_state(sh, "godfather_watermark", max(
            (r[0] for r in gctx["sh"].execute("SELECT decided_at FROM godfather_decisions")),
            default=since,
        ))
        print(json.dumps(out["godfather"], indent=1), flush=True)
    if "--skip-guardian" not in sys.argv and "--godfather" not in sys.argv:
        out["guardian_ab"] = guardian_all(bot, sh, repo, reader, since, workers)
        print(json.dumps(out["guardian_ab"], indent=1), flush=True)
    Path("data/entry_research/evidence_backfill.json").write_text(
        json.dumps(out, indent=1, default=str), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
