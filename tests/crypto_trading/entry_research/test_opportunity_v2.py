from datetime import UTC, datetime, timedelta

from crypto_trading.entry_research.opportunity_v2 import select

T0 = datetime(2026, 9, 20, tzinfo=UTC)


def _c(run, sym, score, minutes, no_mom=False):
    return {
        "discovery_run_id": run,
        "symbol": sym,
        "t0": T0 + timedelta(minutes=minutes),
        "feat": {"candidate_score": score},
        "no_mom": no_mom,
    }


def _runs(cs):
    d = {}
    for c in cs:
        d.setdefault(c["discovery_run_id"], []).append(c)
    return d


def test_select_takes_top_k_per_run_by_the_key():
    runs = _runs([_c("r1", s, sc, 0) for s, sc in (("A", 1), ("B", 5), ("C", 3), ("D", 4))])
    assert [c["symbol"] for c in select(runs, "candidate_score", k=2)] == ["B", "D"]


def test_diverse_skips_a_symbol_picked_within_two_hours_and_fills_with_the_next():
    runs = _runs(
        [_c("r1", "A", 9, 0), _c("r2", "A", 9, 60), _c("r2", "B", 1, 60), _c("r3", "A", 9, 180)]
    )
    picked = [
        (c["discovery_run_id"], c["symbol"])
        for c in select(runs, "candidate_score", diverse=True, k=1)
    ]
    assert picked == [("r1", "A"), ("r2", "B"), ("r3", "A")]


def test_no_momentum_candidates_are_skipped_only_when_asked():
    runs = _runs([_c("r1", "A", 9, 0, no_mom=True), _c("r1", "B", 1, 0)])
    assert [c["symbol"] for c in select(runs, "candidate_score", k=1)] == ["A"]
    assert [c["symbol"] for c in select(runs, "candidate_score", skip_no_mom=True, k=1)] == ["B"]
