"""Pure statistical primitives for GODFATHER's Experience Memory.

Why this module exists at all: the whole point of Experience Memory is to
be able to say "this is an edge" and, far more often, "this is noise" or
"there is not enough data to tell". Without real significance machinery
that distinction collapses into "the win rate looks high", which is
exactly how a learning system teaches itself to be worse.

Everything here is stdlib-only (the project has no scipy/numpy
dependency), pure, and deterministic - `bootstrap_mean_ci` takes an
explicit seed so the same evidence always produces the same interval and
a stored classification can be re-derived later, byte for byte.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# 1.959963985 = two-sided 95% normal quantile. Hardcoded rather than
# computed because there is no erfinv in the stdlib and this is the only
# confidence level the whole subsystem uses.
Z_95 = 1.959963985


@dataclass(frozen=True)
class Interval:
    """A closed confidence interval. `lower`/`upper` are always finite and
    ordered; an empty sample yields None from the producing function
    instead of a degenerate interval."""

    lower: float
    upper: float

    def excludes_zero_above(self) -> bool:
        return self.lower > 0.0

    def excludes_zero_below(self) -> bool:
        return self.upper < 0.0


def wilson_interval(successes: int, n: int, z: float = Z_95) -> Interval | None:
    """Wilson score interval for a binomial proportion.

    Wilson rather than the textbook normal approximation deliberately: at
    the sample sizes this system actually has (tens of trades, not
    thousands) the normal approximation produces intervals that reach
    below 0 or above 1 and is badly miscalibrated near 0% / 100% - which
    is precisely where a small-sample "pattern" will look most exciting.
    """
    if n <= 0 or successes < 0 or successes > n:
        return None
    p = successes / n
    denom = 1.0 + (z * z) / n
    centre = (p + (z * z) / (2 * n)) / denom
    margin = (z / denom) * math.sqrt((p * (1 - p) / n) + (z * z) / (4 * n * n))
    return Interval(lower=max(0.0, centre - margin), upper=min(1.0, centre + margin))


def _binomial_pmf(k: int, n: int, p: float) -> float:
    if k < 0 or k > n:
        return 0.0
    return math.comb(n, k) * (p**k) * ((1 - p) ** (n - k))


def binomial_test_two_sided(successes: int, n: int, p_null: float) -> float:
    """Exact two-sided binomial test p-value (the "method of small
    p-values": sum the probability of every outcome no more likely than
    the observed one). Exact, not normal-approximated, for the same
    small-sample reason as `wilson_interval`. n here is at most a few
    hundred trades, so the O(n) `math.comb` loop is free.

    Returns 1.0 (never a significant result) for a degenerate input rather
    than raising - a pattern with no samples is not evidence of anything.
    """
    if n <= 0 or successes < 0 or successes > n:
        return 1.0
    p_null = min(max(p_null, 0.0), 1.0)
    if p_null <= 0.0:
        return 1.0 if successes == 0 else 0.0
    if p_null >= 1.0:
        return 1.0 if successes == n else 0.0
    observed = _binomial_pmf(successes, n, p_null)
    # Floating-point slack so an outcome with mathematically identical
    # probability (the symmetric tail) is not excluded by a 1e-17 wobble.
    tolerance = observed * (1 + 1e-9)
    total = sum(
        pmf for k in range(n + 1) if (pmf := _binomial_pmf(k, n, p_null)) <= tolerance
    )
    return min(1.0, total)


def benjamini_hochberg(p_values: list[float], q: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg FDR control. Returns, per input position,
    whether that p-value is a discovery at false-discovery-rate `q`.

    This is the single most important guard against learning noise in the
    whole system: Experience Memory scans MANY candidate patterns over the
    same few hundred trades, so at the usual alpha=0.05 roughly one in
    twenty pure-noise patterns would look "significant" by construction.
    Uncorrected per-pattern testing is how a bot ends up confidently
    trading its own sampling error.
    """
    n = len(p_values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: p_values[i])
    passed = [False] * n
    largest_k = 0
    for rank, idx in enumerate(order, start=1):
        if p_values[idx] <= (rank / n) * q:
            largest_k = rank
    for rank, idx in enumerate(order, start=1):
        if rank <= largest_k:
            passed[idx] = True
    return passed


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def bootstrap_mean_ci(
    values: list[float], iterations: int = 2000, alpha: float = 0.05, seed: int = 20260925
) -> Interval | None:
    """Percentile bootstrap CI for the mean (used for expectancy, whose
    distribution is fat-tailed and nothing like normal - a handful of big
    losses dominate it, which is exactly the shape a t-interval handles
    worst).

    Deterministic by construction: same values + same seed => same
    interval, so a stored `EDGE` verdict can always be re-derived and
    audited rather than taken on trust.
    """
    n = len(values)
    if n < 2:
        return None
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iterations):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    lo_idx = int((alpha / 2) * iterations)
    hi_idx = min(iterations - 1, int((1 - alpha / 2) * iterations))
    return Interval(lower=means[lo_idx], upper=means[hi_idx])


def split_halves(values: list) -> tuple[list, list]:
    """Chronological split of an already chronologically ordered list.

    Used for the robustness requirement: a real edge shows up in both
    halves of its own history. A "pattern" that only exists in one half is
    either regime-dependent or an artifact, and either way must not be
    promoted as a stable edge. An odd-length list puts the extra sample in
    the SECOND (more recent) half - the recent evidence is the one that
    matters more for a decaying edge.
    """
    n = len(values)
    if n < 2:
        return (values[:], [])
    cut = n // 2
    return (values[:cut], values[cut:])


def sign_flip_p_value(
    values: list[float], iterations: int = 20000, seed: int = 20260925
) -> float:
    """Two-sided paired randomisation test of "mean effect = 0".

    Under the null that a policy is irrelevant to a trade, the sign of
    each per-trade delta is as likely to be + as -, so flipping signs at
    random generates the null distribution of the sum. Unlike the sign
    test this uses magnitudes - which is the point for P/L, where one
    destroyed winner can outweigh several small savings. Seeded, so a
    stored p-value re-derives byte for byte; the +1 correction keeps a
    Monte Carlo p-value from ever being exactly 0.
    """
    if not values:
        return 1.0
    observed = abs(sum(values))
    if observed == 0.0:
        return 1.0
    rng = random.Random(seed)
    tolerance = observed * 1e-12
    extreme = 0
    for _ in range(iterations):
        total = 0.0
        for value in values:
            total += value if rng.random() < 0.5 else -value
        if abs(total) >= observed - tolerance:
            extreme += 1
    return (extreme + 1) / (iterations + 1)


def bootstrap_diff_ci(
    a: list[float], b: list[float], iterations: int = 2000, alpha: float = 0.05,
    seed: int = 20260925,
) -> Interval | None:
    """Percentile bootstrap CI for mean(a) - mean(b), resampling each
    group independently. Used where the two groups are different trades
    (TAKE vs WAIT), so no pairing exists."""
    if len(a) < 2 or len(b) < 2:
        return None
    rng = random.Random(seed)
    diffs: list[float] = []
    for _ in range(iterations):
        mean_a = sum(a[rng.randrange(len(a))] for _ in a) / len(a)
        mean_b = sum(b[rng.randrange(len(b))] for _ in b) / len(b)
        diffs.append(mean_a - mean_b)
    diffs.sort()
    lo_idx = int((alpha / 2) * iterations)
    hi_idx = min(iterations - 1, int((1 - alpha / 2) * iterations))
    return Interval(lower=diffs[lo_idx], upper=diffs[hi_idx])


def permutation_diff_p_value(
    a: list[float], b: list[float], iterations: int = 20000, seed: int = 20260925
) -> float:
    """Two-sided label-permutation test of mean(a) == mean(b). Exact in
    spirit, Monte Carlo in practice, seeded and +1-corrected like
    `sign_flip_p_value`."""
    if not a or not b:
        return 1.0
    observed = abs(sum(a) / len(a) - sum(b) / len(b))
    pooled = list(a) + list(b)
    n_a = len(a)
    rng = random.Random(seed)
    tolerance = observed * 1e-12
    extreme = 0
    for _ in range(iterations):
        rng.shuffle(pooled)
        diff = abs(sum(pooled[:n_a]) / n_a - sum(pooled[n_a:]) / (len(pooled) - n_a))
        if diff >= observed - tolerance:
            extreme += 1
    return (extreme + 1) / (iterations + 1)


def sequential_blocks(values: list, blocks: int = 4) -> list[list]:
    """Chronological, contiguous, near-equal blocks for walk-forward
    stability checks: an effect that is real should keep its sign block
    after block, each block judged only on what happened in it."""
    n = len(values)
    if n == 0 or blocks <= 0:
        return []
    size, extra = divmod(n, blocks)
    out: list[list] = []
    start = 0
    for index in range(blocks):
        end = start + size + (1 if index < extra else 0)
        if end > start:
            out.append(values[start:end])
        start = end
    return out
