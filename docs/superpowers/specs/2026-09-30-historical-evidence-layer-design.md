# Historical Evidence Layer – design (2026-09-30)

## Purpose

Make GODFATHER **historically informed**, not the bot more restrictive.

The layer answers one question per candidate or position: *what did history say about this kind of signal, in this market state, at the moment of the decision, and how sure is that?*

It is **evidence and context only**:
- It creates no filters.
- It opens and closes nothing.
- It cannot move a stop.
- The 5 % and 10 % caps stay log-only, and no new caps are introduced.

## Architecture

```
entry_research (offline)                         bot (read-only)
------------------------                         ---------------
exit_lab checkpoints ──► evidence_builder ──►  data/historical_evidence.db  ──►  crypto_trading/evidence/
(2.4 M entries, 22 exits,  (snapshot per as_of)   (SQLite, opened mode=ro)       store.lookup(... decision_time)
 regimes point-in-time)                                                          verdict (pure rules)
                                                                                 context.evidence_context() -> plain dict
                                                                                        │
                                            (later, behind a flag, default OFF)         ▼
                                            Guardian AI context / GODFATHER strategist / priority (read only)
```

- **Writing is research only.** `evidence_builder` in `entry_research` is the only writer. The bot never imports it.
- **Reading goes through a separate file**, `data/historical_evidence.db`, opened `?mode=ro` so the bot *cannot* write. It is never the bot DB.
- **The evidence package** `crypto_trading/evidence/` imports no connectors, execution, safety_kernel, paper_trading, guardian or godfather. It returns plain, immutable data. This is tested with AST.
- **Guardian and the Safety Kernel are unchanged.** Evidence can only reach the *AI context*, which interprets. The deterministic decisions (HOLD/WATCH/PROTECT/EXIT, kernel APPROVE/REJECT, TIGHTEN_SL/TAKE_PROFIT in authority) do not read the evidence package. This is tested with AST.

## Data model (`data/historical_evidence.db`)

### `evidence_snapshot`

One row per build and cut-off point.

| Column | Meaning |
|---|---|
| snapshot_id | `as_of` in ISO format, plus the code hash |
| as_of | UTC. **No outcome with an exit time ≥ as_of is included.** |
| built_at, code_hash, source | Reproducibility |
| data_start | The earliest data used |
| symbols, survivorship_note | The universe = symbols listed today. Survivorship bias is stated, not hidden. |
| selection_sha | The frozen selection from the regime and exit labs (protocol status) |

### `evidence_cell`

Keyed by snapshot × signal_type × side × regime_dim × regime_value × exit_model × period.

| Column | Meaning |
|---|---|
| signal_type | One of the 17 event families, `ANY` (pooled, one per symbol/side/hour) or `BASELINE` (random) |
| side | LONG / SHORT |
| regime_dim, regime_value | `ALL/ALL`, or one of the 8 point-in-time regimes (mkt_trend, mkt_vol, breadth, mkt_funding, mkt_oi, sym_funding, sym_oi, sym_vol) |
| exit_model | `FIXED` (SL 1R / TP 1.5R / 24 h) and `TIME15m` / `TIME30m` / `TIME1h` / `TIME2h` / `TIME4h` (the result after 15 min to 4 h). **5m is missing** from today's data and is marked n/a. |
| period | TRAIN / VALID / TEST / HOLDOUT, plus **OOS** = VALID ∪ TEST ∪ HOLDOUT. Only data < as_of. |
| n, trades_per_day | Sample size |
| mean_r, se_r, ci_low, ci_high | Expectancy after fees, slippage and funding. SE is clustered per UTC day; CI at 95 %. |
| p_pos | One-sided p that mean_r > 0 |
| win_rate, mfe_r, mae_r, hold_min | Win rate, MFE (24 h), MAE and holding time |
| net_usdt_per_trade | At the fixed LIVE size (1000 notional) |
| baseline_mean_r, diff_vs_baseline, p_diff | Against the random baseline, same side/regime/exit/period |

### `evidence_verdict`

One row per snapshot × signal_type × side × regime_dim × regime_value, for the FIXED exit. These are deterministic rules, fixed in advance (`crypto_trading/evidence/verdict.py`).

- **`oos_status`**, based **only on OOS** (VALID+TEST+HOLDOUT). **TRAIN never lifts a status.**
  - `INSUFFICIENT_DATA`: n_oos < 100.
  - `NEGATIVE_OOS`: ci_high < 0.
  - `NO_EDGE`: the CI contains 0.
  - `POSITIVE_UNCONFIRMED`: ci_low > 0, but the pre-registered protocol (TRAIN → VALID → TEST → HOLDOUT, frozen selection) did **not** select it. Given ~1,600 tested cells this may be chance, and it is **never** called profitable.
  - `VALIDATED_EDGE`: **only** if the cell is in a frozen protocol selection *and* passed both TEST and HOLDOUT. There are 0 of these today.
- **`train_only_positive`**: flagged when TRAIN is positive and OOS is not. This is a warning, not an upgrade.
- **`vs_baseline`**: `BETTER` / `WORSE` / `NOT_DIFFERENT`, from p_diff < 0.05 in OOS.
- **`strength`**:
  - `HIGH`: n_oos ≥ 1000 and CI width ≤ 0.10 R.
  - `MEDIUM`: n_oos ≥ 300 and CI width ≤ 0.25 R.
  - `LOW`: otherwise.
- **`headline`**: plain text for the AI context, e.g. "OOS −0.12 R [−0.15, −0.09], n 8,412, worse than random, HIGH certainty".

## Temporal correctness (anti-leakage)

1. **The builder filters on exit time, not signal time.** A trade is included in a snapshot only if `exit_ts < as_of`, so a position that is still open at as_of is invisible.
2. **The regimes are point-in-time** (as in the regime lab): trailing medians only on earlier samples, market regimes from the last hour ≤ T, OI one period delayed, funding only once settled.
3. **Lookup:** `lookup(..., decision_time)` picks the **latest snapshot with as_of ≤ decision_time**, and returns `None` if none exists. No function can read "the latest" without a decision time.
4. **Historical snapshots** (monthly as_of from 2026-02-01) let GODFATHER's post-mortems and replays of old decisions see only what was actually known back then.
5. **Survivorship:** the universe comes from today's list. This is stated in every snapshot and in every context package, and cannot be removed without delisted data.

## Integration with GODFATHER (stage 2, behind a flag, default OFF)

| Consumer | Receives | Can do |
|---|---|---|
| Guardian AI context (`guardian/ai_context.py`) | `historical_evidence`: headline and status for the position's signal type and regime | Interpret. The state (HOLD/PROTECT/EXIT) is **already set** deterministically. |
| GODFATHER strategist / priority strategist | The evidence package as input when proposing heuristics | Propose candidates only, which are validated out of sample as today |
| Future entry decisions | The evidence package on the candidate (shadow table first) | Nothing automatic. Using it requires the user's decision. |

**A live classifier is needed first:** it computes, from BingX 5m bars (`kline_archive`) and derivative data **≤ decision_time**, which event families and regimes a live candidate belongs to. It is built and verified in shadow first.

## Not done now

- No change to LIVE entry logic, the Guardian decision rules or the Safety Kernel.
- No new filter and no new cap.
- No automatic activation.
