# Polymarket liquidity rewards — 7-day PAPER test (pre-registered)

**Status: PRE-REGISTERED 2026-10-02, before any evaluated data exists.** The results are added
below this section once the test is done. Nothing above the results line may change.

## Question
Can a bot with **100 USD** that posts two-sided maker quotes on Polymarket earn more in daily
liquidity rewards than it loses when its quotes get filled (adverse selection)?

## Why a paper test first
- Rewards are public: per-market `rate_per_day`, `max_spread`, `min_size` from the CLOB
  (`/sampling-markets`, cross-checked against `/rewards/markets/<cid>`).
- Order books are public, so our share of the reward pool can be computed every minute with
  Polymarket's own formula.
- Historical order books are not public, so this cannot be backtested. It can only be measured
  going forward.
- Sweden is not geoblocked: `polymarket.com/api/geoblock` returned `blocked:false, country:SE`.
- Makers pay no fees, per the Polymarket fee docs.

## Method (`crypto_trading/polymarket/paper.py`)
Each minute, a virtual portfolio per variant:

**Reward score.** Polymarket's documented formula:
- `S(v,s) = ((v-s)/v)^2`, with spreads in cents measured from the size-cutoff adjusted midpoint.
- `Q_min = max(min(Q1,Q2), max(Q1,Q2)/3)` when the midpoint is in [0.10, 0.90], else `min(Q1,Q2)`.
- Reward for the minute = `rate/1440 * Q_ours / (Q_ours + Q_competitors)`.

**Competitors (conservative).** `Q_competitors = (Q1_book + Q2_book)/2`.
- This is an upper bound on the sum of the other makers' `Q_min`.
- All book levels count, with no per-order min-size filter. Over-counting competitors lowers our share.

**Quotes.**
- Bid at `mid - d` and ask at `mid + d`, in cents, rounded away from mid on the tick and never crossing the book.
- Size = the market's reward `min_size`.
- A side is not quoted when the last traded price is already at or beyond it.

**Fills (pessimistic).** A resting quote counts as filled at its own price, full size, if any of these happen during the next minute:
- (a) a public trade prints at or through it (data-api trade tape);
- (b) the book moves through it;
- (c) the last traded price is at or beyond it.

Rule (c) models informed takers in thin books, where few prints exist but anyone could hit a mispriced quote. Queue position is ignored, which overstates fills.

**Inventory.**
- At most one unpaired fill per side and market.
- Yes+No pairs merge to 1 USD.
- A held market keeps being quoted only on its unwinding side.
- Open orders plus inventory cost never exceed 100 USD.

**Selection.**
- Every hour, the greedy top markets by expected reward per dollar of capital.
- Only markets with midpoint in [0.10, 0.90] whose last trade lies between our quotes.

**Gaps.** If the collector misses more than 3 minutes, resting orders count as cancelled. Missing minutes earn 0 reward.

**Variants.** PRIMARY = `d2_all`. Secondary: `d1_all`, `d3_all`, `d1_long`, `d2_long`, `d3_long`.
- `d1`/`d2`/`d3` = distance in cents from mid.
- `long` = only markets ending more than 48 h ahead.

## Evaluation window
- The warm-up day is 2026-10-02, a partial day. It is included in totals but not in the daily count.
- **7 valid UTC days** follow. A day is valid if at least 80 % of its 1440 minutes were collected.
- Invalid days are skipped, and the run continues until 7 valid days exist.

## Valuation
- Inventory is valued at resolution (`gamma-api` `outcomePrices`, closed markets).
- Unresolved inventory is valued at its last observed mid; the count of such markets is reported.
- Daily net = that day's rewards + that day's change in mark-to-market inventory value.

## PASS criteria (all required, PRIMARY only)
1. Total net (rewards + inventory P&L) > 0.
2. At least 5 of 7 days with net > 0.
3. Total net is still > 0 after removing the single best market's contribution.
4. Average reward of at least 1 USD/day, Polymarket's minimum payout.

## Decision rule
- **PASS** → propose a small real test, 100–200 USD for 2 weeks. That would measure what paper cannot: real queue position and real fills.
- **FAIL** → stop. Do not build a Polymarket bot.
- A secondary variant passing while the primary fails is **not** approval. It may only motivate a new, separately pre-registered 7-day test.

## Known limits (stated before the results)
- Our virtual orders do not exist. Real competitors might react to them, and real takers might target them. Rule (c) is the guard against the second.
- The reward rate is read hourly. A rate cut within the hour is not seen until the next refresh.
- Polymarket can change the reward program at any time. A pass says "it worked in this week", not "it will keep working".

## Sanity invariants (checked by the evaluator)
- Maximum capital ≤ 100 USD.
- `|Yes - No| ≤ size` per market.
- Lesson from the grid lab: an implausibly good result is a bug until proven otherwise.

Smoke runs on 2026-10-02 are only a feasibility check (the collector works, rule (c) fires); they are not data.
- 3 × 4 minutes.
- Snapshot hypothetical rewards were hundreds of USD/day.
- In thin books, rule (c) filled quotes immediately. That is why the last-trade quoting guard was added before registration.

---
## Results
_(pending)_
