from __future__ import annotations

import os
import threading

import uvicorn
from fastapi import FastAPI

from crypto_trading import demo_execution_loop, detective_loop, discovery_loop, monitoring_loop, notify_loop
from crypto_trading.agents.runner import AgentRunner, RealClaudeRunner
from crypto_trading.config.exceptions import ConfigError
from crypto_trading.config.loader import Settings, get_settings, is_demo_execution_enabled
from crypto_trading.connectors.bingx_demo_trading import BingXDemoTradingConnector
from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
from crypto_trading.connectors.external_data import ExternalDataConnector
from crypto_trading.connectors.news_rss import NewsRSSConnector
from crypto_trading.dashboard.api import RepositoryFactory, create_app
from crypto_trading.logging import log_event, new_run_id
from crypto_trading.notify.telegram import TelegramNotifier
from crypto_trading.storage.repository import SQLiteRepository


def build_runner_from_env() -> AgentRunner:
    """Väljer alltid `RealClaudeRunner` om `ANTHROPIC_API_KEY` finns i
    miljön, annars fail-fast med `ConfigError` - denna processens startpunkt
    startar aldrig tyst med en mock i produktion (PLAN_CRYPTO_PHASE5.md
    Task 9/Beslut 7). Modell/timeout/retries är deploy-tidskonfiguration via
    miljövariabler, inte YAML (samma kategori som redan `.env`-hanterade
    secrets, SPEC §17)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ConfigError("ANTHROPIC_API_KEY saknas - kan inte starta med RealClaudeRunner")
    return RealClaudeRunner(
        api_key=api_key,
        model=os.environ.get("CRYPTO_TRADING_CLAUDE_MODEL", "claude-sonnet-5"),
        timeout_seconds=float(os.environ.get("CRYPTO_TRADING_AGENT_TIMEOUT_SECONDS", "60")),
        max_retries=int(os.environ.get("CRYPTO_TRADING_AGENT_MAX_RETRIES", "3")),
    )


def build_screener_runner_from_env() -> AgentRunner:
    """Kostnadsoptimering (2026-09-02): separat, billigare/snabbare
    RealClaudeRunner för Opportunity Screener-etappen (screening/
    candidate_engine.py::apply_opportunity_screening) - samma
    ANTHROPIC_API_KEY (ett Anthropic-konto täcker alla modeller), samma
    fail-fast-princip som build_runner_from_env() ovan, men en egen
    modell-env-variabel så screeningens modell kan bytas oberoende av den
    fulla 7-rollskedjans (CRYPTO_TRADING_CLAUDE_MODEL, orörd)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ConfigError("ANTHROPIC_API_KEY saknas - kan inte starta med RealClaudeRunner")
    return RealClaudeRunner(
        api_key=api_key,
        model=os.environ.get("CRYPTO_TRADING_SCREENER_MODEL", "claude-haiku-4-5"),
        timeout_seconds=float(os.environ.get("CRYPTO_TRADING_AGENT_TIMEOUT_SECONDS", "60")),
        max_retries=int(os.environ.get("CRYPTO_TRADING_AGENT_MAX_RETRIES", "3")),
    )


def build_detective_runner_from_env() -> AgentRunner:
    """Post-Trade Analyst (Detective, 2026-09-04) - egen RealClaudeRunner-
    instans, ALDRIG samma instans som runner/screener_runner: instansen bär
    mutabel last_call_billed/last_call_cost_usd-state (agents/runner.py)
    som skulle racea om två trådar delade den (Detective kör i sin egen
    tråd, se _run_detective_forever()). Default till samma billiga modell
    som screeningen (Haiku 4.5, kostnadskontrollerad batchanalys, inte
    realtidshandelsbeslut) - egen env-variabel så den kan bytas oberoende
    av CRYPTO_TRADING_CLAUDE_MODEL/CRYPTO_TRADING_SCREENER_MODEL."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ConfigError("ANTHROPIC_API_KEY saknas - kan inte starta med RealClaudeRunner")
    return RealClaudeRunner(
        api_key=api_key,
        model=os.environ.get("CRYPTO_TRADING_DETECTIVE_MODEL", "claude-haiku-4-5"),
        timeout_seconds=float(os.environ.get("CRYPTO_TRADING_AGENT_TIMEOUT_SECONDS", "60")),
        max_retries=int(os.environ.get("CRYPTO_TRADING_AGENT_MAX_RETRIES", "3")),
    )


def build_notifier_from_env() -> TelegramNotifier | None:
    """Fas 6 Beslut 2: Telegram är valfritt, INTE fail-fast som
    ANTHROPIC_API_KEY - systemet fungerar helt utan den (bara notify-tråden
    uteblir), samma mönster som news_connector/external_data_connector
    (Fas 5.5) redan hanteras som valfria icke-kritiska källor."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id:
        return None
    return TelegramNotifier(bot_token=bot_token, chat_id=chat_id)


def build_demo_trading_connector_from_env() -> BingXDemoTradingConnector | None:
    """Opt-in, same pattern as build_notifier_from_env(): if the dedicated
    demo credentials aren't set, the thread simply doesn't start - never a
    fail-fast requirement like ANTHROPIC_API_KEY, since PAPER trading works
    completely without it. Deliberately reads ONLY the dedicated
    CRYPTO_TRADING_BINGX_DEMO_API_KEY/_SECRET names, never the generic
    BINGX_API_KEY/BINGX_API_SECRET also present in .env (2026-09-04 design
    decision - a generic name risks accidental reuse by a future live-
    account integration)."""
    api_key = os.environ.get("CRYPTO_TRADING_BINGX_DEMO_API_KEY")
    api_secret = os.environ.get("CRYPTO_TRADING_BINGX_DEMO_API_SECRET")
    if not api_key or not api_secret:
        return None
    return BingXDemoTradingConnector(api_key=api_key, api_secret=api_secret)


def build_dashboard_app_from_env(
    repo_factory: RepositoryFactory, settings: Settings
) -> FastAPI | None:
    """Fas 7: dashboarden är valfri, samma opt-in-princip som Telegram
    (build_notifier_from_env() ovan) - systemet fungerar helt utan den (bara
    dashboard-tråden uteblir), aldrig ett krav för discovery/monitoring/
    notify. Ingen secret att gate:a på (till skillnad från Telegram), så en
    explicit boolesk env-flagga används istället."""
    if not os.environ.get("CRYPTO_TRADING_DASHBOARD_ENABLED"):
        return None
    return create_app(repo_factory, settings)


def _run_discovery_forever(
    connector: BingXMarketDataConnector,
    runner: AgentRunner,
    settings: Settings,
    news_connector: NewsRSSConnector | None,
    external_data_connector: ExternalDataConnector | None,
    screener_runner: AgentRunner | None = None,
) -> None:
    """Konstruerar sin egen Repository (och därmed sqlite3-anslutning) HÄR,
    inne i den tråd som faktiskt kör discovery-loopen. En sqlite3-anslutning
    är trådbunden (check_same_thread=True som default i storage/db.py) -
    AC3-live-körningen 2026-08-28 kraschade omedelbart med
    sqlite3.ProgrammingError eftersom Repository tidigare konstruerades i
    huvudtråden (main()) och sedan skickades in i denna threading.Thread.
    Samma mönster som redan används i
    tests/crypto_trading/storage/test_repository_concurrency.py."""
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    discovery_loop.run_forever(
        connector,
        repo,
        runner,
        settings,
        news_connector=news_connector,
        external_data_connector=external_data_connector,
        screener_runner=screener_runner,
    )


def _run_monitoring_forever(connector: BingXMarketDataConnector, settings: Settings) -> None:
    """Samma trådbundna-anslutning-fix som _run_discovery_forever() ovan,
    monitoring-sidan."""
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    monitoring_loop.run_forever(connector, repo, settings)


def _run_detective_forever(runner: AgentRunner, settings: Settings) -> None:
    """Samma trådbundna-anslutning-fix som _run_discovery_forever()/
    _run_monitoring_forever() ovan - Detective (Post-Trade Analyst) är en
    femte, oberoende tråd."""
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    detective_loop.run_forever(repo, runner, settings)


def _run_demo_execution_forever(
    market_data_connector: BingXMarketDataConnector,
    demo_connector: BingXDemoTradingConnector,
    settings: Settings,
) -> None:
    """Same thread-bound-connection fix as the other _run_*_forever()
    functions above. quantity_precision_by_symbol is built ONCE here from
    the existing, read-only get_contracts() - not re-fetched every tick."""
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    contracts = market_data_connector.get_contracts()
    quantity_precision_by_symbol = {
        c["symbol"]: int(c.get("quantityPrecision", 0)) for c in contracts
    }
    demo_execution_loop.run_forever(repo, demo_connector, quantity_precision_by_symbol, settings)


def _run_notify_forever(notifier: TelegramNotifier, settings: Settings) -> None:
    """Samma trådbundna-anslutning-fix som _run_discovery_forever()/
    _run_monitoring_forever() ovan - Fas 6:s tredje, oberoende loop."""
    repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    notify_loop.run_forever(notifier, repo, settings)


def _run_dashboard_forever(app: FastAPI, settings: Settings) -> None:
    """Fas 7:s fjärde, oberoende tråd. Till skillnad från de tre ovan
    konstruerar denna INTE en delad Repository här - `app` byggdes redan i
    build_dashboard_app_from_env() med en `repo_factory` som `dashboard/
    api.py` anropar EN gång per HTTP-request (se dess docstring för varför:
    en delad sqlite3-anslutning skulle krascha när FastAPI kör synkrona
    route-funktioner i sin egen threadpool). `uvicorn.run()` blockerar denna
    tråd, exakt som `discovery_loop.run_forever()`/`monitoring_loop.
    run_forever()`/`notify_loop.run_forever()` gör i sina respektive
    trådar.

    Code-review-fynd (2026-08-30): `uvicorn.run()` kan misslyckas direkt vid
    start (t.ex. porten redan upptagen) - utan denna try/except dog felet
    tyst för projektets egen loggning (syntes bara i uvicorns egen stderr,
    aldrig via `log_event()`), samma disciplin som redan gäller
    `run_discovery_tick()`/`run_monitoring_tick()`/`run_notify_tick()`.
    Processen/övriga trådar kraschar aldrig av detta oavsett (ett undantag i
    en icke-huvudtråd stoppar bara den tråden) - men nu syns felet även i
    den strukturerade loggen.

    `except (Exception, SystemExit)`, INTE bara `Exception` - empiriskt
    verifierat (manuell körning mot en redan upptagen port) att uvicorns
    egen `Server.run()` vid ett bindningsfel INTE kastar ett vanligt
    Python-undantag utan `SystemExit(3)` (via dess interna felhantering,
    loggat till uvicorns EGEN logger innan den avslutar). `SystemExit` ärver
    `BaseException`, inte `Exception` - ett rent `except Exception` hade
    sett ut att fånga felet (testat med en OSError-mock) men aldrig
    fångat/loggat det VERKLIGA felet. Fångar medvetet inte bredare
    `BaseException` (skulle även svälja `KeyboardInterrupt`)."""
    run_id = new_run_id()
    try:
        uvicorn.run(
            app, host=settings.dashboard.host, port=settings.dashboard.port, log_level="warning"
        )
    except (Exception, SystemExit) as exc:
        error_detail = str(exc.code) if isinstance(exc, SystemExit) else str(exc)
        log_event(
            run_id,
            event="dashboard_server_failed",
            error_type=type(exc).__name__,
            error=error_detail,
        )


def main() -> None:
    settings = get_settings()
    runner = build_runner_from_env()
    screener_runner = build_screener_runner_from_env()
    detective_runner = build_detective_runner_from_env()
    connector = BingXMarketDataConnector(
        base_url=settings.pipeline.bingx_base_url,
        timeout_seconds=10.0,
        max_retries=settings.pipeline.bingx_max_retries,
        requests_per_second=settings.pipeline.bingx_requests_per_second,
        cache_ttl_seconds=settings.pipeline.bingx_cache_ttl_seconds,
    )
    # Icke-kritiska källor (SPEC §8.2, Fas 5.5 Task 2/3/4): låg anropsfrekvens
    # och en generös TTL-cache räcker gott och väl - nyheter/Fear&Greed
    # ändras inte candidate-till-candidate inom samma discovery-cykel, och
    # ett cache-träff sparar ett nätverksanrop varje gång news_sentiment-
    # rollen byggs för nästa candidate i samma tick.
    news_connector = NewsRSSConnector(
        base_url=settings.pipeline.news_rss_base_url,
        timeout_seconds=10.0,
        max_retries=3,
        requests_per_second=1,
        cache_ttl_seconds=300,
    )
    external_data_connector = ExternalDataConnector(
        base_url=settings.pipeline.fear_greed_base_url,
        timeout_seconds=10.0,
        max_retries=3,
        requests_per_second=1,
        cache_ttl_seconds=300,
    )

    discovery_thread = threading.Thread(
        target=_run_discovery_forever,
        args=(connector, runner, settings, news_connector, external_data_connector, screener_runner),
        daemon=True,
    )
    monitoring_thread = threading.Thread(
        target=_run_monitoring_forever,
        args=(connector, settings),
        daemon=True,
    )
    detective_thread = threading.Thread(
        target=_run_detective_forever,
        args=(detective_runner, settings),
        daemon=True,
    )
    threads = [discovery_thread, monitoring_thread, detective_thread]

    notifier = build_notifier_from_env()
    if notifier is not None:
        threads.append(
            threading.Thread(target=_run_notify_forever, args=(notifier, settings), daemon=True)
        )
    else:
        log_event(
            "startup",
            event="telegram_notify_disabled",
            reason="TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing",
        )

    def _dashboard_repo_factory() -> SQLiteRepository:
        return SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)

    dashboard_app = build_dashboard_app_from_env(_dashboard_repo_factory, settings)
    if dashboard_app is not None:
        threads.append(
            threading.Thread(
                target=_run_dashboard_forever, args=(dashboard_app, settings), daemon=True
            )
        )
    else:
        log_event(
            "startup",
            event="dashboard_disabled",
            reason="CRYPTO_TRADING_DASHBOARD_ENABLED not set",
        )

    if is_demo_execution_enabled():
        demo_connector = build_demo_trading_connector_from_env()
        if demo_connector is not None:
            threads.append(
                threading.Thread(
                    target=_run_demo_execution_forever,
                    args=(connector, demo_connector, settings),
                    daemon=True,
                )
            )
        else:
            log_event(
                "startup",
                event="demo_execution_disabled",
                reason="CRYPTO_TRADING_BINGX_DEMO_API_KEY/_SECRET missing",
            )
    else:
        log_event(
            "startup",
            event="demo_execution_disabled",
            reason="CRYPTO_TRADING_DEMO_EXECUTION_ENABLED not set",
        )

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
