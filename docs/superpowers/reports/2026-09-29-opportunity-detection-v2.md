# Opportunity Detection v2 – forensic + research (2026-09-29)

Research only. Nothing in LIVE was changed. The group cap is still 0.10 in LIVE, and will stay there until the user explicitly asks to revert it.

## Periods compared

- **A – LIVE-100:** 26/9 17:27 → 28/9 11:15, 41.8 h. This is before P0; no kernel existed.
- **B – now:** since 29/9 12:43, 7.1 h, on 22ee411 with group cap 10 %.

| Funnel step | A | B | Change |
|---|---|---|---|
| discovery cycles / h | 2.25 | 2.84 | + |
| markets per cycle | ~839 | ~1239 | + |
| candidates / h | 14.7 | 39.8 | **×2.7** |
| BUDGET_LIMITED share | 83 % | 87 % | ≈ |
| AI analyses / h | 2.0 | 5.1 | **×2.5** |
| distinct symbols analysed | 42 | 27 (in 7 h) | + per hour |
| repeat analyses of the same symbol | 49 % (2Z 10×) | 25 % | better |
| CONFIRMED / h | 0.96 | 1.55 | + |
| reached LIVE / h | **0.89** | **0.28** | **−69 %** |

## A. Why volume dropped

Candidate and AI volume did **not** drop; both rose about 2.5×. What dropped is **CONFIRMED → LIVE**. In order of contribution:

1. **Safety Kernel caps against the fixed 1000 notional.**
   - B: 6 of 8 kernel evaluations were REJECT (75 %), all PORTFOLIO_RISK_CAP or GROUP_RISK_CAP, even at 10 %.
   - A had no kernel. Replaying A's 37 LIVE trades through today's kernel at 5 %/10 % gives 1 APPROVE, 30 cap REJECTs and 6 liquidation REJECTs (~97 % rejected).
   - The mechanism: at ~390 equity, 10 % is ~39 USDT of worst case. Worst case is the stop distance plus 0.4 % costs on 1000 notional, so the stop can be at most ~3.5 % away. At 5 % the limit is ~1.6 %, which most alt stops exceed.
   - This single rule explains most of the drop.
2. **The daily AI cap** (500 calls ≈ 70 analyses/day) ran out mid-day. 40 candidates were `daily_ai_cost_cap`-limited until 00:00 UTC. That is a pause of several hours with zero chance of a trade.
3. **The per-cycle AI cap = free LIVE slots** (the live discovery gate). This is normally 3, so 87 % of candidates are BUDGET_LIMITED without ever being looked at.
4. **The Gate (P1).** Of 36 analyses, 18 were QA-rejected, 5 had R:R < 1 and 3 were SIGNAL_STALE. The 3 stale ones came from the bug fixed at abff4cb and cannot happen again.
5. **The stale-CONFIRMED bug** (before abff4cb) lost 3 CONFIRMED in B. It is fixed.

## B. Parts that are unnecessarily restrictive (volume without evidence of protection)

- **The daily AI cap as a hard wall.** It cuts the flow by time of day, not by quality. The last hours of the UTC day become blind. (This is a cost question, not a risk question.)
- **The live discovery gate's AI cap = free slots.** It ties analysis volume to open positions. But the kernel rejects most CONFIRMED anyway, so candidates are analysed that could never be traded. The cost is spent, but the selection is not improved.
- **Symbol repetition.** A few symbols take much of the budget: PUMP 18, NCSKMSTR 16, ZRO 14 and SOON 10 candidates in 7 h. The REJECTED cooldown is 60 min and the kernel cooldown 120 min, but CONFIRMED/NO_TRADE outcomes have no symbol cooldown.

## C. Parts that genuinely protect (keep)

- **The Safety Kernel caps.**
  - The B2 replay (group 10 %) gave ~3× the trades, OOS −82 USDT and −25 % drawdown.
  - Every period with more trades was worse, because the underlying candidates have negative expectancy (see E).
  - The caps are therefore the only thing currently limiting the loss rate, and this is the strongest argument for going back to 5 %.
- **The liquidation guard, P1 R:R ≥ 1, and the signal age of 30 min** (now correctly measured).
- **The kernel-reject cooldown.** It stops the same REJECT from repeatedly consuming the AI budget.

## D. Opportunity Detection v2 – proposals tested

The replay covers every historical discovery run with the **same** cap (k = 3 per cycle) as LIVE, using different selection rules:

- **CURRENT:** top-3 by `candidate_score` (today).
- **R_BEST:** the best single pre-trade feature for ranking, chosen **on TRAIN only** out of 12 features × 2 directions. The winner was `accel` ascending.
- **DIVERSE:** CURRENT, but a symbol picked within the last 2 h is skipped (more distinct symbols).
- **NO_MOMENTUM_X:** CURRENT, but the edge lab's no-4h-momentum candidates are skipped.
- **COMBO:** R_BEST + DIVERSE + NO_MOMENTUM_X.

The outcome is the standardized R from the entry research: entry +23 min, stop/target 2/3 ATR15, 6 h, real fees and slippage. The split is purged TRAIN < 13/9, VALID 13–19/9, TEST ≥ 26/9. Significance uses cluster-bootstrap (4 h blocks) plus Benjamini–Hochberg.

## E. Historical / OOS expectancy per change (mean R)

| Rule | TRAIN | VALID | TEST | symbols TEST | q (vs CURRENT) |
|---|---|---|---|---|---|
| CURRENT | −0.20 | −0.18 | **−0.42** | 68 | – |
| R_BEST (accel asc) | **+0.14** | −0.28 | −0.40 | 53 | 0.66 |
| DIVERSE | −0.01 | −0.21 | −0.40 | **81** | 0.66 |
| NO_MOMENTUM_X | −0.15 | −0.19 | −0.38 | 67 | 0.66 |
| COMBO | **+0.17** | −0.16 | −0.46 | 61 | 0.66 |
| ALL candidates | +0.08 | −0.20 | **−0.49** | 100 | – |

No-momentum flag on all candidates:

| Period | flagged | not flagged |
|---|---|---|
| TRAIN | −1.39 R (n 64) | +0.16 R (n 959) |
| VALID | −0.16 R (n 30) | −0.16 R (n 518) |
| TEST | −0.73 R (n 79) | −0.42 R (n 542) |

What the table shows:

- **No rule improves OOS.**
  - The positive TRAIN results for R_BEST and COMBO are selection overfitting; they collapse in VALID and TEST.
  - No difference against CURRENT is significant (all q = 0.66).
- **DIVERSE** gives +19 % distinct symbols at the same expectancy (TEST −0.40 vs −0.42). It is neutral, not positive.
- **No-momentum** points the right way in 2 of 3 periods (TRAIN and TEST), is equal in VALID, and is not significant. It stays a FAILURE_HYPOTHESIS in shadow, as before.
- **The core finding:** in TEST, the mean R of **all** candidates is −0.49. No choice of 3 out of a pool with negative expectancy becomes positive. The pool itself is the problem, not the selection.

## F. Estimated volume change

| Change | Candidates → AI | LIVE trades |
|---|---|---|
| R_BEST / NO_MOMENTUM_X | ±0 | ±0 (same k) |
| DIVERSE / COMBO | −10–20 % selected | ≈ ±0 |
| Raising the daily AI cap | + several hours/day of flow | small: the kernel still rejects most |
| Reverting group cap to 5 % | ±0 | **−50–80 %** vs now (kernel replay) |
| Loosening kernel caps | ±0 | +, but B2: OOS −82 USDT |

Ranking cannot create LIVE volume. Only the kernel caps and the notional size decide it, and both are locked by the user's fixed-size decision.

## G. False-signal risk

- **Higher volume with the current pool means more losses** (TEST −0.4 to −0.5 R per trade, SL rate ~60 %).
- **R_BEST and COMBO:** high overfitting risk. They looked like +0.14 to +0.17 R on TRAIN and turned negative OOS.
- **DIVERSE:** low risk, but no gain either.
- **No-momentum as a hard filter:** risk of false rejects. The VALID period shows no effect, and the estimate method earlier rejected the only approved night trade (PEOPLE). It stays shadow-only.

## Decision under the rule "shadow only with positive OOS evidence"

**Nothing new is implemented in shadow or LIVE.** No v2 rule has positive OOS evidence. The existing shadows keep collecting forward data: pre-AI feasibility, NO_4H_MOMENTUM, market_observations (OI/funding).

Code: `crypto_trading/entry_research/opportunity_v2.py` (read-only research). Tests: `tests/crypto_trading/entry_research/test_opportunity_v2.py`. Data: `data/entry_research/opportunity_v2.json` (gitignored).
