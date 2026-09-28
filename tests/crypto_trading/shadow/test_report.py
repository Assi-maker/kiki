"""Shadow report statistics (2026-09-28): nothing is called an EDGE because a
small sample happens to look good."""
import random

from crypto_trading.shadow.report import benjamini_hochberg, build_report, classify


def test_edge_requires_size_significance_and_oos_agreement():
    assert classify(n=29, q=0.001, oos_same_sign=True) == "INSUFFICIENT_DATA"
    assert classify(n=200, q=0.20, oos_same_sign=True) == "NOISE"
    assert classify(n=200, q=0.01, oos_same_sign=False) == "NOT_CONFIRMED_OOS"
    assert classify(n=200, q=0.01, oos_same_sign=None) == "NOT_CONFIRMED_OOS"
    assert classify(n=200, q=0.01, oos_same_sign=True) == "EDGE"


def test_benjamini_hochberg():
    q = benjamini_hochberg([0.01, 0.04, 0.03, 0.20])
    assert [round(x, 3) for x in q] == [0.04, 0.053, 0.053, 0.2]


def _record(i, r, flag, cohort="IN_SAMPLE_HISTORICAL"):
    return {
        "candidate_id": f"c{i}", "decided_at": f"2026-09-{1 + i % 27:02d}T12:00:00+00:00",
        "cohort": cohort, "gate_outcome": "CONFIRMED", "trigger_reasons": ["volume" if i % 2 else "momentum_breakout"],
        "candidate_score": i / 100, "features": {"risk_reward": "1.5", "forecast_uncertainty": 0.95,
                                                 "symbol_vol_1h_pct": 0.2, "volume_zscore": 1.0},
        "veto_flags": {"BEARISH_DOMINANT": flag, "ALT_LONG_WHILE_BTC_FALLING": None},
        "outcome": {"r": r, "mfe_pct": 1.0, "mae_pct": -1.0},
        "variants": {"BE_1.0": {"r": r + ((i % 5) - 2) * 0.1}, "TRAIL_1.0_0.5": {"r": r}},
        "live": None,
    }


def test_a_pure_noise_sample_yields_no_edge_and_reports_every_section():
    random.seed(1)
    records = [_record(i, random.gauss(0, 1), bool(i % 3)) for i in range(60)]
    report = build_report(records)
    assert "EDGE |" not in report  # no row classified EDGE
    for section in ("Veto rules", "Trigger types", "Continuous features", "Break-even", "Trailing",
                    "GODFATHER"):
        assert section in report
    assert "n=60" in report


def test_a_real_consistent_improvement_is_detected_as_edge():
    """Sanity: the machinery CAN find an edge when one really exists."""
    random.seed(2)
    records = [_record(i, random.gauss(0, 1), False) for i in range(80)]
    for r in records:
        r["variants"]["BE_1.0"] = {"r": r["outcome"]["r"] + 0.2}
    report = build_report(records)
    be_row = next(line for line in report.splitlines() if line.startswith("| BE_1.0"))
    assert be_row.endswith("| EDGE |")
