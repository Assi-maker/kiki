from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse

from crypto_trading.config.loader import Settings
from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.schemas.assessments import AssessmentBase
from crypto_trading.schemas.candidate import Candidate
from crypto_trading.schemas.forecast import ForecastRecord
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository

RepositoryFactory = Callable[[], Repository]

_TOP_N_UNAVAILABLE = "unavailable — not persisted historically"
_UNREALIZED_PNL_UNAVAILABLE = "unavailable — live price not persisted"
_MFE_MAE_STATUS = "not yet tracked"
_DEFAULT_PAGE_LIMIT = 50
_MAX_PAGE_LIMIT = 500
_RATE_LIMIT_EVENTS_UNAVAILABLE = "unavailable — throttle decisions not persisted (known gap)"
_CALIBRATION_NOT_AVAILABLE_YET = (
    "not_available_yet — Phase 8 (Brier score / calibration curve require "
    "accumulated ForecastRecord history and a central calibration module)"
)
_PERFORMANCE_NOT_AVAILABLE_REASON = (
    "Win rate, expectancy, drawdown, profit factor and cumulative PnL require a "
    "central calculation source, deliberately deferred to Phase 8 to avoid "
    "duplicated business logic."
)

_ASSESSMENT_FIELD_NAMES = (
    "news_sentiment",
    "technical",
    "bull_thesis",
    "forecast",
    "risk",
    "bear_adversarial",
    "qa",
)

_IN_PROGRESS_STATUSES = ("CANDIDATE", "UNDER_AI_ANALYSIS", "ANALYSIS_INTERRUPTED")

_FRONTEND_INDEX_HTML = Path(__file__).resolve().parent / "frontend" / "index.html"


def create_app(repo_factory: RepositoryFactory, settings: Settings) -> FastAPI:
    """Fas 7: lokal, strikt read-only dashboard. Läser uteslutande via
    `Repository` - samma sanningskälla som notify_loop.py/notify/telegram.py
    (Fas 6) redan använder. Ingen route i denna app är någonsin
    POST/PUT/PATCH/DELETE (SPEC §13 AC1) - varje ny vy lägger bara till fler
    GET-routes.

    `repo_factory` anropas EN gång per request, aldrig en delad instans -
    samma "egen anslutning per körningskontext"-princip som redan gäller
    discovery/monitoring/notify-trådarna (run.py), fast på request-nivå: en
    sqlite3-anslutning är trådbunden, och FastAPI kör synkrona route-
    funktioner i en threadpool (en ny tråd per anrop), så en enda delad
    Repository-instans skulle krascha med `sqlite3.ProgrammingError` på
    andra requesten. Ingen ändring i storage/db.py eller
    storage/repository.py krävdes för detta - lösningen är helt intern i
    dashboard-paketet."""
    app = FastAPI(title="crypto_trading dashboard")
    app.state.settings = settings

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_FRONTEND_INDEX_HTML, media_type="text/html")

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/live")
    def live() -> dict:
        repo = repo_factory()
        in_progress = [
            _candidate_summary(repo, c)
            for status in _IN_PROGRESS_STATUSES
            for c in repo.find_candidates_by_status(status)
        ]
        confirmed = [
            _candidate_summary(repo, c) for c in repo.find_candidates_by_status("CONFIRMED")
        ]
        no_trade = [_candidate_summary(repo, c) for c in repo.find_candidates_by_status("NO_TRADE")]
        open_positions = [_position_summary(p) for p in repo.find_open_positions()]

        return {
            "last_discovery_run": repo.find_latest_run("discovery"),
            "top_n_instruments": _TOP_N_UNAVAILABLE,
            "in_progress_candidates": in_progress,
            "confirmed_candidates": confirmed,
            "no_trade_candidates": no_trade,
            "open_positions": open_positions,
            "risk_exposure": _risk_exposure(repo, settings),
        }

    @app.get("/api/trade-history")
    def trade_history(
        limit: int = Query(default=_DEFAULT_PAGE_LIMIT, ge=1, le=_MAX_PAGE_LIMIT),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        repo = repo_factory()
        candidates = [_candidate_summary(repo, c) for c in repo.find_all_candidates(limit, offset)]
        positions = [
            _trade_history_position_summary(p) for p in repo.find_all_positions(limit, offset)
        ]
        return {"candidates": candidates, "positions": positions}

    @app.get("/api/system-health")
    def system_health(
        limit: int = Query(default=_DEFAULT_PAGE_LIMIT, ge=1, le=_MAX_PAGE_LIMIT),
    ) -> dict:
        repo = repo_factory()
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "recent_runs": [_run_summary(r) for r in repo.find_recent_runs(limit)],
            "ai_calls_today": repo.count_ai_calls_since(day_start),
            "budget_limited_candidates_today": repo.count_candidates_by_status_since(
                "BUDGET_LIMITED", day_start
            ),
            "failed_runs_today": repo.count_runs_by_status_since("error", day_start),
            "rate_limit_events": _RATE_LIMIT_EVENTS_UNAVAILABLE,
        }

    @app.get("/api/forecast")
    def forecast(
        limit: int = Query(default=_DEFAULT_PAGE_LIMIT, ge=1, le=_MAX_PAGE_LIMIT),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        repo = repo_factory()
        forecasts = [_forecast_summary(f) for f in repo.find_all_forecasts(limit, offset)]
        return {"forecasts": forecasts, "calibration": _CALIBRATION_NOT_AVAILABLE_YET}

    @app.get("/api/performance")
    def performance() -> dict:
        return {"status": "not_available_yet", "reason": _PERFORMANCE_NOT_AVAILABLE_REASON}

    return app


def _candidate_summary(repo: Repository, candidate: Candidate) -> dict:
    evidence = candidate.evidence_record
    return {
        "candidate_id": candidate.candidate_id,
        "instrument": candidate.instrument,
        "status": candidate.status,
        "candidate_score": evidence.candidate_score,
        "trigger_reasons": evidence.trigger_reasons,
        "assessments_status": {
            field_name: _assessment_status(getattr(candidate, field_name))
            for field_name in _ASSESSMENT_FIELD_NAMES
        },
        "gate_decision": repo.get_gate_decision(candidate.candidate_id),
    }


def _assessment_status(assessment: AssessmentBase | None) -> str | None:
    return assessment.status if assessment is not None else None


def _position_summary(position: Position) -> dict:
    return {
        "position_id": position.position_id,
        "instrument": position.instrument,
        "direction": position.direction,
        "entry": str(position.simulated_fill_entry),
        "stop_loss": str(position.stop_loss),
        "target": str(position.target),
        "unrealized_pnl": _UNREALIZED_PNL_UNAVAILABLE,
    }


def _trade_history_position_summary(position: Position) -> dict:
    """TRADE HISTORY (Fas 7): till skillnad från _position_summary() (LIVE,
    bara öppna positioner) täcker denna hela livscykeln inkl. PnL för
    stängda positioner. PnL beräknas via paper_trading.execution.compute_pnl
    - samma, enda centrala funktion som notify/telegram.py::
    format_closed_message() (Fas 6) redan använder, ingen ny formel."""
    pnl = str(compute_pnl(position)) if position.status == "CLOSED" else None
    return {
        "position_id": position.position_id,
        "candidate_id": position.candidate_id,
        "instrument": position.instrument,
        "direction": position.direction,
        "status": position.status,
        "entry": str(position.simulated_fill_entry),
        "exit": (
            str(position.simulated_fill_exit) if position.simulated_fill_exit is not None else None
        ),
        "pnl": pnl,
        "fees": str(position.fees) if position.fees is not None else None,
        "funding": str(position.funding) if position.funding is not None else None,
        "exit_reason": position.exit_reason,
        "fill_model_version": position.fill_model_version,
        "mfe": None,
        "mae": None,
        "mfe_mae_status": _MFE_MAE_STATUS,
    }


def _run_summary(run: dict) -> dict:
    """SYSTEM HEALTH (Fas 7): en `runs`-rad, oformaterad, plus en trivial
    visningsberäkning av varaktighet (två redan hämtade timestamps) - ingen
    ny mätpunkt, ingen ny logik."""
    started_at = run["started_at"]
    completed_at = run["completed_at"]
    duration_seconds = None
    if started_at is not None and completed_at is not None:
        duration_seconds = (
            datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)
        ).total_seconds()
    return {
        "run_id": run["run_id"],
        "run_type": run["run_type"],
        "started_at": started_at,
        "completed_at": completed_at,
        "status": run["status"],
        "errors": json.loads(run["errors"]) if run["errors"] else [],
        "instruments_scanned": run["instruments_scanned"],
        "duration_seconds": duration_seconds,
    }


def _forecast_summary(forecast: ForecastRecord) -> dict:
    return {
        "forecast_id": forecast.forecast_id,
        "candidate_id": forecast.candidate_id,
        "instrument": forecast.instrument,
        "scenario_probabilities": forecast.scenario_probabilities,
        "horizon": forecast.horizon,
        "forecast_version": forecast.forecast_version,
        "actual_outcome": forecast.actual_outcome,
    }


def _risk_exposure(repo: Repository, settings: Settings) -> dict:
    risk_limits = settings.risk_limits
    return {
        "open_positions_count": repo.count_open_positions(),
        "open_positions_notional": str(repo.sum_open_positions_notional()),
        "max_concurrent_positions": risk_limits.max_concurrent_positions,
        "max_total_exposure_pct": str(risk_limits.max_total_exposure_pct),
        "starting_capital_usdt": str(risk_limits.starting_capital_usdt),
    }
