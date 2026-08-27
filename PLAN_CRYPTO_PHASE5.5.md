# Crypto Trading — Phase 5.5 (Trading-Integrity Remediation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Status: KOMPLETT (2026-08-27) — SAMTLIGA 9 TASKS OCH 8 ACCEPTANCE CRITERIA VERIFIERADE

Skriven på användarens uttryckliga begäran efter en strategisk genomgång som visade att `PLAN_CRYPTO.md`s Phase 6 (Telegram) inte borde köras före två gap som (a) redan skulle ha varit klara enligt Fas 2/3:s egna ursprungliga scope och (b) orsakar en kostnad som ökar för varje dag systemet körs utan fixen. Denna fas skjuts in **mellan Fas 5 och Fas 6** i roadmapen. Godkänd och implementerad task-för-task med TDD i samma session.

**Vad som är klart:** Task 1 (config), Task 2 (rollmedveten context + fail-safe nyhetsinjektion), Task 3 (connectors trädda genom hela discovery-kedjan), Task 4 (`run.py` kopplar in riktiga connectorer), Task 5 (AC1 bevisad genom hela den riktiga live-kedjan), Task 6 (Storage: `save_forecast_record`/`get_forecast_record`), Task 7 (Orchestrator persisterar `ForecastRecord`), Task 8 (AC3–AC6 bevisade genom hela den riktiga pipelinen: `ForecastRecord` skapas, överlever en genuin restart-simulering, och förblir länkad till den faktiska stängda positionens resultat via delat `candidate_id` — `actual_outcome`/`outcome_timestamp` bekräftat orörda). **Vad som återstår:** enbart Task 9 (slutverifiering — explicit re-körning av `test_replay.py`, `intelligence/`-diff, `test_no_intelligence_coupling.py`, dokumentation av multi-timeframe-beslutet, slutgiltig statusuppdatering). Samtliga sex acceptance criteria i §4 är nu sakligt bevisade genom tester; Task 9 är en formell avslutande kontroll, inte ytterligare funktionalitet.

**Vad denna fas INTE är:** en ny funktionsfas. Den lägger till noll ny affärslogik — den kopplar bara ihop redan byggd, redan godkänd kod (Fas 3:s connectors, Fas 0:s `ForecastRecord`-schema och `forecasts`-tabell) på det sätt de ursprungligen var avsedda att användas.

---

**Goal:** (1) Se till att News/Sentiment-agenten faktiskt får nyhets-/Fear&Greed-underlag i sin kontext, matchande vad dess redan skrivna prompt (`.claude/agents/crypto-news-sentiment.md`) och schema (`NewsSentimentAssessment`) förutsätter. (2) Börja persistera `ForecastRecord`-rader vid varje lyckad Forecast-bedömning, så att Fas 8 har verklig historik att kalibrera mot den dag den faktiskt körs — utan att bygga någon kalibreringslogik nu.

**Architecture:** Ingen ny arkitektur. Två strikt additiva ändringskedjor:
1. `news_connector`/`external_data_connector` blir **valfria** (default `None`) parametrar som trär genom en redan existerande anropskedja (`Orchestrator` → `run_discovery_cycle` → `run_single_cycle` → `run_discovery_tick`), på samma sätt som Fas 5 redan gjorde med `runner`/`repo`/`settings`. `replay.py`/`test_replay.py` skickar aldrig in dem (förblir `None`) — replay-vägen är därför bevisligen opåverkad.
2. Ett nytt, litet sidoeffekt-anrop (`repo.save_forecast_record(...)`) direkt efter att Forecast-rollen redan sparat sin `ForecastAssessment` — ingen ny statusövergång, ingen ny event-typ, ingen ändring i gate-logiken.

**Tech Stack:** Oförändrat. Inga nya beroenden.

**Spec:** `SPEC_CRYPTO.md` §6 (agentkontext, "delad, read-only kontext"), §8.2 (icke-kritisk extern data får saknas utan att pipelinen stoppas), §9 (ForecastRecord-kontraktet, citerat exakt nedan), §14 (nyhetskälla/Fear&Greed redan valda i Fas 3: CoinDesk RSS, alternative.me — bekräftat via `tests/crypto_trading/connectors/test_news_rss.py`/`test_external_data.py`, som är de ENDA ställena de faktiska URL:erna står hårdkodade idag). `PLAN_CRYPTO.md` Phase 3:s "Levererar"-text ("Matar News/Sentiment Analyst") och Phase 8:s "kräver data över tid".

---

## 1. Exakt vilka filer som berörs

**Ändras:**
- `crypto_trading/config/loader.py` — två nya, defaultade `PipelineConfig`-fält.
- `crypto_trading/config/pipeline.yaml` — två nya nycklar.
- `crypto_trading/orchestrator.py` — `Orchestrator.__init__`, `_build_context` (blir rollmedveten + instansmetod), `process_candidate` (fail-safe nyhetsinjektion + forecast-persistence-anrop), `run_discovery_cycle` (två nya valfria kwargs).
- `crypto_trading/paper_trading/replay.py` — `run_single_cycle` (två nya valfria kwargs, vidarebefordrade, aldrig satta av `run_replay`).
- `crypto_trading/discovery_loop.py` — `run_discovery_tick`/`run_forever` (två nya valfria kwargs).
- `crypto_trading/run.py` — konstruerar och trär in riktiga `NewsRSSConnector`/`ExternalDataConnector`-instanser.
- `crypto_trading/storage/db.py` — inga schemaändringar (`forecasts`-tabellen finns redan sedan Fas 0, oanvänd).
- `crypto_trading/storage/repository.py` — två nya metoder: `save_forecast_record`, `get_forecast_record`.

**Skapas:**
- `tests/crypto_trading/test_orchestrator_news_context.py` (eller motsvarande) — rollmedveten kontext + fail-safe-tester.
- `tests/crypto_trading/storage/test_repository_forecast_record.py`.
- `tests/crypto_trading/test_phase5_5_integration.py` — de två end-to-end-bevisen användaren efterfrågat.

**Rörs INTE (se §5 för fullständig lista och motivering):** `schemas/`, `state_machine.py`, `gate/`, `screening/`, `paper_trading/position_*.py`, `monitoring_loop.py`, `agents/loader.py`, `agents/roles.py`, `.claude/agents/*.md` (agentprompterna, inklusive `crypto-news-sentiment.md` — dess instruktioner ("om kontexten saknar nyhets-/sentimentdata... skriv det explicit") förutsätter redan exakt den graceful-degradation-semantik denna plan bygger, ingen promptändring behövs), `intelligence/`, samtliga Fas 0–5-tester utanför de tre nya filerna ovan.

## 2. Befintliga kontrakt som berörs

| Kontrakt | Idag | Efter Fas 5.5 | Bakåtkompatibelt? |
|---|---|---|---|
| `Orchestrator.__init__(repo, runner, settings)` | 3 obligatoriska param | + 2 valfria (`news_connector=None, external_data_connector=None`) | Ja — alla Fas 3/4/5-anrop (`Orchestrator(repo=..., runner=..., settings=...)`) fortsätter fungera oförändrat |
| `Orchestrator._build_context(candidate, run_id)` (statisk) | Samma innehåll för alla 7 roller | Blir instansmetod `self._build_context(candidate, role, run_id)` — nyhets-/Fear&Greed-nycklar bara närvarande när `role == "news_sentiment"` | **Nej, brytande signatur** — men metoden är privat (`_`-prefix), anropas bara internt i samma fil, inga externa tester anropar den direkt (verifierat via grep innan Task 2) |
| `run_discovery_cycle(repo, runner, settings, run_id)` | 4 param | + 2 valfria kwargs | Ja — `replay.py` och samtliga Fas 4/5-tester som anropar den utan de nya kwargs:en är opåverkade |
| `run_single_cycle(snapshot, repo, runner, settings, run_id)` | 5 param | + 2 valfria kwargs | Ja — `run_replay()` och `test_replay.py` skickar aldrig in dem |
| `run_discovery_tick(connector, repo, runner, settings)` | 4 param | + 2 valfria kwargs | Ja — samtliga Task 7/10/11-tester (`test_discovery_loop.py`, `test_phase5_integration.py`) fortsätter fungera oförändrat |
| `PipelineConfig` (Pydantic) | N obligatoriska/defaultade fält | + 2 nya, **defaultade** fält (`news_rss_base_url`, `fear_greed_base_url`) | Ja, explicit — en defaultad (inte obligatorisk) tillägg, för att undvika en upprepning av Fas 4:s "steg 12a"-regression (ett nytt obligatoriskt fält bröt samtliga `_settings()`-hjälpare i hela testsviten) |
| `Repository`-protokollet | 16 metoder (Fas 1–5) | + 2 nya (`save_forecast_record`, `get_forecast_record`) | Ja — rent tillägg, ingen befintlig metod ändras |
| `forecasts`-tabellen (schema) | Provisionerad, oanvänd sedan Fas 0 | Samma schema, nu skriven till | Ja — inget kolumntillägg, bara första skrivningen |

**Inget SPEC-, arkitektur- eller testkonflikt identifierad** under denna genomgång — samtliga ändringar är additiva/valfria, och `_build_context`s signaturändring är en privat, internt begränsad brytning utan externa konsumenter.

## 3. Teststrategi / TDD

Samma disciplin som Fas 4/5: test-först, RED verifierad, minimal implementation, GREEN verifierad, regression + `ruff` efter varje task. Två kategorier:

- **Enhetstester** per ändrad fil (Task 1–4, 6–7): verifierar den lokala kontraktsändringen isolerat (t.ex. "rollen `news_sentiment` får nyckeln `news_headlines`, rollen `technical` gör det inte").
- **Integrationstester** (Task 5, 8): kör hela den riktiga anropskedjan (`run_discovery_tick` → ... → `Orchestrator`) med en riktig (om än stubbad) connector, för att bevisa att kopplingen faktiskt fungerar end-to-end — inte bara att signaturen accepterar parametern.

Mock-only: `NewsRSSConnector`/`ExternalDataConnector` testas mot enkla stub-/fake-objekt (samma stil som `market_snapshot.py`s tester), aldrig riktiga HTTP-anrop i default `pytest`.

## 4. Acceptance Criteria

1. En candidate som analyseras med en konfigurerad `news_connector`/`external_data_connector` producerar en `NewsSentimentAssessment` vars underliggande LLM-anrop **bevisligen fick** nyhetsrubriker/Fear&Greed-data i sin kontext (verifierat genom en spionerande `AgentRunner`-stub som fångar det faktiska context-argumentet, inte bara att inga fel kastades).
2. Om `news_connector`/`external_data_connector` är `None`, eller om ett anrop till dem kastar `ConnectorUnavailableError`, fortsätter analysen normalt med `status="ok"` på `news_sentiment`-rollen (aldrig `"failed"` pga en icke-kritisk källas frånvaro, SPEC §8.2) — testat explicit i båda fallen.
3. Varje lyckad (`status="ok"`) Forecast-bedömning resulterar i en persisterad `ForecastRecord`-rad med korrekt `candidate_id`, `instrument`, `scenario_probabilities`, `horizon`, `forecast_version`, `market_state_metadata`.
4. En omstart (ny `SQLiteRepository`-instans mot samma databasfil) läser tillbaka en identisk `ForecastRecord` — bevisar persistens överlever restart.
5. Given en candidate som når `CONFIRMED` och vars position sedan stängs, kan `ForecastRecord`-raden och den faktiska positionens resultat (entry/exit/PnL-relevanta fält/`exit_reason`) kopplas ihop via delat `candidate_id` — bevisar länkbarhet enligt SPEC §9, utan att beräkna eller lagra något `actual_outcome`-värde (se §5, explicit utanför scope).
6. En återkörd (Task 4-återupptagen) candidates andra Forecast-bedömning skriver över (inte dubblerar) den ursprungliga `ForecastRecord`-raden — samma idempotensprincip som redan gäller för assessments.
7. Fas 4:s determinism- och look-ahead-bias-tester (`tests/crypto_trading/paper_trading/test_replay.py`) förblir gröna, **oförändrade**, utan en enda ny parameter i sina anrop — bevisar att replay-vägen mekaniskt inte kan påverkas av denna fas.
8. Full `tests/crypto_trading/`-svit och hela repo-sviten gröna, `ruff` ren, `intelligence/` orört, `test_no_intelligence_coupling.py` grön (fångar `notify/`-liknande nya filer automatiskt via sina glob-baserade tester).

## 5. Explicit utanför scope för Fas 5.5

- **Multi-timeframe screening.** Analyserad explicit: varken News/Sentiment-kontext-kopplingen eller `ForecastRecord`-persistensen har något beroende av eller påverkan på `quant_screener.evaluate_candidate()`s enkel-timeframe-begränsning — de är helt oberoende kodvägar (screening sker i Fas 2:s lager, långt innan AI-rollerna eller forecast-persistensen körs). **Slutsats: krävs inte för dataintegriteten i punkt 1–2, implementeras inte i denna fas.** Kvarstår dokumenterad som en öppen Fas 2-lucka för en framtida fas.
- **`actual_outcome`/`outcome_timestamp`-ifyllning.** SPEC §9 nämner dessa fält som en del av `ForecastRecord`-kontraktet, "fylls i när fönstret passerat" — men att avgöra VAD som räknas som ett matchande utfall (prisriktning? mål-/stop-träff? vilket tidsfönster exakt?) är i sig ett kalibreringsnära designbeslut. Denna fas skapar och persisterar `ForecastRecord`, bevisar länkbarhet till det faktiska resultatet (AC5), men fyller **aldrig** i `actual_outcome`/`outcome_timestamp` och bygger ingen bakgrundsjobb/loop för det. Explicit Fas 8-jobb.
- **Telegram (Fas 6), Dashboard (Fas 7), kalibreringsberäkning (Fas 8)** — orörda, som instruerat.
- **Live-körning (Fas 5 Task 12/AC3)** — ingen körning mot riktig BingX/Claude-data i denna fas. Kvarstår som öppen punkt, oberoende av Fas 5.5.
- **Filtrering av nyheter per instrument** (t.ex. "bara BTC-relaterade rubriker till en BTCUSDT-candidate"). Agentens egen prompt instruerar den att skilja "verified facts" från "source claims" och att aldrig hitta på — den kan rimligen avgöra själv vilka av de senaste N rubrikerna som är relevanta, exakt som SPEC §6 redan litar på att varje AI-roll gör sin egen tolkning. Att bygga en separat nyckelordsfiltreringsmotor nu vore ny funktionalitet utöver vad som krävs.
- **Nya connector-klasser eller byte av nyhetskälla/Fear&Greed-leverantör.** `NewsRSSConnector`/`ExternalDataConnector` (Fas 3) återanvänds oförändrade.

## 6. Risker

- **Risk: `_build_context`s brytande signaturändring missar en extern anropare.** Mitigerat genom en grep-verifiering (Task 2, Step 0) innan ändringen görs — metoden är redan `_`-prefixad och privat till `orchestrator.py`.
- **Risk: nätverksanrop introduceras i en tidigare helt lokal/synkron kodväg (`_build_context`), vilket kan sakta ner eller (om ohanterat) krascha en candidates analys.** Mitigerat genom obligatorisk `try/except ConnectorUnavailableError` runt anropen (AC2), och genom att koppla in de riktiga connectorernas redan existerande TTL-cache (samma instans återanvänds över en hel discovery-cykels alla candidates, inte en ny instans per candidate) — håller nätverkstrafiken låg.
- **Risk: `PipelineConfig`s två nya fält blir obligatoriska av misstag och bryter alla `_settings()`-testhjälpare (Fas 4:s "steg 12a" upprepas).** Mitigerat genom att explicit göra dem defaultade (§2-tabellen), inte obligatoriska.
- **Risk: `ForecastRecord`-persistens läcker in i determinism-/look-ahead-bias-testerna för replay.** Mitigerat arkitektoniskt — forecast-persistensen triggas av samma `assessment.status == "ok"`-villkor oavsett live/replay, men eftersom **inget av `test_replay.py`s tre tester någonsin läser `forecasts`-tabellen eller assertar mot den**, tillkommer ingen ny risk för dem; de förblir precis lika gröna som innan (AC7 verifierar detta explicit, inte bara antar det).
- **Risk: en instabil extern nyhetskälla (CoinDesk RSS nere) blockerar hela discovery-cykeln.** Mitigerat genom att nyhets-/Fear&Greed-anropen ENDAST görs för `news_sentiment`-rollens kontext-byggande (ett enda, litet, try/except-skyddat anrop per candidate), aldrig i en väg som kan blockera övriga sex rollers körning eller Risk/Signal Gate.
- **Ingen risk identifierad mot Risk/Signal Gate-determinismen** — gaten läser bara `candidate.qa`/övriga assessments status, aldrig context-innehållet direkt.

## 7. Hur Fas 5:s garantier verifieras oförsämrade

- **Determinism:** `test_replay.py`s tre befintliga tester (inklusive `test_replay_is_deterministic_on_repeated_runs`) körs oförändrade, utan en enda ny parameter — grönt resultat bevisar mekaniskt att replay-vägen inte kan ha påverkats (den nya koden är helt ovidkommande för anrop som aldrig sätter de nya kwargs:en).
- **Look-ahead-bias:** samma resonemang — `test_replay_decision_at_time_t_is_unaffected_by_injected_future_data` rör en helt annan kodväg (kline-filtrering i `quant_screener`), otouchad av denna fas.
- **Risk/Signal Gate:** `gate/risk_signal_gate.py` ändras inte alls i denna plan — noll rader.
- **Recovery (Task 4:s återupptagningspolicy, sweep, dagligt AI-tak):** `run_discovery_cycle()`s befintliga logik för sweep/återupptagning/budget-kontroll är helt oberörd — de två nya kwargs:en konsumeras bara av `Orchestrator`-konstruktionen längst ner i samma funktion, ingen ändring i själva cykel-/budgetlogiken. En resumed candidates ANDRA forecast-persistering (AC6) är den enda nya interaktionen med recovery-policyn, och den är en ren UPSERT (skriver över, kraschar aldrig, dubblerar aldrig).
- **`intelligence/`/crypto_trading-gränsen:** inga nya filer refererar `intelligence/` i någon riktning; `test_no_intelligence_coupling.py`s glob-baserade tester täcker automatiskt alla nya/ändrade filer utan att själva testet behöver ändras.

---

## Global Constraints

- **Ingen broker/order-exekvering** — oförändrat.
- **Icke-kritisk data får saknas utan att blockera** (§8.2): nyhets-/Fear&Greed-frånvaro ger aldrig `status="failed"` på `news_sentiment`-rollen, aldrig `DATA_INVALID` på candidaten.
- **Config-drivna URL:er** (Task 1): `news_rss_base_url`/`fear_greed_base_url` i `pipeline.yaml`, defaultade till de redan i test-filerna hårdkodade, verifierade värdena (`https://www.coindesk.com/arc/outboundfeeds/rss/`, `https://api.alternative.me/fng/`).
- **Mock-only default `pytest`** — noll riktiga HTTP-anrop till CoinDesk/alternative.me krävs för grön testsvit.
- **Ingen ändring i `agents/roles.py`, `agents/loader.py`, eller någon `.claude/agents/*.md`-prompt.**
- `intelligence/` rörs inte. `ruff` line-length 100, regler `E,F,I,UP,B`.

---

## Task 1: Config — `news_rss_base_url`/`fear_greed_base_url`

**Files:**
- Modify: `crypto_trading/config/loader.py`
- Modify: `crypto_trading/config/pipeline.yaml`
- Modify: `tests/crypto_trading/config/test_loader.py`

**Interfaces:** `PipelineConfig.news_rss_base_url: str = "https://www.coindesk.com/arc/outboundfeeds/rss/"`, `PipelineConfig.fear_greed_base_url: str = "https://api.alternative.me/fng/"` (defaultade — se §2/§6).

- [x] **Step 1: Write the failing tests** — `test_get_settings_loads_phase5_5_news_urls` (assert defaultvärdena laddas), `test_pipeline_config_allows_overriding_news_urls` (explicit override fungerar). Skrivna i `tests/crypto_trading/config/test_loader.py`, samma stil som befintliga `test_get_settings_loads_phaseN_fields`-tester.
- [x] **Step 2: Run tests to verify they fail** — `AttributeError: 'PipelineConfig' object has no attribute 'news_rss_base_url'`.
- [x] **Step 3: Implement** — två nya, defaultade fält i `PipelineConfig` (`config/loader.py`) samt motsvarande explicita nycklar tillagda i `config/pipeline.yaml` (för synlighet/dokumentation, även om Python-defaulten ensam räcker för bakåtkompatibiliteten).
- [x] **Step 4: Run tests to verify they pass** — 15/15 gröna i `test_loader.py` (13 befintliga + 2 nya).
- [x] **Step 5: Run hela `tests/crypto_trading/config/` + full crypto_trading-svit** — `pytest tests/crypto_trading/ -q`: **300 passed, 1 deselected** (298 innan Task 1 + 2 nya) — bekräftar mekaniskt, inte bara i teorin, att inget befintligt `_settings()`-hjälpobjekt i hela testsviten (test_orchestrator.py, test_market_snapshot.py, test_discovery_wiring.py, test_replay.py, m.fl.) kraschade av de två nya fälten. `ruff check`/`format --check`: rena. Ingen SPEC-/arkitekturkonflikt uppstod.

---

## Task 2: `Orchestrator` — rollmedveten kontext + fail-safe nyhetsinjektion

**Files:**
- Modify: `crypto_trading/orchestrator.py`
- Modify: `tests/crypto_trading/test_orchestrator.py`

**Interfaces:** `Orchestrator.__init__(self, repo, runner, settings, news_connector=None, external_data_connector=None)`. `self._build_context(candidate, role, run_id) -> dict` (instansmetod, inte längre `@staticmethod`).

- [x] **Step 0 (verifiering, inget kodsteg):** `grep -rn "_build_context" tests/ crypto_trading/` — bekräftat: enda träffar var `crypto_trading/orchestrator.py` (definitionen + det enda anropet), ingen extern konsument. Signaturbrytningen i Step 3 var alltså riskfri exakt som antaget.
- [x] **Step 1: Write the failing tests** — samtliga fyra planerade tester skrivna i `tests/crypto_trading/test_orchestrator.py`, plus stödklasser `_SpyRunner` (fångar det faktiska context-argumentet per rollanrop, ärver `MockAgentRunner` oförändrat i övrigt), `_StubNewsConnector`/`_StubExternalDataConnector`/`_RaisingNewsConnector`/`_RaisingExternalDataConnector`. Test 1 assertar även explicit att `"evidence_record"` fortfarande finns kvar i både news_sentiment- och en annan rolls context (bekräftar att befintligt fält inte tappades bort).
- [x] **Step 2: Run tests to verify they fail** — 3/4 röda med `TypeError: Orchestrator.__init__() got an unexpected keyword argument 'news_connector'` (de tre som konstruerar `Orchestrator` med de nya kwargs:en); det fjärde (`..._omits_news_keys_when_connectors_are_none`) passerade redan innan implementation eftersom nuvarande kod aldrig la till dessa nycklar — vaket, inte ett fel, blev en meningsfull assertion efter Step 3.
- [x] **Step 3: Implement** — `self._news_connector`/`self._external_data_connector` (typade `object | None` — protokollet formaliseras inte ytterligare i denna task, se Task 3/4) sparade i `__init__`. `_build_context` blev instansmetod, tar `role`; nyhets-/Fear&Greed-nycklarna läggs bara till när `role == "news_sentiment"` och respektive connector inte är `None`, omslutet av `try/except ConnectorUnavailableError: pass` per anrop (oberoende av varandra — ett fel i den ena hindrar inte den andra). Anropsplatsen i rollloopen uppdaterad till `self._build_context(candidate, role, run_id)`.
- [x] **Step 4: Run tests to verify they pass** — 11/11 gröna i `test_orchestrator.py` direkt efter implementation (7 befintliga + 4 nya), ingen ytterligare iteration krävdes.
- [x] **Step 5: Run hela `tests/crypto_trading/test_orchestrator.py` + `test_phase3_integration.py`** — 14/14 gröna. Full `tests/crypto_trading/`-svit: 304 passed, 1 deselected (300 innan Task 2 + 4 nya). Explicit re-körning av `tests/crypto_trading/paper_trading/test_replay.py` (oförändrad fil): 4/4 gröna — determinism och look-ahead-bias-skyddet bevisat opåverkat, inte bara antaget. `ruff check`/`format --check`: rena. Ingen SPEC-/arkitekturkonflikt uppstod.

---

## Task 3: Trä connectors genom `run_discovery_cycle` → `run_single_cycle` → `run_discovery_tick`

**Files:**
- Modify: `crypto_trading/orchestrator.py` (`run_discovery_cycle`)
- Modify: `crypto_trading/paper_trading/replay.py` (`run_single_cycle`)
- Modify: `crypto_trading/discovery_loop.py` (`run_discovery_tick`, `run_forever`)
- Modify: `tests/crypto_trading/test_discovery_wiring.py`, `tests/crypto_trading/test_discovery_loop.py` (bara om nödvändigt — se Step 5)

**Interfaces:** `run_discovery_cycle(repo, runner, settings, run_id, news_connector=None, external_data_connector=None)`. `run_single_cycle(snapshot, repo, runner, settings, run_id, news_connector=None, external_data_connector=None)`. `run_discovery_tick(connector, repo, runner, settings, news_connector=None, external_data_connector=None)`. `run_forever(connector, repo, runner, settings, news_connector=None, external_data_connector=None)`.

- [x] **Step 1: Write the failing test** — `test_run_discovery_cycle_forwards_news_connector_to_orchestrator`, skriven i `tests/crypto_trading/test_discovery_wiring.py`, återanvänder `_SpyRunner`/`_StubNewsConnector`/`_StubExternalDataConnector` från `test_orchestrator.py` (samma cross-file-importmönster som redan etablerat i den filen) — testar den fulla vägen (context faktiskt fångad hos `news_sentiment`-rollen), inte bara att parametern accepteras. Ett andra, tillagt test: `test_run_discovery_cycle_omits_news_keys_when_connectors_not_passed` — bekräftar explicit att det befintliga anropsmönstret utan connectors ger identiskt beteende (Global Constraints/valfrihet).
- [x] **Step 2: Run test to verify it fails** — `TypeError: run_discovery_cycle() got an unexpected keyword argument 'news_connector'` för det första testet; det andra passerade redan (väntat — bekräftar baslinjen innan ändringen).
- [x] **Step 3: Implement** — rena passthrough-tillägg i samtliga tre funktionssignaturer (`run_discovery_cycle` i `orchestrator.py`, `run_single_cycle` i `replay.py`, `run_discovery_tick`/`run_forever` i `discovery_loop.py`), samtliga defaultade till `None`, exakt enligt planen.
- [x] **Step 4: Run test to verify it passes** — 9/9 gröna i `test_discovery_wiring.py` direkt (7 befintliga + 2 nya).
- [x] **Step 5: Run `tests/crypto_trading/test_discovery_wiring.py`, `tests/crypto_trading/test_discovery_loop.py`, `tests/crypto_trading/paper_trading/test_replay.py` i sin helhet** — 17/17 gröna. **Bekräftat, inte bara antaget:** varken `test_discovery_loop.py` eller `test_replay.py` krävde en enda radändring — de anropar redan funktionerna utan de nya kwargs:en och fick identiskt beteende, exakt som planen förutspådde. Live discovery (via `test_discovery_loop.py`) och replay/determinism (via `test_replay.py`, inklusive `test_replay_is_deterministic_on_repeated_runs` och `test_replay_decision_at_time_t_is_unaffected_by_injected_future_data`) bekräftat opåverkade.
- [x] **Full regression + Ruff:** hela `tests/crypto_trading/`-svit: 312 passed, 1 deselected (310 innan Task 3 + 2 nya). `ruff check`/`format --check`: rena, inga fixar behövdes. Inga ändringar i `connectors/news_rss.py`/`external_data.py` eller `intelligence/` (verifierat via `git status`). Ingen SPEC-/arkitekturkonflikt uppstod.

---

## Task 4: `run.py` — koppla in riktiga connectorer

**Files:**
- Modify: `crypto_trading/run.py`
- Modify: `tests/crypto_trading/test_run_bootstrap.py` (om `main()`s wiring bryts ut till en testbar hjälpfunktion, samma mönster som Fas 6-planens Task 9 föreslog för Telegram-tråden — annars inga nya tester här, `main()` förblir otestad per Fas 5:s princip)

**Interfaces:** `main()` oförändrad extern signatur.

- [x] **Val: manuell verifiering, inte ett nytt TDD-test** — matchar exakt planens egna "annars"-gren: `main()` förblir en tunn, otestad wrapper (samma princip som Fas 5:s `run_forever()`, redan etablerat när `run.py` skapades). Att bryta ut wiring till en testbar hjälpfunktion enbart för denna task hade varit en orelaterad refaktorering utöver vad Task 4 kräver.
- [x] **Steg A: Implement** — `main()` konstruerar `NewsRSSConnector`/`ExternalDataConnector` med `base_url` från `settings.pipeline.news_rss_base_url`/`fear_greed_base_url` (Task 1), och rimliga hårdkodade `timeout_seconds=10.0, max_retries=3, requests_per_second=1, cache_ttl_seconds=300` (låg frekvens/generös cache för icke-kritiska källor, se kommentar i koden) — matchar precedensen att BingX-connectorns `timeout_seconds=10.0` redan är hårdkodad i samma funktion. Skickas in till `discovery_loop.run_forever(...)` via `threading.Thread(..., kwargs={"news_connector": ..., "external_data_connector": ...})` (monitoring-tråden är oförändrad — connectorerna hör bara till discovery/news_sentiment-rollen).
- [x] **Steg B: Manuell verifiering** — (1) `py_compile` + import av `crypto_trading.run` lyckas, `main()`s signatur oförändrad (`() -> None`); (2) en fristående körning konstruerade båda connectorerna med `get_settings()`s RIKTIGA `pipeline.yaml`-värden och bekräftade `_base_url` blev exakt `https://www.coindesk.com/arc/outboundfeeds/rss/` respektive `https://api.alternative.me/fng/`; (3) `tests/crypto_trading/test_run_bootstrap.py` (de två befintliga `build_runner_from_env`-testerna) fortsatt gröna, oförändrade.
- [x] **Regression + Ruff:** full `tests/crypto_trading/`-svit: 312 passed, 1 deselected (oförändrat antal jämfört med innan Task 4 — väntat, eftersom inga nya tester lades till i denna task). Full repo-svit: 416 passed, 1 deselected. `ruff check`/`format --check`: rena. `git diff master -- intelligence/`: tomt. Ingen SPEC-/arkitekturkonflikt uppstod.

---

## Task 5: Integrationstest — News/Fear&Greed når faktiskt fram (AC1)

**Files:**
- Create: `tests/crypto_trading/test_phase5_5_integration.py`

**Interfaces:** inga nya.

- [x] **Step 1: Write the failing test** — `test_news_sentiment_role_actually_receives_news_and_fear_greed_context`, skriven i den nya `tests/crypto_trading/test_phase5_5_integration.py`. Kör `run_discovery_tick()` genom hela den riktiga live-kedjan (`build_live_snapshot` → `run_single_cycle` → `run_discovery_cycle` → `Orchestrator`) med en triggande stub-BingX-connector (återanvänd `_stub_connector_that_triggers_a_candidate()` från `test_discovery_loop.py`), `_StubNewsConnector`/`_StubExternalDataConnector` (återanvända från `test_orchestrator.py`, samma unika testvärde `"UNIK_TESTRUBRIK"`), och en spionerande `_SpyRunner` (också återanvänd). Asserterar exakt innehåll för `news_sentiment`, frånvaro i samtliga övriga sex rollers context, att `evidence_record` fortfarande finns kvar, och att pipelinen fortfarande producerar en position (bevisar att den befintliga discoveryn inte gick sönder).
- [x] **Step 2: Run test to verify it fails/passes** — **grönt direkt vid första körning**, inte rött. Detta är förväntat och korrekt, inte ett TDD-brott: Task 2 (rollmedveten context i `Orchestrator`) och Task 3 (connectors trädda genom hela kedjan) hade redan implementerat och verifierat den underliggande mekaniken var för sig; detta test är - precis som Fas 5:s Task 10/11 - primärt ett regressions-/AC-bekräftelsetest som bevisar att helheten fungerar genom den RIKTIGA entry point (`run_discovery_tick`), inte bara att `run_discovery_cycle` (Task 3:s nivå) vidarebefordrar rätt. Assertionerna är substantiella (exakt innehållsjämförelse, frånvaro i sex separata roller, bevarat `evidence_record`, en faktisk position) - inte ett vakuöst godkännande.
- [x] **Step 3: Confirm passing, fix any discovered gap** — inget gap upptäcktes; ingen produktionskodändring gjordes i denna task (i linje med användarens explicita instruktion att inte ändra fungerande kod i onödan).
- [x] **Regression + Ruff:** `test_phase5_5_integration.py` + `test_discovery_wiring.py` + `test_discovery_loop.py` + `test_orchestrator.py`: 27/27 gröna. Full `tests/crypto_trading/`-svit: 313 passed, 1 deselected (312 innan Task 5 + 1 ny). `ruff check` (en `I001`-importordning, auto-fixad) / `format --check`: rena efteråt. Inga ändringar i `intelligence/`, connectors eller gate-logik. Ingen SPEC-/arkitekturkonflikt uppstod.

---

## Task 6: Storage — `save_forecast_record`/`get_forecast_record`

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Create: `tests/crypto_trading/storage/test_repository_forecast_record.py`

**Interfaces:** `Repository.save_forecast_record(record: ForecastRecord) -> None` (UPSERT på `forecast_id`, samma mönster som `save_assessment`). `Repository.get_forecast_record(candidate_id: str) -> ForecastRecord | None`.

- [x] **Step 1: Write the failing tests** — exakt de fyra planerade testerna, skrivna i `tests/crypto_trading/storage/test_repository_forecast_record.py`.
- [x] **Step 2: Run tests to verify they fail** — 4/4 röda med `AttributeError: 'SQLiteRepository' object has no attribute 'save_forecast_record'`.
- [x] **Step 3: Implement** — `INSERT INTO forecasts (...) VALUES (...) ON CONFLICT(forecast_id) DO UPDATE SET ...`, ordagrant enligt planen, samma stil som `save_assessment`/`save_gate_decision`. `get_forecast_record` frågar på `candidate_id`-kolumnen (matchar den publicerade signaturen `get_forecast_record(candidate_id)`), deserialiserar JSON-fälten och timestamparna, hanterar `outcome_timestamp is None` explicit.
- [x] **Step 4: Run tests to verify they pass** — 4/4 gröna direkt, ingen ytterligare iteration krävdes.
- [x] **Step 5: Lägg till båda metoderna i `Repository`-protokollet** — klart.

---

## Task 7: `Orchestrator` — persistera `ForecastRecord` efter lyckad Forecast-bedömning (AC3)

**Files:**
- Modify: `crypto_trading/orchestrator.py`
- Modify: `tests/crypto_trading/test_orchestrator.py`

**Interfaces:** ingen ny publik funktion — en ny privat hjälpfunktion `_build_forecast_record(candidate: Candidate, assessment: ForecastAssessment, now: datetime) -> ForecastRecord` (ren, testbar separat) anropad inifrån rollloopen.

- [x] **Step 1: Write the failing tests** — de två planerade testerna, skrivna i `tests/crypto_trading/test_orchestrator.py`. Det första assertar dessutom explicit `record.actual_outcome is None`/`record.outcome_timestamp is None` — dokumenterar §5:s gräns som ett levande test, inte bara en kommentar.
- [x] **Step 2: Run tests to verify they fail** — 2/2 röda med `AttributeError: 'SQLiteRepository' object has no attribute 'get_forecast_record'` (väntat innan Task 6 kördes i samma session — bekräftar rätt beroendeordning).
- [x] **Step 3: Implement** — `forecast_id = candidate.candidate_id`, ordagrant enligt planen. `_build_forecast_record()` skriven som en modulnivåfunktion (samma stil som `_utc_day_start`/`_send_to_budget_limited`, ingen `self` behövs). Anropad i rollloopen direkt efter `self._repo.save_assessment(...)`, villkorat på `role == "forecast" and assessment.status == "ok"`.
- [x] **Step 4: Run tests to verify they pass** — 13/13 gröna i `test_orchestrator.py` direkt (11 befintliga + 2 nya), ingen ytterligare iteration krävdes.
- [x] **Regression + Ruff (Task 6+7 gemensamt):** `test_orchestrator.py` + `storage/` (samtliga, inklusive candidate-/position-/runs-/ai-call-testerna) + `test_phase3_integration.py` + `test_discovery_wiring.py`: 77/77 gröna — bekräftar explicit att befintliga candidates/positions/events-tester är opåverkade. Full `tests/crypto_trading/`-svit: 310 passed, 1 deselected (304 innan Task 6/7 + 6 nya). `ruff check` (en radlängdsfix i testfilen) / `format --check`: rena efteråt. Ingen SPEC-/arkitekturkonflikt uppstod — `ForecastRecord`-schemat, `forecasts`-tabellen och SPEC §9:s text verifierades stämma överens exakt innan implementation.

---

## Task 8: Integrationstest — Forecast persisterar, överlever restart, länkas till faktiskt resultat (AC3–AC6)

**Files:**
- Modify: `tests/crypto_trading/test_phase5_5_integration.py`

**Interfaces:** inga nya.

- [x] **Step 1: Write the failing test** — `test_forecast_record_survives_restart_and_links_to_actual_trade_result`, tillagd i `tests/crypto_trading/test_phase5_5_integration.py`, exakt enligt planens fyra delsteg: (1) `run_discovery_tick` med realistiska stop/target (`test_replay.py`s `_happy_fixtures(stop_loss="53000", target="57000")`, importerad som `_realistic_fixtures` — samma mönster som Fas 5:s `test_phase5_integration.py`) till `CONFIRMED`, `ForecastRecord` verifierad; (2) en genuint **ny** `SQLiteRepository`-instans mot samma DB-fil (`repo_after_restart`, en separat sqlite3-anslutning, inte samma Python-objekt), `get_forecast_record` läser tillbaka identiskt innehåll; (3) `run_monitoring_tick` med pris under stop (52000 < 53000) stänger positionen via den riktiga monitoring-logiken; (4) länkbarhet bevisad via delat `candidate_id` (`forecast.candidate_id == position.candidate_id == position.position_id == candidate_id`), samt explicit `assert forecast_after_close.actual_outcome is None` och `outcome_timestamp is None` — dokumenterar AC5:s gräns som ett levande test.
- [x] **Step 2: Run test to verify it fails/passes** — **grönt direkt vid första körning**, återigen förväntat: Task 6 (persistens/idempotens) och Task 7 (skapande efter lyckad Forecast-roll) hade redan implementerat hela mekaniken; detta test bevisar att den fungerar genom den RIKTIGA pipelinen (`run_discovery_tick` → `run_monitoring_tick`, inget slutresultat konstruerat manuellt) och över en genuin restart-simulering. Substantiella assertions (identitetsjämförelse, statusövergångar, explicit `None`-kontroll), inte ett vakuöst godkännande.
- [x] **Step 3: Confirm passing, fix any discovered gap** — inget gap upptäcktes; **noll produktionskodändringar** i denna task (bekräftat via `git status` — samma uppsättning ändrade `crypto_trading/`-filer som efter Task 5, bara testfilen växte).
- [x] **Regression + Ruff:** `test_phase5_5_integration.py` + `test_repository_forecast_record.py` + `test_orchestrator.py` + `test_monitoring_loop.py` + `test_discovery_loop.py`: 27/27 gröna. Full `tests/crypto_trading/`-svit: 314 passed, 1 deselected (313 innan Task 8 + 1 ny). `ruff check` (en radlängdsfix i testfilen) / `format --check`: rena efteråt. `git diff master -- intelligence/`: tomt. Ingen SPEC-/arkitekturkonflikt uppstod.

---

## Task 9: Slutverifiering

**Files:** inga (bara verifieringskommandon).

- [x] **Step 1: Full testsvit för crypto_trading** — `pytest tests/crypto_trading/ -v`: **314 passed, 1 deselected**. Alla gröna.
- [x] **Step 2: Explicit re-run av Fas 4:s determinism-/look-ahead-bias-tester** — `pytest tests/crypto_trading/paper_trading/test_replay.py -v`: 4/4 gröna (inkl. `test_replay_is_deterministic_on_repeated_runs` och `test_replay_decision_at_time_t_is_unaffected_by_injected_future_data`), **utan modifiering av testfilen** (AC7 — mekaniskt bevis, inte antagande).
- [x] **Step 3: Ruff check + format** — `ruff check crypto_trading/ tests/crypto_trading/`, `ruff format --check crypto_trading/ tests/crypto_trading/`: båda rena.
- [x] **Step 4: Verifiera att `intelligence/` fortfarande är orört** — `git diff master -- intelligence/`: tom output.
- [x] **Step 5: Full repo-testsvit** — `pytest -v`: **418 passed, 1 deselected**. Ingen regression.
- [x] **Step 6: Importgräns och broker-frihet** — `pytest tests/crypto_trading/test_no_intelligence_coupling.py -v`: 3/3 PASS.
- [x] **Step 7: Uppdatera `PLAN_CRYPTO_PHASE5.5.md`** — samtliga `- [x]` ikryssade löpande (Task 1–9). Multi-timeframe-analysen (§5) höll: ingen kod skriven för den, bekräftat orört. Fas 5 Task 12/AC3 (manuell live-körning mot riktig BingX/Claude) förblir en **separat, öppen punkt** — inte rörd, inte ersatt, inte uppfylld av något i Fas 5.5. Fas 5.5 innehåller ingen manuell/live-verifiering alls; samtliga åtta ACs ovan är uteslutande automatiskt testverifierade.

**Slutresultat, samtliga acceptance criteria (§4):**

| AC | Bevisad av | Status |
|---|---|---|
| 1 | `test_phase5_5_integration.py::test_news_sentiment_role_actually_receives_news_and_fear_greed_context` | ✅ |
| 2 | `test_orchestrator.py::test_build_context_degrades_gracefully_when_{news,external_data}_connector_raises` + `..._omits_news_keys_when_connectors_are_none` | ✅ |
| 3 | `test_orchestrator.py::test_process_candidate_persists_forecast_record_on_successful_forecast_role` | ✅ |
| 4 | `test_repository_forecast_record.py::test_forecast_record_survives_a_fresh_repository_instance` + `test_phase5_5_integration.py::test_forecast_record_survives_restart_and_links_to_actual_trade_result` | ✅ |
| 5 | `test_phase5_5_integration.py::test_forecast_record_survives_restart_and_links_to_actual_trade_result` | ✅ |
| 6 | `test_repository_forecast_record.py::test_save_forecast_record_upserts_on_resumed_candidate` | ✅ |
| 7 | `test_replay.py` (4/4, oförändrad fil) | ✅ |
| 8 | full svit (314+418 passed) + `ruff` + `intelligence/`-diff + coupling-test | ✅ |

**Fas 5.5: KOMPLETT.** Samtliga automatiskt verifierbara acceptance criteria gröna. Ingen produktionskodändring krävdes i Task 9 (slutverifieringen avslöjade inga fel).

---

## Self-review (utfört innan planen sparas)

**Spec-täckning:** News/Sentiment-kontextkoppling (Task 2–5, matchar Fas 3:s ursprungliga "Matar News/Sentiment Analyst"-intention exakt), `ForecastRecord`-persistens (Task 6–8, SPEC §9:s kontrakt utan dess kalibreringsdel). Samtliga åtta ACs i §4 täckta.

**Placeholder-scan:** inga TBD/TODO. Multi-timeframe-gapet är explicit analyserat och medvetet INTE åtgärdat (§5), med motivering, inte bara utelämnat.

**Typkonsekvens:** `_build_forecast_record()`s output matchar `ForecastRecord`-schemat fältexakt (Task 7 mot `schemas/forecast.py`, läst och citerat i sin helhet i denna plan). `news_connector`/`external_data_connector`-parametrarna har identiskt namn och default (`None`) genom hela kedjan (Orchestrator → run_discovery_cycle → run_single_cycle → run_discovery_tick → run_forever).

**Scope-kontroll:** ingen Telegram-, dashboard- eller kalibreringskod. Ingen ändring i `gate/`, `state_machine.py`, `screening/`, eller någon agentprompt. Ingen ny nyhetskälla. Ingen `actual_outcome`-ifyllningslogik. Fas 5 Task 12/AC3 uttryckligen inte denna fas.
