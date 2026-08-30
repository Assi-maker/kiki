# Crypto Trading — Phase 8 (Forecast Calibration + Performance) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** GODKÄND FÖR IMPLEMENTATION (2026-08-30) — scope låst av användarens meddelande samma dag, alternativ 3 (se §0).

**Goal:** Leverera riktiga, deterministiska PERFORMANCE- och FORECAST-kalibreringsmått (`performance/metrics.py`, `calibration/brier_score.py`, `calibration/calibration_curve.py`) och koppla in dem i Fas 7:s redan existerande `/api/performance`- och `/api/forecast`-platshållare — utan att bygga `actual_outcome`-ifyllning, utan schemaändring, utan att röra Fas 0–7:s beslutslogik.

**Architecture:** Två nya, rena beräkningspaket (`performance/`, `calibration/`) som tar `list[Position]`/`list[ForecastRecord]` som indata och returnerar rena dict/primitiver — exakt samma mönster som `paper_trading/execution.py::compute_pnl()` redan etablerat (ingen egen DB-åtkomst, ingen sidoeffekt). Två nya, obegränsade read-only `Repository`-metoder (`find_closed_positions`, `find_forecasts_with_outcome`) — paginerade metoder från Fas 7 (`find_all_positions`/`find_all_forecasts`, `le=500`) är fel verktyg för en aggregatberäkning över hela historiken. `dashboard/api.py`s två befintliga platshållar-routes byter ut sin statiska kropp mot anrop till dessa nya moduler — ingen ny route, ingen ändrad HTTP-metod, ingen ändrad path.

**Tech Stack:** Oförändrat. Ingen ny dependency (Brier score/calibration-kurva är ren aritmetik på `Decimal`/`float`, redan tillgängligt via stdlib).

**Spec:** `SPEC_CRYPTO.md` §9 (Forecast/kalibrering, citerat i sin helhet i §2 nedan), §13 (Dashboard PERFORMANCE/FORECAST-vyerna). `PLAN_CRYPTO.md` §Phase 8 (Omfattning/Levererar/AC1–AC3). Denna plan implementerar en **medvetet avgränsad delmängd** av Fas 8:s fulla omfattning — se §0.

---

## 0. Beslutslogg — vad som INTE byggs i denna fas, och varför

Användaren godkände **alternativ 3** från READ-ONLY-inventeringen (2026-08-30): kalibreringsmatematiken byggs och testas fullt ut, men **`actual_outcome`-ifyllning byggs inte i Fas 8**.

**Motivering (från inventeringen, bekräftad):** `actual_outcome` kräver en av tre saker som alla är utanför denna fas: (a) fri-text-scenarier utan numeriska gränser gör generell matchning odefinierad, (b) `horizon` är oparsad fri text, (c) ingen kontinuerlig prishistorik persisteras för godtycklig historisk tidpunkt (`market_data_snapshots` byggdes aldrig). Att bygga outcome-ifyllning nu skulle kräva antingen en ny prishistorik-mekanism (schemamigrering + ny bakgrundstråd — betydande ny arkitekturrisk) eller en gissningsbaserad heuristik (uttryckligen förbjudet av användaren).

**Explicit beslut:** `actual_outcome`-ifyllning är **Fas 8.5 / framtida arbete**, inte del av denna plan. Kalibreringskoden i denna fas konsumerar **endast** `ForecastRecord`-rader där `actual_outcome` redan är persisterat (av vilken framtida mekanism som helst) — `WHERE actual_outcome IS NOT NULL`. Om noll sådana rader finns (vilket är det **garanterade läget vid denna plans start**, eftersom ingen kod någonsin fyller i fältet ännu), ger kalibreringskoden `CalibrationStatus: insufficient_data`, `sample_size: 0` — aldrig ett fabricerat resultat, aldrig en krasch.

**Explicit beslut (expectancy):** definieras som **genomsnittlig PnL per stängd trade** (`sum(compute_pnl(p) for p in closed) / len(closed)`) — direkt förenligt med `compute_pnl()`, enklast att verifiera deterministiskt (användarens uttryckliga val, ej vinst%×snittvinst-formeln).

**Explicit beslut ("utveckling över tid"):** en kronologisk (sorterad på `closed_at`) equity/PnL-tidsserie — lista av `{closed_at, cumulative_pnl}` — inte bara ett slutvärde.

**Explicit uteslutet ur denna fas (bekräftat i inventeringen, ingen ändring):** "marknadsregim"-nedbrytning (ingen regimklassificering existerar någonstans i kodbasen — skulle kräva en helt ny, separat analyskomponent). Kalibreringens nedbrytning begränsas till per-`horizon` (grupperat på det redan persisterade fri-text-värdet, ingen parsning/tolkning) och per-scenario-nyckel (bullish/bearish/etc., direkt från `scenario_probabilities`s egna nycklar).

## Global Constraints

- **Ingen `actual_outcome`-ifyllning, ingen ny prishistorik, ingen ny bakgrundstråd/loop, ingen gissning från fri text.** Se §0.
- **`compute_pnl()` (`paper_trading/execution.py`) är den enda PnL-källan** — importeras och återanvänds rakt av i `performance/metrics.py`, ingen ny formel.
- **Ingen look-ahead bias:** samtliga beräkningar läser uteslutande redan `CLOSED`-positioner respektive forecasts med redan persisterat `actual_outcome` — data som per definition var känd/finaliserad innan beräkningen körs. Se §4 för explicita determinism-/no-look-ahead-tester.
- **`intelligence/` rörs inte.** `notify/`, `notify_loop.py`, `agents/`, `gate/`, `screening/`, `state_machine.py`, `discovery_loop.py`, `monitoring_loop.py`, och paper-trading-**beslutslogiken** (`position_sizing.py`, `position_opening.py`, `position_closing.py`, `monitoring.py`) rörs inte — enda beröringspunkten med `paper_trading/` är en ren `import compute_pnl` (läsning, ingen ändring av den filen).
- **Dashboardens GET-only-garanti försvagas aldrig.** Inga nya routes, inga nya HTTP-metoder — bara två redan existerande `GET`-routes byter platshållar-kropp mot riktig data. `test_no_mutating_routes_exist` (Fas 7) måste förbli grönt oförändrat.
- **Ingen schemaändring.** Två nya `Repository`-metoder är rena `SELECT`:ar mot befintligt schema (`positions`, `forecasts`).
- **Alla trösklar från config, inget hårdkodat:** `settings.pipeline.min_sample_size_for_calibration`/`calibration_preliminary_sample_size` (redan existerande sedan Fas 0) — aldrig en ny, hårdkodad gräns.
- **`ruff` line-length 100, regler `E,F,I,UP,B`.**

---

## 1. Exakt vilka filer som berörs

**Skapas:**
- `crypto_trading/performance/__init__.py`
- `crypto_trading/performance/metrics.py`
- `crypto_trading/calibration/__init__.py`
- `crypto_trading/calibration/brier_score.py`
- `crypto_trading/calibration/calibration_curve.py`
- `tests/crypto_trading/performance/__init__.py`
- `tests/crypto_trading/performance/test_metrics.py`
- `tests/crypto_trading/calibration/__init__.py`
- `tests/crypto_trading/calibration/test_brier_score.py`
- `tests/crypto_trading/calibration/test_calibration_curve.py`
- `tests/crypto_trading/storage/test_repository_performance_reads.py`

**Ändras:**
- `crypto_trading/storage/repository.py` — två nya read-only metoder (protokoll + `SQLiteRepository`).
- `crypto_trading/dashboard/api.py` — `/api/performance` och `/api/forecast`s `calibration`-fält byter platshållare mot riktiga anrop. Ingen route-signatur, path eller HTTP-metod ändras.
- `tests/crypto_trading/dashboard/test_dashboard_performance.py` — nuvarande "inga tal alls"-assertion (Fas 7) ersätts av tester mot riktiga, beräknade tal. **Medveten, nödvändig ändring** — inte en försvagning (se §7, det gamla testets syfte — "aldrig fabricerade tal utan data" — lever vidare som ett nytt zero-trades-test).
- `tests/crypto_trading/dashboard/test_dashboard_forecast.py` — nuvarande statiska `"calibration": "not_available_yet..."`-assertion ersätts av tester mot den nya strukturerade kalibreringsdicten (även den med `insufficient_data`-status som förväntat default-läge, eftersom `actual_outcome` aldrig fylls i av någon kod ännu — se §0).

**Rörs INTE:** `intelligence/`, `notify/`, `notify_loop.py`, `agents/`, `gate/`, `screening/`, `state_machine.py`, `discovery_loop.py`, `monitoring_loop.py`, `paper_trading/position_sizing.py`, `paper_trading/position_opening.py`, `paper_trading/position_closing.py`, `paper_trading/monitoring.py`, `paper_trading/replay.py`, `storage/db.py` (schema), `run.py`, samtliga Fas 7-säkerhetstester (`test_dashboard_security.py`, `test_dashboard_pagination.py`) — förblir oförändrade och gröna.

## 2. SPEC §9 citerat (styr §5/§6 nedan ordagrant)

> `ForecastAssessment` producerar typade, ömsesidigt uteslutande scenarier som summerar till 1.0... Varje forecast loggas (`ForecastRecord`) med minst: `instrument`, `forecast_timestamp`, `timeframe/horizon`, `scenario_probabilities`, `forecast_version`, ... `actual_outcome`/`outcome_timestamp` som fylls i när fönstret passerat.
>
> **Kalibrering (Fas 8):** ... Beräknas: **Brier score**, **calibration buckets/kurva** (av alla gånger agenten sa "~60%", hur ofta inträffade det verkligen), antal observationer per bucket, calibration error, nedbrutet per timeframe/riktning/scenario/marknadsregim där datamängden räcker.
>
> **Explicit `CalibrationStatus` per redovisat mått**... `insufficient_data` / `preliminary` / `calibrated`. Sample size och `CalibrationStatus` visas **alltid tillsammans** med varje kalibreringssiffra.

Denna plan implementerar: Brier score ✅, calibration-kurva/buckets ✅, antal observationer per bucket ✅, `CalibrationStatus` alltid tillsammans med sample size ✅, nedbrytning per timeframe (`horizon`) ✅, per scenario ✅. **Explicit uteslutet:** nedbrytning per "riktning" tolkat som marknadsriktning (redan täckt av per-scenario, se §0) och per "marknadsregim" (ingen sådan data existerar, se §0).

## 3. Datakällor / sanningskälla (verifierat)

| Data | Källa | Ny/befintlig |
|---|---|---|
| Stängda positioner | `positions`-tabellen, `status='CLOSED'` | Befintlig tabell, ny read-metod |
| Forecasts med utfall | `forecasts`-tabellen, `actual_outcome IS NOT NULL` | Befintlig tabell, ny read-metod |
| PnL per trade | `paper_trading/execution.py::compute_pnl()` | Befintlig, oförändrad, återanvänd |
| Kalibreringströsklar | `settings.pipeline.min_sample_size_for_calibration`/`calibration_preliminary_sample_size` | Befintlig (Fas 0) |

Ingen ny parallell datamodell. Samma `Repository`-gränssnitt som Fas 0–7 redan använder — `dashboard/api.py` fortsätter anropa `repo_factory()` en gång per request (Fas 7:s redan lösta trådsäkerhetsmönster, oförändrat).

## 4. Determinism / no-look-ahead — krav och tester

- Samtliga funktioner i `performance/metrics.py`/`calibration/*.py` är **rena funktioner**: samma indata → identisk utdata, ingen `datetime.now()`, inget slumptal, ingen DB-åtkomst inuti funktionerna själva.
- `find_closed_positions()`/`find_forecasts_with_outcome()` läser bara rader vars avgörande fält (`status='CLOSED'` respektive `actual_outcome IS NOT NULL`) redan är **finala** vid lästillfället — omöjligt att läsa in ofullständig/framtida data eftersom dessa fält skrivs atomärt vid faktisk händelse (samma atomära `close_position_with_event()`-UPDATE som Fas 7:s granskning redan bevisade).
- **Obligatoriska tester (Task 9):**
  1. `test_performance_summary_is_deterministic_on_repeated_calls` — samma `list[Position]` in två gånger → identisk dict ut.
  2. `test_calibration_summary_is_deterministic_on_repeated_calls` — motsvarande för `list[ForecastRecord]`.
  3. `test_equity_curve_never_includes_a_position_still_open` — en `OPEN_POSITION` i indatan (skulle aldrig komma från `find_closed_positions()`, men funktionen måste vara defensivt korrekt ändå) exkluderas tyst ur summan, kraschar aldrig.
  4. `test_calibration_excludes_forecast_with_outcome_after_an_explicit_as_of_cutoff` — bevisar att om en framtida "as of"-gräns någonsin införs (inte i denna fas, men beteendet testas ändå på den nuvarande, orestricted-varianten) inkluderar beräkningen ALDRIG en rad vars data inte fanns tillgänglig — konkret: ett test som skapar två forecasts, ett med `outcome_timestamp` tidigt och ett sent, och bevisar att brier-score-beräkningen för en delmängd (filtrerad innan anrop, av testet self, inte av funktionen) ger olika, korrekta resultat beroende på vilken delmängd som skickas in — bevisar att funktionen själv aldrig "kikar" utanför sin indata.

## 5. `performance/metrics.py` — exakta funktionssignaturer

```python
from __future__ import annotations
from decimal import Decimal
from crypto_trading.paper_trading.execution import compute_pnl
from crypto_trading.schemas.trade import Position

def trade_pnls(positions: list[Position]) -> list[Decimal]:
    """compute_pnl() per position med status == 'CLOSED' - filtrerar bort
    allt annat internt (defensivt, oavsett vad anroparen skickar in)."""

def compute_cumulative_pnl(pnls: list[Decimal]) -> Decimal:
    """sum(pnls), Decimal('0') för tom lista - alltid ett riktigt tal,
    aldrig None (en tom historik HAR verkligen noll kumulativ PnL)."""

def compute_win_rate(pnls: list[Decimal]) -> Decimal | None:
    """count(p > 0) / count(alla) - break-even (p == 0) räknas i nämnaren,
    aldrig i täljaren. None om pnls är tom (odefinierat, inte 0%)."""

def compute_expectancy(pnls: list[Decimal]) -> Decimal | None:
    """sum(pnls) / len(pnls) - None om tom (§0: explicit vald definition)."""

def compute_profit_factor(pnls: list[Decimal]) -> Decimal | None:
    """sum(vinster) / abs(sum(förluster)). None om pnls tom ELLER om det
    inte finns några förluster (division med noll - odefinierat, INTE
    oändligt/fabricerat). 0 (ett giltigt tal) om det finns förluster men
    inga vinster."""

def compute_drawdown(positions: list[Position]) -> Decimal | None:
    """Max peak-to-trough över en kronologisk (sorterad på closed_at,
    beräknat INUTI funktionen - litar aldrig på anroparens ordning) kumulativ
    PnL-kurva. None om inga stängda positioner. 0 (giltigt) om det finns
    trades men aldrig en nedgång från toppen (t.ex. en enda vinnande trade)."""

def compute_equity_curve(positions: list[Position]) -> list[dict]:
    """[{"closed_at": iso-sträng, "cumulative_pnl": str(Decimal)}, ...],
    kronologiskt sorterat internt. Tom lista om inga stängda positioner."""

def compute_breakdown_by_instrument(positions: list[Position]) -> dict[str, dict]:
    """{"BTCUSDT": {"trade_count": int, "cumulative_pnl": str, "win_rate": str|None}, ...}"""

def compute_breakdown_by_direction(positions: list[Position]) -> dict[str, dict]:
    """{"LONG": {...}, "SHORT": {...}} - samma delfält som breakdown_by_instrument.
    Riktningsagnostisk kod (itererar över de riktningar som faktiskt finns i
    indatan) - antar ALDRIG bara LONG, trots att produktionspipelinen idag
    bara producerar LONG (paper_trading/position_closing.py: _DIRECTION =
    "LONG", oförändrad i denna fas)."""
```

Alla `Decimal`-returer serialiseras till `str()` av `dashboard/api.py` vid assemblering (samma disciplin som Fas 7) — funktionerna själva returnerar `Decimal`/`None`, aldrig förformaterad JSON.

## 6. `calibration/brier_score.py` + `calibration/calibration_curve.py` — exakta funktionssignaturer

```python
# calibration/brier_score.py
from __future__ import annotations
from decimal import Decimal
from crypto_trading.schemas.forecast import ForecastRecord

def compute_brier_score(forecasts: list[ForecastRecord]) -> dict:
    """Multi-kategori Brier score (Briers ursprungliga formulering):
    BS = mean over forecasts( sum over scenario_probabilities-nycklar(
        (predicted_prob - indicator)^2 ) )
    där indicator = 1 om nyckeln == forecast.actual_outcome, annars 0.

    Ett forecast vars actual_outcome INTE är en av dess EGNA
    scenario_probabilities-nycklar exkluderas explicit (räknas i
    excluded_count, ingen gissning om vilken kategori som "egentligen"
    menades) - täcker "invalid/missing forecast outcomes"-edge-caset.
    En forecast med actual_outcome=None exkluderas likaså (naturligt,
    `None in scenario_probabilities` är alltid False).

    Returnerar:
    {
        "value": str(Decimal) | None,   # None om sample_size == 0
        "sample_size": int,             # antal forecasts SOM KUNDE bedömas
        "excluded_count": int,          # antal forecasts som inte kunde matchas
    }
    """


# calibration/calibration_curve.py
from __future__ import annotations
from decimal import Decimal
from crypto_trading.schemas.forecast import ForecastRecord

def compute_calibration_status(
    sample_size: int, min_sample_size: int, preliminary_sample_size: int
) -> str:
    """"insufficient_data" (sample_size < preliminary_sample_size),
    "preliminary" (preliminary_sample_size <= sample_size < min_sample_size),
    "calibrated" (sample_size >= min_sample_size). Ren, trösklarna
    ALLTID skickas in explicit av anroparen från settings.pipeline -
    aldrig hårdkodade i denna funktion."""

def compute_calibration_curve(
    forecasts: list[ForecastRecord],
    min_sample_size: int,
    preliminary_sample_size: int,
    bucket_width: Decimal = Decimal("0.1"),
) -> list[dict]:
    """Standardnedbrytning: varje forecast med N scenario_probabilities-
    nycklar ger N binära observationspunkter (predicted_prob för nyckeln,
    1/0 om den nyckeln == actual_outcome) - EXAKT SPEC §9:s "av alla gånger
    agenten sa ~60%, hur ofta inträffade det verkligen". Forecasts vars
    actual_outcome inte matchar någon egen nyckel bidrar noll punkter (samma
    exkluderingsprincip som brier_score.py, aldrig en gissning).

    10 fasta buckets (bucket_width=0.1 default): [0.0,0.1), [0.1,0.2), ...,
    [0.9,1.0]. Returnerar en lista, en dict per bucket:
    [{
        "bucket_low": str(Decimal), "bucket_high": str(Decimal),
        "sample_size": int,
        "mean_predicted": str(Decimal) | None,
        "observed_frequency": str(Decimal) | None,
        "calibration_status": "insufficient_data"/"preliminary"/"calibrated",
    }, ...]
    mean_predicted/observed_frequency är None när bucketen är tom (sample_size
    == 0) - aldrig 0.0 som skulle kunna misstas för ett verkligt observerat
    värde."""

def compute_calibration_breakdown_by_horizon(
    forecasts: list[ForecastRecord], min_sample_size: int, preliminary_sample_size: int
) -> dict[str, dict]:
    """Grupperar på forecast.horizon (redan persisterad fri-textsträng,
    grupperas LITERALT - ingen parsning/tolkning av vad "4h" betyder).
    {"4h": {"brier_score": {...samma shape som compute_brier_score...}}, ...}"""

def compute_calibration_breakdown_by_scenario(
    forecasts: list[ForecastRecord], min_sample_size: int, preliminary_sample_size: int
) -> dict[str, dict]:
    """Grupperar på scenario-nyckel (t.ex. "bullish" för sig, "bearish" för
    sig) - för varje unik nyckel som förekommer i NÅGON forecasts
    scenario_probabilities: sample_size/mean_predicted/observed_frequency/
    calibration_status, byggt av samma binära punkter som
    compute_calibration_curve() men grupperat på nyckel istället för
    sannolikhetsintervall."""
```

## 7. Repository — två nya read-only metoder

```python
def find_closed_positions(self) -> list[Position]: ...
def find_forecasts_with_outcome(self) -> list[ForecastRecord]: ...
```
- `find_closed_positions`: `SELECT * FROM positions WHERE status = 'CLOSED'` (ingen `LIMIT` — medvetet obegränsad, till skillnad från Fas 7:s paginerade `find_all_positions`, eftersom en aggregatberäkning över hela historiken kräver ALLA rader, inte en sida). Ordning garanteras INTE av metoden — `performance/metrics.py`s funktioner sorterar själva internt (§5), så korrekthet beror aldrig på repository-lagrets radordning.
- `find_forecasts_with_outcome`: `SELECT * FROM forecasts WHERE actual_outcome IS NOT NULL`, samma deserialisering som befintlig `get_forecast_record()`/`find_all_forecasts()` (JSON-fält, timestamps).

## 8. `dashboard/api.py` — exakt integration

**`GET /api/performance`** (samma path/metod, ny kropp):
```python
@app.get("/api/performance")
def performance() -> dict:
    repo = repo_factory()
    positions = repo.find_closed_positions()
    pnls = trade_pnls(positions)
    return {
        "trade_count": len(pnls),
        "cumulative_pnl": str(compute_cumulative_pnl(pnls)),
        "win_rate": _optional_str(compute_win_rate(pnls)),
        "expectancy": _optional_str(compute_expectancy(pnls)),
        "profit_factor": _optional_str(compute_profit_factor(pnls)),
        "max_drawdown": _optional_str(compute_drawdown(positions)),
        "equity_curve": compute_equity_curve(positions),
        "by_instrument": _stringify_breakdown(compute_breakdown_by_instrument(positions)),
        "by_direction": _stringify_breakdown(compute_breakdown_by_direction(positions)),
    }
```
(`_optional_str`/`_stringify_breakdown` är triviala lokala serialiseringshjälpare i `dashboard/api.py`, samma stil som befintlig `_position_summary()` — ingen ny affärslogik, bara `Decimal`→`str`.)

**`GET /api/forecast`** (samma path/metod/paginerade `forecasts`-lista oförändrad, `calibration`-fältet byter shape):
```python
@app.get("/api/forecast")
def forecast(limit=..., offset=...) -> dict:
    repo = repo_factory()
    forecasts_page = [_forecast_summary(f) for f in repo.find_all_forecasts(limit, offset)]
    scored = repo.find_forecasts_with_outcome()  # obegränsat, egen fråga - inte samma som den paginerade listan ovan
    min_n = settings.pipeline.min_sample_size_for_calibration
    prelim_n = settings.pipeline.calibration_preliminary_sample_size
    brier = compute_brier_score(scored)
    return {
        "forecasts": forecasts_page,
        "calibration": {
            "brier_score": {
                **brier,
                "calibration_status": compute_calibration_status(brier["sample_size"], min_n, prelim_n),
            },
            "calibration_curve": compute_calibration_curve(scored, min_n, prelim_n),
            "breakdown_by_horizon": compute_calibration_breakdown_by_horizon(scored, min_n, prelim_n),
            "breakdown_by_scenario": compute_calibration_breakdown_by_scenario(scored, min_n, prelim_n),
        },
    }
```
**Explicit:** `scored` (obegränsad `find_forecasts_with_outcome()`) är en **separat** fråga från den paginerade `forecasts`-listan (`find_all_forecasts(limit, offset)`) — kalibrering ska alltid räknas över **hela** historiken, oavsett vilken sida av forecast-listan klienten råkar visa.

## 9. TDD-plan / testmatris (samtliga edge cases från uppdraget)

| Test | Fil | Verifierar |
|---|---|---|
| `test_trade_pnls_filters_to_closed_only` | `test_metrics.py` | En OPEN_POSITION i indata exkluderas, kraschar inte |
| `test_zero_trades_gives_none_metrics_and_zero_cumulative` | `test_metrics.py` | `win_rate`/`expectancy`/`profit_factor`/`drawdown` → `None`; `cumulative_pnl` → `Decimal("0")`; `equity_curve`/breakdowns → tomma |
| `test_all_wins_gives_profit_factor_none` | `test_metrics.py` | Inga förluster → `None` (odefinierat), inte `Infinity` |
| `test_all_losses_gives_profit_factor_zero` | `test_metrics.py` | Inga vinster → `Decimal("0")` (giltigt tal, skiljs från all-wins-fallet) |
| `test_breakeven_trade_counts_toward_total_not_wins` | `test_metrics.py` | pnl==0 räknas i nämnare, aldrig i täljare för win_rate |
| `test_drawdown_handcalculated_example` | `test_metrics.py` | Handräknad sekvens (t.ex. +10,+5,-20,+3) → exakt känt max drawdown |
| `test_drawdown_single_winning_trade_is_zero_not_none` | `test_metrics.py` | En trade, ingen nedgång → `Decimal("0")`, inte `None` |
| `test_equity_curve_is_chronological_regardless_of_input_order` | `test_metrics.py` | Positions skickas in i FEL ordning, output ändå sorterat på `closed_at` |
| `test_breakdown_by_direction_handles_a_manually_constructed_short_position` | `test_metrics.py` | Bevisar riktningsagnostisk kod (LONG vs SHORT), trots att pipelinen idag bara producerar LONG |
| `test_breakdown_by_instrument_separates_two_instruments` | `test_metrics.py` | Två instrument, separata delresultat |
| `test_performance_summary_is_deterministic_on_repeated_calls` | `test_metrics.py` | Determinism (§4) |
| `test_brier_score_matches_a_handcalculated_example` | `test_brier_score.py` | AC1: känt facit, t.ex. ett forecast med {"bullish":0.6,"bearish":0.4}, actual="bullish" → BS=(0.4)²+(0.4)²=0.32 |
| `test_brier_score_excludes_forecast_with_unmatched_actual_outcome` | `test_brier_score.py` | actual_outcome="unknown" (inte en av forecastens egna nycklar) → `excluded_count=1`, exkluderad ur `value` |
| `test_brier_score_is_none_when_sample_size_zero` | `test_brier_score.py` | Tom lista → `value: None, sample_size: 0` |
| `test_calibration_status_boundaries` | `test_calibration_curve.py` | AC2: exakt vid `preliminary_sample_size-1`→insufficient, `preliminary_sample_size`→preliminary, `min_sample_size`→calibrated (tre gränstester, config-värden skickas in explicit) |
| `test_calibration_curve_bucket_matches_handcalculated_example` | `test_calibration_curve.py` | AC1: känt facit för en bucket |
| `test_calibration_curve_empty_bucket_has_none_not_zero` | `test_calibration_curve.py` | Tom bucket → `mean_predicted`/`observed_frequency` = `None`, aldrig `0.0` |
| `test_calibration_breakdown_by_horizon_separates_groups` | `test_calibration_curve.py` | Två horizon-strängar, separata Brier-resultat |
| `test_calibration_breakdown_by_scenario_separates_groups` | `test_calibration_curve.py` | Två scenario-nycklar, separata resultat |
| `test_calibration_summary_is_deterministic_on_repeated_calls` | `test_calibration_curve.py` | Determinism (§4) |
| `test_find_closed_positions_returns_only_closed_status` | `test_repository_performance_reads.py` | Repository-nivå |
| `test_find_closed_positions_returns_unbounded_beyond_dashboard_page_cap` | `test_repository_performance_reads.py` | Skapar >500 stängda positioner (Fas 7:s dashboard-pagineringstak), bevisar `find_closed_positions()` returnerar ALLA — inte begränsad av Fas 7:s `_MAX_PAGE_LIMIT` |
| `test_find_forecasts_with_outcome_excludes_null_outcome` | `test_repository_performance_reads.py` | Repository-nivå |
| `test_dashboard_performance_returns_real_numbers_for_seeded_closed_positions` | `test_dashboard_performance.py` (uppdaterad) | Integrationstest genom `/api/performance` |
| `test_dashboard_performance_zero_trades_gives_null_metrics_not_zero` | `test_dashboard_performance.py` (uppdaterad) | Samma zero-trades-disciplin genom hela HTTP-vägen |
| `test_dashboard_forecast_calibration_is_insufficient_data_by_default` | `test_dashboard_forecast.py` (uppdaterad) | Eftersom `actual_outcome` aldrig fylls i av någon kod (§0), är detta det GARANTERADE default-läget — explicit testat, inte antaget |
| `test_dashboard_forecast_calibration_reflects_a_manually_seeded_outcome` | `test_dashboard_forecast.py` (uppdaterad) | En test-seedad `actual_outcome` (satt direkt via `save_forecast_record`/rå UPDATE i testet — INTE via någon produktionsmekanism, som inte finns) visas korrekt genom hela HTTP-vägen |
| (§4) `test_equity_curve_never_includes_a_position_still_open` | `test_metrics.py` | No-look-ahead/determinism |
| (§4) `test_calibration_excludes_forecast_with_outcome_after_an_explicit_as_of_cutoff` | `test_calibration_curve.py` | No-look-ahead/determinism |

**Befintliga tester som måste förbli gröna, oförändrade:** hela `tests/crypto_trading/paper_trading/`, `tests/crypto_trading/storage/test_repository_forecast_record.py`, `tests/crypto_trading/storage/test_repository_position*.py`, `tests/crypto_trading/notify/`, `tests/crypto_trading/dashboard/test_dashboard_security.py`, `test_dashboard_pagination.py`, `test_dashboard_live.py`, `test_dashboard_trade_history.py`, `test_dashboard_system_health.py`, `test_dashboard_frontend.py`, `tests/crypto_trading/test_run_bootstrap.py`, `tests/crypto_trading/test_run_thread_safety.py`, `tests/crypto_trading/test_no_intelligence_coupling.py`.

**Medvetet ändrade (inte "förblir gröna oförändrade"):** `test_dashboard_performance.py`, `test_dashboard_forecast.py` — de gamla platshållar-assertionerna ersätts, som redan flaggat.

## 10. Uppgiftsindelning (Task 1–9)

- **Task 1:** Repository — `find_closed_positions`, `find_forecasts_with_outcome` (TDD, se §7/§9).
- **Task 2:** `performance/metrics.py` — kärntal (`trade_pnls`, `compute_cumulative_pnl`, `compute_win_rate`, `compute_expectancy`, `compute_profit_factor`) + zero/all-wins/all-losses/break-even-tester.
- **Task 3:** `performance/metrics.py` — `compute_drawdown`, `compute_equity_curve` + handräknat exempel + kronologi-oberoende-av-indataordning-test.
- **Task 4:** `performance/metrics.py` — `compute_breakdown_by_instrument`, `compute_breakdown_by_direction` + SHORT-test.
- **Task 5:** `calibration/brier_score.py` + handräknat AC1-exempel + exkluderingstest.
- **Task 6:** `calibration/calibration_curve.py` — `compute_calibration_status`, `compute_calibration_curve`, breakdown-funktionerna + gränstester (AC2).
- **Task 7:** `dashboard/api.py::/api/performance`-integration + uppdaterade tester.
- **Task 8:** `dashboard/api.py::/api/forecast`-integration + uppdaterade tester.
- **Task 9:** Determinism/no-look-ahead-tester (§4), full regression (`tests/crypto_trading/` + hela repo), `ruff check`/`format --check`, `git diff --check`, `intelligence/`-diff, självständig diffgranskning, rapport — **ingen commit/push utan explicit godkännande**.

Varje task följer samma TDD-disciplin som Fas 7: skriv testet, kör det rött, implementera minimalt, kör grönt, regression, `ruff`, commit-redo (men ingen faktisk commit förrän hela planen är klar och godkänd).

---

## Self-review (utfört innan planen sparas)

**Spec-täckning:** Brier score ✅ (Task 5), calibration-kurva/buckets ✅ (Task 6), `CalibrationStatus` alltid med sample size ✅ (§6/Task 6), nedbrytning per horizon/scenario ✅ (Task 6), performance-måtten i sin helhet ✅ (Task 2–4), dashboard-integration ✅ (Task 7–8), determinism/no-look-ahead ✅ (§4/Task 9).

**Placeholder-scan:** inga TBD/TODO. Varje funktions None/0-hanteringsregel är explicit specificerad i §5/§6 med motivering, inte vagt beskriven.

**Typkonsekvens:** samtliga funktionssignaturer i §5/§6/§7 är de som Task 2–8 implementerar ordagrant. `min_sample_size`/`preliminary_sample_size`-parameternamnen är identiska genom `calibration_curve.py` och `dashboard/api.py`s anrop.

**Scope-kontroll:** ingen ändring i `intelligence/`, `notify/`, `notify_loop.py`, `agents/`, `gate/`, `screening/`, `state_machine.py`, `discovery_loop.py`, `monitoring_loop.py`, eller paper-trading-beslutslogiken. Ingen `actual_outcome`-ifyllning, ingen ny prishistorik, ingen ny bakgrundstråd (§0, hårt beslutat). Ingen schemaändring. `compute_pnl()` enda PnL-källan, aldrig omimplementerad.
