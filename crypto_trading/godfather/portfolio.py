"""Portfolio layer: four concurrent trades are not four independent bets.

Three measurements and one advisory rule, all from data the system
already has:

* **Theme.** A coarse, deterministic grouping from the instrument name
  (the exchange's own prefixes mark tokenised equities `NCSK*`,
  commodities `NCCO*`, FX `NCFX*`; a short list of majors; everything
  else is an alt). Not fitted.
* **Return correlation of concurrently held positions**, from the per-tick
  price paths Guardian recorded while both were open.
* **Outcome dependence inside a cohort** (signals confirmed in the same
  discovery run): the intraclass correlation of their real P/L, and the
  "effective number of independent bets" it implies.
* **Diversified selection (advisory).** Walking a cohort in rank order,
  a signal is SKIP_CONCENTRATION when its theme already holds
  `MAX_PER_THEME` positions counting the ones open at that moment. It can
  only remove trades - never add one, never raise a limit, never bypass
  max-positions or capital limits - and whether it pays is measured on
  real outcomes.

BTC beta is reported UNAVAILABLE: there is no continuous BTC price series
in the database, and inventing one from regime factors would be a guess.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal

from crypto_trading.godfather import stats
from crypto_trading.godfather.entry_selection import EntrySignal, two_group_test
from crypto_trading.godfather.path import PathPoint

_ZERO = Decimal("0")

MAX_PER_THEME = 2
MIN_OVERLAP_POINTS = 30
_BUCKET_MINUTES = 2.0

_MAJORS = {"BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "TRX", "LTC", "AVAX", "DOT", "LINK"}


def theme_of(instrument: str) -> str:
    base = instrument.split("-")[0].upper()
    if base.startswith("NCSK"):
        return "tokenised_equity"
    if base.startswith("NCCO"):
        return "commodity"
    if base.startswith("NCFX"):
        return "fx"
    if base in _MAJORS:
        return "crypto_major"
    return "crypto_alt"


def _bucketed_returns(opened_at: datetime, points: list[PathPoint]) -> dict[int, float]:
    """Price per 2-minute wall-clock bucket, then bucket-to-bucket log
    returns keyed by bucket index."""
    prices: dict[int, float] = {}
    for p in points:
        bucket = int(p.observed_at.timestamp() // (_BUCKET_MINUTES * 60))
        prices[bucket] = float(p.price)
    returns: dict[int, float] = {}
    for bucket in sorted(prices):
        previous = prices.get(bucket - 1)
        if previous and prices[bucket] > 0:
            returns[bucket] = math.log(prices[bucket] / previous)
    return returns


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy)


def concurrent_return_correlation(
    paths: list[tuple[str, str, datetime, list[PathPoint]]],
) -> dict:
    """`paths` = (position_id, instrument, opened_at, points). Pairs of
    DIFFERENT instruments held at the same time with at least
    `MIN_OVERLAP_POINTS` common return buckets."""
    series = [
        (pid, instrument, _bucketed_returns(opened, points))
        for pid, instrument, opened, points in paths
    ]
    same: list[float] = []
    cross: list[float] = []
    for i in range(len(series)):
        for j in range(i + 1, len(series)):
            _a, inst_a, ra = series[i]
            _b, inst_b, rb = series[j]
            if inst_a == inst_b:
                continue
            common = sorted(set(ra) & set(rb))
            if len(common) < MIN_OVERLAP_POINTS:
                continue
            rho = _pearson([ra[k] for k in common], [rb[k] for k in common])
            if rho is None:
                continue
            (same if theme_of(inst_a) == theme_of(inst_b) else cross).append(rho)
    return {
        "pairs_same_theme": len(same),
        "mean_corr_same_theme": stats.mean(same),
        "pairs_cross_theme": len(cross),
        "mean_corr_cross_theme": stats.mean(cross),
        "btc_beta": "UNAVAILABLE",
    }


def cohort_outcome_dependence(signals: list[EntrySignal]) -> dict:
    """One-way ANOVA intraclass correlation of real P/L across cohorts
    with at least two scored trades. ICC > 0 means trades taken together
    win and lose together; with average cohort size m the effective
    number of independent bets per cohort is m / (1 + (m - 1) ICC)."""
    groups: dict[str, list[float]] = {}
    for s in signals:
        if s.outcome is not None:
            groups.setdefault(s.discovery_run_id, []).append(float(s.outcome))
    groups = {k: v for k, v in groups.items() if len(v) >= 2}
    values = [v for members in groups.values() for v in members]
    k = len(groups)
    n = len(values)
    if k < 5:
        return {"status": "INSUFFICIENT_DATA", "cohorts": k, "trades": n}
    grand = sum(values) / n
    ss_between = sum(len(m) * (sum(m) / len(m) - grand) ** 2 for m in groups.values())
    ss_within = sum(sum((v - sum(m) / len(m)) ** 2 for v in m) for m in groups.values())
    ms_between = ss_between / (k - 1)
    ms_within = ss_within / (n - k) if n > k else 0.0
    m0 = (n - sum(len(m) ** 2 for m in groups.values()) / n) / (k - 1)
    denom = ms_between + (m0 - 1) * ms_within
    icc = (ms_between - ms_within) / denom if denom else None
    mean_size = n / k
    effective = (
        mean_size / (1 + (mean_size - 1) * icc) if icc is not None and icc > -1 else None
    )
    return {
        "status": "ESTIMATE",
        "cohorts": k,
        "trades": n,
        "icc_pnl": icc,
        "mean_cohort_size": mean_size,
        "effective_independent_bets_per_cohort": effective,
    }


def assign_portfolio_verdicts(
    signals: list[EntrySignal],
    open_themes_at: dict[str, list[str]],
) -> None:
    """`open_themes_at[candidate_id]` = themes of positions already OPEN
    when this signal was decided (known at the time). Walk each cohort in
    rank order; a TAKE signal whose theme is already at `MAX_PER_THEME`
    becomes SKIP_CONCENTRATION."""
    cohorts: dict[str, list[EntrySignal]] = {}
    for s in signals:
        cohorts.setdefault(s.discovery_run_id, []).append(s)
    for members in cohorts.values():
        members.sort(key=lambda s: s.cohort_rank)
        held: dict[str, int] = {}
        for theme in open_themes_at.get(members[0].candidate_id, []):
            held[theme] = held.get(theme, 0) + 1
        for s in members:
            if s.selection_verdict != "TRADE":
                s.portfolio_verdict = "NOT_TAKEN"
                continue
            if held.get(s.theme, 0) >= MAX_PER_THEME:
                s.portfolio_verdict = "SKIP_CONCENTRATION"
                continue
            s.portfolio_verdict = "KEEP"
            held[s.theme] = held.get(s.theme, 0) + 1


def evaluate_diversification(signals: list[EntrySignal], cut: datetime) -> dict:
    kept = [s for s in signals if s.scored and s.portfolio_verdict == "KEEP"]
    skipped = [s for s in signals if s.scored and s.portfolio_verdict == "SKIP_CONCENTRATION"]
    test = two_group_test(
        [s.outcome for s in skipped], [s.outcome for s in kept]
    )
    return {
        "kept": len(kept),
        "skipped_concentration": len(skipped),
        "skipped_total_pnl_usdt": str(sum(
            (s.realized_pnl for s in skipped if s.realized_pnl is not None), _ZERO
        )),
        "skipped_minus_kept": test,
        "skipped_train_n": sum(1 for s in skipped if s.decided_at < cut),
        "skipped_test_n": sum(1 for s in skipped if s.decided_at >= cut),
    }


def exposure_profile(
    positions: list[tuple[datetime, datetime | None, Decimal, str]],
) -> dict:
    """Concurrent directional exposure over time: (opened, closed, notional,
    theme). Every position in this codebase is LONG, so gross = net."""
    events: list[tuple[datetime, Decimal, str]] = []
    for opened, closed, notional, theme in positions:
        events.append((opened, notional, theme))
        if closed is not None:
            events.append((closed, -notional, theme))
    events.sort(key=lambda e: e[0])
    current = _ZERO
    peak = _ZERO
    by_theme: dict[str, Decimal] = {}
    peak_theme_share = 0.0
    for _moment, change, theme in events:
        current += change
        by_theme[theme] = by_theme.get(theme, _ZERO) + change
        peak = max(peak, current)
        if current > _ZERO:
            peak_theme_share = max(
                peak_theme_share, float(max(by_theme.values()) / current)
            )
    return {
        "direction": "LONG_ONLY",
        "peak_concurrent_notional_usdt": str(peak),
        "peak_single_theme_share": peak_theme_share,
    }
