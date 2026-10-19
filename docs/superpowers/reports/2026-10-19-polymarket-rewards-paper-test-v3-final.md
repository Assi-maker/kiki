# Polymarket liquidity rewards — 7-day PAPER test v3 FINAL (pre-registered, binding)

**Status: PRE-REGISTERED 2026-10-19, before any v3 data exists.** Nothing above the results line may
change.

**This is the final paper test.** Its verdict is binding. It is written automatically by the
collector and is not reinterpreted, re-run with changed rules or replaced by a v4. The only
exceptions are listed under "What can invalidate the test".

History: v1 (2026-10-02) was declared INVALID because of bugs and uncontested rewards. v2 was
withdrawn before its first evaluation day. v3 keeps every v2 rule and closes the remaining
optimistic shortcuts.

## Question
Can a bot with **100 USD** that posts two-sided maker quotes on Polymarket earn more in daily
liquidity rewards than it loses when its quotes get filled? This must hold even where it is one
maker among several.

## Method
The code is `crypto_trading/polymarket/paper.py` and `evaluate.py` at the commit that adds this file.
The tests are in `tests/crypto_trading/test_polymarket_paper.py`.

**Unchanged from v1.**
- Polymarket's reward formula:
  - `S = ((v-s)/v)^2`;
  - `Q_min`, with the single-sided /3 rule for a mid in [0.10, 0.90];
  - our share per minute = `Q_ours / (Q_ours + Q_competitors)`.
- Quotes at mid ± d cents, with size = the market's reward `min_size`.
- Pessimistic fills: (a) a trade printed at or through the quote, (b) the book moved through it,
  (c) the last trade is at or beyond it. Queue position is ignored, which overstates fills.
- Inventory limits, the 100 USD capital cap, hourly greedy selection, and the gap rule.

**From v2.**
- **Honest mid.** The visible spread is ≤ 2 × max spread, the size-cutoff mid is within
  max spread / 2 of the visible mid, and the mid is within max spread of the last trade if one exists.
  - Otherwise the market cannot be selected and earns 0.
- A held market that left the reward list is kept at rate 0.
- **Competitors** are `max(book upper bound, Polymarket market_competitiveness)`.
  - The competitiveness figure is fetched hourly for the top 40 candidates of each variant.
  - A market can be selected only if it was fetched in the same hour.

**New in v3.**
1. **Rate.** The rate is the lower of `/sampling-markets` and `/rewards/markets/<cid>`
   (`rate_per_day`). A market that `/rewards/markets` no longer lists gets rate 0. Markets with
   rate 0 are never selected.
2. **Unread tape.** A minute in which a resting market's trade tape could not be read earns **0**
   reward for that market.
3. **Inventory marking.** Unresolved inventory is marked at the **worse** of last trade and mid for
   the side held: the lower price for Yes, the higher for No. Resolved markets use the resolution price.
4. **Totals.** Totals use only the 7 valid days. Rewards from the warm-up day and from invalid days
   are excluded. Inventory P&L is measured from the start of the first valid day.

## Variants
All six are pre-declared. `d1`, `d2` and `d3` are the distance from mid in cents. `long` means only
markets ending more than 48 h ahead.

| Variant | Distance | Markets |
|---|---|---|
| **`d2_all`** (reported as primary) | 2 c | all |
| `d1_all` | 1 c | all |
| `d3_all` | 3 c | all |
| `d1_long` | 1 c | ending > 48 h ahead |
| `d2_long` | 2 c | ending > 48 h ahead |
| `d3_long` | 3 c | ending > 48 h ahead |

## Evaluation window
- The warm-up day is the deploy day (partial), followed by **7 valid UTC days**.
- A day is valid if at least 80 % of its 1440 minutes were collected. Invalid days are skipped, and
  the run continues until 7 valid days exist.
- **Automatic, frozen evaluation.**
  - At the first hourly selection after the 7th valid day has ended, the collector runs the
    evaluator once.
  - It writes `data/polymarket_paper/paper_v3_result.json` and stops.
  - A restart after that does not collect again.
  - **That file is the result.**

## PASS criteria (per variant, all required)
1. Total net (rewards + inventory P&L) > 0.
2. At least 5 of 7 days with net > 0.
3. Total net > 0 after removing the single best market's contribution (rewards + inventory).
4. Average reward ≥ 1 USD/day.
5. Total net > 0 after removing all rewards from minutes where our share of the pool was above 50 %.

## Decision rule (binding)
- **PASS** = at least one of the six variants meets all five criteria. The verdict field in the
  result file says `PASS` and lists the passing variants.
  - **Next step:** a scoring check with real money, about 20–50 USD, for the user to decide on. It
    takes 1–2 days, not another week.
  - Small real orders are placed, and Polymarket's `/order-scoring` and `/rewards/user/percentages`
    are compared with the model's share.
  - Only if the share is confirmed does a 100–200 USD, 2-week real test follow.
- **FAIL** = no variant meets all five. **Stop. Do not build a Polymarket bot, and do not run a v4
  paper test.**
- Six variants raise the chance of a lucky pass. That is accepted, because a PASS leads only to the
  cheap real-money check, which is the actual gate.

## What can invalidate the test (exhaustive)
Only these, all checked mechanically:
- `verdict = INVALID`, meaning the capital or inventory invariant was breached (a code bug).
- Fewer than 7 valid days by **2026-11-02**, because of infrastructure failure. The test is then
  `INCOMPLETE` and reported as such, not re-run with other rules.

Nothing else makes the result invalid. In particular, these are accepted limits:
- a "surprisingly good" or "surprisingly bad" number;
- a market that behaves oddly;
- a known limit listed below.

## Known limits (accepted in advance, not grounds for invalidation)
- Paper cannot show whether Polymarket scores a real order exactly as the formula says. The
  real-money scoring check answers that, after a PASS.
- `market_competitiveness` is undocumented. It is used only as a floor, so it can only lower our share.
- Real competitors and takers may react to real orders. Queue position is ignored.
- Rewards can change at any time. A pass means "it worked in this week".
- Inventory still unresolved at evaluation time is marked pessimistically.

## Expectation stated before data
Estimated from v1 data under these rules: rewards from minutes where our share was ≤ 50 % were about
0.8 USD/day, against about −4 USD/day of inventory loss. **FAIL is the most likely outcome.** The
estimate does not change the rules.

Smoke runs on 2026-10-19 (4.5 minutes) only checked that the collector works. They are not data.

---
## Results
_(pending — copied from `paper_v3_result.json` when it exists)_
