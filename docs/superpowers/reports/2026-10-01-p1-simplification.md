# P1: enklare, billigare, snabbare, robustare (2026-10-01)

Boten är stoppad under hela arbetet och efteråt. Ingen entry-strategi är ändrad: samma quant-signal, samma Risk Agent för SL/TP, samma Gate-regler, samma sizing (100 × 10), max 4 / 1 per symbol, Safety Kernel och PP.

Underlag: `2026-10-01-critical-system-audit.md`. Aktuell arkitektur: `docs/superpowers/ARCHITECTURE.md`.

## Sammanfattning före / efter

| | Före P1 | Efter P1 |
|---|---|---|
| Steg i LIVE-vägen (signal → exchange) | 13: quant → screener (AI, shadow) → News → Technical → Bull → Forecast → Risk → Bear → QA-LLM → Gate → Kernel → execution, plus GODFATHER självförbättring och pre-AI-shadow inne i discovery | **6**: quant → Risk (AI) → deterministisk QA → Gate → Kernel → execution |
| AI-anrop per analyserad kandidat | 7, plus ett screener-anrop för topp 5 | **1** |
| AI-tid per kandidat (median) | ~92 s för de mätbara rollerna plus News; signal → CONFIRMED i median 12,6 min | **~16 s** (Risk) |
| AI-kostnad per kandidat | 0,134 USD | **0,0155 USD (−88 %)** |
| AI-anrop senaste 30 dagar | 5 037 i kedjan + 852 screener + 21 detective/strategist | Motsvarande ~719 (bara Risk), 0, 0 |
| AI-kostnad senaste 30 dagar (loggad) | 75,9 USD kedja + 3,4 USD detective/strategist (screenern loggades inte) | ~9,6 USD |
| Trådar i trading-processen | 11 | 7 (5 frysta borta, `stop_watch` tillkommen) |
| Avstängning | bara genom att döda processen | ordnad: stoppfil, live-tick avslutas, exit 0 |
| Krasch | boten låg död tills någon märkte det | supervisor startar om (10 s → 10 min backoff) |
| Manuella stängningar | blandades in i botens resultat | egen tabell, egen statistik |
| Testsvit | 2 566 gröna, 8 fallerande | **2 634 gröna, 0 fallerande** |

Siffrorna för kostnad och latens kommer från botens egna `AI_CALL_MADE`-händelser:
- 662 fullständiga kedjor.
- News-rollens tid går inte att mäta separat, eftersom dess starttid loggas som cykelstart.
- Screenerns kostnad loggades aldrig i händelserna.

## 1. QA-agenten ersatt med kod (`gate/qa_checks.py`)

**Vad QA-agenten gjorde** (`.claude/agents/crypto-qa-gate.md`):
- (a) kontrollerade att de sex föregående bedömningarna fanns och var ifyllda;
- (b) letade semantiska motsägelser *mellan* dem, till exempel Bull mot Bear, Forecast-horisont mot Risk, Risk-nedsida mot Bull-setup;
- bedömde aldrig innehållet i sig.

**Ersättning:**
- (a) görs nu i kod, med samma kontrakt (`QAAssessment.passed/violations`, REJECTED vid fel):
  - varje körd roll finns och har status ok;
  - evidence-instrumentet stämmer;
  - datakvaliteten är `ok`;
  - Risk-fälten är ifyllda;
  - SL/TP är positiva;
  - SL-avståndet är 0,1–25 % och TP-avståndet högst 100 %;
  - Forecast-invarianter kontrolleras när Forecast körs.
- (b) har inget underlag kvar, eftersom rollerna den jämförde inte körs. Inget behåller därför en språkmodell.
- Det Gate redan kontrollerar upprepas inte: SL < pris < TP, R:R och signalålder. En dubblering skulle ha gjort Gatens NO_TRADE till REJECTED och ändrat audit-spårets betydelse.

**Verifiering:**
- På alla 604 historiska kandidater med giltigt Risk-svar (280 CONFIRMED, 265 REJECTED, 59 NO_TRADE) flaggar den deterministiska QA:n **0**. Den ändrar alltså inget historiskt beslut och fångar bara trasiga svar.
- De 265 som LLM-QA:n avvisade tidigare gav −1,28 USDT/trade, *bättre* än CONFIRMED (−2,43). Avvisningen hade alltså inget mätbart värde.

## 2. Agenter som inte påverkar beslutet

| Agent | Producerar | Läses av ett senare beslut? | Kostnad/anrop, latens (median) | Evidens för bättre resultat | Beslut |
|---|---|---|---|---|---|
| Risk Agent | SL och TP (absoluta priser) | **Ja**: Gate kontrollerar dem, positionen öppnas med dem | 0,0155 USD, 16 s | Ingen, men den krävs strukturellt | **Behålls** |
| News/Sentiment | fakta, källpåståenden, tolkning | Nej | 0,027 USD, ej mätbar | Ingen | Tas bort från entry-vägen |
| Technical | tolkning av redan strukturerad data | Nej | 0,016 USD, 15 s | Ingen | Tas bort |
| Bull Thesis | hypotes | Nej (bara Telegram-text och Guardians AI-förklaring) | 0,013 USD, 14 s | Ingen | Tas bort |
| Forecast | scenariosannolikheter | Bara en shadow-kontroll; 79 % av CONFIRMED hade bear ≥ bull | 0,007 USD, 4 s | Brier sämre än klimatologi (09-28) | Tas bort |
| Bear/Adversarial | motargument | Nej | 0,021 USD, 24 s | Ingen | Tas bort |
| QA-LLM | konsistens mellan ovanstående | Ja (REJECTED) | 0,035 USD, 20 s | REJECTED var bättre än CONFIRMED | **Ersatt med kod** |
| Opportunity Screener | förscreening-poäng | Nej (`enforce: false`, shadow) | ~852 anrop, ologgad kostnad | Ingen | Anropas inte längre |
| Detective + GODFATHER-strategist | post-trade-analys, heuristikförslag | Nej (0 promoverade heuristiker) | 3,4 USD | Ingen | FROZEN |
| Guardian AI-förklaring | förklarar en redan satt övergång | Nej (observerar) | 0,005 USD, 343 anrop totalt | — | **Behålls**: del av safety-vägen och billig |

- Inga säkerhetsfunktioner togs bort. Guardian, Guardian Authority, Safety Kernel, PP, SL/TP på exchange, tidsgränsen, instanslåset och watchdogen är oförändrade.
- Agentdefinitionerna finns kvar. Kedjan styrs av `config/ai_pipeline.yaml`; kodens default är den gamla kedjan, YAML-filen är P1-beslutet.
- Följder som har hanterats:
  - Telegram-notisen visar Bull/Forecast bara om de finns.
  - Funnel-rapporterna räknar bara de roller som körts.
  - Kostnadsbudgeten räknar det faktiska antalet anrop per kandidat.

## 3. Maker-entry (research, `entry_research/maker_entry_sim.py`)

**Antaganden:**
- Post-only BUY-limit vid P0 × (1 − offset), där P0 = senaste 1m-close före beslutet.
- Fill räknas bara om priset handlas *igenom* nivån med minst 1 bp (köpositionen är okänd).
- SL/TP behåller sina absoluta priser.
- Exit-avgiften är taker.
- Taker-baseline: P0 × (1 + 0,03 %). Det är den uppmätta medianslippagen för 106 LIVE-fills.
- Avgifter: taker 0,05 % (uppmätt). Maker 0,02 % som BingX-standard; kontots maker-avgift är **overifierad** eftersom alla historiska ordrar var taker. 0,04 % testades som känslighet.
- Offsets 0 / 0,02 / 0,05 / 0,10 / 0,20 % × livstid 1 / 3 / 5 / 15 min.
- 1 135 signaler och 250 bot-trades, TRAIN/VALID/TEST/HOLDOUT.

**Resultat (urval), USDT per 1000 notional:**

| Variant | Fill-andel (signaler) | Samma trade: maker − taker | Missade trades (taker-utfall) | 100 % retention (taker om ingen fill) | CI90 |
|---|---|---|---|---|---|
| vid pris, 5 min | 91 % | **+0,50** (+0,44 / +0,29 / +0,40 / +0,70 per period) | +1,83/trade (missar vinnare) | −0,09 | [−0,52, +0,31] |
| −0,02 %, 3 min | 79 % | +0,78 | −0,83 | +0,05 | [−0,35, +0,51] |
| −0,10 %, 5 min | 56 % | +1,78 | +0,10 | −0,22 | [−0,89, +0,45] |
| bot-trades, vid pris, 5 min | 92 % | +0,02 (+2,42 TEST, −0,98 HOLDOUT) | −1,13 | −0,09 | [−1,00, +0,70] |

**Slutsats:**
- Avgifts- och prisvinsten är verklig på de trades som fylls (≈ +0,5 USDT/trade på signalerna, stabil i alla fyra perioder).
- Den äts upp av **adverse selection**: ordern fylls när priset faller och missar när det stiger direkt.
- Med full retention blir nettot ≈ 0 med CI kring 0, på både signaler och bot-trades.
- Maker-only ser bättre ut per signal bara för att den tar *färre* trades. Det är inte en execution-förbättring.
- **Inte verifierat, inte implementerat i LIVE.**
- Förutsättning för ett nytt försök: verifiera kontots maker-avgift och mät verkliga fills i PAPER/demo med orderbok, eftersom köpositionen inte kan simuleras på 1m-data.

## 4. Drift: Windows-tjänst

**Mekanism:** `install_bot_service.ps1` registrerar Task Scheduler-uppgiften `crypto_trading_bot`, som startar `pythonw -m crypto_trading.supervisor run` utan konsolfönster. Supervisorn (`crypto_trading/supervisor.py`):
- håller ett eget OS-lås (en supervisor);
- startar boten som barnprocess;
- startar om vid krasch med backoff 10 s → 10 min, nollställt efter 30 min friskt;
- väntar 15 min vid konfigurations-/credential-fel (exit 2);
- backar 2 min vid "redan igång" (exit 3);
- utför ordnad stopp via stoppfil (`stop_bot.bat`);
- loggar i `logs/supervisor.log` och `logs/supervisor_heartbeat.json`.

**I boten:**
- Ny `crypto_trading/lifecycle.py`: SHUTDOWN-event, stoppfil, Ctrl-C/SIGTERM/SIGBREAK.
- Live-ticken körs under ett lås, och ingen ny tick startar efter stopp.
- Konfigurations- och credential-kontroll innan någon tråd startar.
- `CRYPTO_TRADING_SAFE_MODE=1` tvingar av LIVE, demo, Guardian, Telegram och discovery oavsett `.env`.
  - Behövdes eftersom en tömd variabel fylls på igen från `.env` av `load_dotenv`.
  - Det upptäcktes i förkontrollen av röktestet: den visade `live True` innan något startades.
- `CRYPTO_TRADING_DISCOVERY_DISABLED=1` ger ett manage-only-läge.
- `start_bot.bat` startar nu supervisorn, inte boten direkt; den gamla vägen förbi supervisorn är borta.

**Praktisk verifiering**, med riktig supervisor och riktig bot via en tillfällig Task Scheduler-uppgift, på en *kopia* av DB:n, med SAFE_MODE och verifierat avstängda flaggor:

| Krav | Resultat |
|---|---|
| Överlever Claude-session och terminal | Föräldern (cmd från Task Scheduler) avslutades direkt. Supervisor och bot är inte barn till någon terminal eller Claude-process. |
| Exakt en instans | En andra supervisor: "another supervisor is already running", exit 3. En andra bot direkt: `startup_refused already_running`, exit 3. |
| Crash → restart | Tolken dödades hårt: `EXIT code=-1 → RESTART in 10s → START #2`. Startprogrammet dödades: tolken dog med (inga föräldralösa processer), backoff 30 s → START #3. |
| Graceful shutdown | `supervisor stop`: `shutdown_requested → shutdown_complete live_tick_finished=true`, bot exit 0 på 1,8 s, ingen omstart, supervisorn avslutad, stoppfilen borta, heartbeat `stopped`. |
| Heartbeat och logg | `heartbeat.json` (bot), `supervisor_heartbeat.json`, `supervisor.log`, `crypto_trading.log` med `bot_started`, `frozen_layer_not_started`, `discovery_disabled`. |
| Avstämning vid omstart | Mot verklig BingX, via en proxy som vägrar allt utom läsande anrop. De 4 ACTIVE-raderna från 10-01 stängdes och verifierades (bara `get_position`, `get_order_history`, `get_income`). |
| Exchange-side SL kvar | Inget i avstängnings- eller omstartsvägen avbryter eller stänger något (testat: läsproxy, `test_live_loop_never_starts_a_tick_after_shutdown`). |
| Saknade credentials / config | Exit 2 och `startup_refused` loggas (enhetstestat). Kan inte testas praktiskt utan att ändra `.env`, eftersom dotenv fyller på nyckeln. |
| Inga konkurrerande starter | `crypto_trading_bot` är ersatt (supervisor, IgnoreNew, restart 999 × 1 min). `start_bot.bat` går via supervisorn. `crypto_trading_evidence_shadow` är Disabled. Watchdog-uppgiften larmar bara. |
| Windows-omstart | Trigger "vid inloggning" är konfigurerad men inte fysiskt testad med en omstart. |

**Begränsningar:**
- Shellet saknar admin. Uppgiften är därför installerad som **"vid inloggning" (Interactive)**: den överlever terminal, Claude-session, krasch och omstart (när du loggar in), men **inte utloggning**.
- För att överleva utloggning och starta utan inloggning: kör `install_bot_service.ps1` som administratör. Den registrerar då S4U + start vid Windows-uppstart. Det gick inte att registrera utan admin ("Åtkomst nekad").
- Efter en hård kill kan Windows släppa DB-låset med någon sekunds fördröjning. En omedelbar omstart kan då få "redan igång", och supervisorn försöker igen efter 2 min.

## 5. Manuella och externa stängningar (`crypto_trading/external_closes.py`)

**Källa** bestäms från exchangens egen order, aldrig från priset:

| Källa | Betydelse |
|---|---|
| `BOT` | Botens market-close |
| `EXCHANGE_PROTECTIVE_ORDER` | Botens SL/TP/BE triggad av exchange |
| `EXTERNAL_MANUAL` | Ingen bot-`clientOrderId`: användaren, eller en annan process med samma nyckel; går inte att skilja åt |
| `EXCHANGE_FORCED` | Likvidation, ADL eller okänd typ |
| `MIXED_INCLUDES_EXTERNAL` | Flera fills, minst en extern |

**Lagring:**
- Tabellen `external_closes` har tidpunkt, symbol, riktning, entry, stängningspris, kvantitet, P/L efter avgifter och funding, avgifter, klassning, källa, order-id, upptäckt-tid och `reason`.
- Orsak anges med `python -m crypto_trading.external_closes note <position_id> "<orsak>"`.
- Raden skrivs automatiskt när en exit verifieras. Det är idempotent, och en noterad orsak skrivs aldrig över.
- Historiken fylls med `backfill`.
- `performance/live_track_report.py` redovisar `bot_managed_*` och `external_close_*` separat.

**På en kopia av verklig DB:**
- 19 externa stängningar (17 äldre plus ADA och GOOGL 10-01).
- Botstyrda exits: **−228,87 USDT (97 trades)**.
- Externa: **−14,91 USDT (19 trades)**.

## 6. De 8 inaktuella testerna

Alla 8 testade gamla default-värden som avsiktligt slagits på (`authority_shadow_enabled`, `authority_enabled`, live PP, PP-experimentet), eller slog larm på en *kommentar* som nämner `position_sizing`. De är omskrivna så att de testar:
- kodens säkra default (False);
- att YAML har ett uttryckligt bool som laddas oförändrat;
- att `authority_enabled` deklareras exakt en gång;
- att kommentarer inte räknas som användning.

Ingen produktlogik ändrades för att få dem gröna.

## 7. FROZEN / RESEARCH-ONLY

`config/frozen_layers.yaml` (allt `false`), med `FROZEN.md` i paketen godfather, evidence, evidence_shadow, shadow och detective. Frysta lager:
- Detective (+ strategist);
- GODFATHER intelligence + kline-arkiv;
- GODFATHER självförbättring;
- shadow evaluation;
- entry quality shadow;
- pre-AI feasibility shadow;
- evidence shadow-tjänsten (redan Disabled).

GODFATHER-koden är oförändrad. Guardian Authority fortsätter som en del av safety-vägen; bara dess inlärning är fryst.

## 8. Arkitektur

Se `docs/superpowers/ARCHITECTURE.md`. Kvarvarande LIVE-väg:

```
quant screener -> kandidat/budget -> LIVE discovery gate -> Risk Agent (enda AI-anropet)
  -> deterministisk QA -> Risk/Signal Gate -> PAPER-position -> Safety Kernel
  -> LIVE execution (entry + SL + TP på exchange)
SAFETY: SL/TP på exchange, PP, Guardian (+ Authority), Kernel stop-move, 6 h-gräns, avstämning
```

## 9. Tester

| Svit | Resultat |
|---|---|
| Hela sviten | **2 634 gröna, 0 fel** (före: 2 566 gröna, 8 fel) |
| Isolering (`-k isolation`) | 95 gröna |
| Guardian | 432 gröna |
| Safety Kernel | 61 gröna |
| LIVE execution + PP + exit-verifiering | 145 gröna |
| Restart / reconciliation / recovery | 136 gröna |
| GODFATHER | 369 gröna |
| Nya: supervisor, dubbelinstans, lifecycle, SAFE_MODE | 16 gröna (riktiga barnprocesser, 4 körningar i rad) |
| Nya: manual-close-loggning | 17 gröna |
| Nya: maker-simulering | 6 gröna |
| Nya: P1 AI-pipeline + deterministisk QA | 20 gröna |
| Nytt: screener skippas i shadow-off | 1 grönt |

## 10. Kända, ej åtgärdade fynd

- `performance/live_track_report.py` *adderar* `realized_funding_usdt`, som lagras som kostnad (positivt = betalt). Teckenfelet fanns före P1. Beloppen är små (±0,2 USDT totalt); rättas separat.
- Guardians AI-förklaring får inte längre Bull/Forecast i sin kontext. Den är bara förklarande och påverkar inget beslut.

## Läget efter P1

- Boten är **stoppad**: inga processer, `crypto_trading_bot` och `crypto_trading_evidence_shadow` är Disabled.
- Exchange: 0 positioner, 0 öppna ordrar, 0 USDT.
- Före en start måste DB-raderna för 10-01 stämmas av; det sker automatiskt vid start (praktiskt verifierat ovan).
- Starta: `Enable-ScheduledTask crypto_trading_bot; Start-ScheduledTask crypto_trading_bot`, eller kör först `install_bot_service.ps1` som administratör för att överleva utloggning.
- Lönsamhetsproblemet är oförändrat: entry-signalen saknar edge (P3).
