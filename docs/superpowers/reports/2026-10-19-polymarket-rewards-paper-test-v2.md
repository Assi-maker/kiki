# Polymarket liquidity rewards — 7-day PAPER test v2 (pre-registered)

**Status: PRE-REGISTERED 2026-10-19, before any v2 data exists.** Results are added below the
results line once the test is done. Nothing above that line may change.

v1 (`2026-10-02-polymarket-rewards-paper-test.md`) was declared **INVALID**. Its PASS rested on
two bugs and on minutes where the model saw no competitor at all. v2 asks the same question with
those defects removed. It also adds a criterion that the result must not depend on being alone in a
market.

## Question (unchanged)
Can a bot with **100 USD** that posts two-sided maker quotes on Polymarket earn more in daily
liquidity rewards than it loses when its quotes get filled?

## What is unchanged from v1
These parts of the v1 method still apply:
- the reward formula;
- quote placement and size;
- the pessimistic fill rules (a) tape, (b) book-through and (c) last-trade;
- inventory limits;
- greedy hourly selection;
- the gap rule;
- the six variants (PRIMARY `d2_all`);
- the 7-valid-day window with 80 % coverage;
- resolution via `gamma-api`.

The code is `crypto_trading/polymarket/paper.py` and `evaluate.py` at the commit that adds this file.

## Changes from v1 (all fixed before any v2 data)
1. **Honest mid.** A market's mid counts as honest only if all three conditions hold:
   - the visible spread (best ask − best bid) is ≤ 2 × max spread;
   - the size-cutoff mid is within max spread / 2 of the visible mid;
   - if a last trade exists, the mid is within max spread of it.

   If the mid is not honest, the market cannot be selected and earns **0** reward that minute.
   Quoting to unwind inventory and fill checks continue as before.
2. **No stale rates.** A held market that leaves the reward list is kept at rate **0** so it can
   still be unwound.
3. **Competitors are never below Polymarket's own figure.** Every hour, the top 40 candidates
   per variant are fetched from `/rewards/markets/<cid>`. Competitors are the larger of the v1 book
   bound and Polymarket's `market_competitiveness`. A market can be selected only if that figure was
   fetched in the same hour. The last value is carried over for markets already held.
   - Before registration, 60 random reward markets were spot-checked. `market_competitiveness`
     tracks our Q bound in the same units: log-correlation 0.67, and it is usually below the bound.
     In 3 of the 60 markets it was positive where the book bound was 0.
4. **Valuation at last trade.** Unresolved inventory is valued at the last traded price, or at the
   mid when no trade exists. v1 used the size-cutoff mid. The last trade price is now stored in
   every sample.
5. **New database.** The test writes to `data/polymarket_paper/paper_v2.db`. No v1 data is reused.

## PASS criteria (all required, PRIMARY `d2_all` only)
1. Total net (rewards + inventory P&L) > 0.
2. At least 5 of 7 days with net > 0.
3. Total net > 0 after removing the single best market's contribution.
4. Average reward ≥ 1 USD/day.
5. **New:** total net > 0 after removing all rewards from minutes where our share of the pool was
   above 50 % (`dominant_rewards` in the evaluator output). This checks that the strategy also works
   where we are one maker among several. It must not depend on being alone in a new or neglected
   market, where competition would arrive in real life.

## Decision rule
- **PASS** → propose a **scoring check with real money**, about 20–50 USD, for the user to decide on.
  - Small real orders are placed, and Polymarket's `/order-scoring` and `/rewards/user/percentages`
    are compared with the model's share.
  - Only if the model's share is confirmed does a 100–200 USD, 2-week real test follow, as in v1.
- **FAIL** → stop. Do not build a Polymarket bot.
- A secondary variant passing while the primary fails is not approval.

## Known limits (stated before the results)
- Paper still cannot show whether Polymarket actually scores a real order the way the formula
  says. Only the real-money scoring check can.
- `market_competitiveness` is an undocumented field. It is used only as a floor that can lower our
  share, never raise it.
- Queue position is still ignored, and real takers may target real quotes.
- The reward program can change at any time. A pass says "it worked in this week".

## Sanity invariants (checked by the evaluator)
- Maximum capital ≤ 100 USD.
- `|Yes - No| ≤ size` per market.
- An implausibly good result is a bug until proven otherwise. The evaluator therefore reports
  `dominant_rewards` next to the total.

Smoke runs on 2026-10-19 of 3.5 and 4.5 minutes only checked that the collector works. They are
not data.

---
## Results

**WITHDRAWN 2026-10-19, before its first evaluation day.** v2 ran on Railway only during its
warm-up day, from 20:17 UTC. No day that would have counted was collected.

v2 is replaced by `2026-10-19-polymarket-rewards-paper-test-v3-final.md`. v3 closes the remaining
optimistic shortcuts:
- the rate is cross-checked against `/rewards/markets`;
- minutes whose trade tape could not be read earn nothing;
- inventory is marked pessimistically;
- totals exclude the warm-up day;
- the verdict is binding and written automatically.

The v2 database (`paper_v2.db`) is kept in the volume and is not used.
