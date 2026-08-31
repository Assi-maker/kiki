from __future__ import annotations

import time
from datetime import UTC, datetime

from crypto_trading.agents.runner import AgentRunner
from crypto_trading.config.loader import Settings
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.market_snapshot import LiveMarketDataSource, build_live_snapshot
from crypto_trading.paper_trading.replay import run_single_cycle
from crypto_trading.schemas.trade import Position
from crypto_trading.storage.repository import Repository


def run_discovery_tick(
    connector: LiveMarketDataSource,
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    news_connector: object | None = None,
    external_data_connector: object | None = None,
) -> list[Position]:
    """En periodisk discovery-tick (SPEC §7, PLAN_CRYPTO_PHASE5.md Task 7):
    bygger en live `MarketSnapshot` (Task 6) och kör den genom exakt samma
    `run_single_cycle()`-pipeline som `replay.py` (Task 5/Beslut 1) - ingen
    duplicerad pipeline-logik, ingen skillnad mellan replay och live utöver
    varifrån snapshoten kommer. Det dagliga AI-anropstaket och
    `ANALYSIS_INTERRUPTED`-återupptagningen (Task 4) körs oförändrat inuti
    `run_single_cycle -> run_discovery_cycle`, aldrig kringgått här.

    Fail-safe på loop-nivå (Global Constraints, SPEC §8.3): ett oväntat
    undantag - connector nere, ett programmeringsfel mitt i en candidates
    analys, vad som helst - kraschar aldrig anroparen (`run_forever`).
    Det fångas, loggas och skrivs till `runs.errors`; en candidate som redan
    hann bli `UNDER_AI_ANALYSIS` innan kraschen läks av nästa ticks
    `sweep_interrupted_analyses` + återupptagningspolicy (Task 4) - ingen ny
    recovery-mekanism behövs här, den är redan komponerad av de tidigare
    tasken.

    `clock=lambda: datetime.now(UTC)` (bugfix 2026-08-31, bekräftad mot en
    riktig live-körning): `build_live_snapshot()`s staleness-kontroll för
    varje hämtad post bedöms mot en färsk tidpunkt tagen direkt efter just
    den postens nätverksanrop, inte mot detta `now` (fånget här, före hela
    den sekventiella hämtningsloopen). Utan detta blev varje instrument som
    hämtades mer än några sekunder in i en flera-minuter-lång live-hämtning
    felaktigt `data_quality_invalid` - se market_snapshot.py::
    build_live_snapshot() för full förklaring."""
    run_id = new_run_id()
    now = datetime.now(UTC)
    repo.start_run(run_id, "discovery", now)
    try:
        snapshot = build_live_snapshot(connector, settings, now, clock=lambda: datetime.now(UTC))
        positions = run_single_cycle(
            snapshot,
            repo,
            runner,
            settings,
            run_id,
            news_connector=news_connector,
            external_data_connector=external_data_connector,
        )
        repo.complete_run(
            run_id, datetime.now(UTC), "ok", [], instruments_scanned=len(snapshot.instruments)
        )
        return positions
    except Exception as exc:
        log_event(
            run_id, event="discovery_tick_failed", error_type=type(exc).__name__, error=str(exc)
        )
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])
        return []


def run_forever(
    connector: LiveMarketDataSource,
    repo: Repository,
    runner: AgentRunner,
    settings: Settings,
    news_connector: object | None = None,
    external_data_connector: object | None = None,
) -> None:
    while True:
        run_discovery_tick(
            connector,
            repo,
            runner,
            settings,
            news_connector=news_connector,
            external_data_connector=external_data_connector,
        )
        time.sleep(settings.pipeline.discovery_interval_minutes * 60)
