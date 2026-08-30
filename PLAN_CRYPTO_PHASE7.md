# Crypto Trading — Phase 7 (Dashboard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** En lokal, strikt read-only FastAPI-dashboard (`crypto_trading/dashboard/`) med fem vyer (LIVE, TRADE HISTORY, PERFORMANCE, FORECAST, SYSTEM HEALTH) som läser **uteslutande** redan persisterad data via `Repository` — samma sanningskälla som `notify_loop.py`/`notify/telegram.py` redan använder. Ingen ny affärslogik, ingen ny parallell beräkningsmotor, ingen extern BingX/Claude/Telegram-kommunikation från dashboardkoden.

**Architecture:** Ett nytt, isolerat paket `crypto_trading/dashboard/` (app-factory `create_app(repo, settings)` i `api.py`) + en enkel statisk frontend (`index.html` + vanilla JS mot JSON-endpoints). Fem `GET`-only routers, en per vy. Sex nya read-only metoder på `Repository`-protokollet (ren tillägg, ingen befintlig signatur ändras). En ny `DashboardConfig` i `Settings`. `run.py` får en fjärde, valfri tråd (samma "egen Repository/sqlite3-anslutning per tråd"-mönster som redan används för discovery/monitoring/notify), gated bakom en explicit env-var (samma opt-in-princip som Telegram, se Task 8) — dashboard-frånvaro ändrar noll i befintligt beteende.

**Tech Stack:** `fastapi` (ny), `uvicorn` (ny, endast för att faktiskt binda ett socket i `run.py` — testerna använder `fastapi.testclient.TestClient`, som redan får sitt HTTP-lager gratis av den befintliga `httpx`-dependencyn, ingen ny testdependency). Inget annat nytt.

**Spec:** `SPEC_CRYPTO.md` §13 (Dashboard, vy-innehåll), §12 (Telegram — delad sanningskälla-principen dashboarden ärver), §16 (Storage), §17 (secrets/observability), §19 (säkerhet — inga broker-anrop). `PLAN_CRYPTO.md` §Phase 7 (Levererar/AC1–AC3). Denna plans exakta scope är låst av användarens meddelande 2026-08-30 (citerat i sin helhet i §5 nedan) — det meddelandet är den bindande specen för vad som byggs i denna fas, inte SPEC §13 i sin fulla bredd.

## Global Constraints

- **Strikt read-only:** ingen route i `dashboard/api.py` är POST/PUT/PATCH/DELETE. Verifierat mekaniskt (AC1), inte bara manuell granskning.
- **Ingen ny affärslogik/parallell beräkningsmotor:** varje siffra i dashboarden kommer antingen direkt från en `Repository`-läsning, eller från en redan existerande config-modell (`Settings`). Enda tillåtna aritmetik i dashboardkoden är trivial visningsformatering av redan hämtade tal (t.ex. `started_at`/`completed_at` → varaktighet) — aldrig ett beslut, aldrig en ny tröskel, aldrig en gissning.
- **Saknad data visas som `unavailable`/`not_available_yet`, aldrig rekonstrueras:** historiskt Top N, MFE/MAE, unrealized PnL (inget levande pris persisteras — se §3), Brier score, calibration curve, och samtliga performance-mått (win rate/expectancy/drawdown/profit factor) tills en central beräkningskälla finns (Fas 8).
- **Ingen extern kommunikation:** `dashboard/`-paketet importerar aldrig `httpx`, `anthropic`, `crypto_trading.connectors.*`, `crypto_trading.notify.telegram`, eller `crypto_trading.agents.runner` — verifierat mekaniskt (Task 3), samma AST-import-scanning-mönster som `tests/crypto_trading/test_no_intelligence_coupling.py`.
- **Default bind `127.0.0.1`.** Verifierat mekaniskt.
- **Ingen schemamigrering.** Sex nya `Repository`-metoder är rena `SELECT`:ar mot befintligt schema.
- **`intelligence/` rörs inte.** `ruff` line-length 100, regler `E,F,I,UP,B`.
- **Filer som INTE ändras** (om inget AC uttryckligen kräver det — inget gör det i denna plan): `intelligence/`, `notify/`, `notify_loop.py`, `agents/`, `gate/`, `screening/`, `state_machine.py`, `paper_trading/`, `discovery_loop.py`, `monitoring_loop.py`, `storage/db.py` (schema).

---

## 1. Exakt vilka filer som berörs

**Skapas:**
- `crypto_trading/dashboard/__init__.py`
- `crypto_trading/dashboard/api.py` — app-factory, fem routers.
- `crypto_trading/dashboard/frontend/index.html` — enkel statisk sida, vanilla JS `fetch()` mot `/api/*`.
- `crypto_trading/config/dashboard.yaml`
- `tests/crypto_trading/dashboard/__init__.py`
- `tests/crypto_trading/dashboard/test_dashboard_security.py` (AC1, säkerhet, read-only, default host)
- `tests/crypto_trading/dashboard/test_dashboard_live.py`
- `tests/crypto_trading/dashboard/test_dashboard_trade_history.py`
- `tests/crypto_trading/dashboard/test_dashboard_system_health.py`
- `tests/crypto_trading/dashboard/test_dashboard_forecast.py`
- `tests/crypto_trading/dashboard/test_dashboard_performance.py`
- `tests/crypto_trading/dashboard/test_dashboard_frontend.py`
- `tests/crypto_trading/storage/test_repository_dashboard_reads.py`

**Ändras:**
- `pyproject.toml` — `fastapi`, `uvicorn` i `[project].dependencies`.
- `crypto_trading/config/loader.py` — ny `DashboardConfig`, nytt fält på `Settings`.
- `crypto_trading/storage/repository.py` — sex nya read-only metoder (protokoll + `SQLiteRepository`).
- `crypto_trading/run.py` — valfri fjärde tråd, gated bakom `CRYPTO_TRADING_DASHBOARD_ENABLED`.
- `tests/crypto_trading/config/test_loader.py` — nya tester för `DashboardConfig`.

**Rörs INTE:** samtliga filer listade i Global Constraints ovan. `test_no_intelligence_coupling.py` kräver ingen ändring — dess `rglob("*.py")`-baserade tester täcker automatiskt de nya `dashboard/`-filerna.

## 2. Befintliga kontrakt som berörs

| Kontrakt | Idag | Efter Fas 7 | Bakåtkompatibelt? |
|---|---|---|---|
| `Repository`-protokollet | 24 metoder (Fas 0–6) | + 6 nya (se Task 2) | Ja — rent tillägg |
| `Settings` (Pydantic) | 5 obligatoriska fält | + 1 nytt obligatoriskt fält `dashboard: DashboardConfig` | Nej för `Settings(...)`-konstruktion utan `dashboard=` — men `get_settings()` (den enda produktionsanropspunkten) läser alltid `config/dashboard.yaml`, som skapas i Task 1 samtidigt som fältet läggs till, så `get_settings()` fortsätter fungera oförändrat. Test-hjälpare som konstruerar `Settings(...)` manuellt (grep innan Task 1, Step 0) måste uppdateras — se Task 1 Step 0. |
| `run.py::main()` | 3 trådar (discovery, monitoring, ev. notify) | + 1 valfri fjärde tråd | Ja — dashboard-tråden startar bara om `CRYPTO_TRADING_DASHBOARD_ENABLED` är satt, exakt samma opt-in-princip som redan gäller Telegram (`build_notifier_from_env()`) |

**Inget SPEC-, arkitektur- eller testkonflikt identifierad.** `Settings`s nya obligatoriska fält är den enda tekniskt brytande ändringen, mitigerad enligt tabellen ovan (Task 1, Step 0: grep efter alla manuella `Settings(...)`-konstruktioner i testsviten innan ändringen görs, samma disciplin som Fas 5.5 §2 tillämpade för `_build_context`).

## 3. Verifierade datagap (grund för §-scope nedan, redan bekräftat under READ-ONLY-inventeringen)

- **Top N persisteras aldrig.** `market_snapshot.py::build_live_snapshot()` beräknar `top_n_symbols` helt i minnet (rad 95) och skriver det aldrig till DB — bara `instruments_scanned` (en count) persisteras via `complete_run()`. LIVE-vyn visar alltså `instruments_scanned` (talet), aldrig en lista över vilka instrument som var Top N — och absolut ingen **historisk** Top N-vy.
- **Ingen levande pris persisteras för öppna positioner.** `monitoring_loop.py::run_monitoring_tick()` hämtar `ticker`/`kline`/`funding` färskt varje tick och skriver dem aldrig till `positions` eller `events` — bara vid en faktisk stängning skrivs `theoretical_exit`/`simulated_fill_exit`. **Unrealized PnL kan alltså inte visas** utan antingen ett nytt live BingX-anrop (förbjudet i dashboardkoden) eller en gissning (förbjudet). LIVE-vyn visar öppna positioners entry/stop/target, markerar `unrealized_pnl: "unavailable"` explicit.
- **MFE/MAE finns varken i `Position`-schemat, `positions`-tabellen, eller beräknas någonstans** (`grep -r "mfe\|mae"` gav bara en kommentarsträng i `notify_loop.py`). Bekräftat i förra inventeringen — TRADE HISTORY visar `mfe: null, mae: null` med en explicit `"not yet tracked"`-markering, ingen migrering i denna fas.
- **Brier score/calibration curve beräknas ingenstans** — `ForecastRecord.actual_outcome`/`outcome_timestamp` är alltid `None` i praktiken (ingen kod fyller i dem, bekräftat: Fas 8-jobb enligt PLAN_CRYPTO.md). FORECAST-vyn visar `scenario_probabilities`/`forecast_version`/`actual_outcome` rakt av, markerar `calibration: "not_available_yet — Phase 8"`.
- **Ingen central performance-beräkningskälla existerar.** Varken `win_rate`, `expectancy`, `drawdown`, `profit_factor` eller `cumulative_pnl` beräknas i någon fil idag (`notify_loop.py`s daily report skickar uttryckligen bara operativa räknetal, inte performance-mått — dokumenterat medvetet uppskjutet till Fas 8 i dess egen docstring). PERFORMANCE-vyn returnerar `{"status": "not_available_yet", "reason": "..."}`, inga siffror alls.
- **Rate-limit-beslut persisteras inte.** `connectors/base.py::_rate_limit()` är ett rent lokalt `time.sleep()`-throttle mot config, skriver aldrig ett event. **Explicit INTE åtgärdat i denna plan** (se Global Constraints/användarens instruktion: kräver ett separat stopp-och-rapportera-steg innan connector-ändring). SYSTEM HEALTH visar `rate_limit_events: "unavailable — throttle decisions not persisted (known gap)"`.

## 4. Repository — sex nya read-only metoder (Task 2)

```python
def find_all_candidates(self, limit: int, offset: int = 0) -> list[Candidate]: ...
def find_all_positions(self, limit: int, offset: int = 0) -> list[Position]: ...
def get_gate_decision(self, candidate_id: str) -> dict | None: ...
def find_latest_run(self, run_type: str) -> dict | None: ...
def find_recent_runs(self, limit: int, offset: int = 0) -> list[dict]: ...
def find_all_forecasts(self, limit: int, offset: int = 0) -> list[ForecastRecord]: ...
```

Alla `ORDER BY <tidsfält> DESC LIMIT ? OFFSET ?`. `find_all_candidates`/`find_all_positions` återanvänder exakt samma deserialiseringshjälpare som redan finns (`get_candidate()` per rad — samma `CorruptCandidateStateError`-hoppa-över-princip som `find_candidates_by_status()`; `_row_to_position()` för positions).

## 5. Låst scope per vy (ordagrant från användarens godkännande 2026-08-30)

Se användarens meddelande i konversationen för den fullständiga, bindande scope-texten per vy (LIVE/TRADE HISTORY/PERFORMANCE/FORECAST/SYSTEM HEALTH, Repository, Run/konfiguration, TDD/AC). Denna plan implementerar den texten ordagrant — sammanfattad i tabellform:

| Vy | Visar | Visar EXPLICIT som unavailable |
|---|---|---|
| LIVE | senaste discovery-run/scan-status, instruments_scanned, aktuella (icke-terminala) candidates, gate-resultat, AI-assessment-status, CONFIRMED/NO_TRADE, öppna positioner (entry/stop/target), räknad exponering (`sum_open_positions_notional`/`count_open_positions` vs `risk_limits`-config) | Top N (historiskt), unrealized PnL |
| TRADE HISTORY | alla candidates (paginerat), status+beslut, gate decisions, alla positions inkl. stängda, entry/exit, PnL (`paper_trading.execution.compute_pnl`, **samma funktion Telegram redan använder** — se Task 4, inte en ny implementation), fees, funding, exit_reason, fill_model_version | MFE/MAE |
| PERFORMANCE | — | ALLT (`not_available_yet`) |
| FORECAST | scenario_probabilities, forecast_version, actual_outcome | Brier score, calibration curve |
| SYSTEM HEALTH | run-status, errors, instruments_scanned, AI calls (`count_ai_calls_since`), BUDGET_LIMITED (`count_candidates_by_status_since`), misslyckade runs, run-timing (started_at/completed_at) | rate-limit-events (känt gap) |

**PnL-beräkning i TRADE HISTORY är den enda platsen där dashboarden "räknar" något utöver ren visning** — och det är uttryckligen INTE en ny beräkningsmotor: `paper_trading/execution.py::compute_pnl()` är redan den enda, centrala PnL-funktionen (Fas 6 använder den rakt av i `notify/telegram.py::format_closed_message()`, se rad 86 i den filen). Dashboarden importerar och anropar samma funktion, skriver aldrig en egen formel. Detta är alltså INTE ett brott mot "ingen duplicerad affärslogik" — det är exakt motsatsen: återanvändning av den redan existerande, enda sanningskällan.

## 6. Teststrategi / TDD

Samma disciplin som tidigare faser: test-först, RED verifierad, minimal implementation, GREEN verifierad, regression + `ruff` efter varje task. `fastapi.testclient.TestClient` (synkron, ingen riktig socket, ingen `live`-markering behövs) för samtliga endpoint-tester. AC2-testerna (samma data som Telegram) bygger candidate/position-fixtures i exakt samma stil som `tests/crypto_trading/notify/test_telegram.py` (samma fält, samma hjälpfunktionsmönster `_evidence()`/`_confirmed_candidate()`), sparar dem via en riktig `SQLiteRepository` mot en temp-DB, anropar sedan **både** `format_confirmed_message()`/`format_closed_message()` (Fas 6) **och** dashboard-endpointen på samma rad, och jämför de underliggande fältvärdena — inte bara att båda "lyckas".

---

## Task 1: Dependencies + `DashboardConfig`

**Files:**
- Modify: `pyproject.toml`
- Modify: `crypto_trading/config/loader.py`
- Create: `crypto_trading/config/dashboard.yaml`
- Modify: `tests/crypto_trading/config/test_loader.py`

**Interfaces:** `class DashboardConfig(BaseModel): host: str; port: int`. `Settings.dashboard: DashboardConfig` (nytt, obligatoriskt fält — motiverat i §2 ovan).

- [x] **Step 0 (verifiering, inget kodsteg):** `grep -rn "Settings(" tests/crypto_trading/ crypto_trading/` — tre träffar: `test_replay.py`, `test_orchestrator.py`, `test_market_snapshot.py`, samtliga en lokal `_settings()`-hjälpfunktion. Uppdaterade i Step 3.
- [x] **Step 1: Write the failing tests** — `test_get_settings_loads_dashboard_config_with_localhost_default`, `test_dashboard_config_rejects_invalid_port`, `test_dashboard_config_rejects_port_above_65535` i `test_loader.py`.
- [x] **Step 2: Run tests to verify they fail** — `ImportError: cannot import name 'DashboardConfig'`.
- [x] **Step 3: Implement** — `dashboard.yaml` skapad, `DashboardConfig` + `Settings.dashboard`-fält i `loader.py`, `fastapi`/`uvicorn` i `pyproject.toml`, samtliga tre `_settings()`-hjälpare uppdaterade med `dashboard=DashboardConfig(host="127.0.0.1", port=8000)`.
- [x] **Step 4: `uv sync`** — fastapi 0.141.1, uvicorn 0.52.4, starlette, click, annotated-doc installerade.
- [x] **Step 5: Run tests to verify they pass** — 21/21 gröna i `test_loader.py`.
- [x] **Step 6: Full `tests/crypto_trading/` regression** — 401 passed, 1 deselected (398 innan + 3 nya). `ruff check`/`format --check`: rena.

---

## Task 2: Repository — sex nya read-only metoder

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Create: `tests/crypto_trading/storage/test_repository_dashboard_reads.py`

**Interfaces:** exakt signaturerna i §4 ovan, tillagda på `Repository`-protokollet och implementerade på `SQLiteRepository`.

- [x] **Step 1: Write the failing tests** — 14 tester i `test_repository_dashboard_reads.py`, exakt enligt planen (tom-DB, happy path, limit/offset, korrupt-rad-hoppa-över för `find_all_candidates`).
- [x] **Step 2: Run tests to verify they fail** — 14/14 röda, `AttributeError` för samtliga sex metoder.
- [x] **Step 3: Implement** — sex metoder på `Repository`-protokollet + `SQLiteRepository`, ren `SELECT ... ORDER BY <fält> DESC LIMIT ? OFFSET ?`.
- [x] **Step 4: Run tests to verify they pass** — 14/14 gröna.
- [x] **Step 5: Full svit regression: 415 passed, 1 deselected (401 innan + 14 nya). `ruff check`/`format --check`: rena efter en radlängdsfix i testfilen.**

---

## Task 3: `dashboard/api.py` — appskelett + AC1 + säkerhet + read-only-bevis

**Files:**
- Create: `crypto_trading/dashboard/__init__.py`
- Create: `crypto_trading/dashboard/api.py`
- Create: `tests/crypto_trading/dashboard/__init__.py`
- Create: `tests/crypto_trading/dashboard/test_dashboard_security.py`

**Interfaces:** `create_app(repo: Repository, settings: Settings) -> fastapi.FastAPI`. Skelettet monterar inga vyer än (de läggs till i Task 4–8) — bara en tom `APIRouter` + evt en `/api/health`-`GET` för att ha minst en route att introspektera meningsfullt mot.

- [x] **Step 1: Write the failing tests** i `test_dashboard_security.py` — fyra tester, `MagicMock(spec=Repository)` istället för en manuell stub (enklare, samma effekt: `assert_not_called()` per write-metod).
- [x] **Step 2: Run tests to verify they fail** — `ModuleNotFoundError: No module named 'crypto_trading.dashboard'`.
- [x] **Step 3: Implement** — `dashboard/__init__.py`, `dashboard/api.py::create_app(repo, settings)`, `GET /api/health`.
- [x] **Step 4: Run tests to verify they pass** — 4/4 gröna.
- [x] **Step 5: Regression: 419 passed, 1 deselected (415 innan + 4 nya). `ruff` ren.**

---

## Task 4: LIVE-vyn (AC2 för CONFIRMED/NO_TRADE)

**Files:**
- Modify: `crypto_trading/dashboard/api.py`
- Create: `tests/crypto_trading/dashboard/test_dashboard_live.py`

**Interfaces:** `GET /api/live` → JSON:
```json
{
  "last_discovery_run": {"run_id": "...", "started_at": "...", "completed_at": "...", "status": "...", "instruments_scanned": 0} ,
  "top_n_instruments": "unavailable — not persisted historically",
  "in_progress_candidates": [...],
  "confirmed_candidates": [...],
  "no_trade_candidates": [...],
  "open_positions": [{"...position fields...", "unrealized_pnl": "unavailable — live price not persisted"}],
  "risk_exposure": {"open_positions_count": 0, "open_positions_notional": "0", "max_concurrent_positions": 5, "max_total_exposure_pct": "0.25", "starting_capital_usdt": "10000"}
}
```
Per-candidate-objekt inkluderar `status`, `evidence_record` (score/trigger_reasons), sju assessments status (`ok`/`failed`/`timeout`/`null` om ej körd), och `gate_decision` (via `get_gate_decision`, `null` om ej ännu utvärderad).

- [x] **Step 1: Write the failing tests** — fyra tester i `test_dashboard_live.py`, exakt enligt planen.
- [x] **Step 2: Run tests to verify they fail** — röda (route saknades, `KeyError`).
- [x] **Step 3: Implement** `/api/live`. **Arkitekturkorrigering upptäckt under implementation (dokumenterad här för spårbarhet):** `create_app(repo, settings)` med en delad `Repository`-instans kraschade med `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread` — FastAPI kör synkrona route-funktioner i en threadpool (en ny OS-tråd per anrop), och detta hade krashat i produktion också (inte bara i testklienten), oavsett Task 10:s trådwiring. Löst helt internt i `dashboard/api.py`, UTAN att röra `storage/db.py`/`storage/repository.py`: `create_app` tar nu en `repo_factory: Callable[[], Repository]` istället för en delad instans, och varje route-funktion anropar `repo_factory()` själv — en färsk anslutning per request, samma "egen anslutning per körningskontext"-princip som redan gäller discovery/monitoring/notify-trådarna, fast på request-nivå. Task 3:s redan gröna tester uppdaterade i samma steg (`create_app(lambda: mock, settings)`), ingen ny regression.
- [x] **Step 4: Run tests to verify they pass** — 8/8 gröna (4 nya + Task 3:s 4 om-anpassade).
- [x] **Step 5: Regression: 423 passed, 1 deselected. `ruff check`/`format --check`: rena (efter `ruff format`-körning).**

---

## Task 5: TRADE HISTORY-vyn (AC2 för CLOSED, PnL via `compute_pnl`, MFE/MAE-gap)

**Files:**
- Modify: `crypto_trading/dashboard/api.py`
- Create: `tests/crypto_trading/dashboard/test_dashboard_trade_history.py`

**Interfaces:** `GET /api/trade-history?limit=50&offset=0` → JSON:
```json
{
  "candidates": [{"...", "gate_decision": {...} | null}],
  "positions": [{"...position fields...", "pnl": "..." | null, "mfe": null, "mae": null, "mfe_mae_status": "not yet tracked"}]
}
```
`pnl` beräknas via `paper_trading.execution.compute_pnl(position)` **endast** för `status == "CLOSED"` positioner (samma villkor som `compute_pnl` redan kräver — `simulated_fill_exit` måste finnas), `null` för öppna.

- [x] **Step 1: Write the failing tests** — fem tester i `test_dashboard_trade_history.py` (paginering, MFE/MAE-gap, AC2 PnL vs Telegram, öppen position har `pnl: null`, gate decisions).
- [x] **Step 2: Run tests to verify they fail** — 5/5 röda, `KeyError` (routen saknades).
- [x] **Step 3: Implement** `/api/trade-history` — `repo.find_all_candidates(limit, offset)`, `repo.find_all_positions(limit, offset)`, `compute_pnl(position)` (import från `paper_trading.execution`, ren funktion, inget skrivanrop) för `CLOSED`-positioner, `null` för öppna.
- [x] **Step 4: Run tests to verify they pass** — 5/5 gröna.
- [x] **Step 5: Regression: 428 passed, 1 deselected. `ruff` ren (en radlängdsfix).**

---

## Task 6: SYSTEM HEALTH-vyn (AC3 — BUDGET_LIMITED från simulerad testkörning)

**Files:**
- Modify: `crypto_trading/dashboard/api.py`
- Create: `tests/crypto_trading/dashboard/test_dashboard_system_health.py`

**Interfaces:** `GET /api/system-health?limit=50` → JSON:
```json
{
  "recent_runs": [{"run_id": "...", "run_type": "...", "started_at": "...", "completed_at": "...", "status": "...", "errors": [...], "instruments_scanned": 0, "duration_seconds": 12.3 | null}],
  "ai_calls_today": 0,
  "budget_limited_candidates_today": 0,
  "failed_runs_today": 0,
  "rate_limit_events": "unavailable — throttle decisions not persisted (known gap)"
}
```
`duration_seconds` = trivial `(completed_at - started_at).total_seconds()` när båda finns, annars `null` — ren visningsformattering av två redan hämtade timestamps, ingen ny mätpunkt.

- [x] **Step 1: Write the failing tests** — tre tester i `test_dashboard_system_health.py`. AC3-testet importerar och återanvänder `_run_three_ticks_against_fresh_repo` direkt från `tests/crypto_trading/test_phase5_integration.py` (samma cross-file-testimportmönster som redan är etablerat i den filsviten) — en verklig discovery-cykel med `max_ai_calls_per_day=14`, exakt två fulla analyser, tredje candidaten blir `BUDGET_LIMITED` genom den riktiga gate-logiken, inte en fixture.
- [x] **Step 2: Run tests to verify they fail** — 3/3 röda, `KeyError` (routen saknades). Baslinjen bekräftades samtidigt: den simulerade körningen gav faktiskt exakt en `BUDGET_LIMITED`-candidate.
- [x] **Step 3: Implement** `/api/system-health` — `repo.find_recent_runs(limit)`, `repo.count_ai_calls_since(day_start)`, `repo.count_candidates_by_status_since("BUDGET_LIMITED", day_start)`, `repo.count_runs_by_status_since("error", day_start)`. `day_start` beräknas lokalt i `dashboard/api.py` (`datetime.now(UTC).replace(hour=0,...)`) — INTE importerad från `notify_loop.py`, som förblir orört.
- [x] **Step 4: Run tests to verify they pass** — 3/3 gröna, inklusive AC3.
- [x] **Step 5: Regression: 431 passed, 1 deselected. `ruff` ren.**

---

## Task 7: FORECAST-vyn (rå scenario-data, Brier/calibration explicit uteslutet)

**Files:**
- Modify: `crypto_trading/dashboard/api.py`
- Create: `tests/crypto_trading/dashboard/test_dashboard_forecast.py`

**Interfaces:** `GET /api/forecast?limit=50&offset=0` → JSON:
```json
{
  "forecasts": [{"forecast_id": "...", "instrument": "...", "scenario_probabilities": {...}, "forecast_version": "...", "horizon": "...", "actual_outcome": null}],
  "calibration": "not_available_yet — Phase 8 (Brier score / calibration curve require accumulated ForecastRecord history and a central calibration module)"
}
```

- [x] **Step 1: Write the failing tests** — två tester i `test_dashboard_forecast.py`.
- [x] **Step 2: Run tests to verify they fail** — 2/2 röda, `KeyError`.
- [x] **Step 3: Implement** `/api/forecast` — `repo.find_all_forecasts(limit, offset)`, statisk `calibration`-sträng.
- [x] **Step 4: Run tests to verify they pass** — 2/2 gröna.
- [x] **Step 5: Regression: 433 passed, 1 deselected. `ruff` ren.**

---

## Task 8: PERFORMANCE-vyn (ren platshållare)

**Files:**
- Modify: `crypto_trading/dashboard/api.py`
- Create: `tests/crypto_trading/dashboard/test_dashboard_performance.py`

**Interfaces:** `GET /api/performance` → JSON: `{"status": "not_available_yet", "reason": "Win rate, expectancy, drawdown, profit factor and cumulative PnL require a central calculation source, deliberately deferred to Phase 8 to avoid duplicated business logic."}`

- [x] **Step 1: Write the failing test** — negativ assertion mot en uppsättning förbjudna numeriska nycklar.
- [x] **Step 2: Run test to verify it fails** — `404 Not Found`.
- [x] **Step 3: Implement** — statisk `GET /api/performance`, inga Repository-anrop alls.
- [x] **Step 4: Run test to verify it passes** — grönt.
- [x] **Step 5: Regression: 434 passed, 1 deselected. `ruff` ren. Samtliga 19 dashboard-tester gröna.**

---

## Task 9: Enkel frontend

**Files:**
- Create: `crypto_trading/dashboard/frontend/index.html`
- Modify: `crypto_trading/dashboard/api.py` (montera `StaticFiles`/en `GET /`-route som serverar filen)
- Create: `tests/crypto_trading/dashboard/test_dashboard_frontend.py`

**Interfaces:** `GET /` → `200`, `Content-Type: text/html`.

- [x] **Step 1: Write the failing test** — `test_root_serves_html_frontend`.
- [x] **Step 2: Run test to verify it fails** — `404`.
- [x] **Step 3: Implement** — statisk `index.html`: fem flikar (LIVE/TRADE HISTORY/PERFORMANCE/FORECAST/SYSTEM HEALTH), vanilla JS `fetch()` per flik, ingen byggprocess, ingen extern CDN, allt inline (CSS+JS i samma fil, inga separata assets) — därför ingen `StaticFiles`-mount, bara en explicit `GET /` → `FileResponse`. AC1-testet (route-introspektion) bekräftar `/`-routen är GET-only tillsammans med resten.
- [x] **Step 4: Run test to verify it passes** — grönt. Samtliga 20 dashboard-tester gröna.
- [x] **Step 5: Regression: 435 passed, 1 deselected. `ruff` ren.**

---

## Task 10: `run.py` — valfri fjärde tråd

**Files:**
- Modify: `crypto_trading/run.py`
- Modify: `tests/crypto_trading/test_run_bootstrap.py`

**Interfaces:** `build_dashboard_app_from_env(repo: Repository, settings: Settings) -> FastAPI | None` — analog med `build_notifier_from_env()`, returnerar `None` om `CRYPTO_TRADING_DASHBOARD_ENABLED` inte är satt (opt-in, samma princip som Telegram). `main()`s wiring lägger till en fjärde `threading.Thread` (daemon) som kör `uvicorn.run(app, host=settings.dashboard.host, port=settings.dashboard.port, log_level="warning")` — bara om appen inte är `None`.

- [x] **Step 1: Write the failing tests** — två tester i `test_run_bootstrap.py`.
- [x] **Step 2: Run tests to verify they fail** — `ImportError: cannot import name 'build_dashboard_app_from_env'`.
- [x] **Step 3: Implement** — `build_dashboard_app_from_env(repo_factory, settings)` i `run.py`, gated bakom `CRYPTO_TRADING_DASHBOARD_ENABLED`. `_run_dashboard_forever(app, settings)` kör `uvicorn.run(...)` (blockerande, samma mönster som övriga `run_forever()`-trådar). `main()` bygger en `_dashboard_repo_factory()`-stängning (samma "egen anslutning per körningskontext" som Task 4:s fix, konsumerad av `dashboard/api.py` per request) och trådar in dashboarden villkorat, precis som notify-tråden.
- [x] **Step 4: Run tests to verify they pass** — 7/7 gröna i `test_run_bootstrap.py`.
- [x] **Step 5: Manuell verifiering** — `from crypto_trading.run import main, build_dashboard_app_from_env` importerar rent; en byggd app (flagga satt) svarade `200 {"status": "ok"}` på `/api/health` via `TestClient`.
- [x] **Step 6: Full regression: 437 passed (crypto_trading), 541 passed (hela repot), 1 deselected. `ruff check`/`format --check`: rena (en radlängdsfix, auto-formaterad).**

---

## Task 11: Slutverifiering (obligatorisk innan commit)

**Files:** inga (bara verifieringskommandon).

- [x] **Step 1: Full testsvit** — `tests/crypto_trading/`: 437 passed, 1 deselected. Hela repot: 541 passed, 1 deselected. Alla gröna.
- [x] **Step 2: `ruff check` + `ruff format --check`** — båda rena över `crypto_trading/` och `tests/crypto_trading/`.
- [x] **Step 3: `git diff --check`** — inga whitespace-/konfliktmarkör-fel (bara harmlösa LF/CRLF-info-warnings från Windows radslutskonfiguration, inte fel).
- [x] **Step 4: `intelligence/`-diff** — `git diff -- intelligence/` tomt.
- [x] **Step 5: `test_no_intelligence_coupling.py`** — 3/3 gröna, bekräftar `dashboard/` inte importerar `intelligence` och inga broker-termer smugit in (glob-baserad, täcker nya filer automatiskt).
- [x] **Step 6: Explicit re-run av `test_replay.py`** — 5/5 gröna, inkl. `test_replay_is_deterministic_on_repeated_runs` och `test_replay_decision_at_time_t_is_unaffected_by_injected_future_data`, oförändrad fil.
- [x] **Step 7: Självständig full diffgranskning** — `git diff --stat` (10 ändrade filer, +250/-1 rader, plus nya `dashboard/`-paket och testfiler) + fil-för-fil-läsning av samtliga. Resultat: (a) samtliga åtta routes (`/`, `/api/health`, `/api/live`, `/api/trade-history`, `/api/system-health`, `/api/forecast`, `/api/performance`) är GET, ingen mutation; (b) inga `Decimal`/hemligheter läcker orediergerat — `runs.errors` redigeras redan vid `complete_run()`-skrivning (Fas 6), `candidate_score` är typmässigt `float` (inte en monetär `Decimal`), alla monetära `Decimal`-fält explicit `str()`-serialiserade; (c) `dashboard/`-paketet importerar inget ur den förbjudna listan (mekaniskt bekräftat); (d) samtliga sex kända datagap (Top N, unrealized PnL, MFE/MAE, calibration, performance-mått, rate-limit-events) returnerar explicita `unavailable`/`not_available_yet`-strängar, ingen gissning. Inga övriga oväntade filer i diffen (de sex `research/*.md`-filerna i `git status` är förbefintliga, orörda av detta arbete).
- [x] **Step 8: Redovisa slutresultat till användaren** — se sammanfattning i konversationen. **Ingen commit, ingen push.**

---

## Self-review (utfört innan planen sparas)

**Spec-täckning:** samtliga fem vyer, AC1 (Task 3), AC2 (Task 4 Step 1.3, Task 5 Step 1.3), AC3 (Task 6 Step 1.3), Säkerhet/default-host (Task 3 Step 1.2), Read-only/no-external-calls (Task 3 Step 1.3–1.4), Data gaps (Task 4 Step 1.2, Task 5 Step 1.2, Task 6 Step 1.2, Task 7 Step 1.2, Task 8 Step 1) — alla explicit täckta av namngivna tester.

**Placeholder-scan:** inga TBD/TODO. Varje "unavailable"-sträng är ordagrant specificerad i varje tasks Interfaces-block, inte en vag beskrivning.

**Typkonsekvens:** `DashboardConfig.host`/`.port` namngivna identiskt i Task 1/Task 10. `find_all_candidates`/`find_all_positions`/`get_gate_decision`/`find_latest_run`/`find_recent_runs`/`find_all_forecasts` — signaturer i §4 återanvända ordagrant i Task 2–7.

**Scope-kontroll:** ingen ändring i `notify/`, `notify_loop.py`, `agents/`, `gate/`, `screening/`, `state_machine.py`, `paper_trading/` (utöver en ren, oskrivande `import`+anrop av `compute_pnl` i Task 5 — funktionen själv rörs inte), `discovery_loop.py`, `monitoring_loop.py`, `storage/db.py` (schema). Ingen rate-limit-/connector-observability byggd (explicit uteslutet per uppdrag — separat stopp-och-rapportera-steg krävs om det ska göras). MFE/MAE, Top N-historik, Brier/calibration, performance-mått — samtliga explicit `unavailable`, ingen ny beräkningslogik.
