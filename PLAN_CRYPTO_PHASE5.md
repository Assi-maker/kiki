# Crypto Trading — Phase 5 (Live Paper Operation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Status: TASK 1–11 + 13 (automatiserbar del) KLARA (2026-08-27) — AC3/TASK 12 ÅTERSTÅR (manuell live-körning)

Denna plan skrevs efter genomläsning av `SPEC_CRYPTO.md`, `PLAN_CRYPTO.md` och hela `crypto_trading/`-kodbasen som den såg ut efter Fas 4 (committat direkt på `master`, `ee6447f`), och godkändes av användaren 2026-08-27. Sju beslut plus fyra under exekveringen upptäckta konflikter (Conflict A–D, se respektive task) har lösts, samtliga av användaren explicit, ingen på egen hand.

**Vad som är klart:** Task 1–11 (repository-utökningar, orchestrator-ändringar, `market_snapshot.py`, `discovery_loop.py`, `monitoring_loop.py`, `run.py`, samt Fas 5:s två integrationstester för AC1/AC2) är implementerade, TDD-verifierade och gröna. Task 13:s fem automatiserbara steg är körda (se nedan). **Task 12 (AC3 — en manuell, verklig körning mot riktig BingX-data och riktiga Claude-anrop) har INTE utförts** — det kräver ett aktivt, mänskligt beslut att spendera riktig API-kostnad och köra processen live under 30–60 minuter, vilket ligger utanför vad som görs autonomt i denna session. Fas 5 är i praktiken funktionellt klar; AC3 är den enda kvarstående öppna punkten.

**Task 13 — Slutverifiering, status per 2026-08-27:**
- [x] **Step 1: Full testsvit för `crypto_trading`** — `pytest tests/crypto_trading/ -v`: **298 passed, 1 deselected** (det avsiktligt exkluderade `@pytest.mark.live`-testet, oförändrat sedan tidigare faser).
- [x] **Step 2: Ruff check + format** — `ruff check crypto_trading/ tests/crypto_trading/` och `ruff format --check crypto_trading/ tests/crypto_trading/`: båda rena, inga fel.
- [x] **Step 3: `intelligence/` fortfarande orört** — `git diff master -- intelligence/`: tomt. (Arbetet har skett direkt på `master`, samma mönster som Fas 0–4; ingen separat branch användes.)
- [x] **Step 4: Full repo-testsvit** — `pytest -v` (hela repot, inklusive `intelligence/` och `test_setup`): **402 passed, 1 deselected**, ingen regression utanför `crypto_trading/`.
- [x] **Step 5: Importgräns och broker-frihet** — `pytest tests/crypto_trading/test_no_intelligence_coupling.py -v`: 3/3 PASS (ingen import mellan `crypto_trading/`↔`intelligence/` i någon riktning, inga broker-/order-/kontotermer i `crypto_trading/`).
- [ ] **Step 6 (delvis):** `- [x]` har satts task-för-task i takt med exekveringen (se respektive Task 1–11 ovan) snarare än i en enda slutgenomgång. **Kvar:** statusbannern kan inte slutföras förrän Task 12:s manuella körning är genomförd och dess resultat (antal ticks, candidates, ev. CONFIRMED/positions) dokumenterats här — se Task 12 nedan.

**AC3/Task 12 — explicit öppen punkt:** ingen live-körning mot riktig BingX-data eller riktiga Claude-anrop har gjorts i denna session (ingen `ANTHROPIC_API_KEY`-drift kostnad har ådragits, ingen `python -m crypto_trading.run` har körts). Detta är en medveten paus, inte ett missat steg — Task 12 kräver ett aktivt användarbeslut (sätta en riktig nyckel, acceptera kostnaden, köra processen live under en begränsad tidsperiod och manuellt granska resultatet). Görs när användaren väljer att genomföra den, enligt Task 12:s befintliga instruktioner.

**Ej lösta, explicit flaggade luckor (oförändrat sedan planen skrevs, upprepas här så de inte glöms bort inför Fas 6+):** `news_rss.py`/`external_data.py` fortfarande inte kopplade till agent-kontexten (§"Explicit utanför scope"); multi-timeframe screening fortfarande bara konsumerar `screener_timeframes[0]` (Beslut 5).

---

**Goal:** Gör discovery- och monitoring-pipelinen (redan bevisad deterministisk och look-ahead-bias-fri i Fas 4:s `replay.py`) till två faktiskt körbara, periodiska processer mot **riktig** BingX-marknadsdata: en `discovery_loop.py` som periodiskt bygger en live `MarketSnapshot` och kör hela kedjan, och en tätare `monitoring_loop.py` som stänger triggade positioner mot live-pris. Lägger till det som krävs för att detta ska vara säkert över många cykler och en process-krasch: ett persisterat dagligt AI-anropstak (`max_ai_calls_per_day`, hittills helt operationslöst), en definierad återupptagningspolicy för `ANALYSIS_INTERRUPTED` (hittills bara upptäckt, aldrig återupplivad), och grundläggande system-health-loggning i den redan provisionerade men aldrig skrivna `runs`-tabellen.

**Architecture:** Ingen ny pipeline-logik uppfinns — `paper_trading/replay.py`s per-snapshot-kropp (eligibility → Top N → quant screener → candidate engine → discovery cycle → position opening/closing) faktoriseras ut till en delad, ren funktion `run_single_cycle()` som både `run_replay()` (loopar historiska snapshots) och `discovery_loop.py` (anropar den en gång per tick med en live-byggd snapshot) återanvänder — SPEC kräver identisk logik i replay och live (§8.4, §11), duplicerad kod vore både ett DRY-brott och en risk för att de två vägarna tyst divergerar. `market_snapshot.py` är ett nytt, rent översättningslager: riktiga BingX-anrop (Fas 1:s `BingXMarketDataConnector`) + Fas 1:s `connectors/data_quality`-funktioner (aldrig faktiskt ihopkopplade förrän nu) → en `MarketSnapshot`. `discovery_loop.py`/`monitoring_loop.py` exponerar var sin testbar `run_*_tick()`-funktion (ingen `sleep`/nätverk i pytest) plus en tunn, otestad `run_forever()`-wrapper — samma isolering av testbar logik från oändlig schemaläggning som Fas 4 höll mellan `replay.py` och en riktig live-loop.

**Tech Stack:** Python 3.13, `pydantic` v2, `pytest`, `respx` (för att mocka discovery/monitoring-loopens BingX-anrop, samma mönster som Fas 1). Inga nya beroenden.

**Spec:** `SPEC_CRYPTO.md` §7 (två oberoende loopar), §8.1/§8.2 (data-quality, tillämpat live för första gången), §8.4 (look-ahead-bias, ärvt oförändrat från Fas 4), §8.5 (crash-safe state machine — `ANALYSIS_INTERRUPTED`-återupptagning, byggs nu för första gången), §8.6 (idempotens över omstart), §10 (kostnadskontroll — `max_ai_calls_per_day`, byggs nu för första gången), §17 (observability/`runs`-tabell). `PLAN_CRYPTO.md` Phase 5-avsnittet (Omfattning/Levererar/Acceptance criteria 1–3, citerade i respektive tasks nedan).

## Vad som redan finns (Fas 0–4) — återanvänds rakt av

- **`paper_trading/replay.py`**: hela kedjan eligibility→screener→candidate_engine→discovery_cycle→position_opening→position_closing, redan bevisad deterministisk (AC1) och look-ahead-bias-fri (AC2) i Fas 4. Faktoriseras om i Task 5, beteendet ändras inte.
- **`connectors/bingx_market_data.py`** (Fas 1): `get_contracts()`, `get_ticker(symbol)`, `get_klines(symbol, interval, limit)`, `get_funding_rate(symbol, limit)`, `get_open_interest(symbol)` — samtliga publika, nyckellösa, redan retry/rate-limit/cache-täckta via `BaseMarketDataConnector`.
- **`connectors/data_quality.py`** (Fas 1): `check_completeness`, `check_staleness`, `check_kline_consistency`, `classify` — implementerade och enhetstestade i Fas 1, men **aldrig anropade tillsammans mot en fullständig, flerfälts live-datapunkt någonstans i kodbasen** (Fas 2–4 tar alltid `data_quality_status` som en redan given indata i sina tester). Task 6 är första gången de faktiskt kedjas ihop.
- **`schemas/market.py`**: `InstrumentMetadata.from_raw()`, `Kline.from_raw()`, `Ticker.from_raw()`, `FundingRate.from_raw()`, `OpenInterest.from_raw()` — färdiga parsers från BingX rådata, oanvända utanför Fas 1:s egna tester.
- **`state_machine.py`**: `sweep_interrupted_analyses()` (Fas 0) upptäcker föräldralösa `UNDER_AI_ANALYSIS`-candidates och sveper dem till `ANALYSIS_INTERRUPTED` — anropas redan vid varje `run_discovery_cycle()`. `ANALYSIS_INTERRUPTED → UNDER_AI_ANALYSIS` är redan en tillåten övergång i `ALLOWED_TRANSITIONS`, men **ingenting i kodbasen utlöser den övergången** — se Beslut 2.
- **`storage/db.py`**: `runs`-tabellen (kolumner: `run_id`, `run_type`, `started_at`, `completed_at`, `status`, `errors`) provisionerad i Fas 0, **aldrig skriven till av någon kod**.
- **`config/loader.py` → `BudgetLimitsConfig`**: `max_ai_calls_per_day`, `warning_threshold_pct` — validerade sedan Fas 0, **aldrig lästa av någon annan kod än testerna för själva configen**. Bara `max_ai_calls_per_discovery_run` (ett lokalt, icke-persisterat räknarvärde per `Orchestrator.process_candidate()`-anrop) är operationellt idag.
- **`orchestrator.run_discovery_cycle()`** (Fas 3): entry point som Fas 5:s `discovery_loop.py` anropar per tick, oförändrad signatur.
- **`paper_trading/position_closing.close_triggered_positions()`** (Fas 4): entry point som Fas 5:s `monitoring_loop.py` anropar per tick, oförändrad signatur.

## Vad som saknas — byggs i denna fas

- Riktig live-datahämtning ihopkopplad med data-quality-klassificering → `MarketSnapshot` (`market_snapshot.py`, existerar inte).
- En faktisk återupptagningspolicy för `ANALYSIS_INTERRUPTED`-candidates (Beslut 2) — utan denna blir `ANALYSIS_INTERRUPTED` ett permanent dödläge i en live-drift, i strid med SPEC §8.5:s krav ("återkörs sedan enligt en definierad policy... aldrig tyst").
- Persisterat, återstartssäkert dagligt AI-anropstak (Beslut 3) — utan detta kan `max_ai_calls_per_day` aldrig respekteras över flera cykler/en omstart (PLAN_CRYPTO.md Phase 5 AC2 kräver explicit detta).
- `runs`-tabellen börjar faktiskt skrivas till (Task 1) — grunddata för Fas 7:s System Health-vy (PLAN_CRYPTO.md Phase 5 "Levererar").
- `discovery_loop.py`, `monitoring_loop.py`, `run.py` — existerar inte alls idag (SPEC §3 namnger dem, ingen fas har byggt dem).

## Beslut som fattats för att hålla Fas 5 minimal och SPEC-trogen (flaggade för din granskning, inte blockerande)

**Beslut 1 — Delad cykel-logik, `replay.py` faktoriseras om.**
`run_replay()`s per-snapshot-kropp bryts ut till `run_single_cycle(snapshot, repo, runner, settings, run_id) -> list[Position]` i samma fil. `run_replay()` blir en tunn loop som anropar `run_single_cycle()` per snapshot i tidsordning (oförändrat utåt-beteende, alla Fas 4-tester ska vara gröna oförändrat efteråt). `discovery_loop.py` anropar samma funktion en gång per tick med en live-byggd snapshot. Alternativet — duplicera kedjelogiken i `discovery_loop.py` — avvisas: SPEC kräver att replay och live delar exakt samma beslutslogik (§8.4/§11), och två kopior skulle oundvikligen divergera över tid.

**Beslut 2 — `ANALYSIS_INTERRUPTED`-återupptagning: full återkörning, ingen ny status, ingen ny nyckel.**
SPEC §5-tabellen nämner "nytt `analysis_run_id`" som exempel på recovery-spårning, men `Candidate`-schemat (Fas 0) har aldrig haft ett sådant fält — bara `discovery_run_id` (satt en gång vid skapande). Att lägga till ett nytt persisterat fält nu vore en schemaändring utanför vad AC1 faktiskt kräver. Istället: **varje transition (inklusive återupptagning) skrivs redan idag som ett eget `Event`-rad med sitt eget `run_id`** (Fas 0:s "en enda skrivväg"-princip, `events`-tabellen) — det ger fullständig, granskningsbar historik över exakt vilken körning som satte `UNDER_AI_ANALYSIS` första gången (kraschade) och vilken som satte den andra gången (lyckades), utan ett nytt fält på `Candidate` självt.

Policyn: `run_discovery_cycle()` (Task 4) hämtar **både** `CANDIDATE`- och `ANALYSIS_INTERRUPTED`-statuscandidates varje cykel, sorterar dem tillsammans efter `created_at` (äldst först, deterministiskt), och kör **hela** 7-rollskedjan om från början för varje `ANALYSIS_INTERRUPTED`-candidate (ingen delvis återupptagning från den roll den kraschade på — `save_assessment()` är redan en UPSERT, så ett omkört tidigare-lyckat rollsteg skriver bara över med ett nytt, konsistent resultat). En mer finkornig "återuppta bara från den roll som saknas"-optimering är medvetet utanför scope (YAGNI) — inget i SPEC kräver det, och det skulle komplicera `Orchestrator.process_candidate()`s redan enkla linjära loop.

En `ANALYSIS_INTERRUPTED`-candidate som blockeras av det dagliga AI-anropstaket (Beslut 3) transitioneras **inte** till `BUDGET_LIMITED` — den övergången är inte tillåten i `ALLOWED_TRANSITIONS` och skulle dessutom vara sakligt fel (`BUDGET_LIMITED` betyder "fick ALDRIG någon AI-analys"; en `ANALYSIS_INTERRUPTED`-candidate fick redan minst en delvis analys innan kraschen). Den lämnas orörd i `ANALYSIS_INTERRUPTED` och försöks igen nästa cykel.

**Beslut 3 — Persisterat dagligt AI-anropstak via ett nytt `AI_CALL_MADE`-event, kontrollerat per candidate innan analys påbörjas, med en projected-cost-formel (uppdaterad 2026-08-27 under Task 4-exekvering, se "Conflict A" nedan).**
`max_ai_calls_per_day` måste vara sant över processomstarter (annars är det inte ett verkligt tak, bara ett minnesvärde som nollställs vid krasch — i strid med hela systemets fail-safe/idempotens-linje, §8.3/§8.6). `events`-tabellen är redan append-only och är den enda sanningskällan (Fas 0-princip) — den nya tabellen `runs` är fel plats för individuella anropsräkningar (den är en tabell per *körning*, inte per *anrop*). Lösning: `Orchestrator.process_candidate()` skriver ett `AI_CALL_MADE`-event (via en ny, tunn `Repository.record_ai_call_event()`) för **varje** faktiskt `runner.run()`-anrop (oavsett om resultatet blev `ok`/`failed`/`timeout` — ett anrop kostade ändå, oavsett utfall). Ett dygn definieras som **UTC-kalenderdygn** (`00:00:00 UTC`–`23:59:59 UTC`), inte ett rullande 24-timmarsfönster — enklast att resonera om och konsekvent med att alla timestamps i systemet redan är UTC.

Kontrollen sker **bara på candidate-nivå, innan en candidate påbörjar sin rollkedja** (aldrig mitt i en pågående candidates 7-rollsanalys) — annars skulle en candidate kunna avbrytas efter t.ex. 3 av 7 roller, vilket skulle få `evaluate_risk_signal_gate()` att se saknade assessments och ge `NO_TRADE`/`REJECTED` istället för det sakligt korrekta `BUDGET_LIMITED` (§8.3:s explicita krav: "budgettak nått → BUDGET_LIMITED, aldrig REJECTED").

**Conflict A, upptäckt under Task 4:s exekvering och löst av användaren:** ett naivt `daily_count_so_far >= max_ai_calls_per_day`-test (ursprunglig formulering nedan i Task 4) motsäger just detta "aldrig avbryta mitt i"-krav för en ensam candidate vars fulla analys inte får plats i den kvarvarande dagsbudgeten — count startar på 0, `0 >= cap` är falskt, candidaten påbörjas och tillåts sedan (per samma beslut) köra klart alla sju roller även om det passerar taket. Löst med en **projected-cost-kontroll**: en candidate får bara påbörja sin rollkedja om
```
daily_count_so_far + planned_calls_for_candidate <= max_ai_calls_per_day
```
där `planned_calls_for_candidate = min(len(_ROLE_ORDER), max_ai_calls_per_discovery_run)` — exakt, inte en uppskattning, eftersom `process_candidate()`s rollloop inte har någon annan datahängig avbrottsväg än just detta per-run-tak (samtliga sju roller körs alltid, oavsett assessment-utfall, tills antingen rollistan eller `max_ai_calls_per_discovery_run` tar slut). En `CANDIDATE`-statuscandidate som blockeras av kontrollen transitioneras till `BUDGET_LIMITED` (redan en tillåten övergång, samma mönster som `candidate_engine.prioritize_and_apply_budget()` redan använder för `max_candidates_per_discovery_run`) — detta är en **ny, andra dimension** av samma `BUDGET_LIMITED`-status (resurstak, inte sakligt underkännande), inte en konflikt med Fas 2/3:s existerande användning.

`warning_threshold_pct` loggas (inte blockerande) en gång per `run_discovery_cycle()`-anrop om dagens räknat-innan-cykeln-antal redan överstiger tröskeln — inte per candidate (skulle spamma loggen).

**Conflict B, upptäckt under Task 4:s exekvering och löst av användaren:** `sweep_interrupted_analyses()` har sedan Fas 3 körts som steg 1 i **varje** `run_discovery_cycle()`-anrop (inte bara vid processtart). Fas 5:s återupptagningspolicy (ovan) frågar efter `ANALYSIS_INTERRUPTED`-status omedelbart efter sweepen, i **samma** funktionsanrop — en candidate som just svepts av just denna cykel blir alltså omedelbart återupptagningsbar i samma cykel, inte först nästa. Detta är avsett och korrekt (snabbast möjliga återhämtning efter en krasch), men bröt ett redan godkänt Fas 0/3-test (`test_run_discovery_cycle_sweeps_interrupted_analyses_first`) som antog att sweep-steget aldrig kunde trigga ett agentanrop (testet gav en tom `MockAgentRunner(fixtures={})`, vilket nu kraschar med `KeyError` istället för en ren assertion). Löst mekaniskt: testet uppdaterat till `test_run_discovery_cycle_sweeps_interrupted_analyses_first_and_resumes_it_same_cycle` med giltiga fixtures, verifierar både att sweep-audit-eventet (`ANALYSIS_INTERRUPTED_DETECTED`) skrevs OCH att candidaten når ett terminalt state (`CONFIRMED`) i samma anrop.

**Beslut 4 — Live data-quality-klassificering, första gången SPEC §8.1 faktiskt tillämpas på riktig, flerfälts data.**
Eligibility (vem kommer in i Top N) kräver bara ticker-nivåns dq (fullständighet + färskhet på `Ticker`) — det är allt `check_eligibility()` någonsin konsumerat. Fullständig SPEC §8.1-klassificering (ticker + kline + kline-konsistens + funding + open interest, kombinerat via `classify()`) körs bara för de instrument som faktiskt tar sig in i Top N, eftersom det är där kline/funding/OI-anrop annars skulle slösas på instrument som aldrig blir candidates. `open_interest` har fortfarande inget eget fält i `MarketSnapshot` (det bidrog aldrig till screener-scoringen ens i Fas 2, se `quant_screener.build_funding_oi_evidence()`s docstring) — det hämtas och används **transient**, bara för dess bidrag till `data_quality_status`, sedan kastas det (ingen schemaändring).

**Beslut 5 — Bara ett konfigurerat timeframe konsumeras faktiskt, en redan existerande Fas 2-lucka, inte något Fas 5 uppfinner.**
`pipeline.yaml` deklarerar `screener_timeframes: ["1h", "4h"]` (två värden), men `quant_screener.evaluate_candidate()` har aldrig tagit emot mer än **en** `klines`-lista — `timeframes`-fältet i `CandidateEvidenceRecord` är rent deskriptivt, ingen kod kombinerar faktiskt flera timeframes analytiskt. Detta upptäcktes under research för denna plan, inte en ny Fas 5-brist. Fas 5:s `market_snapshot.py` hämtar klines för `settings.pipeline.screener_timeframes[0]` (första konfigurerade värdet) och flaggar detta explicit i kod-kommentar — **löser inte** den bredare frågan (skulle vara en Fas 2-omarbetning, utanför denna fas scope), matchar bara den redan existerande implementationens faktiska förmåga.

**Beslut 6 — Ingen ny YAML-konfiguration krävs.** Till skillnad från Fas 4 (som lade till `max_position_hold_hours`) kräver Fas 5 inga nya trösklar i `pipeline.yaml`/`risk_limits.yaml`/`budget_limits.yaml` — alla nycklar som behövs (`discovery_interval_minutes`, `monitoring_interval_seconds`, `max_ai_calls_per_day`, `warning_threshold_pct`, samtliga `max_data_age_seconds`/`required_fields`) finns redan, bara oanvända. `monitoring_loop.py`s pris-/candle-hämtning återanvänder samma `screener_timeframes[0]`-intervall som discovery (istället för att införa ett nytt, separat "monitoring-candle-intervall") — enklast möjliga val, ingen konflikt identifierad med SPEC §11:s gap-fill-krav (den konservativa fill-formeln från Fas 4 är redan robust oavsett candle-upplösning).

**Beslut 7 — `run.py`s runtime-bootstrapping (modell, timeout, retries för `RealClaudeRunner`) via miljövariabler, inte ny YAML.** `RealClaudeRunner.__init__` kräver `api_key`, `model`, `timeout_seconds`, `max_retries` — inget av detta är affärslogik-trösklar (som SPEC §7/§10/§11 explicit kräver i YAML), det är deploy-tidskonfiguration i samma kategori som redan-`.env`-hanterade secrets (`TELEGRAM_BOT_TOKEN` nämns i SPEC §17 som `.env`-mönstret). Nya env-variabler: `ANTHROPIC_API_KEY` (redan `.env`-mönstret via `python-dotenv`, som `config/loader.py` redan laddar), `CRYPTO_TRADING_CLAUDE_MODEL` (default `"claude-sonnet-5"`), `CRYPTO_TRADING_AGENT_TIMEOUT_SECONDS` (default `60`), `CRYPTO_TRADING_AGENT_MAX_RETRIES` (default `3`).

**Explicit utanför scope (flaggat, inte löst i denna fas):**
- **`news_rss.py`/`external_data.py` är fortfarande inte ihopkopplade med agent-kontexten.** `Orchestrator._build_context()` skickar bara `candidate_id`/`instrument`/`evidence_record`/`run_id` till varje AI-roll — News/Sentiment Analyst-rollen får aldrig se en faktisk nyhetsartikel eller Fear & Greed-värde, trots att båda connectorerna byggdes i Fas 3. Detta är en lucka som fanns redan efter Fas 3, upptäckt under research för denna plan. Att koppla in den hör hemma i Fas 3:s omfattning (agent-kontext), inte Fas 5:s (schemaläggning/drift) — flaggas här explicit så att den inte glöms bort, men **åtgärdas inte** i denna plan utan ditt uttryckliga beslut om vilken fas som ska ta den.
- Telegram (§12, Fas 6), dashboard (§13, Fas 7), forecast-kalibrering (§9, Fas 8).
- Multi-timeframe screening (se Beslut 5) — kvarstår som en öppen Fas 2-fråga.
- Bulk/batch-hämtning av hela BingX-universumets tickers i ett anrop (om ett sådant BingX-API faktiskt finns) — Fas 1:s connector har bara en per-symbol `get_ticker()`. Vid ~10 req/s och en typisk USDT-perp-universumstorlek är en sekventiell hämtning av hela universumet (för eligibility) plus Top N:s klines/funding/OI väl inom ett 15-minuters discovery-intervall — ingen optimering görs förrän ett verkligt prestandaproblem observerats (YAGNI).

## Global Constraints

- **Ingen broker/order-exekvering, någonsin** — oförändrat från Fas 1–4, `test_no_intelligence_coupling.py::test_crypto_trading_has_no_broker_account_or_order_code` fångar Fas 5:s nya filer automatiskt.
- **Look-ahead-bias-fritt genomgående** (§8.4): `market_snapshot.py` sätter `simulated_now = now` (verklig aktuell tid vid hämtningstillfället) och skickar `evaluated_at=now` in i `evaluate_candidate()`, exakt samma disciplin som `replay.py` — `_sorted_up_to()`s skydd är redan bevisat, ändras inte.
- **Idempotens** (§8.6): `position_id = candidate_id` (Fas 4) gäller oförändrat. `AI_CALL_MADE`-events har egna unika `event_id` (`f"AI_CALL_MADE:{candidate_id}:{role}:{run_id}"`) så en omkörning av samma (candidate, roll, run_id)-kombination aldrig dubbelräknas i det dagliga taket (samma `INSERT OR IGNORE`-mönster som alla andra events).
- **Fail-safe i loopnivå:** ett oväntat undantag i en enskild `run_discovery_tick()`/`run_monitoring_tick()` får **aldrig** krascha `run_forever()` — fångas, loggas, skrivs till `runs.errors`, och nästa tick körs som vanligt (§8.3:s "systemfel → aldrig en gissning... candidate stannar i sitt sista kända säkra state", tillämpat på processnivå för första gången).
- Config-drivna trösklar (`risk_limits.yaml`/`pipeline.yaml`/`budget_limits.yaml`), aldrig hårdkodade i Python — oförändrad princip, inga nya YAML-nycklar behövs (Beslut 6).
- `intelligence/` rörs inte. `ruff` line-length 100, regler `E,F,I,UP,B`.
- Mock-only default `pytest` — noll riktiga BingX- eller Claude-anrop krävs för grön testsvit (samma som alla tidigare faser); `market_snapshot.py`/`discovery_loop.py`/`monitoring_loop.py` testas med en enkel in-memory fake-connector (samma stil som Fas 1:s `respx`-mockar, men här räcker en Python-stub-klass eftersom vi bara behöver kontrollera returvärden, inte HTTP-lagret).

---

## Task 1: Repository — `runs`-tabellen börjar skrivas till

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Create: `tests/crypto_trading/storage/test_repository_runs.py`

**Interfaces:**
- Produces: `Repository.start_run(run_id: str, run_type: str, started_at: datetime) -> None`, `Repository.complete_run(run_id: str, completed_at: datetime, status: str, errors: list[str]) -> None`.

- [x] **Step 1: Write the failing tests**

```python
def test_start_run_persists_a_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-1", "discovery", datetime(2026, 8, 27, 12, 0, tzinfo=UTC))

    row = repo._conn.execute("SELECT * FROM runs WHERE run_id = 'run-1'").fetchone()
    assert row["run_type"] == "discovery"
    assert row["status"] == "running"
    assert row["completed_at"] is None


def test_complete_run_updates_status_and_errors(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-1", "discovery", datetime(2026, 8, 27, 12, 0, tzinfo=UTC))

    repo.complete_run("run-1", datetime(2026, 8, 27, 12, 5, tzinfo=UTC), "ok", [])

    row = repo._conn.execute("SELECT * FROM runs WHERE run_id = 'run-1'").fetchone()
    assert row["status"] == "ok"
    assert json.loads(row["errors"]) == []


def test_complete_run_persists_error_list_on_failure(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.start_run("run-1", "monitoring", datetime(2026, 8, 27, 12, 0, tzinfo=UTC))

    repo.complete_run(
        "run-1", datetime(2026, 8, 27, 12, 0, 30, tzinfo=UTC), "error", ["ConnectorUnavailableError: BingX otillgänglig"]
    )

    row = repo._conn.execute("SELECT * FROM runs WHERE run_id = 'run-1'").fetchone()
    assert row["status"] == "error"
    assert "ConnectorUnavailableError" in json.loads(row["errors"])[0]
```

- [x] **Step 2: Run tests to verify they fail** — `AttributeError: 'SQLiteRepository' object has no attribute 'start_run'`.
- [x] **Step 3: Implement** — `start_run` gör en enkel `INSERT INTO runs (run_id, run_type, started_at, status) VALUES (?,?,?,'running')` (ingen `OR IGNORE`/idempotens-täckning krävs här — `run_id` genereras alltid unikt av anroparen via `logging.new_run_id()`, samma som varje discovery-cykel redan gör). `complete_run` gör `UPDATE runs SET completed_at=?, status=?, errors=? WHERE run_id=?`, med `json.dumps(errors)`.
- [x] **Step 4: Run tests to verify they pass.**
- [x] **Step 5: Lägg till båda metoderna i `Repository`-protokollet.**

---

## Task 2: Repository — persisterat AI-anrop-event + dagligt-tak-räkning

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Create: `tests/crypto_trading/storage/test_repository_ai_call_budget.py`

**Interfaces:**
- Produces: `Repository.record_ai_call_event(event: Event) -> None`, `Repository.count_ai_calls_since(cutoff: datetime) -> int`.

- [x] **Step 1: Write the failing tests**

```python
def _ai_call_event(candidate_id: str, role: str, run_id: str, at: datetime) -> Event:
    return Event(
        event_id=f"AI_CALL_MADE:{candidate_id}:{role}:{run_id}",
        event_type="AI_CALL_MADE",
        aggregate_type="candidate",
        aggregate_id=candidate_id,
        occurred_at=at,
        run_id=run_id,
        schema_version=1,
        payload={"role": role},
    )


def test_record_ai_call_event_persists_a_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.record_ai_call_event(_ai_call_event("c-1", "risk", "run-1", datetime(2026, 8, 27, tzinfo=UTC)))

    row = repo._conn.execute(
        "SELECT * FROM events WHERE event_type = 'AI_CALL_MADE'"
    ).fetchone()
    assert row is not None


def test_record_ai_call_event_is_idempotent_on_retry(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    event = _ai_call_event("c-1", "risk", "run-1", datetime(2026, 8, 27, tzinfo=UTC))

    repo.record_ai_call_event(event)
    repo.record_ai_call_event(event)  # samma event_id - simulerar en retry

    count = repo._conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE event_type = 'AI_CALL_MADE'"
    ).fetchone()["n"]
    assert count == 1


def test_count_ai_calls_since_only_counts_events_at_or_after_cutoff(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    repo.record_ai_call_event(_ai_call_event("c-1", "risk", "run-1", datetime(2026, 8, 27, 0, 30, tzinfo=UTC)))
    repo.record_ai_call_event(_ai_call_event("c-2", "risk", "run-1", datetime(2026, 8, 26, 23, 59, tzinfo=UTC)))

    count = repo.count_ai_calls_since(datetime(2026, 8, 27, 0, 0, tzinfo=UTC))
    assert count == 1
```

- [x] **Step 2: Run tests to verify they fail.**
- [x] **Step 3: Implement** — `record_ai_call_event` återanvänder befintlig `_insert_event()` + commit (samma `INSERT OR IGNORE`-mönster som redan finns, `events`-tabellens append-only-trigger gäller redan). `count_ai_calls_since` gör `SELECT COUNT(*) FROM events WHERE event_type = 'AI_CALL_MADE' AND occurred_at >= ?`.
- [x] **Step 4: Run tests to verify they pass.**
- [x] **Step 5: Lägg till båda metoderna i `Repository`-protokollet.**

---

## Task 3: Orchestrator — skriv `AI_CALL_MADE` per faktiskt agentanrop

**Files:**
- Modify: `crypto_trading/orchestrator.py`
- Modify: `tests/crypto_trading/test_orchestrator.py`

**Interfaces:** ingen signaturändring på `Orchestrator.process_candidate()` — bara ett nytt sidoeffekt-anrop.

- [x] **Step 1: Write the failing test** — `test_process_candidate_records_one_ai_call_event_per_role_invocation`: kör en candidate genom `Orchestrator.process_candidate()` med en `MockAgentRunner` (alla sju roller `status="ok"`), assert `repo.count_ai_calls_since(candidate.created_at)` == 7 efteråt. Ett andra test: `test_process_candidate_records_ai_call_event_even_when_role_times_out` — en roll i `timeout_agents`, verifiera att `AI_CALL_MADE` ändå skrevs för den rollen (anropet kostade, oavsett utfall).
- [x] **Step 2: Run tests to verify they fail** (count blir 0, metoden anropas inte än).
- [x] **Step 3: Implement** — i `process_candidate()`s roll-loop, direkt efter `assessment = self._runner.run(...)` och `ai_calls += 1`, lägg till:

```python
self._repo.record_ai_call_event(
    Event(
        event_id=f"AI_CALL_MADE:{candidate.candidate_id}:{role}:{run_id}",
        event_type="AI_CALL_MADE",
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=datetime.now(UTC),
        run_id=run_id,
        schema_version=1,
        payload={"role": role, "status": assessment.status},
    )
)
```

- [x] **Step 4: Run tests to verify they pass.**

---

## Task 4: `run_discovery_cycle` — dagligt AI-anropstak + `ANALYSIS_INTERRUPTED`-återupptagning

**Files:**
- Modify: `crypto_trading/orchestrator.py`
- Modify: `tests/crypto_trading/test_discovery_wiring.py` (eller ny `tests/crypto_trading/test_discovery_cycle_budget.py` om filen blir för stor)

**Interfaces:** `run_discovery_cycle(repo, runner, settings, run_id)` — oförändrad extern signatur, ny intern logik.

- [x] **Step 1: Write the failing tests** — skrivna i `tests/crypto_trading/test_discovery_wiring.py`, med den ursprungliga planens illustrativa värden korrigerade enligt Conflict A/B och det redan existerande `_persisted_candidate_in_status(repo, status, candidate_id)`-hjälpmönstret i den filen (inte `_seed_candidate`, som aldrig existerat där):

```python
def test_analysis_interrupted_candidate_is_resumed_and_reaches_confirmed(tmp_path):
    """Simulerar en krasch: en candidate sitter i ANALYSIS_INTERRUPTED (redan
    svept av en tidigare sweep_interrupted_analyses-körning). Nästa
    run_discovery_cycle-anrop ska plocka upp den och köra hela rollkedjan."""
    repo = SQLiteRepository(tmp_path / "t.db")
    _persisted_candidate_in_status(repo, "ANALYSIS_INTERRUPTED", candidate_id="interrupted-1")

    results = run_discovery_cycle(
        repo=repo, runner=MockAgentRunner(_happy_fixtures()), settings=_settings(), run_id="run-2"
    )

    assert len(results) == 1
    assert results[0].candidate_id == "interrupted-1"
    assert results[0].status == "CONFIRMED"


def test_daily_ai_call_cap_sends_candidate_to_budget_limited_not_rejected(tmp_path):
    """AC2: taket nås mitt i en flercykel-körning -> BUDGET_LIMITED, aldrig
    ett sakligt underkännande. Conflict A: kräver projected-cost-kontroll -
    med ett naivt "count redan >= cap"-test skulle denna ENSAMMA candidate
    (0 kvar sedan tidigare, 3 i tak, 7 planerade anrop) ha fått starta och
    köra klart alla sju roller, vilket bryter mot "aldrig avbryta mitt i"."""
    repo = SQLiteRepository(tmp_path / "t.db")
    settings = _settings(max_ai_calls_per_day=3)  # 3 räcker inte till en hel 7-rollsanalys
    _persisted_candidate_in_status(repo, "CANDIDATE", candidate_id="c-1")

    results = run_discovery_cycle(
        repo=repo, runner=MockAgentRunner(_happy_fixtures()), settings=settings, run_id="run-1"
    )

    # candidate hann aldrig starta sin analys, men BUDGET_LIMITED-övergången
    # returneras ändå i results (samma synlighetsprincip som
    # candidate_engine.prioritize_and_apply_budget) - inga assessments satta.
    assert len(results) == 1
    assert results[0].candidate_id == "c-1"
    assert results[0].status == "BUDGET_LIMITED"
    assert results[0].risk is None
    assert repo.get_candidate("c-1").status == "BUDGET_LIMITED"


def test_daily_ai_call_cap_leaves_interrupted_candidate_untouched_not_budget_limited(tmp_path):
    """Minor item: max_ai_calls_per_day har Field(gt=0) sedan Fas 0 - kan
    inte sättas till 0 för att simulera "taket redan nått". Använder istället
    cap=1 plus ett pre-seedat AI_CALL_MADE-event samma UTC-dygn, vilket ger
    exakt samma testade tillstånd (dagens räknare == taket redan innan
    cykeln startar) utan att röra den befintliga valideringen."""
    repo = SQLiteRepository(tmp_path / "t.db")
    settings = _settings(max_ai_calls_per_day=1)
    _persisted_candidate_in_status(repo, "ANALYSIS_INTERRUPTED", candidate_id="interrupted-1")
    repo.record_ai_call_event(
        Event(
            event_id="AI_CALL_MADE:other-candidate:risk:run-0",
            event_type="AI_CALL_MADE",
            aggregate_type="candidate",
            aggregate_id="other-candidate",
            occurred_at=_NOW,
            run_id="run-0",
            schema_version=1,
            payload={"role": "risk"},
        )
    )

    results = run_discovery_cycle(
        repo=repo, runner=MockAgentRunner(_happy_fixtures()), settings=settings, run_id="run-1"
    )

    assert results == []
    assert repo.get_candidate("interrupted-1").status == "ANALYSIS_INTERRUPTED"


def test_daily_ai_call_cap_is_respected_across_two_separate_discovery_cycles(tmp_path):
    """Taket är persisterat - gäller även om en 'ny' run_discovery_cycle
    anropas (simulerar en ny cykel efter omstart), inte bara inom en."""
    repo = SQLiteRepository(tmp_path / "t.db")
    settings = _settings(max_ai_calls_per_day=7)  # exakt en candidates fulla analys
    _persisted_candidate_in_status(repo, "CANDIDATE", candidate_id="c-1")
    _persisted_candidate_in_status(repo, "CANDIDATE", candidate_id="c-2")

    run_discovery_cycle(
        repo=repo, runner=MockAgentRunner(_happy_fixtures()), settings=settings, run_id="run-1"
    )
    run_discovery_cycle(
        repo=repo, runner=MockAgentRunner(_happy_fixtures()), settings=settings, run_id="run-2"
    )

    statuses = {repo.get_candidate("c-1").status, repo.get_candidate("c-2").status}
    assert "BUDGET_LIMITED" in statuses
    assert "CONFIRMED" in statuses or "NO_TRADE" in statuses or "REJECTED" in statuses
```

- [x] **Step 2: Run tests to verify they fail** — 3 av 4 röda för avsedd anledning (funktionaliteten saknades); det fjärde (`..._leaves_interrupted_candidate_untouched...`) råkade vara grönt redan innan implementation eftersom dåvarande kod aldrig ens läste `ANALYSIS_INTERRUPTED`-status - ingen riktig verifiering förrän efter Step 3.
- [x] **Step 3: Implement** — ersatte den tidigare `for candidate in repo.find_candidates_by_status("CANDIDATE")`-loopen. **Avviker från planens ursprungliga pseudokod på en punkt (Conflict A):** villkoret är `count_ai_calls_since(day_start) + planned_calls_for_candidate > daily_cap` (projected-cost), inte `count_ai_calls_since(day_start) >= daily_cap`:

```python
def run_discovery_cycle(repo, runner, settings, run_id):
    sweep_interrupted_analyses(repo, swept_at=datetime.now(UTC), run_id=run_id)

    orchestrator = Orchestrator(repo=repo, runner=runner, settings=settings)
    daily_cap = settings.budget_limits.max_ai_calls_per_day
    day_start = _utc_day_start(datetime.now(UTC))
    planned_calls_for_candidate = min(
        len(_ROLE_ORDER), settings.budget_limits.max_ai_calls_per_discovery_run
    )

    to_analyze = sorted(
        [
            *repo.find_candidates_by_status("CANDIDATE"),
            *repo.find_candidates_by_status("ANALYSIS_INTERRUPTED"),
        ],
        key=lambda c: c.created_at,
    )

    daily_count_at_start = repo.count_ai_calls_since(day_start)
    if daily_count_at_start >= float(settings.budget_limits.warning_threshold_pct) * daily_cap:
        log_event(run_id, event="daily_ai_call_budget_warning", count=daily_count_at_start, cap=daily_cap)

    results: list[Candidate] = []
    for candidate in to_analyze:
        if repo.count_ai_calls_since(day_start) + planned_calls_for_candidate > daily_cap:
            if candidate.status == "CANDIDATE":
                results.append(_send_to_budget_limited(repo, candidate, run_id))
            else:
                log_event(run_id, event="daily_ai_call_budget_reached_interrupted_deferred",
                           candidate_id=candidate.candidate_id)
            continue

        allowed, reason = can_transition(candidate.status, "UNDER_AI_ANALYSIS")
        if not allowed:
            raise AssertionError(f"illegal transition attempted: {reason}")
        now = datetime.now(UTC)
        event = Event(...)  # oförändrat från idag
        repo.transition_candidate_with_event(candidate.candidate_id, "UNDER_AI_ANALYSIS", now, event)
        candidate.status = "UNDER_AI_ANALYSIS"
        results.append(orchestrator.process_candidate(candidate, run_id))
    return results


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _send_to_budget_limited(repo, candidate, run_id) -> Candidate:
    now = datetime.now(UTC)
    allowed, reason = can_transition(candidate.status, "BUDGET_LIMITED")
    if not allowed:
        raise AssertionError(f"illegal transition attempted: {reason}")
    event = Event(
        event_id=f"CANDIDATE_TRANSITIONED:{candidate.candidate_id}:BUDGET_LIMITED",
        event_type="CANDIDATE_TRANSITIONED",
        aggregate_type="candidate",
        aggregate_id=candidate.candidate_id,
        occurred_at=now,
        run_id=run_id,
        schema_version=1,
        payload={"from": candidate.status, "to": "BUDGET_LIMITED", "reason": "daily_ai_call_cap"},
    )
    repo.transition_candidate_with_event(candidate.candidate_id, "BUDGET_LIMITED", now, event)
    return candidate.model_copy(update={"status": "BUDGET_LIMITED", "updated_at": now})
```

- [x] **Step 4: Run tests to verify they pass** — alla 4 gröna efter Conflict A-fixen.
- [x] **Step 5: Run hela `tests/crypto_trading/test_phase3_integration.py` och `test_discovery_wiring.py` igen** — säkerställ ingen regression. **Upptäckte Conflict B:** det befintliga (Fas 0/3) `test_run_discovery_cycle_sweeps_interrupted_analyses_first` kraschade med `KeyError` eftersom det antog att en nyss svept candidate aldrig kunde trigga ett agentanrop i samma cykel - inte längre sant nu när återupptagning finns. Löst mekaniskt (samma typ av regression som Fas 4:s steg 12a): testet döpt om till `test_run_discovery_cycle_sweeps_interrupted_analyses_first_and_resumes_it_same_cycle`, gav det giltiga `_happy_fixtures()`, och uppdaterade assertionen till att verifiera både sweep-audit-eventet och det nya terminala utfallet (`CONFIRMED`), istället för det gamla, nu obsoleta antagandet att candidaten skulle förbli i `ANALYSIS_INTERRUPTED`. Alla 280 tester i `tests/crypto_trading/` gröna efteråt (276 innan Task 4 + 4 nya), `ruff check`/`format --check` rena.

---

## Task 5: `paper_trading/replay.py` — faktorisera ut `run_single_cycle`

**Files:**
- Modify: `crypto_trading/paper_trading/replay.py`
- Modify: `tests/crypto_trading/paper_trading/test_replay.py`

**Interfaces:** `run_single_cycle(snapshot: MarketSnapshot, repo: Repository, runner: AgentRunner, settings: Settings, run_id: str) -> list[Position]` (ny, exporterad). `run_replay()` oförändrad signatur och beteende.

**Genomförande 2026-08-27, ordning omkastad på användarens uttryckliga instruktion** (test-först/RED före refaktorering, inte baseline-check först som ursprungligen skrivet ovan — samma slutresultat, striktare TDD-ordning):

- [x] **Step 1 (omdefinierad): Write the new test first and verify RED** — `test_run_single_cycle_can_be_called_directly_with_one_snapshot` skriven och importerad innan `run_single_cycle` existerade → `ImportError: cannot import name 'run_single_cycle'` vid modul-collection (alla 4 tester i filen röda av samma anledning, inte bara den nya).
- [x] **Step 2: Refactor** — flyttade `run_replay()`s loop-kropp (från `eligible_tickers = ...` t.o.m. `close_triggered_positions(...)`) rakt av till `run_single_cycle(snapshot, repo, runner, settings, run_id) -> list[Position]`, som returnerar de positioner som öppnades i just detta snapshot (inte ackumulerat över flera). `run_replay()` blev:

```python
def run_replay(snapshots, repo, runner, settings, run_id) -> list[Position]:
    ordered = sorted(snapshots, key=lambda s: s.simulated_now)
    all_confirmed: list[Position] = []
    for snapshot in ordered:
        all_confirmed.extend(run_single_cycle(snapshot, repo, runner, settings, run_id))
    return [repo.get_position(p.position_id) for p in all_confirmed]
```

- [x] **Step 3: Run `test_replay.py` to verify GREEN** — alla 4/4 gröna, inklusive `test_replay_is_deterministic_on_repeated_runs` (AC1, determinism) och `test_replay_decision_at_time_t_is_unaffected_by_injected_future_data` (AC2, look-ahead-bias) — Fas 4:s båda garantier bekräftat intakta efter refaktoreringen.
- [x] **Step 4 (redan skrivet i Step 1):** `test_run_single_cycle_can_be_called_directly_with_one_snapshot` — bygger `_build_snapshots()[1]` (spik-steget), kör `run_single_cycle()` direkt (ingen loop), assert en `Position` öppnades med `status == "OPEN_POSITION"`.
- [x] **Step 5: Regression + Ruff** — `tests/crypto_trading/paper_trading/` + `test_discovery_wiring.py` + `test_phase3_integration.py` + `test_phase4_integration.py`: 47/47 gröna. Full `tests/crypto_trading/`-svit: 281 passed, 1 deselected (280 innan Task 5 + 1 ny). `ruff check`/`format --check`: rena. Ingen SPEC-/arkitekturkonflikt uppstod — ren mekanisk extraktion.

---

## Task 6: `market_snapshot.py` — live BingX-hämtning + data-quality-klassificering → `MarketSnapshot`

**Conflict, upptäckt 2026-08-27 innan implementation och löst av användaren innan Step 1 kördes:** planens ursprungliga Step 3-pseudokod (nedan, kvar oredigerad som historisk referens) anropar `Ticker.from_raw()`/`Kline.from_raw()`/`FundingRate.from_raw()`/`OpenInterest.from_raw()` **innan** `check_completeness()` på rådatan. Samtliga fyra `.from_raw()`-metoder indexerar direkt i sin rådata-dict (`raw["lastPrice"]` etc., ingen `.get()`-fallback) — en genuint SAKNAD nyckel ger `KeyError`, inte ett tolkningsbart värde. Detta bryter mot ett redan etablerat Fas 1-kontrakt, namngivet explicit i ett befintligt test: `tests/crypto_trading/connectors/test_market_data_integration.py::test_incomplete_ticker_is_invalid_before_even_reaching_pydantic` — completeness-kontrollen måste köras FÖRE någon pydantic-parsning övervägs, aldrig efteråt.

**Löst av användaren:** `check_completeness()` körs alltid på rådatan innan `.from_raw()` övervägs, för samtliga fyra datatyper (ticker, kline, funding, open interest) — inte bara ticker. Om rådatan är ofullständig hoppas `.from_raw()`-anropet över helt (ingen gissad/fabricerad instans), och `data_quality_status[symbol]` sätts ändå explicit till `"invalid"`. För att en symbol vars ticker inte kunde parsas ändå ska få en explicit status i slutresultatet, hämtas den slutliga symbolmängden för platshållar-ifyllnad från `instruments` (instrument-universumet från `get_contracts()`, alltid tillgängligt) istället för från `tickers` (nu bara den framgångsrikt parsade delmängden). Denna avvikelse är lokal till Task 6 — inga ändringar i `connectors/data_quality.py`, `schemas/market.py` eller andra faser. Se den faktiska implementationen i Step 3 (uppdaterad) nedan; den ursprungliga pseudokoden är kvar ovanför som dokumentation av vad som ursprungligen föreslogs och varför det inte höll.

**Files:**
- Create: `crypto_trading/market_snapshot.py`
- Create: `tests/crypto_trading/test_market_snapshot.py`

**Interfaces:** `build_live_snapshot(connector: BingXMarketDataConnector, settings: Settings, now: datetime) -> MarketSnapshot`.

Testas mot en enkel stub-klass (ingen HTTP), t.ex.:

```python
class _StubConnector:
    def __init__(self, contracts, tickers, klines, funding_rates, open_interest):
        self._contracts = contracts
        self._tickers = tickers        # dict[symbol, raw dict]
        self._klines = klines          # dict[symbol, list[raw dict]]
        self._funding_rates = funding_rates  # dict[symbol, list[raw dict]]
        self._open_interest = open_interest  # dict[symbol, raw dict]

    def get_contracts(self): return self._contracts
    def get_ticker(self, symbol): return self._tickers[symbol]
    def get_klines(self, symbol, interval, limit=100): return self._klines[symbol][-limit:]
    def get_funding_rate(self, symbol, limit=1): return self._funding_rates[symbol][-limit:]
    def get_open_interest(self, symbol): return self._open_interest[symbol]
```

- [x] **Step 1: Write the failing tests** — skrivna i `tests/crypto_trading/test_market_snapshot.py` som fristående funktioner (inte pytest-fixtures som i den ursprungliga skissen nedan) plus en lokal `_settings(top_n, screener_timeframes)`-hjälpare och en `_StubConnector` med inbyggd anropsspårning (`klines_calls`, `klines_interval_used`) — samma stub täcker både dq- och spy-testerna, ingen separat fixture per scenario behövdes. Ett femte test tillkom för conflict-fixen: `assert "BTCUSDT" not in snapshot.tickers` och `assert connector.klines_calls == []` i det ofullständiga-ticker-testet, för att bevisa att ingen `Ticker` fabricerades och att pipelinen aldrig nådde Top N för den symbolen.

```python
def test_build_live_snapshot_produces_ok_quality_for_complete_fresh_data(now, connector_with_one_healthy_symbol):
    snapshot = build_live_snapshot(connector_with_one_healthy_symbol, _settings(), now)
    assert snapshot.data_quality_status["BTCUSDT"] == "ok"
    assert "BTCUSDT" in snapshot.tickers
    assert len(snapshot.klines["BTCUSDT"]) > 0


def test_build_live_snapshot_marks_invalid_when_a_ticker_field_is_missing(now, connector_missing_ticker_field):
    snapshot = build_live_snapshot(connector_missing_ticker_field, _settings(), now)
    assert snapshot.data_quality_status["BTCUSDT"] == "invalid"


def test_build_live_snapshot_marks_invalid_for_stale_kline(now, connector_with_stale_kline):
    snapshot = build_live_snapshot(connector_with_stale_kline, _settings(), now)
    assert snapshot.data_quality_status["BTCUSDT"] == "invalid"


def test_build_live_snapshot_only_fetches_klines_funding_oi_for_top_n_symbols(now, connector_spy_with_two_symbols):
    """Prestandagaranti: instrument som inte klarar eligibility (låg
    quote_volume) ska aldrig trigga ett get_klines/get_funding_rate/
    get_open_interest-anrop."""
    build_live_snapshot(connector_spy_with_two_symbols, _settings(top_n=1), now)
    assert connector_spy_with_two_symbols.klines_calls == ["BTCUSDT"]  # inte "LOWVOLUSDT"


def test_build_live_snapshot_uses_only_the_first_configured_screener_timeframe(now, connector_spy):
    """Beslut 5, dokumenterat som ett levande test."""
    build_live_snapshot(connector_spy, _settings(screener_timeframes=["1h", "4h"]), now)
    assert connector_spy.klines_interval_used == "1h"
```

- [x] **Step 2: Run tests to verify they fail** — `ModuleNotFoundError: No module named 'crypto_trading.market_snapshot'` (modulen fanns inte alls än).
- [x] **Step 3: Implement — ursprunglig pseudokod (visar VARFÖR den bytts ut, se Conflict-rutan ovan; detta är INTE vad som faktiskt implementerades):**

```python
def build_live_snapshot(connector, settings: Settings, now: datetime) -> MarketSnapshot:
    contracts_raw = connector.get_contracts()
    instruments = {c["symbol"]: InstrumentMetadata.from_raw(c, now) for c in contracts_raw}

    tickers: dict[str, Ticker] = {}
    ticker_dq: dict[str, str] = {}
    for symbol in instruments:
        raw_ticker = connector.get_ticker(symbol)
        ticker = Ticker.from_raw(raw_ticker)
        tickers[symbol] = ticker
        completeness = check_completeness(raw_ticker, settings.pipeline.required_fields["ticker"])
        staleness = check_staleness(ticker.observed_at, now, settings.pipeline.max_data_age_seconds["ticker"])
        ticker_dq[symbol] = classify(completeness, staleness)

    eligible = []
    for symbol, ticker in tickers.items():
        ok, _reason = check_eligibility(
            instruments[symbol], ticker, "ok" if ticker_dq[symbol] == "ok" else "invalid",
            settings.pipeline.eligibility_min_quote_volume_24h_usdt,
            settings.pipeline.eligibility_max_spread_pct,
        )
        if ok:
            eligible.append(ticker)
    top_n_symbols = set(select_top_n(eligible, settings.pipeline.top_n))

    interval = settings.pipeline.screener_timeframes[0]  # Beslut 5
    klines: dict[str, list[Kline]] = {}
    funding_rates: dict[str, list[FundingRate]] = {}
    data_quality_status: dict[str, str] = {}

    for symbol in top_n_symbols:
        raw_klines = connector.get_klines(symbol, interval, limit=settings.pipeline.screener_lookback_periods + 5)
        parsed_klines = [Kline.from_raw(k, symbol, interval) for k in raw_klines]
        klines[symbol] = parsed_klines

        raw_funding = connector.get_funding_rate(symbol, limit=settings.pipeline.screener_funding_history_limit)
        parsed_funding = [FundingRate.from_raw(f) for f in raw_funding]
        funding_rates[symbol] = parsed_funding

        raw_oi = connector.get_open_interest(symbol)
        oi = OpenInterest.from_raw(raw_oi)

        kline_completeness = classify(*(
            check_completeness(raw, settings.pipeline.required_fields["kline"]) for raw in raw_klines
        )) if raw_klines else "invalid"
        kline_staleness = check_staleness(
            parsed_klines[-1].observed_at, now, settings.pipeline.max_data_age_seconds["kline"]
        ) if parsed_klines else "invalid"
        kline_consistency = check_kline_consistency(parsed_klines, settings.pipeline.kline_consistency_tolerance_pct)
        funding_completeness = classify(*(
            check_completeness(raw, settings.pipeline.required_fields["funding_rate"]) for raw in raw_funding
        )) if raw_funding else "invalid"
        funding_staleness = check_staleness(
            parsed_funding[-1].observed_at, now, settings.pipeline.max_data_age_seconds["funding_rate"]
        ) if parsed_funding else "invalid"
        oi_completeness = check_completeness(raw_oi, settings.pipeline.required_fields["open_interest"])
        oi_staleness = check_staleness(oi.observed_at, now, settings.pipeline.max_data_age_seconds["open_interest"])

        data_quality_status[symbol] = classify(
            ticker_dq[symbol], kline_completeness, kline_staleness, kline_consistency,
            funding_completeness, funding_staleness, oi_completeness, oi_staleness,
        )

    return MarketSnapshot(
        simulated_now=now,
        instruments={s: instruments[s] for s in top_n_symbols} | {s: instruments[s] for s in tickers if s not in top_n_symbols},
        tickers=tickers,
        klines=klines,
        funding_rates=funding_rates,
        data_quality_status=data_quality_status | {s: "invalid" for s in tickers if s not in top_n_symbols and s not in data_quality_status},
    )
```

  (Notera: `data_quality_status`/`klines`/`funding_rates` fylls bara i för Top N-symboler eftersom det är allt `run_single_cycle()` konsumerar — `_select_eligible_tickers` i `run_single_cycle` kör själv sin egen `check_eligibility`, så `instruments`/`tickers` måste ändå innehålla **hela** universumet, inte bara Top N, annars misslyckas den interna eligibility-omprövningen `run_single_cycle` redan gör. Instrument utanför Top N får `"invalid"` som platshållar-dq eftersom `run_single_cycle` aldrig når screener-steget för dem ändå — se till att detta inte av misstag maskerar ett `check_eligibility`-utfall som annars skulle vara `True`.)

**Faktisk implementation (`crypto_trading/market_snapshot.py`), efter Conflict-fixen:** completeness kontrolleras på rådatan FÖRE varje `.from_raw()`-anrop, för alla fyra datatyper. En ofullständig ticker hoppar över `Ticker.from_raw()` helt och exkluderas ur `tickers`; dess `data_quality_status`-post sätts ändå via platshållar-ifyllnaden i slutet, som nu itererar över `instruments` (från `get_contracts()`) istället för över `tickers`. Samma mönster (completeness-check → hoppa över parsning om ofullständig → `[]`/`"invalid"` istället för en gissning) upprepas för kline/funding/open-interest inom Top N-loopen. Se `crypto_trading/market_snapshot.py` för den fullständiga, körda koden (inte återgiven i sin helhet här för att undvika att planen och koden driver isär — planen dokumenterar avsikten och avvikelsen, inte en andra kopia av implementationen).

- [x] **Step 4: Run tests to verify they pass** — 5/5 gröna direkt efter implementation, ingen ytterligare iteration krävdes.
- [x] **Step 5: Ruff + regression** — `ruff check`: en `E501` (rad för lång i `kline_consistency`-uttrycket), fixad genom att bryta uttrycket över fler rader (ingen `_classify_*`-utbrytning behövdes, funktionen klarade sig utan den föreslagna refaktoreringen). `tests/crypto_trading/test_market_snapshot.py` + `test_replay.py`: 9/9 gröna. Full `tests/crypto_trading/`-svit: 286 passed, 1 deselected (281 innan Task 6 + 5 nya). `ruff format --check`: rena. Ingen ytterligare SPEC-/arkitekturkonflikt uppstod efter Conflict-fixen.

---

## Task 7: `discovery_loop.py`

**Files:**
- Create: `crypto_trading/discovery_loop.py`
- Create: `tests/crypto_trading/test_discovery_loop.py`

**Interfaces:** `run_discovery_tick(connector, repo: Repository, runner: AgentRunner, settings: Settings) -> list[Position]` (en tick, testbar). `run_forever(connector, repo, runner, settings) -> None` (otestad `while True` + `time.sleep`, tunn wrapper).

- [x] **Step 1: Write the failing tests** — de tre nedan skrevs som planerat (med de riktiga fixtur-hjälparna från `test_market_snapshot.py`/`test_orchestrator.py` återanvända, inte fristående pytest-fixtures) plus ett fjärde, tillagt test: `test_run_discovery_tick_recovers_a_mid_analysis_crash_on_the_next_tick` — verifierar explicit den komponerade recovery-policyn (sweep + Task 4:s återupptagning) över TVÅ på varandra följande `run_discovery_tick()`-anrop, med en `_CrashingRunner`-hjälpklass som kastar ett oväntat `RuntimeError` mitt i en candidates rollkedja (skiljer sig från `MockAgentRunner`s `fail_agents`/`timeout_agents`, som bara ändrar UTFALLET av ett lyckat anrop, inte kraschar själva anropet).

```python
def test_run_discovery_tick_persists_a_runs_row_on_success(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _stub_connector_with_one_healthy_symbol()

    run_discovery_tick(connector, repo, MockAgentRunner(_happy_fixtures()), _settings())

    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'discovery'").fetchone()
    assert row["status"] == "ok"
    assert row["completed_at"] is not None


def test_run_discovery_tick_marks_run_as_error_and_does_not_raise_on_connector_failure(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _stub_connector_that_raises(ConnectorUnavailableError("BingX nere"))

    result = run_discovery_tick(connector, repo, MockAgentRunner(_happy_fixtures()), _settings())

    assert result == []  # fail-closed, inget kraschar
    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'discovery'").fetchone()
    assert row["status"] == "error"
    assert "ConnectorUnavailableError" in row["errors"]


def test_run_discovery_tick_returns_confirmed_positions(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    connector = _stub_connector_that_triggers_a_candidate()

    positions = run_discovery_tick(connector, repo, MockAgentRunner(_happy_fixtures()), _settings())

    assert len(positions) == 1
```

- [x] **Step 2: Run tests to verify they fail** — `ModuleNotFoundError: No module named 'crypto_trading.discovery_loop'`.
- [x] **Step 3: Implement** — exakt enligt planens pseudokod, ordagrant (ingen avvikelse denna gång), plus typning av `connector: LiveMarketDataSource` (Task 6:s Protocol) istället för otypat:

```python
def run_discovery_tick(connector, repo: Repository, runner: AgentRunner, settings: Settings) -> list[Position]:
    run_id = new_run_id()
    now = datetime.now(UTC)
    repo.start_run(run_id, "discovery", now)
    try:
        snapshot = build_live_snapshot(connector, settings, now)
        positions = run_single_cycle(snapshot, repo, runner, settings, run_id)
        repo.complete_run(run_id, datetime.now(UTC), "ok", [])
        return positions
    except Exception as exc:  # fail-safe på loop-nivå, se Global Constraints
        log_event(run_id, event="discovery_tick_failed", error_type=type(exc).__name__, error=str(exc))
        repo.complete_run(run_id, datetime.now(UTC), "error", [f"{type(exc).__name__}: {exc}"])
        return []


def run_forever(connector, repo: Repository, runner: AgentRunner, settings: Settings) -> None:
    while True:
        run_discovery_tick(connector, repo, runner, settings)
        time.sleep(settings.pipeline.discovery_interval_minutes * 60)
```

- [x] **Step 4: Run tests to verify they pass** — första körningen gav 2 oväntade fel i de nya, mer komplexa testerna (`test_run_discovery_tick_returns_confirmed_positions` och det tillagda recovery-testet) - **ett testfixturfel, inte en design-/SPEC-konflikt**: fixturerna använde en fryst historisk tidsstämpel (`_NOW` från `test_market_snapshot.py`, avsedd för `build_live_snapshot()`s explicita `now`-parameter) för rådata, men `run_discovery_tick()` sätter alltid `now = datetime.now(UTC)` internt (ingen injicerbar klocka, exakt enligt planen) med skarpa `max_data_age_seconds`-trösklar (ticker: 30s) - all fixturdata blev därför "stale" så fort riktig tid hunnit gå om den frusna konstanten. Fixat genom att låta testfilens egna hjälpfunktioner ankra tidsstämplar mot `datetime.now(UTC)` vid anropstillfället istället. Efter fixet: 4/4 gröna direkt.
- [x] **Step 5 (regression + Ruff, utöver planens Step 4):** `test_discovery_loop.py` + `test_discovery_wiring.py` + `test_orchestrator.py` + `test_market_snapshot.py` + `paper_trading/` + `test_phase3_integration.py` + `test_phase4_integration.py`: 63/63 gröna. Full `tests/crypto_trading/`-svit: 290 passed, 1 deselected (286 innan Task 7 + 4 nya). `ruff check` (en `I001`-importordning i testfilen, auto-fixad) / `format --check`: rena efteråt. Verifierat explicit: (1) `run_discovery_tick` bygger snapshoten via Task 6:s `build_live_snapshot()` och kör den genom exakt samma `run_single_cycle()` som `replay.py` (Task 5) — ingen duplicerad pipeline; (2) `run_id`/`runs`-observability skrivs via Task 1:s `start_run`/`complete_run`, status `"ok"`/`"error"` korrekt satt; (3) det dagliga AI-anropstaket och `ANALYSIS_INTERRUPTED`-återupptagningen (Task 4) körs okringgått inuti `run_single_cycle -> run_discovery_cycle` — inga nya genvägar i `discovery_loop.py`; (4) connector-/analysfel hanteras enligt den redan komponerade recovery-policyn: ett fångat undantag skriver `runs.status="error"` och kraschar aldrig anroparen, och en candidate som hann bli `UNDER_AI_ANALYSIS` innan kraschen läks korrekt av nästa ticks sweep+återupptagning (bevisat end-to-end av det tillagda testet). Ingen SPEC-/arkitekturkonflikt uppstod.

---

## Task 8: `monitoring_loop.py`

**Files:**
- Create: `crypto_trading/monitoring_loop.py`
- Create: `tests/crypto_trading/test_monitoring_loop.py`

**Interfaces:** `run_monitoring_tick(connector, repo: Repository, settings: Settings) -> list[Position]`. `run_forever(connector, repo, settings) -> None`.

**Conflict, upptäckt 2026-08-27 innan implementation (läsning av planen mot Task 7 och Global Constraints) och löst av användaren innan Step 1 kördes:** planens ursprungliga Step 3-pseudokod (nedan, kvar oredigerad som historisk referens) har bara ett inre `except ConnectorUnavailableError` runt de tre per-position-anropen (`Ticker.from_raw`/`Kline.from_raw`/`FundingRate.from_raw`/`connector.get_*`) - **ingen yttre catch-all runt hela funktionskroppen**, till skillnad från `discovery_loop.run_discovery_tick()` (Task 7), som redan har exakt ett sådant yttre skydd. Samma eager-parsande `.from_raw()`-metoder som i Task 6 (ingen `.get()`-fallback) kan kasta `KeyError`/`ValueError` på genuint ofullständig eller felformad rådata - inget som fångas av `except ConnectorUnavailableError`. Ett sådant undantag skulle propagera okontrollerat ut ur `run_monitoring_tick()` och, via `run_forever()`s triviala `while True`-loop, krascha hela övervakningsprocessen - en direkt motsägelse mot planens egna Global Constraints: *"ett oväntat undantag i en enskild `run_discovery_tick()`/`run_monitoring_tick()` får aldrig krascha `run_forever()`"*.

**Löst av användaren:** hela `run_monitoring_tick()`-kroppen omsluts nu av ett yttre `try/except Exception`, som loggar felet, sätter `runs.status="error"` med felet i `runs.errors`, och returnerar `[]` - exakt samma mönster som redan implementerat och testat i `discovery_loop.run_discovery_tick()` (Task 7). Det inre `except ConnectorUnavailableError` per position lämnas helt oförändrat (ett enskilt otillgängligt instrument ska fortsatt bara hoppas över, inte avbryta övervakningen av övriga öppna positioner - Task 8:s andra givna test kräver just detta). De två lagren är komplementära, inte överlappande: inre = "ett känt, isolerat anslutningsfel för EN symbol", yttre = "vad som helst annat oväntat, för HELA tick:en". Ett fjärde test tillkom: `test_run_monitoring_tick_does_not_crash_on_unexpected_malformed_payload`, som bevisar explicit att en genuint ofullständig raw-ticker (`KeyError`, inte `ConnectorUnavailableError`) fångas av det nya yttre lagret och aldrig når anroparen. Avvikelsen är lokal till Task 8 - ingen ändring i `discovery_loop.py`, `market_snapshot.py` eller andra faser.

- [x] **Step 1: Write the failing tests** — de tre planerade testerna skrevs (med en dedikerad `_MonitoringStubConnector` och `_seed_open_position()`-hjälpare, inte fristående fixtures) plus ett fjärde, `test_run_monitoring_tick_does_not_crash_on_unexpected_malformed_payload`, som direkt bevisar Conflict-fixen: en rå-ticker med en genuint saknad `lastPrice`-nyckel (`KeyError`, inte `ConnectorUnavailableError`) fångas av det nya yttre lagret, `runs.status` blir `"error"`, och anropet kastar aldrig.

```python
def test_run_monitoring_tick_closes_a_triggered_position(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    connector = _stub_connector_with_price_below_stop("BTCUSDT", price=Decimal("48000"))

    closed = run_monitoring_tick(connector, repo, _settings())

    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_loss"


def test_run_monitoring_tick_skips_instrument_on_connector_failure_without_crashing(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    _seed_open_position(repo, instrument="BTCUSDT", stop_loss=Decimal("49000"))
    connector = _stub_connector_that_raises_for("BTCUSDT", ConnectorUnavailableError("nere"))

    closed = run_monitoring_tick(connector, repo, _settings())

    assert closed == []
    assert repo.find_open_positions()[0].status == "OPEN_POSITION"  # kvar öppen, aldrig gissad stängning


def test_run_monitoring_tick_persists_a_runs_row(tmp_path):
    repo = SQLiteRepository(tmp_path / "t.db")
    run_monitoring_tick(_stub_connector_with_no_open_positions_needed(), repo, _settings())

    row = repo._conn.execute("SELECT * FROM runs WHERE run_type = 'monitoring'").fetchone()
    assert row is not None
```

- [x] **Step 2: Run tests to verify they fail** — `ModuleNotFoundError: No module named 'crypto_trading.monitoring_loop'`.
- [x] **Step 3: Implement — ursprunglig pseudokod (visar VARFÖR den bytts ut, se Conflict-rutan ovan; inte vad som faktiskt implementerades):**

```python
def run_monitoring_tick(connector, repo: Repository, settings: Settings) -> list[Position]:
    run_id = new_run_id()
    now = datetime.now(UTC)
    repo.start_run(run_id, "monitoring", now)
    interval = settings.pipeline.screener_timeframes[0]  # Beslut 6
    price_lookup: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}
    errors: list[str] = []

    for position in repo.find_open_positions():
        symbol = position.instrument
        if symbol in price_lookup:
            continue
        try:
            ticker = Ticker.from_raw(connector.get_ticker(symbol))
            latest_kline = Kline.from_raw(connector.get_klines(symbol, interval, limit=1)[-1], symbol, interval)
            raw_funding = connector.get_funding_rate(symbol, limit=1)
            funding_rate = FundingRate.from_raw(raw_funding[-1]).funding_rate if raw_funding else Decimal("0")
        except ConnectorUnavailableError as exc:
            errors.append(f"{type(exc).__name__}: {exc} ({symbol})")
            continue
        price_lookup[symbol] = (latest_kline.low, latest_kline.high, ticker.last_price, funding_rate)

    closed = close_triggered_positions(repo, price_lookup, now, settings.risk_limits, run_id)
    repo.complete_run(run_id, datetime.now(UTC), "ok" if not errors else "partial_error", errors)
    return closed


def run_forever(connector, repo: Repository, settings: Settings) -> None:
    while True:
        run_monitoring_tick(connector, repo, settings)
        time.sleep(settings.pipeline.monitoring_interval_seconds)
```

**Faktisk implementation (`crypto_trading/monitoring_loop.py`), efter Conflict-fixen:** identisk med ovan förutom att hela kroppen (från `interval = ...` till `return closed`) nu ligger i ett `try`-block, med ett nytt `except Exception as exc:` som loggar (`log_event(..., event="monitoring_tick_failed", ...)`), sätter `repo.complete_run(run_id, ..., "error", [f"{type(exc).__name__}: {exc}"])`, och returnerar `[]` — ordagrant samma struktur som `discovery_loop.run_discovery_tick()` (Task 7). `run_id`/`now`/`repo.start_run(...)` ligger kvar FÖRE `try`-blocket, exakt som i Task 7:s redan godkända mönster (om `start_run` självt skulle fela är det inte ett "tick-fel" att fånga defensivt, samma resonemang som redan gäller för discovery).

- [x] **Step 4: Run tests to verify they pass** — 4/4 gröna direkt efter implementation, ingen ytterligare iteration krävdes (utöver tre `ruff`-radlängdsfixar, se nedan).
- [x] **Step 5 (regression + Ruff, utöver planens Step 4):** `test_monitoring_loop.py` + `test_discovery_loop.py` + `paper_trading/` + `test_phase4_integration.py`: 45/45 gröna. Full `tests/crypto_trading/`-svit: 294 passed, 1 deselected (290 innan Task 8 + 4 nya). `ruff check`: tre `E501` (för långa rader i den nya funktionssignaturen och två testrader), fixade genom radbrytning; `ruff format` gjorde därefter en mindre auto-ombrytning av en testrad. Inga ytterligare fel. `ruff format --check`: rent. Ingen ytterligare SPEC-/arkitekturkonflikt uppstod efter Conflict-fixen.

---

## Task 9: `run.py` — processens startpunkt

**Files:**
- Create: `crypto_trading/run.py`
- Create: `tests/crypto_trading/test_run_bootstrap.py`

**Interfaces:** `build_runner_from_env() -> AgentRunner` (väljer `RealClaudeRunner` om `ANTHROPIC_API_KEY` finns i miljön, annars höjer `ConfigError` — denna fil startar aldrig tyst med en mock i produktion). `main() -> None` (startar båda loparna, en per tråd).

**Genomförande 2026-08-27:** ingen konflikt hittades vid genomläsning av Task 9 mot Global Constraints, `config/loader.py`/`.env`-hanteringen och `discovery_loop.py`/`monitoring_loop.py` (Task 7/8) innan implementation — samtliga signaturer (`RealClaudeRunner.__init__`, `BingXMarketDataConnector.__init__`, `SQLiteRepository.__init__`, båda `run_forever`-funktionerna) matchade planens pseudokod exakt, `ANTHROPIC_API_KEY` var redan det etablerade namnet i projektets `.env`/`.env.example`, och `ConfigError` fanns redan i `config/exceptions.py` med precis den fail-fast-semantik Task 9 kräver. Implementerad ordagrant enligt planen, ingen avvikelse.

- [x] **Step 1: Write the failing tests** — exakt de två planerade testerna, inga fler (inget behov identifierades utöver planens egna två för denna smala funktion).

```python
def test_build_runner_from_env_raises_config_error_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        build_runner_from_env()


def test_build_runner_from_env_returns_real_claude_runner_when_api_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")
    runner = build_runner_from_env()
    assert isinstance(runner, RealClaudeRunner)
```

- [x] **Step 2: Run tests to verify they fail** — `ModuleNotFoundError: No module named 'crypto_trading.run'`.
- [x] **Step 3: Implement** — ordagrant enligt planens pseudokod, ingen avvikelse:

```python
def build_runner_from_env() -> AgentRunner:
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
    discovery_repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)
    monitoring_repo = SQLiteRepository(settings.db_path, settings.pipeline.sqlite_busy_timeout_ms)

    discovery_thread = threading.Thread(
        target=discovery_loop.run_forever, args=(connector, discovery_repo, runner, settings), daemon=True
    )
    monitoring_thread = threading.Thread(
        target=monitoring_loop.run_forever, args=(connector, monitoring_repo, settings), daemon=True
    )
    discovery_thread.start()
    monitoring_thread.start()
    discovery_thread.join()
    monitoring_thread.join()


if __name__ == "__main__":
    main()
```

  (Två separata `SQLiteRepository`-instanser, en per tråd — matchar SPEC §7:s "Loopar delar databas men körs oberoende"; `sqlite_busy_timeout_ms` finns redan konfigurerad exakt för denna samtidiga-skrivning-mot-samma-fil-situation, se `storage/db.py`.)

- [x] **Step 4: Run tests to verify they pass** — 2/2 gröna direkt efter implementation. (`main()` självt körs aldrig i pytest — bara `build_runner_from_env()` är enhetstestad; `main()` är en tunn, manuellt verifierad wrapper, samma princip som `run_forever()` i Task 7/8, verifierad först i Task 12:s manuella körpass.)
- [x] **Step 5 (regression + Ruff, utöver planens Step 4):** `test_run_bootstrap.py` + `test_discovery_loop.py` + `test_monitoring_loop.py` + `test_no_intelligence_coupling.py`: 13/13 gröna. Full `tests/crypto_trading/`-svit: 296 passed, 1 deselected (294 innan Task 9 + 2 nya). `ruff check`/`format --check`: rena, inga fixar behövdes. Ingen SPEC-/arkitekturkonflikt uppstod.

---

## Task 10: Krasch/omstart-integrationstest (AC1)

**Files:**
- Create: `tests/crypto_trading/test_phase5_integration.py`

**Interfaces:** inga nya.

- [x] **Step 1: Write the failing test** — `test_multi_cycle_discovery_with_simulated_crash_and_restart_produces_no_duplicates`, byggd exakt enligt planens tre delsteg. **Testfixturfel upptäckt och åtgärdat under Step 2** (inte en design-/SPEC-konflikt): den ursprungliga versionen återanvände `test_orchestrator._happy_fixtures()`s degenererade `suggested_stop_loss="1"`/`suggested_target="2"` — med vilket pris som helst över 2 breddes positionen omedelbart av tick 2:s EGNA inbäddade `close_triggered_positions()`-anrop (samma cykel som öppnar den), så den var redan `CLOSED` innan testets separata, explicita `run_monitoring_tick()`-anrop någonsin kördes — steg 3:s avsedda verifiering ("en redan öppen position kan stängas normalt efteråt") kunde då aldrig faktiskt utövas. Åtgärdat genom att återanvända `test_replay.py`s realistiska `_happy_fixtures(stop_loss="53000", target="57000")` istället, och en ny lokal `_flat_connector_near_entry()`-hjälpare (pris ~55000, mellan stop och target) för tick 2:s discovery-steg, så positionen överlever fram till det explicita övervaknings-tick:et, som sedan stänger den med ett verkligt pris under stop (52000).
- [x] **Step 2: Run test to verify it fails/passes** — grönt direkt för steg 1–2 (candidate/position-idempotens); det ursprungliga fixturfelet (ovan) upptäcktes här via en misslyckad `assert len(closed) == 1` (`0 == 1`), inte en design-brist.
- [x] **Step 3: Confirm passing, fix any discovered gap** — grönt efter fixturfixen. Ingen brist upptäcktes i produktionskoden (Task 1–9); AC1 håller end-to-end.

---

## Task 11: Flercykel dagligt-tak-integrationstest (AC2)

**Files:**
- Modify: `tests/crypto_trading/test_phase5_integration.py`

**Interfaces:** inga nya.

- [x] **Step 1: Write the failing test** — `test_daily_cap_blocks_third_candidate_across_three_discovery_ticks_deterministically`. Tre OLIKA instrument (BTCUSDT/ETHUSDT/SOLUSDT), ett per tick, istället för att återtriggra samma instrument tre gånger — undviker tvetydighet kring `candidate_engine`s cooldown-/dedup-logik (som bara gäller `REJECTED`-status, inte relevant här, men tre skilda instrument gör testet entydigt oavsett vilket utfall varje candidate faktiskt får). Krävde en liten, ordinär fixturutökning: `test_market_snapshot._settings()` fick en ny `max_ai_calls_per_day: int = 500`-parameter (bakåtkompatibel default, inga befintliga anropare påverkade) eftersom inget befintligt `_settings()`-hjälpobjekt exponerade den parametern.
- [x] **Step 2: Run test to verify it fails/passes** — grönt direkt vid första körning, inget fixturfel eller designgap upptäcktes.
- [x] **Step 3: Confirm passing, fix any discovered gap** — inget gap; AC2 håller end-to-end och deterministiskt vid upprepning (två separata repo-körningar med identisk seed-ordning gav identiskt resultat).

**Regression + Ruff (båda tasken, gemensamt):** full `tests/crypto_trading/`-svit: 298 passed, 1 deselected (296 innan Task 10/11 + 2 nya). `ruff check` (en `I001`-importordning, auto-fixad) / `ruff format` (en radbrytning, auto-fixad): rena efteråt. Ingen SPEC-/arkitekturkonflikt i någotdera task.

---

## Task 12: Manuell live-acceptanskörning (AC3, icke-CI)

**Files:** inga (dokumentationssteg + en manuellt körd session).

- [ ] **Step 1:** Sätt `ANTHROPIC_API_KEY` och en begränsad `budget_limits.yaml` (t.ex. `max_ai_calls_per_day: 20`, `max_candidates_per_discovery_run: 2`) i en engångskonfiguration för detta körpass.
- [ ] **Step 2:** Kör `python -m crypto_trading.run` under en begränsad tidsperiod (t.ex. 30–60 minuter, minst en discovery-cykel om `discovery_interval_minutes` sätts lågt för testet, t.ex. 5).
- [ ] **Step 3:** Verifiera manuellt: minst en discovery-tick loggades i `runs`-tabellen med `status='ok'`, inga oväntade undantag i loggen, `events`-tabellen visar en rimlig `AI_CALL_MADE`-räkning under taket, ingen kod-sökväg anropade ett BingX-konto (redan garanterat av `test_no_intelligence_coupling.py`, men bekräfta att inga varningar/fel om autentisering dök upp — ett tecken på att ett fel-endpoint av misstag anropats).
- [ ] **Step 4:** Dokumentera resultatet (antal ticks, antal candidates, ev. `CONFIRMED`/positions) i statusbannern när planen uppdateras (Task 13), samma mönster som Fas 1/5 i `PLAN_CRYPTO.md`.

---

## Task 13: Slutverifiering

**Files:** inga (bara verifieringskommandon).

- [x] **Step 1: Full testsvit för crypto_trading** — `pytest tests/crypto_trading/ -v`: 298 passed, 1 deselected. Alla gröna.
- [x] **Step 2: Ruff check + format** — `ruff check crypto_trading/ tests/crypto_trading/`, `ruff format --check crypto_trading/ tests/crypto_trading/`: båda rena.
- [x] **Step 3: Verifiera att `intelligence/` fortfarande är orört** — `git diff master -- intelligence/`: tom output.
- [x] **Step 4: Full repo-testsvit** — `pytest -v`: 402 passed, 1 deselected. Ingen regression.
- [x] **Step 5: Importgräns och broker-frihet** — `pytest tests/crypto_trading/test_no_intelligence_coupling.py -v`: 3/3 PASS.
- [ ] **Step 6: Uppdatera `PLAN_CRYPTO_PHASE5.md`** — genomfört löpande task-för-task (se `- [x]` i Task 1–11) snarare än i en enda slutgenomgång; statusbannern (toppen av dokumentet) uppdaterad med exakt testantal och de två explicit-utanför-scope-punkterna. **Kan inte slutföras helt** — väntar på Task 12:s manuella körresultat innan den sista raden i statusbannern kan skrivas.

**AC3/Task 12 — INTE utfört i denna session.** Ingen live-körning mot riktig BingX-data eller riktiga Claude-anrop har gjorts; ingen `ANTHROPIC_API_KEY`-kostnad har ådragits. Kräver ett aktivt användarbeslut (se statusbannern högst upp i dokumentet). Fas 5 är i övrigt funktionellt komplett och verifierad.

---

## Self-review (utfört innan planen sparas)

**Spec-täckning:** delad cykel-logik (Task 5, förutsättning för allt annat), live data-quality-klassificering första gången (Task 6, §8.1), periodisk discovery (Task 7, §7), tätare monitoring (Task 8, §7), processentrypoint (Task 9), krasch/omstart-recovery (Task 10, AC1/§8.5/§8.6), flercykel-kostnadstak (Task 4 + Task 11, AC2/§10), manuell live-verifiering (Task 12, AC3), system-health-grund via `runs`-tabellen (Task 1, §17). Alla tre AC:er från `PLAN_CRYPTO.md` täckta.

**Placeholder-scan:** inga TBD/TODO. Sju beslut (delad cykel-logik / återupptagningspolicy / dagligt tak / dq-klassificering / enkelt-timeframe / ingen ny YAML / env-var-bootstrap) är explicit dokumenterade och motiverade, inklusive tre punkter som är **redan existerande luckor från Fas 2–4** (upptäckta under research, inte nya Fas 5-avvikelser): news/external-data aldrig kopplat till agent-kontext, multi-timeframe aldrig faktiskt implementerat, `ANALYSIS_INTERRUPTED` aldrig återupplivad förrän nu.

**Typkonsekvens:** `run_single_cycle()`s signatur matchar exakt vad `run_replay()` redan skickar in per snapshot (Task 5) och vad `discovery_loop.run_discovery_tick()` skickar in (Task 7) — samma fyra positionsargument plus `run_id` överallt. `MarketSnapshot` (Fas 4:s schema) återanvänds oförändrat av `build_live_snapshot()` (Task 6) — inget nytt fält läggs till trots att `open_interest` behövs internt (transient, se Beslut 4). `Repository`-protokollets fyra nya metoder (`start_run`, `complete_run`, `record_ai_call_event`, `count_ai_calls_since`) är konsekvent namngivna med befintliga verb-mönster (`save_*`, `count_*`, `create_*_with_event`).

**Scope-kontroll:** ingen Telegram-, dashboard- eller kalibreringskod. Ingen ändring av `intelligence/`. Ingen ny YAML-konfiguration (Beslut 6). Ingen lösning av multi-timeframe- eller news-context-luckorna (explicit flaggade, inte åtgärdade — kräver ditt beslut om vilken fas som äger dem). Ingen bulk-BingX-optimering utan observerat prestandaproblem (YAGNI).
