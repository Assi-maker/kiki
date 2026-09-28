"""GODFATHER entry-pattern learning (2026-09-28) - evidence only.

Once a day GODFATHER re-derives the entry pattern categories from everything
the shadow Entry Quality Layer has recorded, with the SAME pre-registered
three-stage protocol as crypto_trading.entry_research.patterns, on a rolling
time split (oldest 50 % TRAIN, next 25 % VALID, newest 25 % TEST), and stores
a snapshot in godfather_entry_patterns next to each pattern's previous
category - so an edge that fades shows up as EDGE -> DECAYING_EDGE/NOISE.

Categories: EDGE, WEAK_EDGE, HYPOTHESIS, REGIME_DEPENDENT, NOISE,
FAILURE_PATTERN, FAILURE_HYPOTHESIS, DECAYING_EDGE. A category is never
assigned from a small sample: the protocol requires n >= 30 in TRAIN and
labels anything short of n >= 30 in VALID/TEST as a HYPOTHESIS.

It never changes the frozen registry, a heuristic, a position or an order.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from crypto_trading.entry_research import patterns
from crypto_trading.entry_research.stats import summarize
from crypto_trading.logging import log_event

MIN_RECORDS = 300
POSITIVE = {"EDGE", "WEAK_EDGE", "HYPOTHESIS"}


def _rows(records: list[dict]) -> list[dict]:
    out = []
    for r in records:
        if not r.get("independent") or not r.get("outcome"):
            continue
        out.append({"t0": datetime.fromisoformat(r["t0"]), "feat": r["features"],
                    "outcomes": {"primary": r["outcome"]}})
    return sorted(out, key=lambda x: x["t0"])


def derive(records: list[dict]) -> list[dict] | None:
    rows = _rows(records)
    if len(rows) < MIN_RECORDS:
        return None
    a, b = len(rows) // 2, (3 * len(rows)) // 4
    train, valid, test = rows[:a], rows[a:b], rows[b:]
    cuts = patterns.tertile_cuts(train)
    selected, n_tested = patterns.discover(train, cuts)
    items = patterns.evaluate(selected, valid, test)
    out = []
    for i in items:
        def s(part, i=i):
            recs = [{**r["outcomes"]["primary"], "t0": r["t0"]} for r in i[part]["rows"]]
            return summarize(recs) if recs else {"n": 0}
        out.append({
            "pattern": patterns.label(i["pattern"]), "side": i["side"], "category": i["class"],
            "n_total": i["train"]["n"] + i["valid"]["n"] + i["test"]["n"],
            "stats": {"train": s("train"), "valid": s("valid"), "test": s("test"),
                      "train_family_q": i["train_family_q"], "valid_q": i["valid_q"], "test_q": i["test_q"],
                      "patterns_tested": n_tested, "split": [str(train[0]["t0"]), str(valid[0]["t0"]),
                                                             str(test[0]["t0"]), str(test[-1]["t0"])]},
        })
    return out


def learn(repo, now: datetime) -> int:
    derived = derive(repo.list_entry_quality())
    if derived is None:
        log_event("godfather", event="godfather_entry_patterns_skipped", reason="fewer than %d records" % MIN_RECORDS)
        return 0
    previous = repo.latest_godfather_entry_patterns()
    for d in derived:
        prev = previous.get((d["pattern"], d["side"]))
        d["previous_category"] = prev
        if prev in POSITIVE and d["category"] in ("NOISE", "DECAYING_EDGE"):
            d["category"] = "DECAYING_EDGE"
    repo.save_godfather_entry_patterns(uuid.uuid4().hex, now, derived)
    counts: dict[str, int] = {}
    for d in derived:
        counts[d["category"]] = counts.get(d["category"], 0) + 1
    log_event("godfather", event="godfather_entry_patterns_learned", patterns=len(derived), categories=counts)
    return len(derived)
