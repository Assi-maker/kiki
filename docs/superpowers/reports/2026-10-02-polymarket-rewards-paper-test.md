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

**Verdict: INVALID, neither PASS nor FAIL.** The evaluator printed PASS for `d2_all`, but a review on
2026-10-19 found that the result rests on model defects and on a question paper data cannot answer.
The decision rule's PASS branch, proposing a real-money test, is **not** triggered. A corrected test
is pre-registered separately in `2026-10-19-polymarket-rewards-paper-test-v2.md`.

**Run.**
- The collector ran locally from 2026-10-02, then on Railway (EU) from 2026-10-06 to 2026-10-19.
- The 7 valid days were 2026-10-07 to 10-13, each with 1440/1440 ticks. Days 10-03 to 10-06 were
  below 80 % coverage and skipped under the rules.
- A database snapshot is kept locally as `data/polymarket_paper/railway_v1_2026-10-19.db`
  (sha256 of the gzip: `7ae43e10…dba85`).

**Evaluator output (as printed, 2026-10-19).**

| Variant | Verdict | Net | Rewards | Inventory | Fills | Positive days |
|---|---|---|---|---|---|---|
| d2_all (primary) | PASS | +318.80 | 346.83 | −28.03 | 53 | 7/7 |
| d1_all | PASS | +128.75 | 193.65 | −64.90 | 37 | 5/7 |
| d3_all | PASS | +427.66 | 440.13 | −12.47 | 58 | 7/7 |
| d1_long | PASS | +118.65 | 165.40 | −46.75 | 38 | 5/7 |
| d2_long | FAIL | +293.57 | 327.55 | −33.98 | 62 | 4/7 |
| d3_long | PASS | +295.05 | 318.49 | −23.44 | 37 | 7/7 |

Invariants held: max capital 100.00, max net inventory ratio 1.0. 27 markets were unresolved and marked at mid.

**Why the result is invalid.**
- **Rewards came almost entirely from uncontested minutes.** `d2_all` earned 262.98 USD in the
  7-day window. Of that:
  - 245.69 USD (93 %) came from minutes with no competitor in the book within max spread, where our
    share was 100 %;
  - 162.70 USD (62 %) came from one-sided quotes that were unwinding inventory.
  - Counting only minutes with at least one competitor leaves 17.29 USD. That is less than the
    inventory loss alone.
- **The mid sat in an empty gap of the book.** Example: the Calder Trophy market.
  - With dust under `min_size`, the size-cutoff mid was about 0.32 while the market traded at about 0.03.
  - Our lone ask at about 0.33 then "won" the whole 28 USD/day pool for days.
  - About 77 USD (29 %) came from quotes clamped by such hollow books.
- **Stale reward rate (bug).** Held markets that left the reward list kept their last rate, so
  rewards kept being credited. The Calder Trophy market had `rates: null` by 10-19, yet it was
  credited at a constant 28 USD/day.
- **Inventory marked at a false mid (bug).** Unresolved inventory was valued at the last
  size-cutoff mid, so Calder Yes bought at 0.165 was marked near 0.32 instead of about 0.03. The
  inventory loss is therefore understated.

**What held.** Yes and No books are exact mirrors, so no competitor was missed on the No side. The
capital and inventory invariants held.

**Lesson.** The pre-registered "implausibly good is a bug until proven otherwise" was right. The
evaluator should have reported how much of the reward depended on being alone in a market.
