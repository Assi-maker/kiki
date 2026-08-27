from __future__ import annotations

import os
import threading

from crypto_trading import discovery_loop, monitoring_loop
from crypto_trading.agents.runner import AgentRunner, RealClaudeRunner
from crypto_trading.config.exceptions import ConfigError
from crypto_trading.config.loader import get_settings
from crypto_trading.connectors.bingx_market_data import BingXMarketDataConnector
from crypto_trading.connectors.external_data import ExternalDataConnector
from crypto_trading.connectors.news_rss import NewsRSSConnector
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


def main() -> None:
    settings = get_settings()
    runner = build_runner_from_env()
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
    # Två separata Repository-instanser, en per tråd - loopar delar databas
    # men körs oberoende (SPEC §7); sqlite_busy_timeout_ms är redan
    # konfigurerad exakt för denna samtidiga-skrivning-situation.
    discovery_repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    monitoring_repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)

    discovery_thread = threading.Thread(
        target=discovery_loop.run_forever,
        args=(connector, discovery_repo, runner, settings),
        kwargs={
            "news_connector": news_connector,
            "external_data_connector": external_data_connector,
        },
        daemon=True,
    )
    monitoring_thread = threading.Thread(
        target=monitoring_loop.run_forever,
        args=(connector, monitoring_repo, settings),
        daemon=True,
    )
    discovery_thread.start()
    monitoring_thread.start()
    discovery_thread.join()
    monitoring_thread.join()


if __name__ == "__main__":
    main()
