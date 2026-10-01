# crypto_trading – aktuell arkitektur (P1, 2026-10-01)

Översikten visar vad som faktiskt körs efter P1. Detaljer och siffror finns i
`docs/superpowers/reports/2026-10-01-p1-simplification.md`.

Varje komponent har en av fyra roller:

| Roll | Betydelse |
|---|---|
| **LIVE-BESLUT** | Påverkar om, hur stort eller hur en LIVE-position öppnas eller stängs. |
| **SAFETY** | Skyddar kapitalet. Kan bara blockera, stänga eller strama åt, aldrig öka risken. |
| **OBSERVERAR** | Mäter och loggar men påverkar aldrig ett beslut. |
| **FROZEN** | Research. Koden finns kvar i git men körs inte i trading-processen ([config/frozen_layers.yaml](../../crypto_trading/config/frozen_layers.yaml)). |

## LIVE PATH: signal → beslut → safety → execution

```
BingX marknadsdata (publik)
  -> quant screener (screening/quant_screener.py)            LIVE-BESLUT  anomali: |Δpris 30m| >= 2 %, RSI >= 70,
                                                                          volym-z >= 2.5, funding >= 0.05 %
  -> kandidat + budget (screening/candidate_engine.py)        LIVE-BESLUT  rankning, cooldown, LIVE-platser
  -> LIVE discovery gate (paper_trading/live_discovery_gate)  SAFETY       analyserar bara om en LIVE-plats är ledig
  -> Risk Agent (AI, crypto-risk-agent.md)                    LIVE-BESLUT  föreslår SL / TP (enda AI-anropet)
  -> deterministisk QA (gate/qa_checks.py)                    SAFETY       komplett, rätt instrument, datakvalitet,
                                                                          rimliga SL/TP-avstånd (ingen AI)
  -> Risk/Signal Gate (gate/risk_signal_gate.py)              LIVE-BESLUT  SL < pris < TP, R:R >= 1, signalålder
                                                                          <= 30 min, kapacitet
  -> PAPER-position (paper_trading/position_opening.py)       LIVE-BESLUT  samma SL/TP som LIVE
  -> Safety Kernel (safety_kernel.py)                         SAFETY       likvidationsavstånd; 100 x 10 fast;
                                                                          %-taken loggas bara
  -> LIVE execution (paper_trading/live_execution.py)         LIVE-BESLUT  max 4, max 1 per symbol; entry + SL + TP
                                                                          på exchange; deterministiska client-id
```

Från kandidat till LIVE-beslut görs **ett** AI-anrop (före P1: sju, plus ett screener-anrop i shadow).

AI-roller som inte längre körs på entry-vägen, men vars agentdefinitioner finns kvar och kan slås på i [config/ai_pipeline.yaml](../../crypto_trading/config/ai_pipeline.yaml):

- News/Sentiment
- Technical
- Bull Thesis
- Forecast
- Bear/Adversarial
- QA-LLM
- Opportunity Screener (shadow)

## SAFETY PATH: Guardian → Safety Kernel → exchange-skydd

```
exchange-side SL + TP (placerade vid entry)                   SAFETY  ligger kvar oavsett om boten kör
LIVE Profit Protection (live_profit_protection.py)            SAFETY  +1 % -> SL till entry; add-then-remove;
                                                                      aldrig lägre SL
Position Guardian (guardian/)                                 SAFETY  HOLD/WATCH/PROTECT/EXIT; AI-förklaring vid
                                                                      övergång (observerar)
Guardian Authority (guardian/authority*.py)                   SAFETY  kan bara TIGHTEN_SL / CLOSE_EARLY via
                                                                      heuristiker; 0 heuristiker -> inaktiv i
                                                                      praktiken; inlärningen är FROZEN
Safety Kernel stop-move-check (safety_kernel.py)              SAFETY  stop flyttas bara uppåt, aldrig nära
                                                                      likvidation
6 h tidsgräns (live_execution.close_time_limit_positions)     SAFETY
Exchange = source of truth: reconcile_active_executions       SAFETY  vid varje tick och vid omstart
  + verify_closed_live_exits (endast läsning)
```

## DRIFT

```
Task Scheduler "crypto_trading_bot" (install_bot_service.ps1)
  -> supervisor (crypto_trading/supervisor.py, pythonw, inget konsolfönster)
       en supervisor (OS-lås), restart med backoff, stoppfil -> ordnad avstängning,
       logs/supervisor.log + logs/supervisor_heartbeat.json
     -> bot (crypto_trading/run.py)
          en bot per DB (OS-lås), exit-koder 0/1/2/3 (crypto_trading/lifecycle.py),
          SAFE_MODE=1 tvingar LIVE/Guardian/Telegram/discovery av
          trådar: discovery, monitoring, notify, guardian, live_execution, watchdog, stop_watch
Task Scheduler "crypto_trading_watchdog" -> logs/heartbeat.json (larmar bara)
```

- **Start:** `Start-ScheduledTask crypto_trading_bot`, eller `start_bot.bat`.
- **Stopp:** `stop_bot.bat`.
- **Status:** `python -m crypto_trading.supervisor status`.
- **Hård brytare (sedan 2026-10-01, LIVE stoppad):** `.env` innehåller `CRYPTO_TRADING_SAFE_MODE=1` bredvid `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED=1`.
  - Effekt: LIVE, demo/VST, Guardian, Telegram och discovery är av även om tasken aktiveras eller `start_bot.bat` körs. Ingen signerad trading-connector byggs.
  - Verifierat via den riktiga startvägen (supervisor → bot): bara monitoring körde, inga order och inga positioner.
  - Testad i `tests/crypto_trading/test_supervisor.py`.
  - LIVE kan bara startas igen om raden tas bort ur `.env`.

## RESEARCH PATH: PAPER/shadow → mätning → rapport

| Komponent | Roll |
|---|---|
| PAPER-positioner och monitoring | OBSERVERAR: samma beslut som LIVE, simulerad fill |
| `external_closes` + `performance/live_track_report.py` | OBSERVERAR: manuella och externa stängningar hålls utanför botens egen statistik |
| `entry_research/` (trade_review, maker_entry_sim, exit/regime/edge labs) | Research, offline |
| GODFATHER intelligence + kline-arkiv | FROZEN |
| GODFATHER självförbättring (strategist-LLM) | FROZEN |
| GODFATHER priority boost, entry quality / thesis enforcement | Var redan avstängda (godfather.yaml) |
| Detective (post-trade-AI) | FROZEN |
| Shadow evaluation (P3–P6) | FROZEN |
| Entry Quality shadow | FROZEN |
| Pre-AI feasibility shadow | FROZEN |
| Evidence shadow-tjänsten (`evidence_shadow/`) | FROZEN; Task Scheduler-uppgiften är inaktiverad |
| Guardian Authority shadow-observationer, PP-experimentet | OBSERVERAR: körs inne i safety-looparna, oförändrade |

Ett FROZEN-lager slås på igen bara för en konkret, förregistrerad hypotes: ändra flaggan i `config/frozen_layers.yaml` och dokumentera hypotesen och mätningen i en rapport.
