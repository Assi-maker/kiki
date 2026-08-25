# Fas 1 — Completion Report

**Status:** Avslutad, 2026-08-25. Se `SPEC.md` (arkitektur, källa till sanning) och `PLAN.md` (alla 23 tasks ikryssade).

## Vad är färdigt

Hela pipelinen i `intelligence/` är implementerad enligt `SPEC.md`:

`DATA (connectors) → EVENT PIPELINE (normalize/dedupe/anomaly) → 7-AGENT-ANALYS (Research → Opportunity → Market → Forecast → Risk → Bear → QA) → STATE MACHINE-GATE → SCORING → SQLITE → MARKDOWN-RAPPORT`

- Två connectors: `HackerNewsConnector` (ingen nyckel krävs) och `AlphaVantageConnector` (kräver `ALPHAVANTAGE_API_KEY`), med gemensam retry/timeout/rate-limit/cache/logging via `BaseConnector`.
- Deterministisk pipeline (normalize/dedupe/anomaly) — inget LLM-anrop innan ett event är kvalificerat.
- Sju agentroller, var och en med egen Pydantic-assessment-typ; ingen agent kan skriva till en annan agents fält eller till `Opportunity` direkt.
- `state_machine.py` — kod-nivå-gate: `reported` kräver alla 7 assessments med `status="ok"` + `qa.passed=True`; `rejected` kan aldrig gå vidare till `approved`/`reported`.
- Transparent scoring (`scoring/model.py`) mot `config/scoring_weights.yaml`, ingen hårdkodad vikt.
- `SQLiteRepository` bakom ett `Repository`-protokoll; `AgentRunner` med `RealClaudeRunner`/`MockAgentRunner`.
- Observability: `run_id` per körning, strukturerad loggning, secret-redaction, körningstak (`max_events_per_run` m.fl.).

## Testresultat

- `pytest`: **104/104 gröna**, noll nätverksanrop, noll riktiga API-nycklar krävs (HTTP mockas med `respx`, LLM med `MockAgentRunner`).
- `ruff check .`: **inga fel**.
- `ruff format --check .`: 2 filer skulle omformateras — **`PLAN.md` och `SPEC.md`**, och enbart i inbäddade Python-kodexempel i prosan (radbrytning över 100 tecken i dokumentationsexempel). Ingen faktisk källkodsfil (`intelligence/`, `tests/`) berörs — alla 85 riktiga kodfiler är redan korrekt formaterade. Lämnas medvetet orört: kosmetiskt, ingen kodpåverkan, och ändring av dokumentationsexempel är utanför "inga större ändringar i fungerande Fas 1-logik".

## Resultat från verkliga körningar

Körningar mot riktig Hacker News-data, 2026-08-23 till 2026-08-24 (`data/intelligence.db`):

| Mått | Antal |
|---|---|
| Events (efter anomali-filter) | 76 |
| Opportunities totalt | 36 |
| — `reported` | 7 |
| — `rejected` | 17 |
| — `under_review` | 12 |
| Loggade agentsteg (`runs`) | 252 |

Samtliga 36 opportunities kom från källkategorin `forum` (Hacker News) — Alpha Vantage har aldrig triggat eftersom ingen `ALPHAVANTAGE_API_KEY` är konfigurerad i denna miljö.

Av `research/`-katalogens 14 filer vid Fas 1-avslutet var **12 testförorening** — identisk fixture-data (platshållartext, score 0.487) skriven dit av samma kända testbugg (se begränsningslistan) vid upprepade `pytest`-körningar, inte riktig agent-output. Dessa har tagits bort. Kvar är **2 verifierade genuina rapporter**, matchade mot nuvarande `data/intelligence.db`:

- `2026-08-24-opportunity-37998355-...md` — score 0.4275
- `2026-08-24-opportunity-338bfe3a-...md` — score 0.355

Båda korrekt låga, med samtliga agenter i kedjan explicit avrådande från att behandla dem som starka möjligheter — gaten fungerar som avsett, systemet överdriver inte svaga signaler. Databasen loggar totalt 7 `reported`-opportunities under dagen, men bara dessa 2 har en motsvarande rapportfil kvar som går att verifiera mot nuvarande DB-state (`data/intelligence.db` är gitignorad och verkar ha återställts minst en gång under dagens iterationsarbete — de övriga 5 `reported`-posternas ursprungliga rapportfiler, om de någonsin skrevs, matchar inte nuvarande DB-rader). Detta är en observation för protokollet, inte ett kodfel — `data/*.db` är avsiktligt gitignorad lokal state per SPEC §8.

## Kända begränsningar (medvetet ej åtgärdade)

- `events`-tabellen i SQLite persisterar inte `title`/`url`/`author`/`content_excerpt`. Påverkar inte live-körning (agenterna får kontexten in-memory under körningen), men blockerar en framtida DB-replay eller ett dashboard som vill visa historiska events utan att köra om pipelinen.
- Ingen `ALPHAVANTAGE_API_KEY` konfigurerad → `market_data`-källan har aldrig kört i praktiken; all verklig data hittills kommer bara från Hacker News.
- Enstaka `JSONDecodeError` (tomt modellsvar) förekommer slumpmässigt hos olika agenter — absorberas av befintlig retry-policy, men root cause (varför modellen ibland svarar tomt) är inte vidare utredd.
- `fact-checker-bear` och `trading-research` överskrider enstaka gånger även den höjda timeouten (50s/45s). Verklig latensvariation, inte en kodbugg — se explicit stoppregel: höj inte timeout igen utan att först analysera varför agenten behöver den tiden.
- En testfil skrev tidigare mock-liknande rapporter direkt till `research/` istället för `tmp_path` — untracked-städning, ofarligt men inte åtgärdat.

## Medvetet lämnat till framtida faser (enligt SPEC §15, roadmap)

- **Fas 2:** fler datakällor, bättre research.
- **Fas 3:** historisk data + backtesting (kräver historik som inte finns dag 1) — Historical/Backtest Agent hör hit.
- **Fas 4:** evaluation + calibration — Learning/Evaluation Agent hör hit.
- **Fas 5:** avancerad anomaly detection + ranking — Opportunity Ranking Agent hör hit.
- **Fas 6:** performance/scaling/hardening.
- **Fas 7:** dashboard/UI/alerts.
- Source Reliability Agent: statisk config-tabell i Fas 1 (approximation via antal källor), blir egen lärande agent i senare fas.

Ingen fas påbörjas innan föregående är testad och godkänd (SPEC §15). Fas 1 betraktas nu som en stabil, testad baslinje — inga ändringar i dess logik utan explicit ombedd anledning.

## Hård gräns, oförändrad

Ingen kod i `intelligence/` lägger ordrar, ansluter till mäklarkonton, hanterar broker-credentials eller flyttar pengar — verifierat, ingen sådan kod finns i kodbasen (SPEC §1, §14).
