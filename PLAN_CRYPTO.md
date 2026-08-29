# PLAN — Crypto Market Intelligence & Paper Trading System (crypto_trading/)

Status: **Fasroadmap — inte godkänd för implementation.** Detta är en fasindelad roadmap med acceptance criteria per fas, i linje med `SPEC_CRYPTO.md`. Detta är INTE den granulära steg-för-steg-planen (den skrivs per fas av `writing-plans`-skillen efter att denna roadmap och SPEC_CRYPTO.md är godkända, med start i Phase 0 — samma mönster som Fas 1:s `PLAN.md` men skalat till ett större system).

**Grundprincip genom alla faser** (identisk med Fas 1): Pydantic/typade scheman, `Repository`-protokoll, `AgentRunner` Real/Mock, deterministisk pipeline före LLM-anrop, kod-nivå-gates, isolerade komponenter, inga dolda side effects. Ingen fas påbörjas innan föregående fas har gröna tester och uppfyllda acceptance criteria. `intelligence/` rörs aldrig.

---

## Phase 0 — Foundation

**Omfattning:** projektstruktur, konfiguration, datamodeller/scheman, repository-interfaces, event/audit-logg, testinfrastruktur, deterministisk state machine.

**Levererar:**
- `crypto_trading/` paketskelett enligt SPEC_CRYPTO.md §3.
- `config/pipeline.yaml`, `config/risk_limits.yaml`, `config/budget_limits.yaml` — alla nycklar från SPEC §7/§10/§11, inget hårdkodat.
- Alla scheman i `schemas/` (§4): `CandidateEvidenceRecord`, sju `*Assessment`-typer, `ForecastRecord`, `Position`/`Trade`, `Candidate` + `CandidateStatus`/`PositionStatus` (§5).
- `state_machine.py`: rena, testbara funktioner för samtliga övergångar i §5, inklusive `ANALYSIS_INTERRUPTED`-recovery-logik (§8.5), okänt/korrupt state-hantering via `CorruptCandidateStateError` (§8.3) och idempotens-kontroller (§8.6).
- `storage/`: `Repository`-protokoll + `SQLiteRepository`, tabeller enligt §16.
- Event/audit-logg: en enda skrivväg som Telegram (Phase 6) och dashboard (Phase 7) senare båda läser från — grunden läggs nu så att ingen framtida fas kan skapa en avvikande "sanning".
- Testinfrastruktur: `tests/crypto_trading/` speglar paketet, `MockAgentRunner`/`respx`-mönster återanvänt från Fas 1, noll nätverk/API-nycklar i default `pytest`.

**Acceptance criteria:**
1. `pytest` grönt, `ruff check`/`ruff format --check` rent — noll nätverksberoende.
2. Varje status-övergång i §5 har ett explicit test, inklusive alla förbjudna övergångar (t.ex. `BUDGET_LIMITED` kan aldrig tolkas som `REJECTED` av någon kod som konsumerar status).
3. Ett test simulerar en process-krasch mitt i `UNDER_AI_ANALYSIS` och verifierar att recovery ger `ANALYSIS_INTERRUPTED`, aldrig ett tyst/permanent state.
4. Config-nycklar läses uteslutande från YAML — ett test asserterar att inget av §7/§10/§11:s parametrar är hårdkodat i Python.
5. Ingen import mellan `crypto_trading/` och `intelligence/` i någon riktning — verifierat med en enkel importgranskning/test.
6. Ett test läser en `candidates`-rad med ett orepresenterat/korrupt `status`-värde och verifierar att repository-lagret kastar `CorruptCandidateStateError` och skriver ett `CORRUPT_STATE_DETECTED`-event — aldrig konstruerar ett `Candidate`-objekt med det värdet, aldrig en okontrollerad krasch, aldrig ett tyst godkännande eller ett steg vidare mot `CONFIRMED` (§8.3).
7. Idempotens-test på schema-/repository-nivå: samma candidate-identifierare (instrument + discovery-run-id + evidence-hash, §8.6) skapar aldrig två `Candidate`-rader.

---

## Phase 1 — BingX Market Data

**Omfattning:** officiellt verifierade publika endpoints, instrument universe, market-data-adapter, timestamp/staleness-kontroller, data-quality engine, retries/rate limits, komplett mock/testlager.

**Levererar:**
- Verifiering mot **aktuell** officiell BingX API-dokumentation innan kod skrivs — inga endpoint-format antas i förväg (SPEC §14).
- `connectors/bingx_market_data.py`: klines/OHLCV, ticker, volym, funding rate, open interest, instrumentmetadata, orderbok/spread om tillgängligt. **Endast market-data-endpoints.** Ingen kod som ens refererar till account-/order-endpoints.
- `connectors/base.py`: timeout, `tenacity`-retry, rate-limit, TTL-cache, strukturerad loggning, timestamp-validering.
- Data-quality-lager: implementerar §8.1:s deterministiska definitioner av `stale`/`ofullständig`/`inkonsekvent` exakt, med samtliga trösklar (`max_data_age_seconds` per datatyp, obligatoriska-fält-lista per steg, rimlighetsintervall/tolerans) läst från `config/pipeline.yaml` — inget hårdkodat.
- Fullständigt `respx`-mockat testlager — inga riktiga BingX-anrop i default `pytest`.

**Acceptance criteria:**
1. Ett test bevisar: saknad/stale/inkonsekvent kritisk data → connector/data-quality-lagret signalerar detta explicit (aldrig en tyst default eller ett gissat värde), och klassificeringen matchar exakt §8.1:s tre definitioner.
2. Retry/timeout/rate-limit-beteende testat med mockade HTTP-svar, inklusive BingX-nere-scenario (pipeline fortsätter inte gissa, instrument markeras `DATA_INVALID` nedströms i Phase 2).
3. Ingen kod-sökväg i hela `crypto_trading/` refererar till ett BingX-konto, orderendpoint eller broker-credential — verifierat med en explicit grep/test-assertion, inte bara manuell granskning.
4. Varje §8.1-tröskel har ett eget test där värdet ändras via config och beteendet ändras i takt — bevisar att inget är hårdkodat.
5. En (icke-CI, manuell) engångskörning mot riktig BingX-publik-data bekräftar att adaptern faktiskt fungerar mot den verkliga, aktuella API-ytan.

---

## Phase 2 — Universe + Quant Screening

**Omfattning:** eligibility/liquidity filter, dynamiskt Top N, pris/volatilitet, momentum/breakout, funding/OI, volymavvikelse, `CandidateEvidenceRecord`, candidate scoring, deduplication/cooldown.

**Levererar:**
- `screening/eligibility_filter.py`: likviditet, spread, datakomplethet, handelsstatus → deterministiskt urval.
- Dynamiskt Top N (default 30, konfigurerbart) från hela BingX USDT-perpetual-universumet — inget hårdkodat instrument.
- `screening/quant_screener.py`: alla fyra signaltyper (SPEC §4/§14) kombinerade i ett transparent scoring/threshold-system → `CandidateEvidenceRecord`. Reproducerbart: samma indata → samma candidate score, alltid.
- `screening/candidate_engine.py`: dedup/cooldown-logik (§7) och budget-baserad prioriteringsordning (§10), utan att själv göra AI-anrop.

**Acceptance criteria:**
1. Screenern uttalar sig aldrig om riktning — ett test asserterar att `CandidateEvidenceRecord`/dess konsumenter aldrig innehåller ett BUY/SELL/LONG/SHORT-fält på detta steg.
2. Determinism-test: samma rå marknadsdata given till screenern två gånger ger identisk `candidate_score` och identiska `trigger_reasons`.
3. Dedup/cooldown-test: en `REJECTED`-candidate återanalyseras inte inom cooldown-fönstret om inte evidensen förändrats över den konfigurerade tröskeln.
4. Top N är verifierat dynamiskt: ett test visar att ett instrument som byter likviditetsrank faktiskt kan gå in/ur universumet mellan körningar. Ett separat test bevisar att Top N-medlemskap i sig inte skapar en `Candidate`-rad eller något riktningsuttalande (SPEC §2) — bara Quant Screener-utfallet gör det.
5. Schema-test: `CandidateEvidenceRecord` har inget fält som kan tolkas som AI-confidence, forecast-sannolikhet eller trade-kvalitet (SPEC §4-tabellen) — `candidate_score` är typmässigt och namnmässigt oförväxlingsbar med de fälten som tillkommer i Phase 3.

---

## Phase 3 — AI Intelligence Pipeline

**Omfattning:** alla sju agentroller, typade outputs, `AgentRunner` Real/Mock, QA/Gate, den deterministiska Risk/Signal Gate, `CONFIRMED`/`NO_TRADE`-state machine.

**Levererar:**
- `agents/`: samma loader/roles/runner-mönster som Fas 1, sju rollfiler (News/Sentiment, Technical, Bull, Forecast, Risk, Bear, QA) enligt SPEC §6.
- `connectors/news_rss.py` och `connectors/external_data.py`: leverantör/endpoint väljs och dokumenteras här enligt kriterierna i SPEC §14 (kostnadsfri, verifierbar, källangiven) — inte förutbestämt av SPEC. Matar News/Sentiment Analyst.
- `gate/qa_gate.py` (AI, schema-komplethet/konsistens) och `gate/risk_signal_gate.py` (**deterministisk kod**, kan blockera `CONFIRMED` oavsett AI-utfall — implementerar §8.3 fullt ut).
- Fullständig `CandidateStatus`-övergång från `CANDIDATE`/`UNDER_AI_ANALYSIS` till `REJECTED`/`NO_TRADE`/`CONFIRMED`.

**Acceptance criteria (motsvarar Fas 1:s obligatoriska gate-tester, anpassade):**
1. En candidate utan `RiskAssessment` kan inte bli `CONFIRMED`.
2. En candidate utan `BearAdversarialAssessment` kan inte bli `CONFIRMED`.
3. En candidate där `QAAssessment.passed=False` kan inte bli `CONFIRMED`.
4. Ett test bevisar att Risk/Signal Gate kan blockera `CONFIRMED` **även när alla sju AI-assessments är positiva** — gaten är inte bara en formalitet, den har egna hårda regler (t.ex. max exponering redan nådd) som självständigt kan neka.
5. `REJECTED` → `CONFIRMED` är alltid `False` i `can_transition` (samma princip som Fas 1 §5).
6. Agent-timeout/fel → `status="failed"` på just den assessment-typen, blockerar `CONFIRMED`, kraschar aldrig hela discovery-loopen.
7. Mock-only default `pytest` — noll Claude API-anrop krävs för grön testsvit.

---

## Phase 4 — Paper Trading + Historical Replay

**Omfattning:** regelbaserad position sizing, fees/slippage/funding, LONG/SHORT, SL/TP/tidsgräns/invalidation, position monitoring, audit trail, **historisk replay**, look-ahead-bias-tester, konservativ candle/fill-hantering.

*Medvetet placerad före Phase 5 (Live Operation) — signal-, risk- och paper-trading-logiken ska vara verifierad mot historisk data innan systemet någonsin körs mot live-marknaden.*

**Levererar:**
- `paper_trading/position_sizing.py`: risk % av kapital / stop-avstånd → position size, mot `risk_limits.yaml` (§11).
- `paper_trading/execution.py`: beräknar och lagrar `theoretical_entry`/`theoretical_exit` **och** `simulated_fill_price` som separata fält (SPEC §11), inkl. spread/slippage-antagande, fees, funding — allt konfigurerbart.
- `paper_trading/monitoring.py`: SL/TP/tidsgräns/invalidation-logik inkl. gap-through-hantering (konservativt fill-pris vid pris-gap mellan övervakningsintervall, SPEC §11), redo att köras i den tätare loopen (Phase 5).
- `paper_trading/replay.py`: kör hela discovery→gate→paper-trading-kedjan mot historisk BingX-data.
- Explicita look-ahead-bias-tester (§8.4): en simulerad tidpunkt `t` får aldrig se data daterad efter `t` — testat direkt, inte bara antaget.
- Konservativ regel för candle-baserad fill-osäkerhet (t.ex. antag worst-case-ordning av high/low inom en candle när exakt tick-data saknas) — samma regel i replay och live monitoring.

**Acceptance criteria:**
1. Replay mot ett känt historiskt dataset ger reproducerbara, deterministiska resultat (samma indata → samma trades → samma PnL) vid upprepad körning.
2. Look-ahead-bias-test: manipulerad framtida data injicerad i test-fixturen har bevisligen noll effekt på ett beslut fattat vid tidpunkt `t`.
3. Fee/funding/slippage-beräkning verifierad mot handräknade exempel.
4. `theoretical_entry`/`theoretical_exit` skiljer sig alltid explicit från `simulated_fill_price` i lagrat data (aldrig samma fält återanvänt för båda) — ett test verifierar att skillnaden motsvarar det konfigurerade spread/slippage-antagandet.
5. Gap-through-test: en fixture där priset hoppar förbi SL/TP mellan två övervakningstillfällen ger ett fill-pris som är konservativt (aldrig exakt SL/TP-nivån), och `fill_model_version` är satt på tradet.
6. Idempotens: samma `CONFIRMED`-event processat två gånger (t.ex. efter en simulerad krasch och omkörning) skapar inte två positioner (§8.6).

---

## Phase 5 — Live Paper Operation

**Omfattning:** periodisk discovery loop, tätare position monitoring, state persistence, recovery efter restart, duplicate prevention, system-health monitoring.

**Levererar:**
- `discovery_loop.py` och `monitoring_loop.py` som faktiska körbara processer, intervall från `pipeline.yaml`.
- State persistence + recovery: en omstart mitt i pågående analys eller övervakning återupptar korrekt (byggt i Phase 0/4, integrationstestat här end-to-end).
- System-health-loggning: missade scans, agentfel/retries, pipeline-latency, antal agent calls — grunddata för Phase 7:s System Health-vy.

**Acceptance criteria:**
1. Ett integrationstest kör flera discovery-cykler + en pågående position-övervakning över simulerad tid, inklusive en simulerad process-krasch och omstart, utan dubbletter eller förlorat state (§8.5, §8.6).
2. Kostnadstak (§10) respekteras i en flercykel-körning: `BUDGET_LIMITED` uppstår korrekt när taket nås, prioriteringsordningen är verifierbart deterministisk.
3. Ett verkligt (icke-CI, manuellt loggat) körpass mot riktig BingX-data och riktiga Claude-anrop, under en begränsad tidsperiod och budget, bekräftar att hela kedjan fungerar i praktiken — samma typ av verifiering som Fas 1:s slutkörning mot riktig Hacker News-data.

---

## Phase 6 — Telegram

**Status: KÄRNAN KLAR OCH LIVE-VERIFIERAD (2026-08-29).** CONFIRMED- och CLOSED-notiser implementerade, TDD, och verifierade end-to-end mot en riktig Telegram-bot (två isolerade smoke-tester: en syntetisk CONFIRMED-candidate respektive en syntetisk CLOSED-position i en temporär SQLite-databas — aldrig produktions-DB:n, aldrig `crypto_trading.run`/någon loop startad, inga BingX-/Claude-/orderanrop). Idempotens (AC3) bekräftad både i automatiserade tester och i de riktiga smoke-testerna: en andra `run_notify_tick()`-körning mot samma data skickade noll nya meddelanden i båda fallen. Sex commits, `815e120`..`5791715`, pushade till `origin/main`.

**Explicit inte implementerat än (medvetet avgränsat, inte glömt):**
- **Daily report-notisen** (tredje notistypen i ursprungsomfattningen) — inte byggd.
- **Notisnivåerna `decisions`/`debug`** — `NotifyConfig.notification_level` finns i schemat men styr inget beteende ännu; `notify_loop.py` skickar alltid CONFIRMED/CLOSED oavsett konfigurerad nivå (dokumenterat i modulens egen docstring).
- **AC2 (NO_TRADE syns i loggen men notifieras aldrig)** — uppfylld bara genom att NO_TRADE aldrig hämtas alls än, inte via en dedikerad, testad logikgren.

**Omfattning (ursprunglig, delvis kvarstående):** CONFIRMED-, CLOSED- och daily report-notiser, notisnivåer (important/decisions/debug), samma eventmodell som dashboarden.

**Levererar:**
- `notify/telegram.py`, läser uteslutande från event-/audit-loggen (Phase 0) — genererar aldrig egen data.
- Fullständigt fältinnehåll per notistyp enligt SPEC §12.
- Konfigurerbar notisnivå.

**Acceptance criteria:**
1. **[Delvis]** Varje notistyp har ett test som verifierar samtliga obligatoriska fält (§12) är närvarande och korrekt formaterade. — Uppfyllt för CONFIRMED och CLOSED (`tests/crypto_trading/notify/test_telegram.py`), plus live-bekräftat mot en riktig bot. Daily report-notistypen finns inte, så AC1 är inte fullständigt uppfylld.
2. **[Uppfyllt, svagt]** `NO_TRADE` genererar ingen Telegram-notis på `important`-nivå (default), men syns i loggen. — Sant idag, men bara för att NO_TRADE aldrig hämtas av `notify_loop.py` över huvud taget, inte via ett dedikerat, testat undantag.
3. **[Uppfyllt]** Idempotens: samma event skickar aldrig dubbla Telegram-meddelanden vid omkörning/restart (§8.6). — Testat automatiserat och bekräftat i två riktiga Telegram-smoke-tester (CONFIRMED och CLOSED), 2026-08-29.
4. **[Uppfyllt]** Notis-innehåll och det som samtidigt skulle visas i dashboarden (Phase 7) härleds bevisligen från samma underliggande rad i event-loggen — inte två separata beräkningar. — `format_confirmed_message()`/`format_closed_message()` tar redan hämtade `Candidate`/`Position`-objekt, gör aldrig egna DB-frågor.

---

## Phase 7 — Dashboard

**Omfattning:** Live, Trade History, Performance, Forecast, System Health — read-only.

**Levererar:**
- `dashboard/api.py` (FastAPI, read-only) + enkel frontend.
- Fem vyer enligt SPEC §13, med tydlig lagerseparation i UI:t (raw data / screener evidence / AI-bedömningar / QA-resultat / deterministiska riskbeslut / paper trading-resultat).

**Acceptance criteria:**
1. Ingen endpoint i `dashboard/api.py` kan muterera trades, riskregler, signaler eller config — verifierat: samtliga routes är GET/read-only, inget skrivvägs-API existerar.
2. Varje vy har ett test som verifierar att den visar samma underliggande data som motsvarande Telegram-notis/loggrad för samma event (ingen avvikande "sanning").
3. System Health-vyn visar faktiska budget-/rate-limit-beslut från en testkörning (§10, §17).

---

## Phase 8 — Forecast Calibration + Evaluation

**Omfattning:** Brier score, calibration curves, sample sizes, forecast vs actual, performance-mått, nedbrytning per regim/timeframe/instrument där sample size tillåter.

**Levererar:**
- `calibration/brier_score.py`, `calibration/calibration_curve.py` mot ackumulerad `ForecastRecord`-historik (kräver data över tid — analogt med Fas 1:s medvetet uteskjutna Historical/Backtest-roll, men nu inbyggt från start eftersom kalibrering är en uttalad kärnprincip för detta system, se SPEC §9).
- Dashboard-vyn "FORECAST" (Phase 7) kopplas till verklig, ackumulerad kalibreringsdata.

**Acceptance criteria:**
1. Brier score och calibration curve beräknade mot en känd, handräknad fixture-dataset ger matematiskt korrekt resultat.
2. `CalibrationStatus`-test: ett dataset under `min_sample_size_for_calibration` ger `insufficient_data`, ett dataset strax över ger `preliminary`, ett dataset väl över ger `calibrated` — statusen och sample size visas alltid tillsammans, aldrig en siffra utan sin status (§9).
3. Nedbrytning per timeframe/riktning/regim fungerar när data finns, degraderar transparent (inte tyst, alltid med korrekt `CalibrationStatus`) när sample size är otillräcklig.

---

## Sammanfattning: vad som är hårt låst inför implementation

- Ingen `CONFIRMED` utan alla sju AI-assessments + QA-godkännande + deterministisk Risk/Signal Gate-godkännande (Phase 3).
- Ingen riktig order, inget broker-konto, inga broker-credentials — i någon fas (SPEC §1, §19; verifierat explicit i Phase 1 acceptance criterion 3).
- Historisk replay och look-ahead-bias-verifiering **före** live-drift (Phase 4 före Phase 5) — look-ahead-bias-förbudet gäller generellt, inte bara replay (SPEC §1 kärnprincip 4, §8.4).
- Fail-safe i alla riktningar: fel, schemafel, budgettak och okänt state → aldrig `CONFIRMED`, aldrig en gissning (SPEC §8.1–§8.7).
- `candidate_score`, AI-bedömningar, Forecast-sannolikheter och kalibreringsmått är strikt separerade fält som aldrig slås ihop eller förväxlas (SPEC §4, §9).
- Telegram och dashboard delar en enda sanningskälla (Phase 0:s event-logg), aldrig två separata beräkningar.
- `intelligence/` (Fas 1) förblir fryst och orörd genom hela detta arbete.
