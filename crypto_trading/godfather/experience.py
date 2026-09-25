"""GODFATHER Experience Memory: what has actually worked, and how sure are we?

This is the module the user's closing instruction is about - "the bot
must know how much is good and how much is bad; it must not learn noise
or bad things and adjust itself into being worse". Everything here exists
to make it HARD to call something an edge.

A pattern gets an `edge_class`, and the only way to reach `EDGE` is to
clear every one of these, in order:

1. **Sample size.** `n >= min_sample_size` (default 30, the same floor
   `guardian/authority.py::_MIN_SAMPLE_SIZE` already uses). Below it the
   verdict is `INSUFFICIENT_DATA` and no statistic is even reported as
   meaningful. The user asked for this explicitly, and it is the single
   most common honest answer in a system with ~110 scorable trades.

2. **Statistical significance against the system's OWN baseline**, not
   against 50%. The question is never "does this pattern win?", it is
   "does this pattern win MORE than everything else this system does?" -
   otherwise every pattern in a profitable regime looks like an edge.

3. **Multiple-testing control.** Dozens of patterns are scanned over the
   same few hundred trades, so an uncorrected 5% test would manufacture
   roughly one "significant" pattern per twenty out of pure noise.
   Benjamini-Hochberg FDR is applied across the WHOLE sweep. This is the
   single most important line of defence in the file.

4. **A bootstrap CI on expectancy that excludes zero.** Win rate is not
   profit: a pattern can win 70% of the time and still lose money. The
   interval is percentile-bootstrapped because trade P/L is fat-tailed
   and nothing like normal.

5. **Robustness across time.** The lift must have the same sign in both
   chronological halves of the pattern's own history. A "pattern" that
   exists only in one half is regime-dependent or an artifact, and
   either way is not a stable edge.

6. **Walk-forward survival.** Fit on the first 70%, check the sign still
   holds on the untouched last 30%.

Everything short of that gets a weaker, honest label: `WEAK_EDGE`,
`REGIME_DEPENDENT`, `DECAYING_EDGE`, `NOISE` - or, when the evidence is
robustly NEGATIVE, `FAILURE_PATTERN`, which is the most immediately
useful class of all, because avoiding a known loser is a cheaper edge
than finding a winner.

No LLM is involved in any of this. An LLM may propose which patterns to
look at (requirement 11 allows hypothesis generation); it never decides
whether one is real.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from crypto_trading.godfather import stats
from crypto_trading.schemas.godfather import EdgeClass, ExperiencePattern

_ZERO = Decimal("0")

# Defaults. Every one is overridable through `ExperienceConfig` so a
# later retune is config, not a code change - but the defaults are
# chosen conservatively, because the cost of a false edge in a live
# trading system is money and the cost of a missed one is only patience.
DEFAULT_MIN_SAMPLE_SIZE = 30
DEFAULT_MIN_SUPPORT = 8
DEFAULT_FDR_Q = 0.10
DEFAULT_TRAIN_FRACTION = 0.7
DEFAULT_MIN_TEST_SAMPLES = 10
DEFAULT_MIN_REGIME_SAMPLES = 10


@dataclass(frozen=True)
class ExperienceConfig:
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE
    min_support: int = DEFAULT_MIN_SUPPORT
    fdr_q: float = DEFAULT_FDR_Q
    train_fraction: float = DEFAULT_TRAIN_FRACTION
    min_test_samples: int = DEFAULT_MIN_TEST_SAMPLES
    min_regime_samples: int = DEFAULT_MIN_REGIME_SAMPLES


@dataclass(frozen=True)
class ExperienceSample:
    """One closed trade, reduced to what pattern analysis needs.

    `features` holds only already-observed, pre-entry-or-during facts.
    Nothing derived from the outcome may ever enter it - a feature that
    encodes the answer would make every pattern look like a perfect
    edge, which is the textbook way a learning system poisons itself.
    """

    position_id: str
    closed_at: datetime
    pnl: Decimal | None
    mfe_pct: Decimal | None
    mae_pct: Decimal | None
    minutes_to_mfe: float | None
    regime: str
    features: dict[str, object] = field(default_factory=dict)
    # What happened AFTER entry (price path, management, prediction
    # errors, counterfactual deltas) - built by `experience_builder`. It is
    # aggregated into each pattern's evidence file and NEVER matched on:
    # patterns are conditioned on `features` (pre-entry) only.
    profile: dict = field(default_factory=dict)
    live: bool = False
    # Net R (Fas 2A). When present it is THE outcome every statistic and
    # every edge class is computed on - size-independent, so a pattern
    # measures the signal rather than the sizing regime it happened to
    # trade under. USDT (`pnl`) is kept as the separate capital measure.
    r: Decimal | None = None
    observation: dict = field(default_factory=dict)

    @property
    def outcome(self) -> Decimal:
        if self.r is not None:
            return self.r
        if self.pnl is None:
            raise ValueError(f"sample {self.position_id} has neither R nor P/L")
        return self.pnl

    @property
    def win(self) -> bool:
        return self.outcome > _ZERO


@dataclass(frozen=True)
class PatternDefinition:
    family: str
    key: str
    condition: dict

    @property
    def pattern_id(self) -> str:
        return f"{self.family}:{self.key}"


def _matches(sample: ExperienceSample, condition: dict) -> bool:
    """Fail-closed on a missing key, exactly like the live decision
    core's `heuristic_condition_matches` - a condition naming a feature a
    sample does not have matches NOTHING rather than matching
    everything."""
    for name, expected in condition.items():
        if name not in sample.features:
            return False
        if sample.features[name] != expected:
            return False
    return True


def enumerate_patterns(
    samples: list[ExperienceSample], config: ExperienceConfig
) -> list[PatternDefinition]:
    """Every single-feature pattern with at least `min_support` samples.

    Single-feature only, deliberately. Conjunctions multiply the number
    of hypotheses tested (and therefore the FDR correction's severity)
    far faster than a few hundred trades can pay for; with this much
    data a two-feature scan would leave nothing significant after
    correction anyway, while quietly inviting overfitting. When the trade
    history is an order of magnitude larger this is the knob to turn, and
    it should be turned by raising the data, not by lowering the bar.
    """
    counts: dict[tuple[str, object], int] = {}
    for sample in samples:
        for name, value in sample.features.items():
            counts[(name, value)] = counts.get((name, value), 0) + 1
    patterns = [
        PatternDefinition(family=name, key=f"{name}={value}", condition={name: value})
        for (name, value), count in sorted(counts.items(), key=lambda kv: str(kv[0]))
        if count >= config.min_support
    ]
    return patterns


def _expectancy(samples: list[ExperienceSample]) -> Decimal | None:
    if not samples:
        return None
    return sum((s.outcome for s in samples), _ZERO) / Decimal(len(samples))


def _usdt_expectancy(samples: list[ExperienceSample]) -> Decimal | None:
    values = [s.pnl for s in samples if s.pnl is not None]
    if not values:
        return None
    return sum(values, _ZERO) / Decimal(len(values))


def outcome_metric(samples: list[ExperienceSample]) -> str:
    return "R" if samples and all(s.r is not None for s in samples) else "USDT"


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _usdt_ci(samples: list[ExperienceSample], side: int) -> Decimal | None:
    values = [float(s.pnl) for s in samples if s.pnl is not None]
    ci = stats.bootstrap_mean_ci(values) if len(values) >= 2 else None
    if ci is None:
        return None
    return Decimal(str(ci.lower if side == 0 else ci.upper))


def _avg_decimal(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, _ZERO) / Decimal(len(values))


def _avg_float(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


@dataclass
class _PatternStats:
    definition: PatternDefinition
    matched: list[ExperienceSample]
    win_count: int
    win_rate: float | None
    wilson: stats.Interval | None
    expectancy: Decimal | None
    expectancy_ci: stats.Interval | None
    p_value: float
    lift: Decimal | None
    first_half_lift: Decimal | None
    second_half_lift: Decimal | None
    regime_breakdown: dict
    walk_forward_lift: Decimal | None
    walk_forward_n: int


def _compute_pattern_stats(
    definition: PatternDefinition,
    samples: list[ExperienceSample],
    baseline_win_rate: float,
    baseline_expectancy: Decimal,
    config: ExperienceConfig,
) -> _PatternStats:
    matched = [s for s in samples if _matches(s, definition.condition)]
    matched.sort(key=lambda s: s.closed_at)
    n = len(matched)
    wins = sum(1 for s in matched if s.win)
    win_rate = (wins / n) if n else None
    expectancy = _expectancy(matched)
    lift = None if expectancy is None else expectancy - baseline_expectancy

    first, second = stats.split_halves(matched)
    first_lift = None
    second_lift = None
    if first:
        first_expectancy = _expectancy(first)
        first_lift = None if first_expectancy is None else first_expectancy - baseline_expectancy
    if second:
        second_expectancy = _expectancy(second)
        second_lift = (
            None if second_expectancy is None else second_expectancy - baseline_expectancy
        )

    # Walk-forward: fit-window / holdout split on the pattern's OWN
    # chronology. Same 70/30 shape the already-live heuristic validation
    # uses (guardian/self_improvement.py::_TRAIN_FRACTION), so the two
    # learning surfaces answer "did it hold out of sample?" the same way.
    cut = int(n * config.train_fraction)
    holdout = matched[cut:]
    holdout_expectancy = _expectancy(holdout)
    walk_forward_lift = (
        None if holdout_expectancy is None else holdout_expectancy - baseline_expectancy
    )

    regimes: dict[str, list[ExperienceSample]] = {}
    for sample in matched:
        regimes.setdefault(sample.regime, []).append(sample)
    regime_breakdown = {
        regime: {
            "n": len(group),
            "win_rate": (sum(1 for s in group if s.win) / len(group)) if group else None,
            "expectancy": str(_expectancy(group)) if group else None,
            "lift": (
                str(_expectancy(group) - baseline_expectancy) if group else None
            ),
        }
        for regime, group in sorted(regimes.items())
    }

    return _PatternStats(
        definition=definition,
        matched=matched,
        win_count=wins,
        win_rate=win_rate,
        wilson=stats.wilson_interval(wins, n),
        expectancy=expectancy,
        expectancy_ci=(
            stats.bootstrap_mean_ci([float(s.outcome) for s in matched]) if n >= 2 else None
        ),
        p_value=stats.binomial_test_two_sided(wins, n, baseline_win_rate),
        lift=lift,
        first_half_lift=first_lift,
        second_half_lift=second_lift,
        regime_breakdown=regime_breakdown,
        walk_forward_lift=walk_forward_lift,
        walk_forward_n=len(holdout),
    )


def _same_sign(*values: Decimal | None) -> bool:
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return False
    return all(v > _ZERO for v in present) or all(v < _ZERO for v in present)


def _regime_signs_disagree(breakdown: dict, config: ExperienceConfig) -> bool:
    lifts = [
        Decimal(str(entry["lift"]))
        for entry in breakdown.values()
        if entry.get("lift") is not None and entry["n"] >= config.min_regime_samples
    ]
    if len(lifts) < 2:
        return False
    return any(lift > _ZERO for lift in lifts) and any(lift < _ZERO for lift in lifts)


def classify_edge(
    pattern: _PatternStats,
    fdr_significant: bool,
    baseline_win_rate: float,
    config: ExperienceConfig,
) -> tuple[EdgeClass, bool]:
    """The whole anti-noise contract, in one auditable function.

    Returns `(edge_class, survived_walk_forward)`. Every branch is
    reachable and every one is tested - see
    `tests/crypto_trading/godfather/test_experience.py`, which drives
    each class with synthetic data rather than hoping production
    eventually produces one.
    """
    n = len(pattern.matched)
    if n < config.min_sample_size:
        return "INSUFFICIENT_DATA", False

    survived = (
        pattern.walk_forward_n >= config.min_test_samples
        and pattern.walk_forward_lift is not None
        and pattern.lift is not None
        and _same_sign(pattern.walk_forward_lift, pattern.lift)
    )

    if not fdr_significant:
        return "NOISE", survived

    if pattern.lift is None:
        return "NOISE", survived

    robust = _same_sign(pattern.first_half_lift, pattern.second_half_lift, pattern.lift)
    regime_dependent = _regime_signs_disagree(pattern.regime_breakdown, config)

    if pattern.lift > _ZERO:
        ci_clears_zero = (
            pattern.expectancy_ci is not None and pattern.expectancy_ci.excludes_zero_above()
        )
        beats_baseline_win_rate = (
            pattern.wilson is not None and pattern.wilson.lower > baseline_win_rate
        )
        if robust and survived and ci_clears_zero and beats_baseline_win_rate:
            return "EDGE", survived
        if (
            pattern.first_half_lift is not None
            and pattern.second_half_lift is not None
            and pattern.first_half_lift > _ZERO
            and pattern.second_half_lift <= _ZERO
        ):
            return "DECAYING_EDGE", survived
        if regime_dependent:
            return "REGIME_DEPENDENT", survived
        return "WEAK_EDGE", survived

    if robust:
        return "FAILURE_PATTERN", survived
    if regime_dependent:
        return "REGIME_DEPENDENT", survived
    return "NOISE", survived


def _confidence(pattern: _PatternStats, edge_class: EdgeClass, config: ExperienceConfig) -> float:
    """A single number for "how much would I bet on this being real".

    Zero for INSUFFICIENT_DATA and NOISE by construction - those are not
    weak beliefs, they are the absence of a belief, and letting them
    carry a small positive confidence is precisely how a small sample
    sneaks into a live decision.
    """
    if edge_class in ("INSUFFICIENT_DATA", "NOISE"):
        return 0.0
    n = len(pattern.matched)
    size_term = min(1.0, n / (config.min_sample_size * 2))
    significance_term = max(0.0, 1.0 - pattern.p_value)
    robustness_term = 1.0 if edge_class in ("EDGE", "FAILURE_PATTERN") else 0.5
    return round(size_term * significance_term * robustness_term, 4)


def build_experience_memory(
    samples: list[ExperienceSample],
    now: datetime,
    run_id: str,
    config: ExperienceConfig | None = None,
) -> list[ExperiencePattern]:
    """One full sweep: enumerate, test, FDR-correct, classify, emit.

    The FDR correction is applied across the whole sweep at once, which
    is why this returns a list rather than offering a per-pattern entry
    point: a pattern's significance is not a property of the pattern
    alone, it is a property of the pattern AND of how many other
    hypotheses were tested beside it. Exposing a single-pattern API
    would make it trivially easy to accidentally bypass the correction,
    so there isn't one.
    """
    config = config or ExperienceConfig()
    if not samples:
        return []

    ordered = sorted(samples, key=lambda s: s.closed_at)
    baseline_win_rate = sum(1 for s in ordered if s.win) / len(ordered)
    baseline_expectancy = _expectancy(ordered) or _ZERO
    baseline_usdt = _usdt_expectancy(ordered)
    metric = outcome_metric(ordered)
    # Global calibration / out-of-sample boundary: the first 70% of the
    # history (by close time) is calibration, the rest out-of-sample.
    calibration_cut = ordered[min(len(ordered) - 1, int(len(ordered) * config.train_fraction))]
    calibration_cut = calibration_cut.closed_at

    definitions = enumerate_patterns(ordered, config)
    computed = [
        _compute_pattern_stats(
            definition, ordered, baseline_win_rate, baseline_expectancy, config
        )
        for definition in definitions
    ]

    # Only patterns that cleared the sample-size floor participate in the
    # correction. Including under-powered patterns would inflate the
    # number of hypotheses and unfairly penalise the ones that actually
    # have data - and they are getting INSUFFICIENT_DATA regardless.
    testable_indices = [
        i for i, p in enumerate(computed) if len(p.matched) >= config.min_sample_size
    ]
    flags = stats.benjamini_hochberg(
        [computed[i].p_value for i in testable_indices], config.fdr_q
    )
    significant: dict[int, bool] = dict(zip(testable_indices, flags, strict=True))
    entry_quality = _entry_quality_classes(computed, ordered, config)

    results: list[ExperiencePattern] = []
    for index, pattern in enumerate(computed):
        fdr_significant = significant.get(index, False)
        edge_class, survived = classify_edge(
            pattern, fdr_significant, baseline_win_rate, config
        )
        mfes = [s.mfe_pct for s in pattern.matched if s.mfe_pct is not None]
        maes = [s.mae_pct for s in pattern.matched if s.mae_pct is not None]
        times = [s.minutes_to_mfe for s in pattern.matched if s.minutes_to_mfe is not None]
        results.append(
            ExperiencePattern(
                pattern_id=pattern.definition.pattern_id,
                pattern_family=pattern.definition.family,
                pattern_key=pattern.definition.key,
                condition=pattern.definition.condition,
                computed_at=now,
                sample_size=len(pattern.matched),
                win_count=pattern.win_count,
                win_rate=pattern.win_rate,
                wilson_low=pattern.wilson.lower if pattern.wilson else None,
                wilson_high=pattern.wilson.upper if pattern.wilson else None,
                expectancy_usdt=_usdt_expectancy(pattern.matched),
                expectancy_ci_low=_usdt_ci(pattern.matched, 0),
                expectancy_ci_high=_usdt_ci(pattern.matched, 1),
                avg_mfe_pct=_avg_decimal(mfes),
                avg_mae_pct=_avg_decimal(maes),
                avg_minutes_to_mfe=_avg_float(times),
                baseline_win_rate=baseline_win_rate,
                baseline_expectancy_usdt=baseline_usdt,
                lift_expectancy_usdt=(
                    None if baseline_usdt is None or _usdt_expectancy(pattern.matched) is None
                    else _usdt_expectancy(pattern.matched) - baseline_usdt
                ),
                p_value=pattern.p_value,
                fdr_significant=fdr_significant,
                first_half_lift=pattern.first_half_lift,
                second_half_lift=pattern.second_half_lift,
                regime_breakdown=pattern.regime_breakdown,
                edge_class=edge_class,
                confidence=_confidence(pattern, edge_class, config),
                survived_walk_forward=survived,
                detail={
                    # The unit every class-deciding statistic (expectancy,
                    # its CI, lift, halves, walk-forward) is computed in.
                    "outcome_metric": metric,
                    "outcome": {
                        "expectancy": _s(pattern.expectancy),
                        "ci_low": pattern.expectancy_ci.lower if pattern.expectancy_ci else None,
                        "ci_high": pattern.expectancy_ci.upper if pattern.expectancy_ci else None,
                        "lift": _s(pattern.lift),
                        "baseline": _s(baseline_expectancy),
                    },
                    "walk_forward_holdout_n": pattern.walk_forward_n,
                    "walk_forward_lift": (
                        str(pattern.walk_forward_lift)
                        if pattern.walk_forward_lift is not None
                        else None
                    ),
                    "fdr_q": config.fdr_q,
                    "hypotheses_tested_in_sweep": len(testable_indices),
                    "min_sample_size": config.min_sample_size,
                    "profile": summarise_profiles(pattern.matched, config),
                    "evidence": _evidence_split(
                        pattern.matched, calibration_cut, baseline_expectancy
                    ),
                    "entry_quality": entry_quality[index],
                },
                run_id=run_id,
            )
        )
    return results


def _quantiles(values: list[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)

    def q(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, int(fraction * (len(ordered) - 1) + 0.5))]

    return {"n": len(ordered), "p25": q(0.25), "p50": q(0.5), "p75": q(0.75)}


def _median(values: list[float]) -> float | None:
    return _quantiles(values)["p50"] if values else None


def summarise_profiles(samples: list[ExperienceSample], config: ExperienceConfig) -> dict:
    """What trades matching a pattern did AFTER entry - the answer to
    "how far do trades like this usually go, and how much adverse
    movement do they take". Descriptive: it never changes the edge class;
    every statistic carries its own n, and a counterfactual summary with
    fewer than `min_support` observed trades is INSUFFICIENT_DATA."""
    with_path = [s for s in samples if s.profile.get("has_path")]
    levels: dict[str, int] = {}
    for sample in with_path:
        for level in sample.profile.get("levels_reached", []):
            levels[level] = levels.get(level, 0) + 1
    exits: dict[str, int] = {}
    for sample in samples:
        reason = str(sample.profile.get("exit_reason") or "unknown")
        exits[reason] = exits.get(reason, 0) + 1
    entry_outcomes = [
        s.profile["entry_success"] for s in with_path if s.profile.get("entry_success") is not None
    ]
    counterfactuals: dict[str, list[float]] = {}
    for sample in samples:
        for policy, delta in sample.profile.get("counterfactual_delta", {}).items():
            counterfactuals.setdefault(policy, []).append(float(delta))

    def _errors(key: str) -> dict:
        values = [float(s.profile[key]) for s in samples if s.profile.get(key) is not None]
        return {"n": len(values), "mean": stats.mean(values)}

    return {
        "with_price_path": len(with_path),
        "mfe_pct": _quantiles([float(s.mfe_pct) for s in with_path if s.mfe_pct is not None]),
        "mae_pct": _quantiles([float(s.mae_pct) for s in with_path if s.mae_pct is not None]),
        "minutes_to_mfe": _median([s.minutes_to_mfe for s in with_path
                                   if s.minutes_to_mfe is not None]),
        "minutes_to_first_favorable": _median([
            s.profile["minutes_to_first_favorable"] for s in with_path
            if s.profile.get("minutes_to_first_favorable") is not None
        ]),
        "minutes_to_target": _median([
            s.profile["minutes_to_target"] for s in with_path
            if s.profile.get("minutes_to_target") is not None
        ]),
        "minutes_to_sl": _median([
            s.profile["minutes_to_sl"] for s in with_path
            if s.profile.get("minutes_to_sl") is not None
        ]),
        "level_reach_rate": {
            level: count / len(with_path) for level, count in sorted(levels.items())
        } if with_path else {},
        "giveback_ratio": _quantiles([
            float(s.profile["giveback_ratio"]) for s in with_path
            if s.profile.get("giveback_ratio") is not None
        ]),
        "management_capture": _quantiles([
            float(s.profile["management_capture"]) for s in with_path
            if s.profile.get("management_capture") is not None
        ]),
        "entry_success_rate": (
            sum(1 for ok in entry_outcomes if ok) / len(entry_outcomes)
            if entry_outcomes else None
        ),
        "entry_success_n": len(entry_outcomes),
        "exit_mix": exits,
        "forecast_calibration_error": _errors("forecast_error"),
        "thesis_error": _errors("thesis_error"),
        "counterfactual_mean_delta_usdt": {
            policy: (
                {"n": len(values), "mean": stats.mean(values)}
                if len(values) >= config.min_support
                else {"n": len(values), "mean": None, "status": "INSUFFICIENT_DATA"}
            )
            for policy, values in sorted(counterfactuals.items())
        },
    }


def _evidence_split(
    matched: list[ExperienceSample], calibration_cut: datetime, baseline: Decimal
) -> dict:
    """Which of this pattern's evidence is calibration history, which is
    out-of-sample, and which came from LIVE-executed trades. A pattern
    whose support is all calibration has not been tested yet."""
    calibration = [s for s in matched if s.closed_at < calibration_cut]
    oos = [s for s in matched if s.closed_at >= calibration_cut]

    def _lift(group: list[ExperienceSample]) -> str | None:
        expectancy = _expectancy(group)
        return None if expectancy is None else str(expectancy - baseline)

    return {
        "calibration_n": len(calibration),
        "oos_n": len(oos),
        "live_n": sum(1 for s in matched if s.live),
        "calibration_lift": _lift(calibration),
        "oos_lift": _lift(oos),
        "calibration_cut": calibration_cut.isoformat(),
    }


def _entry_quality_classes(
    computed: list[_PatternStats], ordered: list[ExperienceSample], config: ExperienceConfig
) -> list[dict]:
    """ENTRY quality, kept apart from the P/L outcome on purpose.

    "Entry success" = the trade reached +1% before it reached -1%. That is
    decided before any exit rule, target or Guardian intervention acts,
    so a bad entry that a lucky exit turned into a win cannot be learned
    as a good entry. Same anti-noise contract as the P/L classes: n >= 30,
    exact binomial vs the baseline entry-success rate, BH across every
    pattern, and the same sign in both chronological halves.
    """
    baseline_outcomes = [
        s.profile["entry_success"] for s in ordered if s.profile.get("entry_success") is not None
    ]
    if not baseline_outcomes:
        return [{"class": "INSUFFICIENT_DATA", "n": 0} for _ in computed]
    baseline = sum(1 for ok in baseline_outcomes if ok) / len(baseline_outcomes)

    rows: list[dict] = []
    for pattern in computed:
        outcomes = [
            s.profile["entry_success"] for s in pattern.matched
            if s.profile.get("entry_success") is not None
        ]
        n = len(outcomes)
        successes = sum(1 for ok in outcomes if ok)
        first, second = stats.split_halves(outcomes)
        rows.append({
            "n": n,
            "success_rate": successes / n if n else None,
            "baseline_success_rate": baseline,
            "p_value": stats.binomial_test_two_sided(successes, n, baseline) if n else 1.0,
            "first_half_rate": (sum(first) / len(first)) if first else None,
            "second_half_rate": (sum(second) / len(second)) if second else None,
        })
    testable = [i for i, row in enumerate(rows) if row["n"] >= config.min_sample_size]
    flags = stats.benjamini_hochberg([rows[i]["p_value"] for i in testable], config.fdr_q)
    significant = dict(zip(testable, flags, strict=True))
    for index, row in enumerate(rows):
        row["fdr_significant"] = significant.get(index, False)
        if row["n"] < config.min_sample_size:
            row["class"] = "INSUFFICIENT_DATA"
            continue
        above = [r is not None and r > baseline for r in (
            row["success_rate"], row["first_half_rate"], row["second_half_rate"]
        )]
        below = [r is not None and r < baseline for r in (
            row["success_rate"], row["first_half_rate"], row["second_half_rate"]
        )]
        if row["fdr_significant"] and all(above):
            row["class"] = "ENTRY_EDGE"
        elif row["fdr_significant"] and all(below):
            row["class"] = "ENTRY_FAILURE"
        else:
            row["class"] = "NOISE"
    return rows


_SUPPORTING = {"EDGE": 1.0, "WEAK_EDGE": 0.5, "REGIME_DEPENDENT": 0.25}
_OPPOSING = {"FAILURE_PATTERN": 1.0, "DECAYING_EDGE": 0.25}


def experience_evidence(
    patterns: list[dict],
    features: dict[str, object],
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
) -> dict:
    """What Experience Memory says about a situation, as EVIDENCE.

    Every matching pattern is listed with its sample size, class and
    confidence, so a reader sees what GODFATHER recognises and how well.
    Only classified patterns carry weight: NOISE and INSUFFICIENT_DATA
    have confidence 0 by construction and move nothing - "I have seen 12
    of these" is recognition, not evidence. The signed weight is
    confidence x class strength, summed and clipped to [-1, 1].

    `similar_cases` is the price-path profile of the most SPECIFIC
    matching pattern that still has at least `min_sample_size` trades:
    how far such trades went and how much adverse movement they took.
    """
    matched: list[dict] = []
    signed = 0.0
    for row in patterns:
        condition = row.get("condition") or {}
        if not condition or not all(features.get(k) == v for k, v in condition.items()):
            continue
        detail = row.get("detail") or {}
        if isinstance(detail, str):
            detail = json.loads(detail or "{}")
        edge_class = str(row.get("edge_class"))
        confidence = float(row.get("confidence") or 0.0)
        entry = detail.get("entry_quality") or {}
        weight = confidence * (_SUPPORTING.get(edge_class, 0.0) - _OPPOSING.get(edge_class, 0.0))
        if entry.get("class") == "ENTRY_FAILURE":
            weight -= 0.5
        elif entry.get("class") == "ENTRY_EDGE":
            weight += 0.5
        signed += weight
        matched.append({
            "pattern_id": row.get("pattern_id"),
            "sample_size": int(row.get("sample_size") or 0),
            "edge_class": edge_class,
            "entry_quality_class": entry.get("class"),
            "confidence": confidence,
            "expectancy_usdt": None if row.get("expectancy_usdt") is None
            else str(row.get("expectancy_usdt")),
            "weight": weight,
            "profile": detail.get("profile"),
        })
    signed = max(-1.0, min(1.0, signed))
    if not matched:
        verdict = "UNKNOWN_SITUATION"
    elif all(m["weight"] == 0 for m in matched):
        verdict = "NO_EVIDENCE"
    else:
        verdict = "SUPPORTS" if signed > 0 else "OPPOSES" if signed < 0 else "MIXED"
    reliable = [m for m in matched if m["sample_size"] >= min_sample_size and m["profile"]]
    specific = min(reliable, key=lambda m: m["sample_size"]) if reliable else None
    return {
        "verdict": verdict,
        "signed_weight": signed,
        "matched": sorted(matched, key=lambda m: -abs(m["weight"])),
        "similar_cases": (
            {"pattern_id": specific["pattern_id"], "sample_size": specific["sample_size"],
             "profile": specific["profile"]}
            if specific else {"status": "INSUFFICIENT_DATA"}
        ),
    }


def lookup_edge_class(
    patterns: list[dict], features: dict[str, object]
) -> tuple[EdgeClass, Decimal | None, list[str]]:
    """What Experience Memory has to say about a NEW candidate.

    Returns the most decisive applicable verdict, its expectancy, and the
    matching pattern ids. Precedence is deliberately asymmetric:
    `FAILURE_PATTERN` beats everything, because a known way to lose money
    is more reliable and more valuable than a known way to make it. In
    the absence of any classified pattern the answer is
    `INSUFFICIENT_DATA` - never a neutral-sounding guess.
    """
    order = {
        "FAILURE_PATTERN": 0,
        "EDGE": 1,
        "DECAYING_EDGE": 2,
        "REGIME_DEPENDENT": 3,
        "WEAK_EDGE": 4,
        "NOISE": 5,
        "INSUFFICIENT_DATA": 6,
    }
    best: tuple[int, EdgeClass, Decimal | None] = (99, "INSUFFICIENT_DATA", None)
    matched_ids: list[str] = []
    for row in patterns:
        edge_class = str(row.get("edge_class"))
        if edge_class in ("NOISE", "INSUFFICIENT_DATA"):
            continue
        condition = row.get("condition") or {}
        if not condition:
            continue
        if not all(features.get(k) == v for k, v in condition.items()):
            continue
        matched_ids.append(str(row.get("pattern_id")))
        rank = order.get(edge_class, 98)
        if rank < best[0]:
            expectancy = row.get("expectancy_usdt")
            best = (
                rank,
                edge_class,  # type: ignore[arg-type]
                Decimal(str(expectancy)) if expectancy is not None else None,
            )
    return best[1], best[2], matched_ids
