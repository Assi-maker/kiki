# SPEC — Crypto Market Intelligence & Paper Trading System (crypto_trading/)

Status: **Under granskning — inte godkänd för implementation.** Detta dokument är källan till sanning för arkitekturbeslut i `crypto_trading/`. Framtagen genom en fullständig brainstorming-session (se konversationshistorik); varje beslut nedan är explicit bekräftat, inget är antaget.

## 0. Förhållande till `intelligence/` (Fas 1)

`intelligence/` (Market Opportunity Intelligence System, SPEC.md) är **fryst och rörs inte**. `crypto_trading/` är arkitektoniskt helt fristående:

- Ingen import, inget delat state, ingen runtime-koppling mellan `crypto_trading/` och `intelligence/` — om inte uttryckligen beslutat senare.
- Delar bara repo, Python-miljö och utvecklingsverktyg (`pyproject.toml`, `.venv`, `pytest`, `ruff`).
- Egen databas, egen config, egna scheman, egen state machine.
- Återanvänder **principerna** som bevisades fungera i Fas 1 (se §1), inte koden.

## 1. Syfte och icke-mål

Systemet ska kontinuerligt bevaka BingX USDT perpetual futures, deterministiskt upptäcka handelsrelevanta marknadsförhållanden, driva dem genom ett adversarialt multi-agent-analysteam, och — bara om en hård, deterministisk risk-gate godkänner — öppna en **simulerad (paper) position**, övervaka den till stängning, och rapportera resultatet transparent med fullständig historik och kalibreringsmätning.

**Det här är INTE:**
- Ett system som lovar att förutsäga marknaden. Forecast Agent producerar sannolikhetsscenarier, aldrig säkerheter, och dess kalibrering mäts fortlöpande mot faktiskt utfall (§9).
- **Ett tradingsystem i verklig mening.** Ingen kod i `crypto_trading/` får:
  - ansluta till BingX (eller annat) mäklarkonto,
  - läsa kontosaldo,
  - hantera broker-credentials eller API-nycklar för orderläggning,
  - placera en riktig order,
  - flytta pengar.

  Detta är en **hård gräns, inte en konfigurationsflagga** — identisk princip som `intelligence/`s SPEC §1/§14, gäller i alla faser. "Paper trading" betyder uteslutande lokal, simulerad bokföring av hypotetiska positioner mot riktig marknadsdata — aldrig en verklig order.
- En "magisk AI-agent". Deterministisk kod gör allt som kan vara deterministiskt (eligibility-filtrering, quant screening, position sizing, risk-gate, state transitions). LLM används bara för semantisk analys: teknisk tolkning, hypotesgenerering, prognos, adversarial granskning.

**Kärnprinciper (obligatoriska, kod-nivå-garantier):**
1. Ingen enskild AI-agent godkänner en trade. QA/Gate Agent är en AI-baserad kontroll, men den slutliga `CONFIRMED`/`NO_TRADE`-övergången avgörs av en **deterministisk** Risk/Signal Gate som kan blockera oavsett vad AI-agenterna tycker — **inklusive när samtliga sju AI-roller är eniga och positiva.** Gaten har egna, oberoende hårda regler (t.ex. exponeringstak) som aldrig kan övertrumfas av ett AI-utfall.
2. All marknadsdata som är kritisk för en bedömning kommer bara från BingX publika market-data-endpoints, utan API-nyckel. Systemet ansluter aldrig till ett konto.
3. Fail-safe överallt: varje typ av fel leder till att **ingen** `CONFIRMED`-signal uppstår — aldrig till en gissning eller ett implicit godkännande (§8).
4. **Ingen framtida information får någonsin läcka in i ett beslut.** Både live-drift och historisk replay (§11) fattar varje beslut enbart utifrån data som faktiskt fanns tillgänglig vid signalens beslutstimestamp — ingen framtida candle, funding rate, open interest, nyhet eller annan datapunkt får förekomma i underlaget en agent eller gate ser. Generell arkitekturregel, inte bara ett testkrav för replay (§8.4).

## 2. Lagerarkitektur

```
BINGX MARKNADSDATA (publik, nyckellös)  ┐
EXTERNAL DATA (nyheter/sentiment/makro) ┼→ ELIGIBILITY/LIQUIDITY FILTER → TOP N UNIVERSE
                                         ┘        (deterministisk)

TOP N UNIVERSE (marknadsuniversum — medlemskap är INGEN tradingsignal, bara urval för vidare screening)
                → QUANT SCREENER (deterministisk, 4 signaltyper) → CANDIDATE EVIDENCE RECORD
                → CANDIDATE ENGINE (dedup/cooldown/budget-prioritering, deterministisk)
                → 7 AI-ROLLER (LLM, strukturerad output, adversarialt team)
                → QA/GATE AGENT (LLM, schema-komplethet + intern konsistens)
                → RISK/SIGNAL GATE (deterministisk kod — kan blockera oavsett AI-utfall)
                → CONFIRMED | NO_TRADE

CONFIRMED → PAPER TRADING ENGINE (regelbaserad sizing, simulerad fill)
          → POSITION MONITORING (kontinuerlig, tätare loop än discovery)
          → CLOSED (TP/SL/tidsgräns/invalidation)

Allt → EVENT/AUDIT-LOGG (enda sanningskälla) → TELEGRAM + DASHBOARD (read-only, samma datamodell)
Stängda trades + Forecast-loggar → KALIBRERING (Brier score, calibration curve)
```

Beroenderiktning enkelriktad, precis som Fas 1: `schemas` beroendefritt; allt annat beror på `schemas`; orchestrator/loops beror på **interfaces** (`Repository`, `AgentRunner`, `MarketDataConnector`), aldrig konkreta implementationer.

## 3. Filstruktur

```
crypto_trading/
├── config/
│   ├── pipeline.yaml          # discovery-intervall, monitoring-intervall, Top N, cooldown
│   ├── risk_limits.yaml       # max exponering, max samtidiga positioner, risk % per trade
│   └── budget_limits.yaml     # AI-anropstak, per-run/per-dygn, varningsnivå
├── connectors/
│   ├── base.py                 # MarketDataConnector/NewsConnector (ABC) — timeout/retry/rate-limit/cache/logging
│   ├── bingx_market_data.py    # publika endpoints ENDAST: klines, ticker, volym, funding rate, OI, instrumentmetadata, orderbok/spread
│   ├── news_rss.py             # kryptonyheter, RSS/publikt API, kostnadsfritt
│   └── external_data.py        # Fear & Greed Index, relevanta makro-/on-chain-mätvärden
├── screening/
│   ├── eligibility_filter.py   # likviditet/spread/datakomplethet/handelsstatus → Top N
│   ├── quant_screener.py       # 4 signaltyper → CandidateEvidenceRecord, deterministisk, reproducerbar
│   └── candidate_engine.py     # dedup/cooldown, budget-baserad prioritering
├── schemas/
│   ├── market.py                # InstrumentMetadata, Kline, Ticker, FundingRate, OpenInterest
│   ├── evidence.py               # CandidateEvidenceRecord
│   ├── assessments.py            # sju AssessmentTyper (se §6)
│   ├── forecast.py               # ForecastRecord (se §9)
│   ├── trade.py                  # Position, Trade, PositionStatus
│   └── candidate.py              # Candidate (aggregat) + CandidateStatus enum
├── agents/
│   ├── loader.py, roles.py, runner.py   # AgentRunner (Real/Mock) — samma mönster som Fas 1
├── gate/
│   ├── qa_gate.py                # AI: schema-komplethet + intern konsistens
│   └── risk_signal_gate.py       # DETERMINISTISK: hård gräns, kan blockera oavsett AI-utfall
├── paper_trading/
│   ├── position_sizing.py        # regelbaserad, risk % av kapital / stop-avstånd
│   ├── execution.py               # simulerad fill (spread/slippage-antagande), fees/funding
│   ├── monitoring.py              # SL/TP/tidsgräns/invalidation, tätare loop
│   └── replay.py                  # historisk replay, look-ahead-bias-fri (Fas 4)
├── calibration/
│   ├── brier_score.py
│   └── calibration_curve.py
├── notify/
│   └── telegram.py                # delar eventmodell med dashboard, notisnivå important/decisions/debug
├── dashboard/
│   ├── api.py                     # FastAPI, read-only
│   └── static/                    # enkel frontend
├── storage/
│   ├── db.py, repository.py       # SQLiteRepository bakom Repository-protokoll, egen DB-fil
├── state_machine.py                # rena funktioner, crash-safe övergångar
├── discovery_loop.py                # periodisk (default 15 min, config), eligibility→…→gate
├── monitoring_loop.py               # tätare, öppna positioner→exit
└── run.py

tests/crypto_trading/                # speglar paketstrukturen 1:1
```

Inga filer i `intelligence/` ändras.

## 4. Datamodeller (kärnscheman)

**CandidateEvidenceRecord** (från Quant Screener — aldrig riktning, aldrig trade-signal):
- `instrument`, `timeframes: list[str]`, `evaluated_at`
- `price_volatility_evidence`, `momentum_breakout_evidence`, `volume_evidence`, `funding_oi_evidence` — var och en typad med triggerade tröskelvärden, rådata, baseline
- `candidate_score: float` — transparent, reproducerbart från rådata; samma indata ger alltid samma score
- `trigger_reasons: list[str]`
- `data_quality_status` — `ok` / `degraded` / `invalid`
- Utfall: `"worth_deeper_analysis"` eller `"not_a_candidate"` — **aldrig** BUY/SELL/CONFIRMED.

**`candidate_score` är strikt separerad från alla andra tal i systemet och får aldrig förväxlas med dem:**
| Fält | Kommer från | Betyder |
|---|---|---|
| `candidate_score` | Quant Screener (deterministisk) | Hur ovanligt/anomalt marknadstillståndet är — ett prioriterings-/urvalsmått, **inte** en bedömning av trade-kvalitet. |
| Forecast `scenario_probabilities` | Forecast Agent (LLM) | Sannolikhet för ett prisscenario inom en angiven horisont (§9) — **inte** sannolikhet för vinst. |
| Ingen "AI confidence"-summering | — | Systemet exponerar aldrig ett enskilt kombinerat "AI confidence"-tal. Varje agents bedömning redovisas separat i sitt eget typade fält. |
| Ingen "trade quality"-poäng | — | Finns inte som fält. Kvaliteten på en `CONFIRMED`-signal bedöms i efterhand via faktiskt utfall (§9, Fas 8), aldrig via ett förhandsberäknat sammansatt tal. |

Dashboard (§13) och Telegram (§12) måste visa dessa som tydligt separata, namngivna fält — aldrig slås ihop till ett enda "score".

**Sju AssessmentTyper** (samma princip som Fas 1 §4: varje agent returnerar bara sin egen typ, skriver aldrig till `Candidate` direkt eller till en annan agents fält):
- `NewsSentimentAssessment` — separerar strikt: `verified_facts` (källbelagt) / `source_claims` (vad källan påstår, ej verifierat) / `interpretation` (agentens tolkning). Får aldrig ensam skapa en riktningssignal.
- `TechnicalAssessment` — marknadsstruktur: pris/volym/volatilitet/momentum/funding/OI-tolkning.
- `BullThesisAssessment` — hypotes, katalysator, setup.
- `ForecastAssessment` — se §9, strikt definierad.
- `RiskAssessment` — föreslår stop-loss/target/position sizing/riskmått; **rådgivande, inte beslutande** (§8).
- `BearAdversarialAssessment` — motargument, alternativa förklaringar, falsifieringsvillkor. Närvaro är kravet, inte positivt utfall.
- `QAAssessment` — `passed: bool`, `violations: list[str]` — schema-komplethet + intern konsistens, inte sakinnehåll.
- Var och en: `agent_name`, `run_id`, `created_at`, `status` (`ok`/`failed`/`timeout`).

**Candidate** (aggregat) — identitet, evidence record, de sju assessments (`Optional`, `None` tills ifyllt), gate-resultat, `status: CandidateStatus`.

**Position / Trade** — instrument, direction, `theoretical_entry`/`simulated_fill_price` och motsvarande vid exit (separerade fält, §11), stop/target, size, fees/funding/slippage, `fill_model_version`, hold time, exit reason, MFE/MAE, komplett audit trail.

## 5. Status-modell (tre separata dimensioner — aldrig blandas)

**A. CandidateStatus** (en candidates livscykel):
```
CANDIDATE → DATA_INVALID (terminal, om kritisk data saknas/stale/inkonsekvent — se §8.1/§8.2)
          → BUDGET_LIMITED (terminal, aldrig fullt analyserad — se §10)
          → UNDER_AI_ANALYSIS → ANALYSIS_INTERRUPTED (crash-recovery, se §8.5)
                               → REJECTED (analyserad, underkänd av AI-teamet/QA)
                               → NO_TRADE (QA godkände, men Risk/Signal Gate blockerade)
                               → CONFIRMED (godkänd av samtliga gates)
```
Ett okänt/korrupt statusvärde är **inte** ett `CandidateStatus`-värde — det är ett repository-/fail-safe-fel (`CorruptCandidateStateError`), se §8.3.
`NOT_A_CANDIDATE` (screenern kvalificerade aldrig instrumentet) loggas på debug-nivå, persisteras inte som en `Candidate`-rad.

**B. PositionStatus** (bara för `CONFIRMED` → faktisk paper trade): `OPEN_POSITION → CLOSED`.

**C. AssessmentStatus** (per enskilt agentanrop, som Fas 1): `ok` / `failed` / `timeout`.

**Explicit definierade, aldrig sammanblandade:**
| Status | Betyder |
|---|---|
| `REJECTED` | Fullt analyserad av AI-teamet, underkänd på sakliga grunder. |
| `NO_TRADE` | Nådde QA/Gate men blockerades av den deterministiska Risk/Signal Gate. |
| `BUDGET_LIMITED` | Kvalificerade sig som candidate men fick aldrig AI-analys på grund av resurstak — **inte** ett underkännande. |
| `DATA_INVALID` | Blockerad innan AI-analys på grund av otillräcklig/stale/inkonsekvent kritisk data. |
| `ANALYSIS_INTERRUPTED` | Tekniskt avbruten process (krasch/restart) mitt i AI-analys — kräver explicit recovery-policy (ny analys, nytt `analysis_run_id`), blir aldrig ett permanent tyst läge, och sweepen som upptäcker den återupplivar aldrig automatiskt. |

Data-/schemakorruption (ett lagrat statusvärde som inte matchar någon av ovanstående) är **inte** ett `CandidateStatus`-värde och skapar aldrig ett `Candidate`-objekt — se §8.3.

## 6. Agentteam (7 roller)

Se §4 för scheman. Rollerna körs i denna ordning inom "AI-teamet"-steget, var och en oberoende av föregående rolls tolkning (bara delad, read-only kontext — ingen agent kan skriva till en annan agents fält, samma garanti som Fas 1 §16):

`News/Sentiment Analyst → Technical Analyst → Bull/Thesis Agent → Forecast Agent → Risk Agent → Bear/Adversarial Agent → QA/Gate Agent`

## 7. Flöden (två oberoende loopar)

**Discovery/Signal Pipeline** — periodisk, `pipeline.yaml: discovery_interval_minutes` (default 15, konfigurerbart, aldrig hårdkodat):
`Eligibility/Liquidity Filter → Top N → Quant Screener → Candidate Engine → 7 AI-roller → QA/Gate → Risk/Signal Gate → CONFIRMED/NO_TRADE`

**Paper Position Monitoring** — separat, tätare loop, intervall matchat mot vald marknadsdata-upplösning (`pipeline.yaml: monitoring_interval_seconds`):
`Öppna positioner → live marknadsdata → SL/TP/invalidation/tidsgräns → exit`

Loopar delar databas men körs oberoende — en långsam discovery-körning blockerar aldrig position-övervakningen.

**Deduplication/cooldown:** en tidigare `REJECTED`-candidate återanalyseras bara om (a) en konfigurerbar cooldown passerat, eller (b) evidensen förändrats tillräckligt mycket för att motivera ny analys (tröskel definieras i `pipeline.yaml`).

## 8. Fail-safe-regler (obligatoriska, testas explicit)

### 8.1 Data-quality-definitioner (deterministiska, trösklar konfigurerbara i `config/pipeline.yaml`)

Kritisk BingX-data (pris/volym/funding/OI som krävs för screening/risk) klassificeras deterministiskt, inga hårdkodade gränsvärden i Python:
- **Stale:** datapunktens timestamp är äldre än `max_data_age_seconds`, konfigurerbar per datatyp (t.ex. ticks tolererar kortare ålder än funding rate, som i sig bara uppdateras periodvis av börsen).
- **Ofullständig:** ett obligatoriskt fält för det aktuella analyssteget saknas (t.ex. `open_interest` saknas när Quant Screener kräver OI-avvikelse-signalen). Listan över obligatoriska fält per steg dokumenteras i implementation.
- **Inkonsekvent:** datapunkter bryter en definierad invariant — t.ex. `high < low` i en kline, negativ volym, pris utanför ett rimlighetsintervall satt av senaste N klines, eller avvikelse mellan två samtidiga endpoints för samma mätvärde utöver en konfigurerbar tolerans.

Resultatet klassas i `data_quality_status` (§4): `ok` (inget berört), `degraded` (icke-kritiskt fält berört) eller `invalid` (kritiskt fält berört).

### 8.2 Connector-fel ersätter aldrig kritisk data
BingX-marknadsdata klassad `invalid` enligt §8.1 → instrumentet får `DATA_INVALID`, blockeras deterministiskt från vidare analys. Ingen AI-agent får någonsin analysera en candidate som saknar obligatorisk kritisk data. Icke-kritisk extern källa (nyheter/sentiment/makro) som saknas → pipeline fortsätter, men med explicit `data_quality_status` nedärvt till candidate och synligt i rapportering.

### 8.3 Alla fel är fail-closed mot `CONFIRMED`
- saknad, stale eller inkonsekvent kritisk data (§8.1, §8.2) → ingen `CONFIRMED`
- agent-fel (`status="failed"/"timeout"`) → ingen `CONFIRMED`
- **schemafel** — agentens output validerar inte mot sin Pydantic-modell → samma väg som agent-fel (`status="failed"`), aldrig ett gissat/patchat värde → ingen `CONFIRMED`
- QA/Gate-fel eller `passed=False` → ingen `CONFIRMED`
- Risk/Signal Gate-fel eller regelbrott → ingen `CONFIRMED`
- budgettak nått → `BUDGET_LIMITED`, **aldrig** `REJECTED` (skiljer resursbrist från sakligt underkännande) → ingen `CONFIRMED`
- **okänt/korrupt lagrat statusvärde** — om ett lagrat `status`-värde inte matchar något giltigt `CandidateStatus` (t.ex. efter datakorruption) konstrueras **aldrig** ett `Candidate`-objekt med det värdet — det är inte ett domänstatus. Repository-lagret kastar ett explicit `CorruptCandidateStateError` och skriver ett `CORRUPT_STATE_DETECTED`-audit-event via en väg som inte kräver att den korrupta raden deserialiseras. Anroparen måste hantera felet explicit; det finns ingen tyst eller automatisk väg vidare mot `CONFIRMED`
- systemfel (oväntat undantag) → aldrig en gissning eller implicit godkännande; candidate stannar i sitt sista kända säkra state.

### 8.4 Ingen framtida information får läcka in (look-ahead bias)
Generell arkitekturregel (kärnprincip 4, §1), gäller både live-drift och historisk replay — inte bara ett testkrav för replay. Varje agent- och gate-beslut refererar enbart marknads-/nyhetsdata med timestamp ≤ signalens beslutstimestamp. I historisk replay (§11, Fas 4) testas detta explicit: en simulerad tidpunkt `t` får bevisligen inte se någon datapunkt daterad efter `t` — verifieras genom att injicera manipulerad framtidsdata i test-fixturer och bevisa noll effekt på utfallet. I live-drift är kravet detsamma, men risken är i praktiken bara ett implementationsfel (t.ex. en cache som råkar returnera en redan uppdaterad snapshot) — därför testas även livevägens data-hämtning för att aldrig kunna returnera nyare data än den efterfrågade tidpunkten.

### 8.5 State machine är crash-safe
`UNDER_AI_ANALYSIS` kan aldrig bli ett permanent läge efter processkrasch/restart. Vid uppstart upptäcker systemet avbrutna analyser och för dem till `ANALYSIS_INTERRUPTED` (explicit, synligt state) — återkörs sedan enligt en definierad policy (t.ex. om cooldown/evidensvillkor uppfylls), aldrig tyst.

### 8.6 Paper trading och candidate-skapande är crash-safe och idempotenta
Restart, krasch eller retry får aldrig skapa: dubbla `Candidate`-rader för samma evidens/discovery-cykel, dubbla positioner, dubbla `CLOSED`-events, eller dubbla Telegram-notiser. State-övergångar och trade-events är idempotenta — identifierade med stabila ID:n (candidates härleds deterministiskt från instrument + discovery-run-id + evidence-hash; trade-events härleds från position-id) och kontrolleras mot befintligt state innan en ny övergång/notis skapas.

### 8.7 Fullständig rekonstruerbarhet
Varje `CONFIRMED`-signal och varje paper trade kan rekonstrueras i efterhand enbart från persistent state/event-logg — inget beslut existerar bara i minnet.

## 9. Forecast Agent — strikt definierad sannolikhetssemantik

`ForecastAssessment` producerar **typade, ömsesidigt uteslutande scenarier** som summerar till 1.0, med en explicit tidshorisont, t.ex.:
```
bullish: 0.62, neutral: 0.23, bearish: 0.15   (horizon: 4h)
```
Varje forecast loggas (`ForecastRecord`) med minst: `instrument`, `forecast_timestamp`, `timeframe/horizon`, `scenario_probabilities`, `forecast_version`, relevant input-/market-state-metadata, samt `actual_outcome`/`outcome_timestamp` som fylls i när fönstret passerat.

**Kalibrering (Fas 8):** när forecast-fönstret passerat matchas prognosen automatiskt mot faktiskt utfall. Beräknas: **Brier score**, **calibration buckets/kurva** (av alla gånger agenten sa "~60%", hur ofta inträffade det verkligen), antal observationer per bucket, calibration error, nedbrutet per timeframe/riktning/scenario/marknadsregim där datamängden räcker.

**Explicit `CalibrationStatus` per redovisat mått** (konfigurerbar minimigräns i `config/pipeline.yaml`, t.ex. `min_sample_size_for_calibration`):
- `insufficient_data` — antal observationer under gränsen. Brier score/calibration-kurva får beräknas internt men visas i dashboard/Telegram som "otillräckligt underlag", **aldrig** som ett tillförlitligt kalibreringsresultat.
- `preliminary` — över minimigränsen men fortfarande lågt N — visas med tydlig markering och exakt sample size.
- `calibrated` — tillräckligt N (konfigurerbar tröskel) för att redovisas som ett stabilt mått.

Sample size och `CalibrationStatus` visas **alltid tillsammans** med varje kalibreringssiffra — en kalibreringssiffra får aldrig presenteras isolerat eller som stark evidens utan sin status. Forecast-loggningen är designad så att historiska forecasts kan omvärderas senare utan att LLM-analysen körs om.

Forecast Agentens sannolikheter är en del av evidensbilden men **kan aldrig ensamma skapa `CONFIRMED`** — Risk/Signal Gate och övriga krav gäller alltid.

## 10. Kostnadskontroll (multinivå, deterministisk prioritering)

`budget_limits.yaml`, allt konfigurerbart, allt loggat, synligt i dashboardens System Health-vy:
- `max_candidates_per_discovery_run`
- `max_ai_calls_per_discovery_run`
- `max_ai_calls_per_day`
- `max_cost_budget_per_day` (om tillförlitligt mätbart)
- `warning_threshold` (varning innan tak nås) och hårt stopp vid tak.

Quant Screener körs alltid på hela Top N-universumet (kostar noll LLM-anrop). Om fler candidates kvalificerar sig än AI-budgeten tillåter, prioriterar Candidate Engine deterministiskt efter: (1) data quality, (2) screener/candidate score, (3) likviditet, (4) candidatens färskhet, (5) dedup/cooldown-status. Ej analyserade candidates får `BUDGET_LIMITED`, loggas för synlighet (hur många möjligheter som missades pga resursbrist) — behandlas aldrig som `REJECTED`. Budgetbegränsning får aldrig kringgå säkerhets-, data-quality- eller Risk/Signal Gate-regler; hellre avstå än en förenklad/ofullständig bedömning.

## 11. Paper Trading

- **Konto:** fast, konfigurerbart startkapital (t.ex. 10 000 USDT simulerat).
- **Position sizing:** regelbaserad — Risk Agent föreslår stop-avstånd, den deterministiska gaten beräknar position size så att förlust vid stop = konfigurerad % av kapitalet (`risk_limits.yaml`). Max samtidiga öppna positioner och max total exponering konfigurerbara.
- **Execution — explicit separerade fält, aldrig sammanslagna:**
  - `theoretical_entry` — priset vid det ögonblick Risk/Signal Gate fattar `CONFIRMED`-beslutet (referenspris, inte det pris som faktiskt "handlas").
  - `simulated_fill_price` — `theoretical_entry` justerat för ett konfigurerbart spread- och slippage-antagande (`risk_limits.yaml`), det pris positionen faktiskt öppnas/stängs till i simuleringen.
  - `fees`, `funding` — räknas separat, konfigurerbara satser/modell.
  - Samma uppdelning gäller vid stängning (`theoretical_exit` vs `simulated_fill_price` vid exit).
  - Alla antaganden (spread %, slippage %, fee-sats, funding-modell) är explicita konfigurationsvärden, aldrig implicita eller hårdkodade.
- **Gap-through-hantering:** om priset mellan två övervakningsintervall (§7) passerar SL/TP utan att exakt träffa nivån (vanligt vid volatila candlestick-rörelser), fylls positionen till det **konservativa** priset (värst rimliga pris inom det observerade intervallet, aldrig den exakta SL/TP-nivån om marknaden gappade förbi den) — samma princip i både live monitoring och replay.
- **Transparens om modellbegränsning:** varje `Trade`-post taggas med vilken fill-modell som användes (`fill_model_version`). Systemet får aldrig rapportera eller visa en paper trade som mer realistisk/exakt än vad den faktiska simuleringsmodellen kan garantera — dashboard och Telegram visar alltid att resultatet är simulerat, aldrig som om det vore en verifierad verklig exekvering.
- **Monitoring:** kontinuerlig mot live BingX-pris (separat loop, §7), automatisk stängning vid SL/TP/tidsgräns/invalidation — ingen mänsklig inblandning efter `CONFIRMED`.
- **Historisk replay (Fas 4, före Fas 5 Live Operation):** signal-, risk- och paper-trading-logiken verifieras mot historisk data **innan** systemet körs live. Look-ahead-bias-fria tester enligt §8.4 — en simulerad tidpunkt får bara se data som fanns tillgänglig då. Samma konservativa candle/fill-hantering som ovan tillämpas genomgående i replay.

## 12. Telegram

Delar samma underliggande event-/loggmodell som dashboarden — aldrig två sanningar. Notisnivå konfigurerbar: `important` (default: CONFIRMED + CLOSED + daily report) / `decisions` (+ relevanta NO_TRADE) / `debug` (detaljerade pipeline-events). `NO_TRADE` loggas och visas alltid i dashboarden men skickar ingen Telegram-notis som standard.

- **CONFIRMED-notis:** instrument, LONG/SHORT, entry, stop-loss, target, risk/reward, candidate score, viktigaste evidensen, kort sammanfattning av AI-teamets slutsats, Forecast-scenario, timestamp.
- **CLOSED-notis:** instrument, direction, entry/exit, PnL/resultat, fees/funding/slippage, hållningstid, exit reason, Forecast vs faktiskt utfall.
- **Daily report:** antal scannade instrument, candidates, AI-analyser, CONFIRMED, NO_TRADE/rejected, öppna positioner, win rate, expectancy, cumulative paper PnL, drawdown, system-/datafel.

## 13. Dashboard

Lokal, **read-only** FastAPI-app i första versionen — kan inte ändra trades, riskregler, signaler eller config via UI; ingen UI-funktion kan kringgå Risk/Signal Gate. Läser samma datamodell/event-logg som Telegram.

- **LIVE:** systemstatus, senaste scan, aktuellt Top N, aktuella candidates, AI-analyser/status, CONFIRMED/NO_TRADE, öppna positioner (entry/stop/target/unrealized PnL), aktuell risk/exponering.
- **TRADE HISTORY:** alla candidates och beslut, alla paper trades (entry/exit/PnL/fees/funding/slippage/exit reason/MFE/MAE), komplett audit trail.
- **PERFORMANCE:** cumulative PnL, win rate, expectancy, drawdown, profit factor, antal trades, resultat per instrument, LONG vs SHORT, utveckling över tid.
- **FORECAST:** Forecast-sannolikheter, Brier score, calibration curve, sample size, forecast vs faktiskt utfall, nedbrytning per timeframe/scenario.
- **SYSTEM HEALTH:** BingX API/data-status, data quality, missade scans, agentfel/retries, pipeline latency, antal agent calls, kostnads-/anropsstatistik.

Tydlig lagerseparation i UI:t: raw market data / screener evidence / AI-agentbedömningar / QA/Gate-resultat / deterministiska riskbeslut / paper trading-resultat — aldrig sammanblandat.

## 14. Datakällor

- **BingX (kritisk, publik, nyckellös):** klines/OHLCV, ticker/pris, volym, funding rate, open interest, instrumentmetadata, orderbok/spread om tillgängligt. **Endast market-data-endpoints — aldrig account- eller order-endpoints.** Officiell BingX API-dokumentation för aktuell version verifieras innan implementation (Fas 1 i PLAN_CRYPTO.md) — inga endpoint-format antas i förväg.
- **Nyheter (icke-kritisk):** kryptonyheter via RSS/publikt API, kostnadsfritt, begränsat antal kvalitativa källor till att börja med.
- **External Data (icke-kritisk):** Fear & Greed Index, relevanta makro-/on-chain-mätvärden där datakvalitet/åtkomst räcker, större marknadshändelser.
- Alla externa datapunkter loggas med källa, timestamp, instrument/relevans, råinformation — analysen ska kunna granskas i efterhand.
- Modulärt lager: nya källor läggs till som nya connector-klasser utan att agentarkitekturen byggs om.

**Specifik leverantör/URL är INTE låst av denna SPEC.** Kategorierna (kryptonyheter-RSS, Fear & Greed Index, relevant makro-/on-chain-data) är beslutade; exakt vilken tjänst/endpoint som används väljs och dokumenteras vid implementation (Phase 1/2 i PLAN_CRYPTO.md) enligt kriterierna i §14: kostnadsfri, verifierbar, källangiven. Detta påverkar aldrig den kritiska datakedjan eftersom dessa källor per definition är icke-kritiska (§8.2) — ett byte av nyhetskälla kräver bara en ny connector-klass, ingen ändring i screening/agent/gate-lagren.

## 15. Connector-krav (`MarketDataConnector`/`NewsConnector`, ABC)

Samma bas som Fas 1 §6, tillämpat på denna domän: timeout, retry (`tenacity`, exponential backoff), rate-limit-hantering, TTL-cache, strukturerad loggning (secret-redaction — irrelevant för BingX eftersom ingen nyckel används, men gäller Telegram-token), timestamp-validering, data-quality checks, stale-data detection. Isolerad bakom interface — en ny datakälla kräver ingen ändring i screening/agent/gate-lagren.

## 16. Storage

`Repository` som `typing.Protocol`, `SQLiteRepository` som enda implementation i denna fas — identisk princip som Fas 1 §8. Egen databasfil, gitignorad. Tabeller (minst): `instruments`, `market_data_snapshots`, `candidates`, `evidence_records`, `assessments`, `gate_decisions`, `positions`, `trades`, `forecasts`, `forecast_outcomes`, `telegram_events`, `runs` (observability).

## 17. Observability och secrets

`run_id` per loop-körning, strukturerad loggning, `redact()` på alla loggade dict/strängar (samma mönster som Fas 1, matchar nu även `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`). Alla budget-/rate-limit-beslut loggas och visas i dashboardens System Health.

## 18. Fasindelning (sammanfattning — se PLAN_CRYPTO.md för fullständig plan och acceptance criteria)

Phase 0 Foundation → Phase 1 BingX Market Data → Phase 2 Universe + Quant Screening → Phase 3 AI Intelligence Pipeline → Phase 4 Paper Trading + Historical Replay → Phase 5 Live Paper Operation → Phase 6 Telegram → Phase 7 Dashboard → Phase 8 Forecast Calibration + Evaluation.

Varje fas har egna acceptance criteria och automatiska tester; nästa fas påbörjas inte innan föregående är verifierad.

## 19. Säkerhet

- Ingen kod i `crypto_trading/` ansluter till ett mäklarkonto, hanterar broker-credentials, placerar en riktig order, eller flyttar pengar — i någon fas. Hård gräns, inte konfigurationsflagga (§1).
- **Paper trading är 100 % lokal simulering.** Ingen del av `paper_trading/` gör ett nätverksanrop mot ett BingX-konto eller någon order-/account-endpoint — all "execution" är en beräkning mot redan hämtad publik marknadsdata, skriven till den lokala databasen (§16). Verifieras explicit i Phase 1 acceptance criterion 3 (PLAN_CRYPTO.md): ingen kod-sökväg i hela `crypto_trading/` refererar ett BingX-konto, orderendpoint eller broker-credential.
- BingX-anrop är uteslutande publika market-data-endpoints.
- Alla secrets (Telegram) via `.env`, gitignorad, redigeras i loggar.
- Paper trading-rapporter markeras alltid "Simulerad handel — inga verkliga trades, inga verkliga pengar" i Telegram/dashboard/rapporter.

## 20. Självgranskning

| Fråga | Svar |
|---|---|
| Cirkulär dependency mot `intelligence/`? | Nej — `crypto_trading/` importerar aldrig `intelligence/` och tvärtom; separata paket, separat DB. |
| Kan en enskild AI-agent godkänna en trade? | Nej — QA/Gate (AI) är rådgivande, `CONFIRMED` avgörs enbart av den deterministiska Risk/Signal Gate (§1, §5, §6). |
| Kan systemet gissa vid saknad kritisk data? | Nej — `DATA_INVALID` blockerar deterministiskt innan AI-analys (§8.1). |
| Kan ett krasch mitt i analys skapa ett permanent oklart state? | Nej — `ANALYSIS_INTERRUPTED` + definierad recovery-policy (§8.5). |
| Kan en restart skapa dubbla positioner/notiser? | Nej — idempotenta state-övergångar och events (§8.6). |
| Kan Forecast Agent:s sannolikheter presenteras som bevis? | Nej — alltid med sample size, Brier score och calibration curve; låg N flaggas explicit (§9). |
| Kan riktig handel ske av misstag? | Nej — inga account/order-endpoints existerar i kodbasen, ingen broker-anslutning, ingen kod för det (§1, §19). |
| Kan budgetbegränsning kringgå risk-/data-quality-regler? | Nej — budget påverkar bara vilka candidates som analyseras, aldrig gate-logiken (§8.3, §10). |
| Kan framtida data läcka in i ett beslut (live eller replay)? | Nej — generell arkitekturregel (kärnprincip 4, §1), testad explicit i replay och i livevägens datahämtning (§8.4). |
| Kan `candidate_score` misstas för AI-confidence/forecast-sannolikhet/vinstchans? | Nej — separat tabell i §4 låser vad varje tal betyder; inget kombinerat "confidence"-fält existerar. |
| Kan ett lågt kalibreringsunderlag presenteras som ett tillförlitligt resultat? | Nej — explicit `CalibrationStatus` (`insufficient_data`/`preliminary`/`calibrated`) visas alltid tillsammans med sample size (§9). |
| Kan en paper trade se mer exakt/verklig ut än modellen faktiskt kan garantera? | Nej — `theoretical_entry`/`simulated_fill_price` separerade fält, `fill_model_version` taggad, alltid märkt som simulerad (§11). |
| Kan ett okänt/korrupt state tolkas som ett godkännande? | Nej — konstrueras aldrig som ett `Candidate`-objekt; repository kastar `CorruptCandidateStateError` + skriver `CORRUPT_STATE_DETECTED`-event, går aldrig vidare mot `CONFIRMED` (§8.3). |

Ingen öppen inkonsekvens identifierad. Redo för din slutgranskning.
