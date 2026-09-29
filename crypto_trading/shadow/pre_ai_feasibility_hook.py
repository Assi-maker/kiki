"""Discovery-side wiring of the SHADOW pre-AI feasibility check (2026-09-29).

Called once per discovery cycle with the freshly created candidates, BEFORE
the AI chain. It reads current equity (one GET), the open LIVE exposure
(local state, read-only) and the cycle's own 30m klines, assesses every
candidate and stores the verdict in pre_ai_feasibility_shadow. It returns
nothing and never raises: the discovery cycle continues exactly as before,
every candidate still goes to the AI chain, the Gate and the Safety Kernel.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from crypto_trading.logging import log_event
from crypto_trading.shadow import pre_ai_feasibility as paf


class _B:  # minimal bar view for atr_pct
    __slots__ = ("h", "l", "c")

    def __init__(self, kline):
        self.h, self.l, self.c = kline.high, kline.low, kline.close  # noqa: E741


def closed_30m_atr(klines, now: datetime, bars_n: int) -> float | None:
    closed = [k for k in klines if k.observed_at + timedelta(minutes=30) <= now][-bars_n:]
    return paf.atr_pct([_B(k) for k in closed]) if len(closed) >= bars_n - 4 else None


def build_hook(repo, live_connector, settings):
    """Returns hook(candidates, snapshot, run_id). `live_connector` is only
    used for get_balance() (read-only)."""
    from crypto_trading.paper_trading.live_execution import _open_live_exposures  # read-only helper

    calib = paf.Calibration.load()

    def hook(candidates, snapshot, run_id: str) -> None:
        try:
            now = snapshot.simulated_now
            try:
                raw = live_connector.get_balance().get("equity")
                equity = Decimal(str(raw)) if raw not in (None, "") else None
            except Exception:  # noqa: BLE001 - unknown equity -> every verdict UNKNOWN
                equity = None
            exposures = _open_live_exposures(repo, settings)
            open_symbols = {e.symbol for e in exposures}
            counts = {paf.FEASIBLE: 0, paf.INFEASIBLE: 0, paf.UNKNOWN: 0}
            flagged = 0
            for c in candidates:
                if c.status != "CANDIDATE":
                    continue
                ticker = snapshot.tickers.get(c.instrument)
                price = float(ticker.last_price) if ticker is not None else None
                record = paf.assess(
                    symbol=c.instrument, price=price,
                    atr30_pct=closed_30m_atr(snapshot.klines.get(c.instrument, []), now, calib.atr_bars),
                    equity=equity, open_exposures=exposures, open_symbols=open_symbols,
                    max_positions=settings.live_execution.max_concurrent_positions,
                    margin_usdt=settings.live_execution.margin_per_trade_usdt,
                    leverage=settings.live_execution.leverage, limits=settings.safety, calib=calib,
                )
                closes = [k.close for k in snapshot.klines.get(c.instrument, []) if k.observed_at <= now]
                record.update({"candidate_id": c.candidate_id, "signal_at": c.created_at.isoformat(),
                               "run_id": run_id, "calibration_frozen_on": calib.frozen_on,
                               "failure_hypotheses": [paf.no_momentum_4h(closes)]})
                repo.save_pre_ai_feasibility(record, now)
                counts[record["pre_ai_feasible"]] += 1
                flagged += bool(record["failure_hypotheses"][0]["flag"])
            log_event(run_id, event="pre_ai_feasibility_shadow", no_momentum_4h_flagged=flagged,
                      **{f"n_{k}": v for k, v in counts.items()})
        except Exception as exc:  # noqa: BLE001 - shadow measurement never disturbs discovery
            log_event(run_id, event="pre_ai_feasibility_shadow_failed", error_type=type(exc).__name__,
                      error=str(exc)[:300])

    return hook
