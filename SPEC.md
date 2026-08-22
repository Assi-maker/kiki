# SPEC — Market Opportunity Intelligence System

Status: **Godkänd grundarkitektur, Fas 1 scope låst.** Detta dokument är källan till sanning för arkitekturbeslut. Ändringar här ska föregås av en konsekvensanalys (se `AGENTS`-processregel i botten).

## 1. Syfte och icke-mål

Systemet ska kontinuerligt bevaka marknads- och internetdata, upptäcka avvikelser, och driva dem genom en multi-agent-pipeline som producerar en strukturerad, källbelagd, riskbedömd **Opportunity**-rapport med ett transparent score.

**Det här är INTE:**
- Ett system som lovar att förutsäga marknaden.
- Ett tradingsystem. Ingen kod i detta projekt får lägga en order, ansluta till ett mäklarkonto, hantera broker-credentials eller flytta pengar — i någon fas.
- En "magisk AI-agent". Deterministisk kod gör allt som kan vara deterministiskt (anomali-detektion, dedup, state transitions, scoring-aritmetik). LLM används bara för semantisk analys: tolkning, hypotesgenerering, adversarial granskning, syntes.

**Kärnprincip:** ingen agent godkänner en möjlighet ensam, och ingen möjlighet kan rapporteras med ett obligatoriskt analyssteg saknat. Detta är en **kod-nivå-garanti** (state machine), inte en instruktion i en prompt.

## 2. Lagerarkitektur

```
DATA LAYER  →  EVENT PIPELINE  →  AGENT/REASONING LAYER  →  STORAGE  →  SCORING  →  DELIVERY
(connectors)   (deterministisk)   (LLM, strukturerad output)  (SQLite)  (transparent)  (markdown)
```

Beroenderiktning är strikt enkelriktad: `schemas` har inga beroenden på andra `intelligence`-moduler (ren datamodell). Allt annat beror på `schemas`, aldrig tvärtom. `orchestrator` beror på **interfaces** (`Repository`, `AgentRunner`), aldrig på konkreta implementationer (`SQLiteRepository`, `RealClaudeRunner`) — se §9 självgranskning punkt 1.

## 3. Filstruktur (Fas 1)

```
ClaudeProjects/
├── SPEC.md                          # detta dokument
├── .claude/agents/
│   ├── research-agent.md            # BEFINTLIG, återanvänds oförändrad
│   ├── opportunity-hunter.md        # BEFINTLIG, återanvänds oförändrad
│   ├── trading-research.md          # BEFINTLIG, återanvänds som grund för Market Analyst-rollen
│   ├── fact-checker-bear.md         # BEFINTLIG, återanvänds som Bear/Adversarial
│   ├── forecasting-agent.md         # NY
│   ├── risk-agent.md                # NY
│   └── qa-agent.md                  # NY
├── config/
│   └── scoring_weights.yaml         # NY — alla scoring-vikter, inget hårdkodat
├── data/                            # NY, gitignored — sqlite-fil hamnar här
├── intelligence/                    # NYTT Python-paket
│   ├── __init__.py
│   ├── config.py                    # Settings: env-vars, run-limits, sökvägar
│   ├── logging.py                   # run_id, strukturerad logg, secret-redaction
│   ├── connectors/
│   │   ├── __init__.py
│   │   ├── base.py                  # BaseConnector: fetch()+validate(), retry/timeout/rate-limit/cache/logging/dedup-stöd
│   │   ├── exceptions.py            # ConnectorError, ConnectorConfigError, ConnectorUnavailableError
│   │   ├── hackernews.py
│   │   └── alpha_vantage.py
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── normalize.py             # RawRecord → NormalizedRecord
│   │   ├── dedupe.py                 # hash-baserad dedup mot DB-historik
│   │   ├── anomaly.py                # rolling baseline, % change, threshold → Event
│   │   └── event_pipeline.py        # kör connectors→normalize→dedupe→anomaly per körning, feltolerant per källa
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── source.py                 # Source
│   │   ├── event.py                  # RawRecord, NormalizedRecord, Event
│   │   ├── assessments.py            # Research/Opportunity/Market/Forecast/Risk/Bear/QA-Assessment
│   │   └── opportunity.py            # Opportunity (aggregat) + OpportunityStatus enum
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── loader.py                  # läser .claude/agents/*.md → AgentDefinition
│   │   ├── roles.py                   # rollnamn → (agentfil, AssessmentType)
│   │   └── runner.py                  # AgentRunner (ABC), RealClaudeRunner, MockAgentRunner
│   ├── state_machine.py              # rena funktioner: tillåtna transitions, required_assessments_for(status)
│   ├── orchestrator.py                # Lead Orchestrator: kör pipelinen, kombinerar assessments, anropar state machine
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── db.py                      # sqlite3-anslutning + schema
│   │   └── repository.py              # Repository (Protocol) + SQLiteRepository
│   ├── scoring/
│   │   ├── __init__.py
│   │   └── model.py                   # komponent-scores + total, läser config/scoring_weights.yaml
│   ├── reporting/
│   │   ├── __init__.py
│   │   └── report.py                  # Opportunity → markdown, skriver till research/
│   └── run.py                         # entrypoint: en full pipeline-körning (Fas 1-demo)
├── pyproject.toml                     # ÄNDRAS: + pydantic, anthropic, httpx, tenacity, pyyaml, python-dotenv; dev: + respx
├── .env.example                       # ÄNDRAS: + ANTHROPIC_API_KEY
└── tests/intelligence/                # speglar paketstrukturen 1:1, se §8
```

Inga nya befintliga filer tas bort eller dupliceras. De fyra befintliga agentfilerna ändras inte i denna fas (de fungerar redan som fristående interaktiva subagenter och som Fas 1:s Python-återanvända roller utan modifiering).

## 4. Pydantic-scheman

**Genomgående regel:** varje assessment-typ separerar `observed_data`, `verified_facts`, `source_references`, `assumptions`, `hypothesis`, `interpretation`, `forecast`, `confidence`, `uncertainty` — fälten som inte är relevanta för en viss roll utelämnas/är tomma, men fälten byter aldrig betydelse per roll. En LLM-agent returnerar **bara sin egen** assessment-typ; den ser aldrig och kan aldrig skriva till en annan agents fält eller till `Opportunity` direkt.

- **Source** — `source_id`, `name`, `type` (market_data/forum/news/...), `reliability_score` (initialt statisk config-tabell i Fas 1, se §1 i tidigare arkitektur-diskussion), `url`.
- **RawRecord** — `source_id`, `fetched_at`, `payload` (rå, oförändrad).
- **NormalizedRecord** — `source_id`, `observed_at`, typade fält (t.ex. `metric`, `value`), `raw_ref`.
- **Event** — `event_id`, `source_id`, `observed_at`, `category`, `metric`, `baseline`, `deviation`, `description`, `raw_ref`.
- **ResearchAssessment** — verified_facts, source_references, assumptions.
- **OpportunityAssessment** (Opportunity Hunter) — hypothesis, observed_data, interpretation.
- **MarketAssessment** (Market Analyst) — market-strukturdata (pris/volym/volatilitet/momentum), interpretation.
- **ForecastAssessment** — scenarier med sannolikheter, confidence, uncertainty — aldrig presenterat som säkert.
- **RiskAssessment** — downside, liquidity/model/information/timing-risk.
- **BearAssessment** — motargument, alternativa förklaringar, falsifieringsvillkor.
- **QAAssessment** — `passed: bool`, `violations: list[str]` — kontrollerar schema-komplethet och intern motsägelse, inte sakinnehåll.
- Varje assessment har dessutom: `agent_name`, `run_id`, `created_at`, `status` (`ok` / `failed` / `timeout`).
- **Opportunity** — aggregatet. Innehåller identitet/metadata (id, timestamp, category, title, summary, time_horizon, liquidity), de sju assessments (var och en `Optional`, `None` tills ifylld), score-nedbrytning, och `status: OpportunityStatus`.
- **OpportunityStatus** — `candidate → under_review → (approved | rejected) → reported → evaluated`. `evaluated` är förberedd för Fas 4, används inte i Fas 1.

## 5. State machine (obligatorisk gate)

Implementeras i `intelligence/state_machine.py` som rena funktioner, oberoende av agent-kod — testbar helt isolerat.

```
REQUIRED_FOR_REPORTED = {research, opportunity, market, forecast, risk, bear, qa}
```

`can_transition(opportunity, target) -> (bool, reason)`:
- `→ reported` kräver: alla sju assessments närvarande OCH samtliga har `status == "ok"` OCH `qa.passed is True` OCH `bear` har körts (bear behöver inte "godkänna" — bear ska motbevisa, dess *närvaro* är kravet, inte ett positivt utfall) OCH ingen assessment har `status == "failed"`.
- `→ approved` samma krav som ovan minus rapport-specifika fält.
- `→ rejected` kan nås från vilket steg som helst om QA underkänner eller Bear/Risk hittar en diskvalificerande brist. En `rejected` opportunity kan aldrig därefter transitionera till `approved` eller `reported` (transition-tabellen exkluderar den kanten explicit — testas, se §8).
- Saknad/failed assessment → transition nekas med en tydlig `reason`-sträng, aldrig ett tyst fel.

Orchestratorn anropar `can_transition` innan varje statusändring och litar aldrig på egen bedömning av "är det klart nu".

## 6. Connector-interface

`BaseConnector` (ABC) i `connectors/base.py`:

```python
class BaseConnector(ABC):
    source: Source

    @abstractmethod
    def fetch(self) -> list[RawRecord]: ...
    def validate(self, records: list[RawRecord]) -> list[RawRecord]: ...  # strukturell validering, ej affärslogik
```

Ansvarsgräns: connectorn hämtar och strukturellt validerar (rätt fält finns, rätt typ) — den normaliserar INTE till domänfält och gör INGEN anomali-/eventdetektion; det är pipeline-lagrets jobb (`pipeline/normalize.py`, `pipeline/anomaly.py`). Detta håller connectors utbytbara utan att pipeline-logik måste replikeras per källa.

Inbyggt i basklassen (delas av alla connectors, inte omimplementerat per källa):
- **Timeout** — per request, konfigurerbar.
- **Retry** — `tenacity`, exponential backoff, max-försök från config.
- **Rate limiting** — enkel token-bucket eller min-interval mellan anrop, per connector-instans.
- **Caching** — enkel TTL-cache (fil eller in-memory) för att undvika onödiga upprepade anrop inom samma körning.
- **Logging** — via `intelligence/logging.py`, aldrig secrets.
- **Dedup-stöd** — connectorn taggar varje `RawRecord` med ett innehålls-hash; själva dedup-beslutet (mot DB-historik) görs i `pipeline/dedupe.py`.

**HackerNewsConnector** — inget API-nyckel krävs. Ren `fetch()`/`validate()`, ingen affärslogik.

**AlphaVantageConnector** — läser `ALPHAVANTAGE_API_KEY` via `config.py`. Om nyckeln saknas: `fetch()` kastar `ConnectorConfigError` **direkt vid instansiering/första anrop**, med tydligt meddelande — fångas av `event_pipeline.py` och loggas som "source unavailable: config missing", pipelinen fortsätter med övriga källor. Krascha aldrig processen.

## 7. AgentRunner-abstraktion

```python
class AgentRunner(ABC):
    def run(self, agent_def: AgentDefinition, context: dict, output_schema: type[T]) -> T: ...
```

- **RealClaudeRunner** — anropar `anthropic`-SDK:t med `agent_def.system_prompt` (laddad från `.claude/agents/*.md` via `agents/loader.py`) + strukturerad-output-styrning (tool-use/JSON-schema matchande Pydantic-modellen), validerar svaret mot `output_schema` innan retur. Ogiltig output → retry enligt policy → därefter assessment med `status="failed"`, aldrig en gissad/patchad struktur.
- **MockAgentRunner** — returnerar deterministiska fixture-assessments per roll, konfigurerbara per test (t.ex. "simulera QA-fail", "simulera timeout"). Används i alla tester utom en explicit märkt `@pytest.mark.live`-delmängd som kräver riktiga nycklar och exkluderas från default `pytest`-körning.
- Samma `agent_def` (samma `.md`-fil) används av `RealClaudeRunner` oavsett om anropet triggas interaktivt i en Claude Code-session eller programmatiskt av `orchestrator.py` — en källa till sanning för varje rolls instruktioner (`agents/loader.py`).

## 8. Storage

`Repository` som `typing.Protocol` i `storage/repository.py` — orchestrator, scoring och reporting beror bara på detta interface. `SQLiteRepository` är enda implementationen i Fas 1. Tabeller: `sources`, `events`, `opportunities`, `assessments`, `scores`, `runs` (observability-logg, se §10). Inget ORM — `sqlite3` + explicit SQL, minimal yta att testa. En framtida `PostgresRepository` implementerar samma `Protocol` utan att `orchestrator.py` ändras.

## 9. Scoring

`scoring/model.py`, rena funktioner. Komponenter: `signal_strength`, `data_quality`, `source_reliability`, `potential`, `risk`, `confidence`, `novelty`. Varje komponent beräknas från specifika assessment-fält (dokumenteras som kommentar per funktion i koden, inte gissningar). `total_score = Σ(weight_i × component_i)`, vikterna läses från `config/scoring_weights.yaml` — aldrig hårdkodade i agent-prompts eller i Python. Score-nedbrytningen sparas i sin helhet på `Opportunity`, så en rapport alltid kan visa *varför* totalen blev vad den blev.

## 10. Observability och kostnadskontroll

- Varje körning av `run.py` får ett `run_id` (uuid4).
- Loggas strukturerat (JSON-rader) för varje agentsteg: `run_id`, `event_id`, `opportunity_id`, `agent_name`, `started_at`, `completed_at`, `status`, `errors`, `latency_ms`.
- `intelligence/logging.py` har en explicit `redact()` som körs på alla loggade dict:ar/strängar innan skrivning, matchar kända secret-env-namn (`*_API_KEY`, `*_TOKEN`, `*_SECRET`). Testas: en unit test asserterar att ingen loggrad innehåller värdet av en satt testnyckel.
- **Kostnadskontroll i `config.py`**: `max_events_per_run`, `max_opportunities_per_run`, `max_agent_calls_per_run`, `agent_timeout_seconds`. Event-pipelinen filtrerar (anomali-tröskel) **innan** någon agent anropas — de flesta events kostar noll LLM-anrop. Orchestratorn stoppar och loggar tydligt om ett tak nås mitt i en körning, avslutar körningen snyggt istället för att fortsätta okontrollerat.

## 11. Feltolerans

- **Datakälla nere** (t.ex. Hacker News timeout) → `event_pipeline.py` fångar `ConnectorError`, loggar `source unavailable`, fortsätter med övriga källor. Ingen exception läcker till `run.py`.
- **Alpha Vantage saknar nyckel** → samma mönster, specifikt `ConnectorConfigError`.
- **Agent timeout** → `RealClaudeRunner` retry:ar enligt policy (från config), därefter `status="failed"` på just den assessment-typen. State machine blockerar `reported` per §5 — opportunity:n stannar i `under_review` och syns i rapportering som ofullständig, aldrig som falskt godkänd.

## 12. Fas 1 — agentroller

Sju roller, minimala men strukturerat producerande enligt sina scheman i §4:

| Roll | Källa | Status |
|---|---|---|
| Data/Research Agent | `.claude/agents/research-agent.md` | Befintlig, återanvänd |
| Opportunity Hunter | `.claude/agents/opportunity-hunter.md` | Befintlig, återanvänd |
| Market Analyst | `.claude/agents/trading-research.md` | Befintlig, återanvänd |
| Forecasting Agent | `.claude/agents/forecasting-agent.md` | Ny |
| Risk Agent | `.claude/agents/risk-agent.md` | Ny |
| Bear/Adversarial | `.claude/agents/fact-checker-bear.md` | Befintlig, återanvänd |
| QA/Fact Checker | `.claude/agents/qa-agent.md` | Ny |

**Explicit uteslutet från Fas 1** (bygger på stub som ärligt svarar "otillräcklig data", enligt din egen fasplan):
- Source Reliability Agent → statisk config-tabell i Fas 1, blir egen lärande agent i senare fas.
- Historical/Backtest Agent → Fas 3 (kräver historik som inte finns dag 1).
- Opportunity Ranking Agent → Fas 5.
- Learning/Evaluation Agent → Fas 4.

Pipeline-ordning: `DATA → EVENT → RESEARCH → OPPORTUNITY → MARKET ANALYSIS → FORECAST → RISK → BEAR → QA → SCORE → REPORT`, exekverad av `orchestrator.py`, gated av `state_machine.py`.

## 13. Teststrategi

`tests/intelligence/` speglar paketet 1:1. Ingen test i default `pytest`-körning kräver nätverk eller riktiga API-nycklar (HTTP mockas med `respx`, LLM mockas med `MockAgentRunner`). Explicit `@pytest.mark.live`-tester finns separat, exkluderade från default-run, för valfri manuell verifiering mot riktiga API:er.

**Obligatoriska gate-tester** (bevisar §5 med kod, inte bara läsning av koden):
1. Opportunity utan `RiskAssessment` kan inte bli `reported`.
2. Opportunity utan `BearAssessment` kan inte bli `reported`.
3. Opportunity utan `QAAssessment.passed=True` kan inte bli `reported`.
4. `rejected` → `approved`/`reported` är alltid `False` i `can_transition`.
5. Saknad agent-output (agenten kastar/timeoutar) resulterar i `status="failed"` på den assessment:en, inte en krasch och inte en tyst default.
6. Ogiltig Pydantic-output från en agent stoppas vid valideringssteget i `AgentRunner.run`, propagerar som `status="failed"`.
7. Duplicerade events (samma content-hash) skapar inte duplicerade opportunities — testas i `pipeline/dedupe.py`.

**Övrigt:**
- Connector-tester: retry/timeout/rate-limit/cache-beteende med mockade HTTP-svar; Alpha Vantage saknad nyckel → `ConnectorConfigError`, inte krasch.
- Anomaly-tester: threshold, rolling baseline, % change mot kända fixtures.
- Scoring-tester: vikter läses korrekt från YAML, total räknas rätt, nedbrytning är fullständig.
- Repository-tester: mot en temporär SQLite-fil.
- **End-to-end-test** (`tests/intelligence/test_end_to_end.py`): mockad data → normalize → event → opportunity → sju mockade agent-assessments → score → sqlite (tmp-fil) → markdown-rapport genereras och innehåller de obligatoriska rubrikerna. Detta är Fas 1:s "fungerar det verkligen ihop"-bevis.

## 14. Säkerhet

- Ingen kod i detta system lägger ordrar, ansluter till mäklarkonton, hanterar broker-credentials eller flyttar pengar — i någon fas. Detta är en hård gräns, inte en konfigurationsflagga.
- Alla secrets via `.env` (gitignored, redan etablerat mönster i projektet), `.env.example` uppdateras med platshållare utan riktiga värden.
- Loggar redigeras (§10) innan skrivning.
- Tradingrelaterade rapporter markeras alltid "Ej finansiell rådgivning" i markdown-templaten.

## 15. Roadmap (oförändrad, för sammanhang)

Fas 1 (detta dokument) → Fas 2: fler datakällor, bättre research → Fas 3: historisk data + backtesting → Fas 4: evaluation + calibration → Fas 5: avancerad anomaly detection + ranking → Fas 6: performance/scaling/hardening → Fas 7: dashboard/UI/alerts. Ingen fas påbörjas innan föregående är testad och godkänd.

## 16. Självgranskning

| Fråga | Svar |
|---|---|
| Cirkulär dependency? | Nej. `schemas` är beroendefritt inom paketet; allt annat beror på `schemas`. `orchestrator` beror på `Repository`/`AgentRunner`-**interfaces**, aldrig konkreta implementationer. Enkelriktad graf, verifierbar med en enkel importgranskning. |
| Agent med för mycket ansvar? | Nej. Lead Orchestrator är deterministisk Python, inte en LLM-persona — den kombinerar men genererar aldrig innehåll, så den kan per definition inte "hitta på data" (din regel för roll 1). Varje LLM-roll ser bara sitt eget scopade context och returnerar bara sin egen assessment-typ. |
| Kan LLM skriva över verifierade fakta? | Nej. Agenter tar emot och returnerar aldrig hela `Opportunity`-objektet — bara sin egen typade assessment. `verified_facts` sätts enbart av Research-steget; nedströms-agenter får det read-only i sitt context men har inget schema-fält som kan skriva till det. |
| Kan en opportunity rapporteras utan obligatoriska assessments? | Nej, `state_machine.can_transition` kontrollerar detta explicit och testas isolerat (§13, punkt 1–4). |
| Kan ett API-fel krascha hela pipelinen? | Nej, se §11 — varje connector- och agentanrop är inkapslat, fel loggas och körningen fortsätter med det som är tillgängligt. |
| Kan secrets hamna i git eller loggar? | Nej — `.env` gitignored (befintligt mönster), `redact()` körs på all loggning, testas explicit. |
| Kan tester köras utan riktiga API-nycklar? | Ja — default `pytest` använder `MockAgentRunner` + `respx`-mockad HTTP uteslutande; riktiga anrop är opt-in via `@pytest.mark.live`. |
| Kan SQLite senare ersättas med PostgreSQL? | Ja — `Repository` är ett `Protocol`; en `PostgresRepository` som implementerar samma interface kräver noll ändringar i `orchestrator.py`. |
| Kan nya datakällor läggas till utan att orchestratorn byggs om? | Ja — `event_pipeline.py` itererar över en konfigurerad lista av `BaseConnector`-instanser utan att känna till konkret typ; en ny källa är en ny connector-klass + config-rad. |

Ingen öppen inkonsekvens identifierad. Redo för `writing-plans`-steget (detaljerad, filspecifik steg-för-steg-plan).
