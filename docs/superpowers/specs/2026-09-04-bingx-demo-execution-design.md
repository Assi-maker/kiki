# BingX Demo Execution — Design Spec

Date: 2026-09-04
Status: Approved by user, pending SPEC_CRYPTO.md amendment + implementation plan.

## 1. Motivation

`monitoring_loop.py` only evaluates the single latest candle each tick. If the
bot process isn't running continuously, a stop-loss/target touch during the
gap is silently missed — PAPER's simulated results become inaccurate exactly
when the user can't keep the process alive 24/7. Placing the same trades as
real orders on the user's BingX **Demo (VST)** account offloads SL/TP
execution to the exchange's own matching engine, which triggers independent
of whether `crypto_trading`'s process is alive.

## 2. Non-goals / explicit exclusion

This design does **not** touch real money or a live BingX account. It also
does not change any AI/discovery/Gate logic. Confirmed by the user: this is a
deliberate, informed amendment to `SPEC_CRYPTO.md §1/§19/§20`'s previous hard
"never connect to a broker account" boundary — not an accidental violation of
it (see §9 below for the amendment text).

## 3. Core architecture principle (user-mandated, verbatim)

> Demo-exekveringen får aldrig skapa, ändra eller stänga en PAPER-position.
> PAPER och BingX Demo är parallella observatörer av samma redan
> Gate-godkända trade och är inte beroende av varandra.

Concretely: `paper_trading/position_opening.py` and
`paper_trading/position_closing.py` are **not modified**. The `positions`
table and its existing idempotency/reconstructability guarantees (SPEC
§8.6/§8.7) are untouched. Demo execution is a strictly additive,
read-the-same-events / write-to-a-different-table layer.

## 4. Rollout strategy: parallel-first

Phase A (this design): both systems run side by side. PAPER keeps being the
system of record for the Gate/AI pipeline and dashboard. BingX Demo mirrors
each Gate-approved trade as a real demo order, and results are compared via
an extended report. Only after a trust period would a future, separate
decision cut over to Demo as the source of truth for position
lifecycle — out of scope here.

## 5. New components

- `connectors/bingx_demo_trading.py` — new connector, separate class from
  `BingXMarketDataConnector`. Order placement/cancel only; no account-balance
  reads in this phase (position sizing stays on
  `settings.risk_limits.starting_capital_usdt`, per user decision).
- `paper_trading/demo_execution.py` — reads `POSITION_OPENED` events from the
  existing `events` table (no change to how those events are produced) and
  drives demo order placement + lifecycle.
- New table `demo_executions` (schema in §7).
- New daemon thread `demo_execution_loop` wired into `run.py`, analogous to
  `monitoring_loop`: no AI cost, pure market-data/order-status polling.

## 6. Safety guardrails

### 6.1 Exact-host guard (hardened per user feedback — no substring match)

```python
_VST_HOST = "open-api-vst.bingx.com"

def _guard_host(self) -> None:
    parsed = urlparse(self._base_url)
    if parsed.scheme != "https" or parsed.hostname != _VST_HOST:
        raise DemoExecutionGuardError(
            f"refuses to trade against host={parsed.hostname!r}"
        )
```

`_base_url` is a **class-level constant**, never a constructor parameter,
never sourced from settings/env. There is no code path capable of pointing
this connector at `open-api.bingx.com`. `_guard_host()` runs immediately
before every order-placing/amending/cancelling HTTP call — not just once at
construction — so an in-memory mutation between init and call can't slip
through either.

### 6.2 Dedicated, non-reusable credentials

New env vars, read only by `bingx_demo_trading.py`:
`CRYPTO_TRADING_BINGX_DEMO_API_KEY`, `CRYPTO_TRADING_BINGX_DEMO_API_SECRET`.
Never a generic `BINGX_API_KEY` name that a future live-account integration
could accidentally reuse.

### 6.3 Opt-in arm flag, default OFF

`CRYPTO_TRADING_DEMO_EXECUTION_ENABLED` — same opt-in pattern as
`CRYPTO_TRADING_DASHBOARD_ENABLED`/Telegram. Absent → `demo_execution_loop`
thread is never started, `run.py` logs
`event="demo_execution_disabled"` exactly like the existing
`dashboard_disabled`/`telegram_notify_disabled` events.

## 7. `demo_executions` table

| column | type | notes |
|---|---|---|
| `position_id` | TEXT PK | same id as `positions.position_id` (deterministic, SPEC §8.6) |
| `phase` | TEXT | `CLAIMED` → `ENTRY_SUBMITTED` → `ACTIVE` → `CLOSED` / `FAILED` |
| `entry_client_order_id` | TEXT | deterministic, derived from `position_id` |
| `entry_exchange_order_id` | TEXT NULL | set once BingX confirms |
| `exit_reason` | TEXT NULL | `stop_loss` / `target` / `TIME_LIMIT` |
| `exchange_fill_entry` | TEXT NULL | Decimal-as-string, exchange-reported |
| `exchange_fill_exit` | TEXT NULL | Decimal-as-string |
| `last_error` | TEXT NULL | most recent failure, if `phase='FAILED'` |
| `claimed_at` / `updated_at` / `closed_at` | TEXT | ISO timestamps |

Entirely separate from `positions` — no foreign-key coupling beyond the
shared id value, no shared write path.

## 8. Idempotency — claim-before-place

1. On seeing a `POSITION_OPENED` event: attempt
   `INSERT INTO demo_executions (position_id, phase, claimed_at) VALUES (?, 'CLAIMED', ?)`.
   A uniqueness violation means another run/duplicate delivery already
   claimed it — **stop, place nothing**.
2. Only the process whose INSERT succeeded proceeds to call BingX, using a
   deterministic `clientOrderID` derived from `position_id`.
3. On confirmed success: update the row to `ENTRY_SUBMITTED` →
   `ACTIVE`, storing `entry_exchange_order_id`.
4. Crash recovery: a reconciliation pass finds rows stuck in `CLAIMED` past a
   grace window (e.g. 30s) with no `entry_exchange_order_id`. Before
   resubmitting, it **looks up the order by `clientOrderID`** against BingX
   first; only if that lookup finds nothing does it retry placement with the
   same deterministic `clientOrderID`. Never a blind retry.

**Open item to verify live during implementation:** BingX's exact
`clientOrderID` semantics (length/charset limits, whether the exchange
itself rejects a duplicate `clientOrderID` or silently accepts a second
order). The official API docs have moved to a JS-rendered site this design
process couldn't fully read; the repo's own README now just redirects there.
Treat this as unconfirmed until checked against the live authenticated demo
endpoint — same "verified live" discipline already used elsewhere in this
codebase (e.g. `bingx_market_data.py`'s "Verifierad live 2026-08-25" note).
Our own DB-level claim (step 1) is the primary, self-sufficient safeguard
regardless of what BingX does with `clientOrderID`.

## 9. Order mechanics

- **Leverage: 1x**, explicitly set per instrument before order placement.
  PAPER's PnL formula assumes no leverage; a higher default leverage on the
  exchange could liquidate the demo position at a price PAPER's own
  stop-loss would never trigger on, invalidating the comparison.
- **Quantity**: PAPER's USDT notional (`size`) converted to contract quantity
  (`size / entry_price`), rounded down to the instrument's
  `quantityPrecision`/`stepSize` — read via the already-existing, read-only
  `BingXMarketDataConnector.get_contracts()`. No new account-data endpoint
  needed for this.
- **SL/TP attached to the entry order** as `stopLoss`/`takeProfit` JSON
  parameters on the same order-placement call (BingX's native one-request
  attachment), not three separate calls — eliminates the window where a
  filled entry temporarily has no exchange-side protection.
- **`reduceOnly=true`** on the SL/TP legs — they can only close, never flip
  or add to, the position.

## 10. Time-limit parity (user-specified)

BingX has no server-side time-based close; PAPER does
(`max_position_hold_hours`). `demo_execution_loop` replicates it actively:

1. Detect that a position has reached PAPER's `max_position_hold_hours` —
   reusing the **same** time-reference/calculation `check_exit_trigger`
   already uses (imported, not reimplemented) to avoid drift between the two
   systems.
2. Cancel the active SL/TP orders on BingX.
3. Market-close the demo position.
4. Record `exit_reason='TIME_LIMIT'` plus the exchange's reported fill in
   `demo_executions`.
5. Never touch the `positions` row for the same `position_id`.

## 11. Retry / error handling policy

- Network/timeout errors **before any response**: exponential backoff, bounded
  retry count.
- Any response from the exchange (including an error like insufficient
  balance): no blind retry. Mark `phase='FAILED'` with `last_error` and stop.
  This never affects the PAPER position (§3).

## 12. `demo_execution_loop` tick

1. Read new `POSITION_OPENED` events → claim-before-place (§8) → single
   entry+SL/TP order call (§9).
2. Poll `ACTIVE` rows' order/position status on BingX → update fill/close/
   `exit_reason` when the exchange reports an SL/TP trigger.
3. Check `ACTIVE` rows against PAPER's time-limit (§10) → close if reached.
4. Writes exclusively to `demo_executions`. Never writes to `positions`.

## 13. Comparison reporting

Extend `performance/paper_track_report.py` (same read-only, pure-reporting
principle already documented at the top of that file) with a section that
joins `positions` ⋈ `demo_executions` on `position_id`, showing side by side:
PAPER `simulated_fill_entry/exit` vs. BingX's real fill, PAPER `exit_reason`
vs. demo `exit_reason`, and the USDT/time divergence between them. No new
write logic in that file — it stays read-only as designed.

## 14. Testing strategy

- `FakeBingXDemoConnector` test double — same pattern the test suite already
  uses for `BingXMarketDataConnector`. Real network calls are forbidden in
  tests.
- Targeted tests:
  - host guard rejects everything except the exact `open-api-vst.bingx.com`
    host (including near-miss hosts, e.g. a subdomain trick).
  - concurrent/duplicate `POSITION_OPENED` delivery never results in two
    orders (claim-before-place race test).
  - a crash between `CLAIMED` and confirmed response recovers correctly via
    lookup-before-retry.
  - time-limit closing never mutates the `positions` table.
- Before considering this done: a **manual, real verification** against the
  user's actual BingX demo account (a position visibly opens/closes correctly
  in BingX's own UI) — the same live-verification discipline already
  established in this project (Task 12/AC3).

## 15. SPEC_CRYPTO.md amendment

`§1`, `§19`, and the `§20` self-review row currently state an absolute,
all-phases ban on any code connecting to a broker account or placing an
order. This design requires an explicit, narrow, documented exception. Exact
amendment text to apply in the implementation step:

- §1 "Det här är INTE" list: add a qualifier that the ban applies to **live**
  broker accounts; add a new explicit line stating BingX **Demo (VST)**
  order placement is permitted exclusively through
  `connectors/bingx_demo_trading.py`, gated by the exact-host guard (§6.1),
  the dedicated env vars (§6.2), and the opt-in arm flag (§6.3), and that it
  may never create/modify/close a PAPER position (§3).
- §19: same qualifier — "no live broker account", plus a pointer to this
  design doc and the new guardrails.
- §20 self-review table: update the "Kan riktig handel ske av misstag?" row
  to describe the new guardrails (exact-host assert, dedicated credentials,
  default-off arm flag, isolation from `positions`) instead of asserting
  no order-placement code exists at all.

## 16. Out of scope (explicitly deferred, not forgotten)

- Reading real demo account balance to drive position sizing (user chose to
  keep using the simulated capital figure for now).
- Cutting over to Demo as the system of record — a separate future decision
  after the parallel comparison period.
- SHORT positions (system is LONG-only today; unaffected by this change).
