# BingX Live Execution — Design Spec

Date: 2026-09-06
Status: Approved by user, incl. margin_safety_buffer_usdt=1.00 (locked).
Pending SPEC_CRYPTO.md amendment + implementation plan.
Not activated. `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED` stays unset until a separate,
explicit go-ahead after this plan's test suite passes.

## 1. Motivation

Move from PAPER-only (and BingX Demo/VST mirroring) to a small, tightly bounded
real-money test on the user's actual BingX Futures account: max 4 concurrent
positions, 10 USDT margin / 10x leverage each (~100 USDT notional, ~400 USDT
max total notional), to validate the existing signal/Gate/Guardian pipeline
against real fills, real fees, and real funding — not just simulated ones.

## 2. Non-goals / explicit exclusion

This design does **not** change the signal pipeline, the 7 AI roles, Risk
Agent, the Gate's own PAPER-side rules, Guardian's deterministic
classification, or any PAPER parameter (`max_concurrent_positions=20`,
`max_position_notional_usdt=1000`, `max_position_hold_hours=24`). It does not
touch `paper_trading/position_opening.py` or `position_closing.py`. It does
not touch Demo execution (`bingx_demo_trading.py`, `demo_execution.py`,
`demo_execution_loop.py`) at all. AI budget (`max_ai_calls_per_day`,
`max_daily_ai_cost_usd`) is unchanged and unbypassed.

## 3. Core architecture principle (user-mandated)

> LIVE är en separat exekveringskedja och får aldrig skriva till eller ändra
> PAPER-positioner. PAPER förblir system of record för Gate/AI-beslut. Demo
> och PAPER ska förbli separerade och oförändrade.

Concretely: LIVE is a **third**, fully independent parallel observer of the
same Gate-approved `POSITION_OPENED` events PAPER and Demo already read —
same event source, three independent readers, zero coupling between the
readers. LIVE writes exclusively to its own new `live_executions` table.

## 4. Relationship to PAPER capacity/sizing

PAPER's existing caps (`max_concurrent_positions=20`,
`max_position_notional_usdt=1000`) are **not replaced**. LIVE's caps (4
positions, 10 USDT margin) are a second, much lower ceiling layered
independently on top — exactly the same "additional floor, never a
replacement" relationship `max_position_notional_usdt` already has with
`risk_per_trade_pct` (see `position_sizing.py`). A PAPER position that never
gets a live counterpart (because live was full, or margin was short, or the
instrument's exchange minimums didn't fit 10 USDT) is not an error — it is
the expected, safe outcome of LIVE being the more constrained system.

## 5. New components

- `connectors/bingx_live_trading.py` — new connector class, **not** a
  subclass of `BingXDemoTradingConnector` (no shared mutable state, no risk
  that a base-class change silently affects both hosts). Same shape: hardcoded
  `_base_url = "https://open-api.bingx.com"` class constant, exact-host guard
  re-checked immediately before every mutating call (§8). Superset of Demo's
  methods plus:
  - `get_balance() -> dict` — read-only, new. Real USDT-margin
    account balance (`GET /openApi/swap/v2/user/balance` per BingX's
    documented swap-account balance endpoint; exact response field names
    confirmed against the real account during implementation, read-only,
    no order placed — see §14).
  - `get_all_positions() -> list[dict]` — read-only, new. Unlike Demo's
    `get_position(symbol)` (single-instrument), this fetches the account's
    complete open-position list in one call — needed for the
    reconciled-capacity count (§7) without N sequential per-symbol calls.
- `paper_trading/live_execution.py` + `live_execution_loop.py` — new files,
  same tick shape as `demo_execution.py`/`demo_execution_loop.py`
  (claim-before-place, stale-claim recovery, guardian-exit mirror, time-limit
  close) with the additions in §6/§7/§9/§10 below. **Order within a tick is
  reconcile-first** (opposite of Demo's reconcile-after-submit order) — see
  §7.
- New table `live_executions` (schema in §11).
- New config `config/live_execution.yaml` / `LiveExecutionConfig`
  (`check_interval_seconds`, `claim_stale_after_seconds`, `max_retries`,
  `max_concurrent_positions=4`, `margin_per_trade_usdt=10`, `leverage=10`,
  `max_position_hold_hours=6`, `margin_safety_buffer_usdt` — see §9).
- New daemon thread #9 in `run.py`, opt-in via
  `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED` (default unset/off), analogous to
  the Demo/Guardian thread wiring.

## 6. Credentials & safety guards

- Env vars: **`BINGX_API_KEY`** / **`BINGX_API_SECRET`** only — confirmed by
  the user to be the real, dedicated LIVE-account keys already sitting unused
  in `.env` (reserved for exactly this purpose since the Demo design
  explicitly avoided reusing them). Read only by
  `build_live_trading_connector_from_env()` in `run.py`, never logged,
  never printed, never included in any exception message or `log_event()`
  payload — every error path passes only `type(exc).__name__` and a
  BingX-provided error code/message with credentials already stripped by
  `httpx`'s own exception formatting (same discipline `bingx_demo_trading.py`
  already follows: no request body/headers are ever logged).
- Exact-host guard, identical mechanism to Demo's, different constant:
  ```python
  _LIVE_HOST = "open-api.bingx.com"

  def _guard_host(self) -> None:
      parsed = urlparse(self._base_url)
      if parsed.scheme != "https" or parsed.hostname != _LIVE_HOST:
          raise LiveExecutionGuardError(...)
  ```
  `_base_url` is a class-level constant, never a constructor parameter, never
  sourced from settings/env — no code path can point this connector at VST or
  anywhere else. Re-checked immediately before every mutating HTTP call, not
  just at construction (same as Demo).
- Opt-in arm flag `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED`, default unset.
  Absent → thread never starts, `run.py` logs
  `event="live_execution_disabled"`. **This flag stays unset through this
  entire plan** — activation is a separate, later, explicit decision after
  the full test suite passes and config is verified (user's own gate).

## 7. Capacity gating — two layers, reconciliation-based

**User-mandated refinement:** the capacity count must reflect **reconciled**
exchange state, never local DB state alone — a stale/crashed local row must
never let a 5th real position slip through, and must never falsely block a
slot that's actually free again.

- **Wiring note:** `run_discovery_tick()`/`run_forever()` gain one new
  optional parameter, `live_connector: BingXLiveTradingConnector | None =
  None` (same optional-dependency pattern as `news_connector`/
  `external_data_connector`). `run.py::_run_discovery_forever()` passes a
  real instance only when `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED` is set and
  credentials are present; otherwise `None`, and the check below is skipped
  entirely — PAPER-only behavior is provably unchanged when live is off,
  the default state through this whole plan.
- **Layer 1 (coarse, cost-saving)** — `discovery_loop.py` gains one new
  pre-tick check, active only when `live_connector is not None`:
  1. Call a new `reconcile_and_count_live_capacity()` helper (lives in
     `live_execution.py`, imported by `discovery_loop.py` — no duplicated
     logic). It first reconciles: fetches `get_all_positions()` from the
     exchange, and for every local `live_executions` row in
     `CLAIMED`/`ENTRY_SUBMITTED`/`ACTIVE`, confirms it still has a matching
     open exchange position; any that don't are closed out here (same
     "exchange going flat is the proof" principle as Demo's
     `reconcile_active_executions()`, reused not reimplemented).
  2. Returns the **post-reconciliation** open count.
  3. If `count >= 4`, or `get_balance()`'s available margin < 10 USDT
     (+ `margin_safety_buffer_usdt`, see §9), skip the *entire* discovery tick
     — no snapshot, no candidate search, no AI calls, `run.py`-style
     `log_event(event="discovery_suppressed_live_capacity")`.
  4. This is what makes PAPER "not open parallel trades just because live is
     full" true by construction (req. 10): PAPER's only source of new
     positions is this same discovery tick, so pausing it pauses both — no
     separate PAPER-side capacity code is needed or added.
- **Layer 2 (authoritative, race-proof)** — inside `live_execution.py`'s own
  tick, **reconciliation now runs first, claiming second** (deliberately the
  reverse of Demo's order): `recover_stale_claims()` →
  `reconcile_active_executions()` → `close_guardian_exit_positions()` →
  `close_time_limit_positions()` → *then* `process_pending_positions()`. By
  the time a new position is
  claimed, the ACTIVE count already reflects this tick's own fresh
  reconciliation. `process_pending_positions()` re-checks
  count<4 and balance immediately before each claim — this is what actually
  closes the race Gate can create by confirming 2 candidates in one discovery
  cycle (confirmed possible from real trade history: two positions opened at
  the identical timestamp `2026-09-04T14:03:54`). If capacity/margin fails
  here, the position simply stays unclaimed — retried every tick, picked up
  the moment a slot frees — while its PAPER leg is completely unaffected.
- `gate/risk_signal_gate.py` and `orchestrator.py` are **not modified**.
  Layer 2 alone is sufficient to guarantee the hard cap even under a race, so
  the Gate/AI pipeline stays byte-for-byte unchanged.

## 8. Order mechanics

- **Leverage: 10x explicit**, `set_leverage(symbol, leverage=10)` before every
  entry — never exchange default (req. 7).
- **Quantity**: fixed notional, not PAPER's `position.size`:
  `quantity = (margin_per_trade_usdt * leverage) / position.simulated_fill_entry`,
  rounded down to `quantityPrecision` (reusing the existing
  `get_contracts()`-derived precision map, same as Demo).
- **Pre-order minimum validation (new, not present in Demo)**: before
  claiming, check the computed quantity against the instrument's exchange
  minimums (`tradeMinQuantity`/minimum-notional field — exact field name
  confirmed against a real `get_contracts()` response during implementation,
  read-only, already-live-called endpoint, no new safety concern). If 10
  USDT margin's resulting notional can't meet the instrument's minimum, or
  rounds to zero quantity, **skip that trade for LIVE safely**: never round
  up past the fixed margin cap to force a fill. Recorded as
  `phase='SKIPPED'`, `last_error='below_exchange_minimum'` — PAPER is
  unaffected.
- **SL/TP attached to the entry order**, same one-request JSON-attachment
  BingX quirk as Demo (`stopLoss`/`takeProfit` params with
  `quantity`+`price`+`stopPrice`+`workingType`, live-verified 2026-09-04).
- **No `reduceOnly` on close** (hedge-mode account, same as Demo).
- **Fill confirmation before marking ACTIVE (new, hardens a Demo
  simplification)**: after the entry order response, query
  `get_order_by_client_order_id()`/`get_order_status()` and confirm
  `status == FILLED` with `executedQty` matching the submitted quantity
  before transitioning the row to `ACTIVE`. `entry_quantity` is recorded from
  the exchange's own **reported `executedQty`**, never the requested
  quantity — so a later close always targets the true owned size. Any
  non-`FILLED` terminal state (rejected, expired) → `phase='FAILED'`,
  `last_error` set, **local state never shows a position as open that isn't
  real** (req.: partial fills/rejections must never desync local vs.
  exchange truth). A market order's own semantics mean an unfilled remainder
  is exchange-cancelled, not left resting — so "partial" here means the
  filled amount becomes the recorded truth, not a rounding assumption.

## 9. Balance / margin check

- `get_balance()` queried at both gating layers (§7) before any order.
- Required available margin per new trade: `margin_per_trade_usdt` (10) +
  `margin_safety_buffer_usdt` (**locked: 1.00 USDT**, confirmed with user) —
  i.e. a trade may only be sent if available margin ≥ 11 USDT. This buffer
  is a separate fail-safe threshold only: it absorbs entry/exit fees so a
  trade never gets exchange-rejected purely for insufficient margin after
  fees (req. 20's "kontrollera saldo innan order", made concrete). It must
  **never** inflate the position itself — the order quantity is still
  computed from exactly `margin_per_trade_usdt=10` × `leverage=10` (§8),
  never from 11. If available margin is short of 11 USDT, no order is
  sent — same skip-safely behavior as §8's minimum-notional case.
- Exchange-level rejection (any response, including insufficient balance)
  still gets the unconditional fail-safe treatment from §10 — the buffer
  reduces how often that path is hit, it does not replace it.

## 10. Retry / error handling policy (unchanged from Demo's proven policy)

- Network/timeout errors before any response: exponential backoff, bounded
  retry count.
- Any response from the exchange (including a rejection): no blind retry.
  `phase='FAILED'`, `last_error` recorded, stop. Never affects PAPER (§3).
- Crash between `CLAIMED` and confirmed response: `recover_stale_claims()`
  looks the order up by its deterministic `clientOrderID` **before** ever
  resubmitting — never a blind retry, identical to Demo's §8.

## 11. `live_executions` table

Same columns as `demo_executions`, plus:

| column | type | notes |
|---|---|---|
| `margin_usdt` | TEXT | fixed 10, recorded per-row for audit even though currently constant |
| `notional_usdt` | TEXT | `margin_usdt * leverage`, recorded for reporting |
| `leverage` | TEXT | fixed 10, recorded per-row |
| `realized_fees_usdt` | TEXT NULL | pulled from BingX income/commission data at close, exact endpoint/fields confirmed live (read-only) during implementation |
| `realized_funding_usdt` | TEXT NULL | same source, NULL when not applicable |

`phase` values: `CLAIMED` → `ENTRY_SUBMITTED` → `ACTIVE` → `CLOSED` /
`FAILED` / `SKIPPED` (new terminal state for §8's minimum-notional skip).
Entirely separate from `positions` and from `demo_executions` — no
foreign-key coupling beyond the shared `position_id` value, no shared write
path with either.

## 12. Guardian-assisted exit

Reused verbatim as a LIVE variant of `close_guardian_exit_positions()`:
never re-runs Guardian's classification (zero extra AI cost, zero
divergence risk), only mirrors a PAPER position **already** closed with
`exit_reason='guardian_exit'` onto the live exchange position. Guardian can
close a LIVE position earlier than 6h when EXIT fires; Guardian structurally
cannot extend a position past 6h — `close_time_limit_positions()`'s 6h check
runs first in the tick, same "time_limit wins as absolute fallback"
ordering Demo/PAPER already established.

## 13. Time-limit — 6h, LIVE-only (confirmed with user)

PAPER's `max_position_hold_hours=24` is unchanged. LIVE's own
`live_execution.yaml::max_position_hold_hours=6` is a new, independent,
tighter override — same "additional, lower, layered cap" relationship as
§4. `close_time_limit_positions()` reuses `compute_hold_hours()` (same
function PAPER/Demo already use), parametrized with LIVE's own 6h instead of
PAPER's 24h.

## 14. Testing strategy

- Connector-level: `respx`-mocked HTTP against the real
  `BingXLiveTradingConnector` class, same pattern as
  `BingXDemoTradingConnector`'s tests. Real network calls forbidden here.
- Orchestration-level (`live_execution.py`/`live_execution_loop.py`): spy/stub
  connector, same pattern as `demo_execution.py`'s tests.
- New targeted tests, on top of Demo's existing coverage pattern:
  - exact-host guard rejects everything except `open-api.bingx.com`.
  - capacity gate blocks a 5th claim even when local DB race-conditions
    would otherwise allow it (simulated stale/discrepant local state vs.
    mocked `get_all_positions()`).
  - margin check blocks an order when `get_balance()` reports insufficient
    available margin, with and without the safety buffer.
  - below-exchange-minimum instrument is skipped (`phase='SKIPPED'`), never
    rounded up.
  - partial-fill/rejected-order responses never leave a row in `ACTIVE`.
  - Guardian-exit mirror never fires before PAPER's own `guardian_exit`
    closure exists.
  - 6h time-limit closes a LIVE position independent of PAPER's 24h state,
    and Guardian cannot extend past it.
  - discovery-level suppression: zero AI calls, zero new candidates, when
    live capacity/margin is exhausted; resumes automatically once reconciled
    count drops or margin frees up.
  - PAPER/Demo behavior is provably unaffected by every LIVE code path added
    (regression tests on existing Demo/PAPER suites, unchanged).
- **Explicitly excluded from this plan's automated tests, and never
  automated at all:** placing a real order against the live account. The
  only real, authenticated call this plan may make before formal activation
  is a **read-only** `get_balance()`/`get_all_positions()`/`get_contracts()`
  verification (to confirm real field names/shapes) — and that call itself
  requires the user's separate, explicit go-ahead in a live conversation
  before it runs, exactly like the Demo design's own live-verification gate.
  No test order, ever, for verification purposes (explicit user requirement).

## 15. SPEC_CRYPTO.md amendment

Same shape as the Demo amendment (§15 of that design): `§1`/`§19`/`§20` gain
a further explicit, narrow exception for LIVE, gated by the exact-host guard,
dedicated `BINGX_API_KEY`/`_SECRET` credentials, the default-off arm flag,
and the two-layer capacity/margin gate — applied in the implementation step.

## 16. Out of scope (explicitly deferred, not forgotten)

- Activating `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED` — a separate, later,
  explicit decision after this plan's full test suite passes.
- SHORT positions (system is LONG-only today; unaffected).
- Any change to PAPER's or Demo's own caps, sizing, or time limits.
- Any change to the 7-role pipeline, Gate, Risk Agent, or Guardian's
  classification logic.

## 17. Signal staleness / TTL control (added 2026-09-06, post-incident — spec-only, NOT YET IMPLEMENTED)

**Incident this section closes:** on first activation, `find_positions_pending_live_execution()`
(§13 below references the same query) claimed and mirrored three PAPER
positions whose Gate `CONFIRMED` decision was ~53 hours old — the process had
been offline since 2026-09-04 and, on restart, PAPER's own
`max_position_hold_hours=24` monitoring closed these same positions
(`time_limit`) in the same second LIVE's independent thread claimed and
opened real orders against them. No staleness/TTL concept existed anywhere
in §1–§16 above; this section defines one. Nothing in §1–§16 is changed by
this addition — it adds a new, additional precondition to §7's existing
capacity gate, evaluated even earlier in the same pipeline.

### 17.1 Requirement

A LIVE entry may mirror a PAPER/Gate-`CONFIRMED` signal **only if the
signal's age is within an explicit LIVE TTL** at the moment of the
eligibility check. This is a new, independent, LIVE-only precondition,
layered in front of §7's existing two-layer capacity/margin gate — it does
not replace or relax either layer.

**TTL value: not yet decided.** No number is fixed by this section — that is
an explicit, separate user decision required before implementation (see
§17.7). This section specifies the mechanism and invariants only.

### 17.2 TTL is LIVE-specific, never inherited from PAPER

`live_execution.yaml` gains its own field (name TBD at implementation time,
e.g. `signal_ttl_seconds`) — a new, independent `LiveExecutionConfig` value,
following the exact same "additional, lower, layered cap" relationship §4
and §13 already establish for `max_concurrent_positions` and
`max_position_hold_hours`. It must **never** be derived from, defaulted to,
or silently fall back to PAPER's `risk_limits.max_position_hold_hours`
(currently 24h) — the two settings answer different questions (PAPER's
governs how long a position may stay open; LIVE's TTL governs how old a
*not-yet-opened* signal may be before LIVE is no longer allowed to act on
it at all) and must be able to move independently. A missing/unset TTL must
fail closed (treat as "no signal is fresh enough," never as "no limit").

### 17.3 Authoritative signal timestamp — explicitly NOT LIVE claim time

The age calculation is `now - signal_confirmed_at`. **`signal_confirmed_at`
is the timestamp of the candidate's `CANDIDATE_TRANSITIONED` event with
`to: "CONFIRMED"`** (the moment Gate actually confirmed the signal) — sourced
from the append-only `events` table (`event_type='CANDIDATE_TRANSITIONED'`,
`payload.to='CONFIRMED'`, `aggregate_id=candidate_id`), not a mutable column,
so it cannot be silently altered by later, unrelated writes.

Two things this timestamp is explicitly **NOT**, both confirmed wrong during
the incident's forensic trace:
- **Not `positions.opened_at`.** In the current schema this column is
  stamped with the candidate's original *discovery-creation* time, not the
  Gate-confirmation time or the actual position-open moment — confirmed
  during the incident audit, where `opened_at` predated `CONFIRMED` by 11–20
  minutes for the affected positions. Any implementation must verify this
  quirk still holds (or has been fixed elsewhere) before trusting
  `opened_at` for anything staleness-related, and should prefer the
  `events`-table source above regardless.
- **Not `live_executions.claimed_at`.** Claim time measures how long *LIVE
  itself* took to notice the signal, not how old the underlying analysis is
  — using it would make every signal appear "fresh" at the exact moment of
  claim by construction, defeating the entire purpose of this control.

If `candidates.updated_at` is used as an implementation-convenience proxy for
the same moment, it must first be verified that nothing (Guardian, QA,
retries, replay) ever touches a candidate row again after it reaches
`CONFIRMED` — otherwise `updated_at` drifts forward and silently
under-reports signal age. The `events`-table source has no such risk and is
the preferred canonical source.

### 17.4 Where the check runs, and what it must prevent

The TTL check must run **before any claim or submission attempt** — earlier
in the pipeline than §7's Layer 2 capacity/margin check, not merged into it.
Concretely: the check happens either as a filter condition on the same query
`find_positions_pending_live_execution()` uses (joining to the confirming
event/timestamp and excluding anything older than the TTL from the result
set entirely), or as an immediate post-fetch, pre-claim check in
`process_pending_positions()` before `claim_live_execution()` is ever called
for that row. Either shape must guarantee that a signal older than TTL:

- is **never** passed to `claim_live_execution()` (no `live_executions` row
  is ever created for it — not `CLAIMED`, not any other phase, unless
  §17.5's own explicit skip-record applies),
- is **never** submitted to BingX (`place_entry_order_with_sl_tp()` is never
  called),
- **never triggers any AI/Anthropic call** (already true structurally today
  — LIVE's code path has zero AI/agent imports, see the forensic audit this
  section follows from — this requirement makes that existing property an
  explicit, permanent invariant rather than an implicit side effect that a
  future change could accidentally break),
- **never results in a LIVE order** of any kind.

### 17.5 Deterministic handling — never retried

Because signal age is monotonically non-decreasing (a signal only ever gets
older, never fresher), the TTL exclusion is naturally stable: a signal
excluded on one tick is excluded on every subsequent tick too, with no risk
of "flapping" and no persisted state required for correctness. This alone
satisfies "never retried" for the *order-placement* side.

For observability, a stale exclusion must still be recorded durably enough
that a future forensic audit does not again require reconstructing the
decision from log files alone (as this incident did) — one of:
- a structured `log_event(event="live_signal_stale_skipped", position_id=...,
  candidate_id=..., signal_confirmed_at=..., signal_age_seconds=...,
  ttl_seconds=...)` on first exclusion, or
- reusing the existing `SKIPPED` phase/`mark_live_execution_skipped()`
  pattern (§8's `below_exchange_minimum` case) with reason
  `stale_signal_ttl_exceeded` — **only if** doing so can be made compatible
  with never claiming a stale signal (§17.4); if the existing skip pattern
  requires a claim first, a stale signal must use a different, claim-free
  recording mechanism instead of adopting that pattern as-is.

is required at minimum; a durable DB record is preferred if achievable
without contradicting §17.4.

### 17.6 The race this section closes

The observed incident was not purely a missing-TTL problem — `positions`
was already `status='CLOSED'` (PAPER's own `time_limit` closure) roughly one
second *before* LIVE's independent thread read it as `'OPEN_POSITION'` and
claimed it. `monitoring_loop.py` (PAPER's closer) and `live_execution_loop.py`
(LIVE's claimer) run as fully independent threads, each computing its own
`now = datetime.now(UTC)`, with no lock or rendezvous between PAPER's closing
decision and LIVE's claiming decision for the same `position_id`. Two
independent, mandatory defenses, not one:

1. **TTL as the primary defense.** A TTL meaningfully smaller than PAPER's
   own `max_position_hold_hours` (currently 24h) means a signal old enough
   for PAPER to be closing it for time-limit reasons will, in the
   overwhelming majority of cases, already have been excluded by §17.4 long
   before PAPER's own closing logic ever runs — collapsing the exposure
   window from "days, across a process restart" to, at most, a fraction of
   the TTL itself.
2. **A mandatory re-check at claim time, as a second, independent layer.**
   TTL exclusion alone does not make the specific race structurally
   impossible (a signal could in principle be claimed a moment before
   crossing the TTL boundary while PAPER independently closes it for an
   unrelated reason — SL, TP, or a manual action — not just time-limit).
   The claim step itself must therefore re-verify `positions.status =
   'OPEN_POSITION'` immediately adjacent to (ideally inside the same
   transaction as) the atomic claim insert — never relying on a `Position`
   object fetched earlier in the same tick. If PAPER has closed the position
   for any reason by the time the claim actually executes, the claim must
   fail closed (no row created, no order placed), the same way a duplicate
   LIVE claim already fails closed today via `claim_live_execution()`'s
   existing idempotent-insert guard.

### 17.7 Explicit invariant: a stale DB row is never sufficient reason to open

LIVE must never open a position for the sole reason that a `positions` row
with `status='OPEN_POSITION'` still exists in the database. `OPEN_POSITION`
status is necessary but not sufficient; §17.1's TTL check and §17.6's
claim-time re-check are both required in addition, every time, with no
code path that bypasses either for any reason (including manual replay,
backfill, or crash recovery).

### 17.8 Restart safety and idempotency

- The TTL check is a pure function of `(now, signal_confirmed_at, ttl)` —
  it is recomputed from scratch on every tick, including the very first
  tick after any restart, and depends on no in-memory state carried between
  process runs. A signal already stale before a restart is therefore
  excluded on the first tick after that restart, exactly as it would have
  been excluded on any tick before the restart — there is no "grace window"
  or "first tick after restart" exception.
- A signal that was correctly excluded as stale before a restart must never
  become eligible again after a restart merely because whatever recorded
  that exclusion (§17.5) was in-memory or was lost. Since exclusion here is
  computed, not stored, this holds automatically — an implementation must
  not "optimize" this into a cached/stored yes-or-no decision that could
  itself go stale or be lost.
- Symmetrically: nothing about this TTL check may depend on
  `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED` having been continuously set since
  the signal was confirmed. The exact incident this section closes was
  triggered by the flag being set for the *first* time long after the
  signal existed — the TTL check must handle "signal confirmed while LIVE
  was off, discovered stale only once LIVE turns on" as the normal case, not
  an edge case.

### 17.9 Required test coverage (to accompany the future implementation)

All of the following must be covered by new or existing automated tests
before this control is considered implemented — none may be satisfied by
manual/live verification alone:

1. **Fresh signal → eligible.** A signal well within TTL is claimed and
   submitted normally (no regression to §7/§8's existing behavior).
2. **Signal exactly at the TTL boundary → defined behavior.** The boundary
   is inclusive of eligibility: `age <= TTL` is eligible, `age > TTL` is
   stale. The exact-boundary case must have a dedicated test asserting this
   specific rule (not just "some" cutoff behavior).
3. **Signal older than TTL → rejected/stale.** No claim, no order, and
   (§17.5) a durable/observable record of why.
4. **Very old signal (e.g. multi-day) → never LIVE, ever**, regardless of
   how many ticks are run against it — confirms the exclusion is stable
   across repeated ticks, not a one-time check that could be bypassed on a
   later attempt.
5. **PAPER time-limit race → LIVE must not open.** A regression test
   reproducing the exact incident shape: a position simultaneously eligible
   for PAPER's time-limit closure and old enough to exceed LIVE's TTL must
   result in zero LIVE claims/orders, deterministically, not just "usually."
6. **Restart with a stale pending signal → no LIVE order.** A signal
   confirmed while LIVE was disabled, already older than TTL by the time
   LIVE is enabled and its thread starts, must be excluded on that very
   first tick.
7. **4 LIVE positions → no discovery/AI spend** (existing §7 Layer 1
   behavior — included here as a required regression test so the new TTL
   check is proven not to interfere with or weaken the existing capacity
   suppression).
8. **Margin below `margin_per_trade_usdt + margin_safety_buffer_usdt` → no
   LIVE entry** (existing §9 behavior — same rationale: a required
   regression test proving the new TTL check composes correctly with the
   existing margin gate rather than replacing or short-circuiting it).
