# Crypto Trading — Phase 4 (Paper Trading + Historical Replay) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Status: EJ PÅBÖRJAD (skriven 2026-08-26)

Fas 3 är avslutad och mergad till `master` (commit `e843444`). Denna plan väntar på användarens granskning och godkännande innan någon kod skrivs eller något test körs. Ingen exekvering har startat.

---

**Goal:** Ta en `CONFIRMED`-candidate (Fas 3:s slutresultat) hela vägen till en simulerad paper-position: storleksberäkning, simulerad fill (entry/exit separerat från teoretiskt pris), fees/funding, SL/TP/tidsgräns-övervakning med konservativ gap-hantering, och en **historisk replay** som kedjar ihop hela pipelinen (eligibility → screener → candidate engine → AI-team/gates → paper trading) mot redan hämtad historisk data och bevisar determinism och look-ahead-bias-frihet. Fortsatt noll broker-koppling, noll riktiga ordrar — allt är bokföring mot simulerade priser.

**Architecture:** Nytt lager `paper_trading/`, byggt ovanpå redan befintliga scheman/tabeller från Fas 0 (`schemas/trade.py`s `Position`, `storage/db.py`s `positions`-tabell — båda oanvända fram till nu) och Fas 3:s `orchestrator.run_discovery_cycle()` som entry point för `CONFIRMED`-candidates. `paper_trading/position_sizing.py` och `paper_trading/execution.py` är rena, deterministiska funktioner (samma stil som Fas 2:s `quant_screener.py`) — ingen databas, inga sidoeffekter. `paper_trading/monitoring.py` är också ren logik (pris in, beslut ut) — repository-skrivningen sker i en separat wiring-funktion, samma separation som Fas 2:s `candidate_engine.py` höll mellan ren bedömning och persistens.

**Tech Stack:** Python 3.13, `pydantic` v2, `pytest`. Inga nya beroenden.

**Spec:** `SPEC_CRYPTO.md` §8.4 (look-ahead-bias), §8.6 (idempotens), §8.7 (rekonstruerbarhet), §11 (Paper Trading — konto/sizing/execution/gap-hantering/monitoring/replay). `PLAN_CRYPTO.md` Phase 4-avsnittet (Omfattning/Levererar/Acceptance criteria 1–6, citerade i respektive tasks nedan).

## Vad som redan finns (Fas 0–3) — återanvänds rakt av

- **`schemas/trade.py`**: `Position` (alla SPEC §11-fält redan där: `theoretical_entry`/`simulated_fill_entry`, `theoretical_exit`/`simulated_fill_exit` separerade, `fees`, `funding`, `fill_model_version`, `exit_reason`) och `Direction = Literal["LONG", "SHORT"]` — definierade i Fas 0, aldrig använda.
- **`storage/db.py`**: `positions`-tabellen med exakt dessa kolumner, provisionerad i Fas 0, aldrig skriven till.
- **`storage/repository.py`**: `count_open_positions()` (Fas 3) — läser redan `positions`-tabellen, men ingen metod skriver till den ännu.
- **`config/loader.py` → `RiskLimitsConfig`**: `starting_capital_usdt`, `risk_per_trade_pct`, `max_concurrent_positions`, `max_total_exposure_pct`, `spread_pct`, `slippage_pct`, `fee_pct` — alla redan definierade och validerade (Fas 0), tillräckliga för sizing/execution utan nya fält förutom en (se nedan).
- **`RiskAssessment.suggested_stop_loss`/`suggested_target`** (Fas 3, per-candidate, redan persisterade via `save_assessment`) — rådgivande underlag för position sizing.
- **`orchestrator.run_discovery_cycle()`** (Fas 3) — lämnar en candidate i status `CONFIRMED` (terminal). Detta är entry point:en Fas 4 hakar i.
- **`state_machine.py`**: `CandidateStatus.CONFIRMED` redan terminal (`frozenset()`, inga vidare candidate-övergångar) — position-livscykeln är en helt separat state machine (`PositionStatus: OPEN_POSITION → CLOSED`, redan i `schemas/common.py`), inget att ändra där.
- **`screening/eligibility_filter.py` + `screening/quant_screener.py` + `screening/candidate_engine.py`** (Fas 2) — opåverkade, konsumeras direkt av `replay.py`.

## Vad som saknas — byggs i denna fas

- Hela `paper_trading/`-paketet (existerar inte i `crypto_trading/` ännu): `position_sizing.py`, `execution.py`, `monitoring.py`, `replay.py`.
- Repository-utökning: `create_position_with_event`, `get_position`, `find_open_positions`, `close_position_with_event`.
- Ett nytt konfigurationsfält för tidsgräns (se nedan — saknas helt idag).
- Wiring: `CONFIRMED`-candidate → öppnad `Position`; övervakningsutlöst exit → stängd `Position`.
- **En tidigare osynlig lucka, viktig för `replay.py` (AC1/AC2):** ingenting i Fas 2–3 kedjar faktiskt ihop `screening/` (Fas 2) med `orchestrator.run_discovery_cycle()` (Fas 3) mot verklig/historisk marknadsdata — varje fas testades isolerat mot sina egna fixtures. `replay.py` blir den första koden som binder samman hela kedjan `eligibility → Top N → quant_screener → candidate_engine → run_discovery_cycle → paper_trading` i en enda körning.

## Beslut som fattats för att hålla Fas 4 minimal (flaggade för din granskning, inte blockerande)

1. **Riktning: bara LONG i denna fas.** De sju AI-rollerna (Fas 3, SPEC §6) innehåller bara en `Bull/Thesis Agent` — ingen roll genererar en självständig "kort"-tes; `Bear/Adversarial Agent` är uteslutande motargument mot Bull-tesen, aldrig en egen riktningssignal. Arkitekturen som den faktiskt är byggd (Fas 0–3) kan därför strukturellt bara producera long-uppslag. Fas 4 sätter `direction="LONG"` för varje öppnad position — `Direction`-typen behåller `SHORT` som ett giltigt schemavärde (oanvänt tills en framtida fas eventuellt lägger till en kort-tes-genererande roll, vilket INTE är Fas 4:s jobb). Dashboard/Telegrams "LONG vs SHORT"-fält (§12/§13, senare faser) fortsätter fungera oförändrat — bara SHORT-grenen förblir tom tills vidare.
2. **"Tidsgräns" = ett nytt, enkelt konfigurationsfält, inte Forecast Agentens fritextfält `horizon`.** SPEC §11 nämner "SL/TP/tidsgräns/invalidation" men definierar aldrig tidsgränsens källa. Att tolka `ForecastAssessment.horizon` (fri text från en LLM, t.ex. "4h") som en deterministisk exit-trigger vore att låta AI-genererad text styra en kritisk risk-kontroll — i strid med SPEC §1 kärnprincip 1/3 (deterministisk kod ska göra det som kan vara deterministiskt). Löst genom ett nytt fält `max_position_hold_hours` i `risk_limits.yaml` (Task 1) — en egen strategiparameter, i samma anda som Fas 2:s screener-trösklar.
3. **"Invalidation" implementeras som samma sak som tidsgränsen i denna fas**, inte som en separat tredje exit-mekanism. SPEC ger inget konkret, testbart kriterium för "invalidation" utöver SL/TP/tidsgräns. Att hitta på en egen invalideringsregel utan stöd i SPEC vore en scope-utökning. `exit_reason` blir en av: `"stop_loss"`, `"target"`, `"time_limit"`.
4. **Konservativ gap-fill, symmetrisk regel:** stop-fill = `min(candle_low, stop_loss)` (aldrig bättre än stop, ofta sämre vid gap), target-fill = `min(candle_high, target)` (aldrig bättre än target, oavsett hur högt priset gappade). Båda uttrycken degenererar korrekt till exakt SL/TP-nivån när candle:n bara nuddade nivån utan gap — samma formel täcker både det vanliga fallet och gap-fallet.
5. **`replay.py` konsumerar redan hämtad historisk data (dependency injection)** — bygger INTE ett eget system för att paginera/backfilla stora historiska datamängder från BingX. `BingXMarketDataConnector.get_klines(symbol, interval, limit)` (Fas 1) räcker för de små, handkonstruerade test-fixtures denna fas TDD-verifierar mot. Storskalig historisk backfill är en separat, senare fråga (inte en Fas 4-leverans per `PLAN_CRYPTO.md`s ordalydelse, som bara kräver att kedjan är bevisligen deterministisk och bias-fri — inte att den kan replay:a månader av data).

**Explicit utanför scope (nästa faser, inte Fas 4):** den faktiska schemalagda live-loopen (`monitoring.py` "redo att köras i den tätare loopen (Phase 5)" — Fas 4 bygger bara den rena logiken, ingen `while True`/cron-wiring), Telegram (§12), dashboard (§13), forecast-kalibrering (§9/Fas 8), storskalig historisk backfill (se ovan).

## Global Constraints

- **Ingen broker/order-exekvering, någonsin.** `paper_trading/` ansluter aldrig till ett konto, lägger aldrig en riktig order — samma hårda gräns som SPEC §1, verifierad av befintlig `test_no_intelligence_coupling.py::test_crypto_trading_has_no_broker_account_or_order_code` (glob:ar hela `crypto_trading/`, fångar Fas 4:s filer automatiskt).
- **`theoretical_*` och `simulated_fill_*` är alltid separata fält, aldrig sammanslagna** (AC4) — `execution.py` skriver båda explicit, aldrig bara den ena.
- **Look-ahead-bias-fritt genomgående** (§8.4): `replay.py` skickar bara data med `observed_at <= simulated_now` in i varje pipeline-steg — samma disciplin som Fas 2:s `quant_screener._sorted_up_to()`, nu bevisad end-to-end.
- **Idempotens** (§8.6): `position_id = candidate_id` — en candidate kan bara nå `CONFIRMED` en gång (terminal state, Fas 0), så detta räcker för att garantera högst en `Position` per candidate utan en separat hash-funktion. `create_position_with_event` använder samma `INSERT OR IGNORE`-mönster som `create_candidate_with_event`.
- Config-drivna trösklar (`risk_limits.yaml`), aldrig hårdkodade i Python.
- `intelligence/` rörs inte. `ruff` line-length 100, regler `E,F,I,UP,B`.

---

## Task 1: Config — `max_position_hold_hours`

**Files:**
- Modify: `crypto_trading/config/loader.py`
- Modify: `crypto_trading/config/risk_limits.yaml`
- Modify: `tests/crypto_trading/config/test_loader.py`

**Interfaces:** `RiskLimitsConfig.max_position_hold_hours: int = Field(gt=0)`.

- [ ] **Step 1: Write the failing tests** — lägg till i den befintliga `_valid_...`-hjälparen för `RiskLimitsConfig` (om ingen finns, skapa en i samma stil som Fas 2/3:s `_valid_pipeline_kwargs`) plus:

```python
def test_get_settings_loads_phase4_fields():
    settings = get_settings()
    assert settings.risk_limits.max_position_hold_hours > 0


def test_risk_limits_config_rejects_zero_max_position_hold_hours():
    with pytest.raises(ValidationError):
        RiskLimitsConfig(**_valid_risk_limits_kwargs(max_position_hold_hours=0))
```

- [ ] **Step 2: Run tests to verify they fail** — `pytest tests/crypto_trading/config/test_loader.py -v`, förväntat `AttributeError`/`ValidationError`-brist.
- [ ] **Step 3: Lägg till i `risk_limits.yaml`:** `max_position_hold_hours: 24` (strategiparameter, inte SPEC-verifierat faktum — matchar screener_timeframes 1h/4h med en konservativ övre gräns).
- [ ] **Step 4: Lägg till fältet i `RiskLimitsConfig`.**
- [ ] **Step 5: Run tests to verify they pass.**

---

## Task 2: Repository — Position-persistens

**Files:**
- Modify: `crypto_trading/storage/repository.py`
- Create: `tests/crypto_trading/storage/test_repository_position.py`

**Interfaces:**
- Produces: `Repository.create_position_with_event(position: Position, event: Event) -> bool`, `get_position(position_id: str) -> Position | None`, `find_open_positions() -> list[Position]`, `close_position_with_event(position_id, theoretical_exit, simulated_fill_exit, exit_reason, fees, funding, closed_at, event) -> None`.

- [ ] **Step 1: Write the failing tests** — samma stil som `test_repository_candidate.py`: `test_create_position_with_event_persists_both`, `test_create_position_with_event_is_idempotent_on_retry` (AC6, direkt), `test_get_position_returns_none_when_missing`, `test_find_open_positions_returns_only_open_status`, `test_close_position_with_event_updates_exit_fields_and_status`, `test_close_position_with_event_is_atomic_on_failure` (samma `_FailingConnection`-mönster som candidate-testerna).
- [ ] **Step 2: Run tests to verify they fail.**
- [ ] **Step 3: Implement** — `INSERT OR IGNORE`/`UPDATE` mot `positions`-tabellen, samma struktur som `create_candidate_with_event`/`transition_candidate_with_event`. Ingen ny korrupt-state-hantering krävs utöver vad `Position`s egen Pydantic-validering redan ger (positions har ingen motsvarighet till `CandidateEvidenceRecord`s fria-formatsfält, så samma djupa korrupt-state-täckning som candidates är inte nödvändig — en enklare `Position.model_validate(dict(row))` räcker).
- [ ] **Step 4: Run tests to verify they pass.**

---

## Task 3: `paper_trading/position_sizing.py`

**Files:**
- Create: `crypto_trading/paper_trading/__init__.py`
- Create: `crypto_trading/paper_trading/position_sizing.py`
- Create: `tests/crypto_trading/paper_trading/__init__.py`
- Create: `tests/crypto_trading/paper_trading/test_position_sizing.py`

**Interfaces:** `compute_position_size(entry_price: Decimal, stop_loss_price: Decimal, capital: Decimal, risk_per_trade_pct: Decimal, open_positions_notional: Decimal, max_total_exposure_pct: Decimal) -> Decimal`.

- [ ] **Step 1: Write the failing tests** — handräknat exempel (AC3-anda även här, även om AC3 formellt gäller fees/funding/slippage): entry=50000, stop=49000 (2% stop-avstånd), capital=10000, risk_per_trade_pct=0.01 → risk_amount=100, size=100/0.02=5000. Plus: `test_position_size_capped_by_remaining_exposure` (redan 2000 av max 2500 exponering använt → storlek klipps till 500), `test_position_size_is_zero_for_degenerate_zero_distance_stop` (fail-closed).
- [ ] **Step 2: Run tests to verify they fail.**
- [ ] **Step 3: Implement** — ren funktion enligt formeln i plan-headern.
- [ ] **Step 4: Run tests to verify they pass.**

---

## Task 4: `paper_trading/execution.py` (AC3, AC4)

**Files:**
- Create: `crypto_trading/paper_trading/execution.py`
- Create: `tests/crypto_trading/paper_trading/test_execution.py`

**Interfaces:** `compute_fill_price(reference_price, direction, spread_pct, slippage_pct, side: Literal["entry","exit"]) -> Decimal`, `compute_fees(fill_price, size, fee_pct) -> Decimal`, `compute_funding(size, funding_rate, hold_hours) -> Decimal`, `_FILL_MODEL_VERSION = "v1"`.

- [ ] **Step 1: Write the failing tests** — handräknade exempel (AC3, explicit i testnamnen):

```python
def test_compute_fill_price_long_entry_is_worse_than_reference():
    # spread+slippage = 0.001, entry LONG betalar MER
    price = compute_fill_price(Decimal("50000"), "LONG", Decimal("0.0005"), Decimal("0.0005"), "entry")
    assert price == Decimal("50000") * Decimal("1.001")


def test_compute_fees_matches_hand_calculation():
    fees = compute_fees(fill_price=Decimal("50050"), size=Decimal("5000"), fee_pct=Decimal("0.0004"))
    assert fees == Decimal("5000") * Decimal("0.0004")  # fee räknas på notional, inte fill_price*size


def test_compute_funding_matches_hand_calculation():
    # 16h hold = 2 st 8h-funding-perioder
    funding = compute_funding(size=Decimal("5000"), funding_rate=Decimal("0.0001"), hold_hours=Decimal("16"))
    assert funding == Decimal("5000") * Decimal("0.0001") * 2
```

Plus: `test_theoretical_and_simulated_fill_are_never_equal_when_spread_or_slippage_nonzero` (AC4, direkt).

- [ ] **Step 2: Run tests to verify they fail.**
- [ ] **Step 3: Implement** enligt formlerna i plan-headern. Dokumentera explicit i docstring: `compute_funding` samplar en enda funding rate vid positionens öppning och multiplicerar med antal 8h-perioder — en medveten förenkling (verklig funding rate fluktuerar var 8:e timme; att modellera det skulle kräva en tidsserie av funding-observationer under hela hålltiden, utanför denna fas scope).
- [ ] **Step 4: Run tests to verify they pass.**

---

## Task 5: `paper_trading/monitoring.py` — Del A: SL/TP/tidsgräns (exakt träff, ingen gap)

**Files:**
- Create: `crypto_trading/paper_trading/monitoring.py`
- Create: `tests/crypto_trading/paper_trading/test_monitoring.py`

**Interfaces:** `check_exit_trigger(position: Position, candle_low: Decimal, candle_high: Decimal, now: datetime, max_position_hold_hours: int) -> tuple[str, Decimal] | None` (returnerar `(exit_reason, trigger_price)` eller `None`).

- [ ] **Step 1: Write the failing tests** — `test_no_trigger_when_price_stays_within_range`, `test_stop_loss_triggers_at_exact_touch`, `test_target_triggers_at_exact_touch`, `test_time_limit_triggers_after_max_hold_hours`, `test_stop_loss_checked_before_time_limit_when_both_true` (deterministisk prioritetsordning — SL/TP kollas alltid före tidsgräns, dokumenterat i docstring).
- [ ] **Step 2: Run tests to verify they fail.**
- [ ] **Step 3: Implement** (LONG-only, se Global Constraints/beslut 1 — funktionen tar ingen `direction`-parameter i denna fas, hårdkodat LONG-beteende, dokumenterat i docstring varför).
- [ ] **Step 4: Run tests to verify they pass.**

---

## Task 6: `paper_trading/monitoring.py` — Del B: konservativ gap-fill (AC5)

**Files:**
- Modify: `crypto_trading/paper_trading/monitoring.py`
- Modify: `tests/crypto_trading/paper_trading/test_monitoring.py`

**Interfaces:** samma `check_exit_trigger`, nu med de faktiska gap-fill-formlerna från Global Constraints/beslut 4 istället för platshållare.

- [ ] **Step 1: Write the failing tests** — `test_gap_through_stop_loss_fills_at_candle_low_not_stop_level` (candle_low långt under stop → trigger_price == candle_low, `!=` stop_loss), `test_gap_through_target_fills_at_target_not_candle_high` (candle_high långt över target → trigger_price == target, `!=` candle_high), `test_fill_model_version_is_set_on_the_resulting_position` (verifieras i Task 8:s wiring-test, refereras här som påminnelse).
- [ ] **Step 2: Run tests to verify they fail.**
- [ ] **Step 3: Implement** — `min(candle_low, stop_loss)` / `min(candle_high, target)` enligt beslut 4.
- [ ] **Step 4: Run tests to verify they pass.**

---

## Task 7: `CONFIRMED` → öppnad `Position` (wiring + AC6)

**Files:**
- Create: `crypto_trading/paper_trading/position_opening.py`
- Create: `tests/crypto_trading/paper_trading/test_position_opening.py`

**Interfaces:** `open_position_for_candidate(candidate: Candidate, repo: Repository, risk_limits: RiskLimitsConfig, reference_price: Decimal, funding_rate: Decimal, opened_at: datetime, run_id: str) -> Position | None` (returnerar `None` om `candidate.status != "CONFIRMED"` eller om `candidate.risk` saknas — defensivt, ska aldrig hända givet Fas 3:s garantier, men fail-closed snarare än att krascha).

- [ ] **Step 1: Write the failing tests** — `test_opens_position_with_theoretical_and_simulated_fields_separated`, `test_position_id_equals_candidate_id`, `test_calling_twice_for_same_candidate_creates_only_one_position` (AC6, explicit dubbel-anrop-test), `test_returns_none_when_candidate_not_confirmed`, `test_direction_is_always_long` (dokumenterar beslut 1 som ett levande test, inte bara en kommentar).
- [ ] **Step 2: Run tests to verify they fail.**
- [ ] **Step 3: Implement** — parsar `candidate.risk.suggested_stop_loss`/`suggested_target` (strängar) till `Decimal`, anropar `position_sizing.compute_position_size` (med `repo`-läst `open_positions_notional`, se nedan) och `execution.compute_fill_price`, skriver via `repo.create_position_with_event`.

  **Notera:** `open_positions_notional` kräver en summa av `size` över alla öppna positioner — `count_open_positions()` (Fas 3) räcker inte (den räknar bara antal, inte notional-summa). Lägg till en liten hjälpmetod i samma task: `Repository.sum_open_positions_notional() -> Decimal`, med eget litet Red/Green-steg innan `open_position_for_candidate` skrivs.

- [ ] **Step 4: Run tests to verify they pass.**

---

## Task 8: Övervakningsutlöst exit → stängd `Position`

**Files:**
- Create: `crypto_trading/paper_trading/position_closing.py`
- Create: `tests/crypto_trading/paper_trading/test_position_closing.py`

**Interfaces:** `close_triggered_positions(repo: Repository, price_lookup: dict[str, tuple[Decimal, Decimal]], now: datetime, risk_limits: RiskLimitsConfig, run_id: str) -> list[Position]` — itererar `repo.find_open_positions()`, kör `monitoring.check_exit_trigger` per position mot dess instruments `(low, high)` i `price_lookup`, stänger de som triggar via `execution.compute_fill_price(..., side="exit")` + `compute_fees`/`compute_funding`, skriver via `repo.close_position_with_event`.

- [ ] **Step 1: Write the failing tests** — `test_closes_position_on_stop_loss_trigger_with_correct_exit_reason`, `test_closes_position_on_time_limit_trigger`, `test_leaves_position_open_when_nothing_triggers`, `test_closing_is_idempotent_when_called_twice` (positionen är redan `CLOSED` andra gången — ingen dubbel `CLOSED`-event, SPEC §8.6).
- [ ] **Step 2: Run tests to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests to verify they pass.**

---

## Task 9: `paper_trading/replay.py` — hela kedjan mot historisk fixture (AC1)

**Files:**
- Create: `crypto_trading/paper_trading/replay.py`
- Create: `tests/crypto_trading/paper_trading/test_replay.py`

**Interfaces:** `run_replay(historical_snapshots: list[MarketSnapshot], repo: Repository, runner: AgentRunner, settings: Settings, run_id: str) -> list[Position]`, där `MarketSnapshot` är en liten dataclass/BaseModel (`simulated_now: datetime`, `tickers: dict[str, Ticker]`, `klines: dict[str, list[Kline]]`, `funding_rates: dict[str, list[FundingRate]]`) — en handkonstruerad tidsordnad lista, inte hämtad live i denna fas (se Global Constraints/beslut 5).

Kör per snapshot, i tidsordning: (1) `eligibility_filter` + `select_top_n`, (2) `quant_screener.evaluate_candidate` per instrument (med `evaluated_at=snapshot.simulated_now`), (3) `candidate_engine.process_evidence` + `prioritize_and_apply_budget`, (4) `orchestrator.run_discovery_cycle`, (5) för varje ny `CONFIRMED`: `position_opening.open_position_for_candidate`, (6) `position_closing.close_triggered_positions` mot samma snapshots pris.

- [ ] **Step 1: Write the failing tests** — en liten, handkonstruerad 3-stegs historisk fixture (t.ex. BTCUSDT: steg 1 flat, steg 2 pris-spik som triggar `worth_deeper_analysis` → `CONFIRMED`, steg 3 pris rör sig till target). `test_replay_produces_a_confirmed_position_and_closes_it_at_target`, `test_replay_is_deterministic_on_repeated_runs` (AC1, explicit: kör `run_replay` två gånger mot separata, färska `repo`-instanser med identisk fixture, jämför resulterande `Position`-listor fältvis, exklusive genererade ID:n som redan är deterministiska via `position_id=candidate_id`).
- [ ] **Step 2: Run tests to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests to verify they pass.**

---

## Task 10: Look-ahead-bias-test för hela kedjan (AC2)

**Files:**
- Modify: `tests/crypto_trading/paper_trading/test_replay.py`

**Interfaces:** inga nya — rent testtillägg mot `run_replay`.

- [ ] **Step 1: Write the failing test** — `test_replay_decision_at_time_t_is_unaffected_by_injected_future_data`: kör samma fixture som Task 9, men injicera en extra, kraftigt avvikande kline daterad **efter** den sista `simulated_now` i en av snapshots klines-listor (simulerar att en framtida datapunkt av misstag hamnat i en tidigare hämtning). Jämför resultatet mot en körning utan den injicerade framtidspunkten — identiskt resultat.
- [ ] **Step 2: Run test to verify it fails** (eller passerar direkt om `quant_screener`s befintliga `_sorted_up_to`-skydd redan räcker end-to-end — i så fall är detta test en ren regressionsbekräftelse, ingen ny produktionskod).
- [ ] **Step 3: Fix any discovered gap, then confirm passing.**

---

## Task 11: Fullständigt livscykel-integrationstest

**Files:**
- Create: `tests/crypto_trading/test_phase4_integration.py`

**Interfaces:** inga nya.

- [ ] **Step 1: Write the failing test** — `test_full_lifecycle_candidate_confirmed_position_opened_and_closed`: från en redan `CONFIRMED`-candidate (byggd med samma hjälpmönster som Fas 3:s `test_phase3_integration.py`) genom `open_position_for_candidate` → `close_triggered_positions` (SL-scenario) → verifiera slutgiltig `Position`-rad i repot har `status="CLOSED"`, `exit_reason="stop_loss"`, `simulated_fill_exit != theoretical_exit`, `fees is not None`, `funding is not None`.
- [ ] **Step 2: Run test to verify it fails.**
- [ ] **Step 3: Confirm passing** (bör vara grönt direkt om Task 1–10 är korrekt implementerade — rent regressions-/AC-bekräftelsetest).

---

## Task 12: Slutverifiering

**Files:** inga (bara verifieringskommandon).

- [ ] **Step 1: Full testsvit för crypto_trading** — `pytest tests/crypto_trading/ -v`, alla gröna.
- [ ] **Step 2: Ruff check + format** — `ruff check crypto_trading/ tests/crypto_trading/`, `ruff format --check crypto_trading/ tests/crypto_trading/`, inga fel/diff.
- [ ] **Step 3: Verifiera att intelligence/ fortfarande är orört** — `git diff master -- intelligence/`, tom output.
- [ ] **Step 4: Full repo-testsvit** — `pytest -v`, ingen regression.
- [ ] **Step 5: Importgräns och broker-frihet** — `pytest tests/crypto_trading/test_no_intelligence_coupling.py -v`, PASS (fångar `paper_trading/` automatiskt).
- [ ] **Step 6: Grep-guard mot riktningsord utöver LONG** — `grep -rniE "\b(buy|sell|short)\b" crypto_trading/paper_trading/` (LONG är avsiktligt tillåtet denna fas, se beslut 1) — ingen träff förväntad.
- [ ] **Step 7: Uppdatera PLAN_CRYPTO_PHASE4.md** — kryssa i alla `- [ ]`, lägg till statusbanner med exakt testantal och ev. avvikelser upptäckta under exekvering, samma format som Fas 1–3.

---

## Self-review (utfört innan planen sparas)

**Spec-täckning:** position sizing mot `risk_limits.yaml` (Task 3), execution med explicit separerade teoretiska/simulerade fält + fees/funding (Task 4, AC3/AC4), SL/TP/tidsgräns-monitoring (Task 5) med konservativ gap-hantering (Task 6, AC5), idempotent `CONFIRMED`→`Position`-wiring (Task 7, AC6), stängningswiring (Task 8), fullständig historisk replay-kedja (Task 9, AC1) med explicit look-ahead-bias-test (Task 10, AC2). Alla sex ACs från `PLAN_CRYPTO.md` täckta.

**Placeholder-scan:** inga TBD/TODO. Fem beslut (riktning/tidsgräns/invalidation/gap-formel/replay-datakälla) är explicit dokumenterade som medvetna, motiverade förenklingar — inte SPEC-avvikelser, flaggade för din granskning innan godkännande.

**Typkonsekvens:** `Position.direction` förblir `Literal["LONG","SHORT"]` (oförändrat schema, Fas 0) — Fas 4 producerar bara `"LONG"`-värden, ingen typändring. `exit_reason` är fri `str | None` (redan i schemat) — Fas 4 skriver bara `"stop_loss"`/`"target"`/`"time_limit"`, ingen schemaändring krävd.

**Scope-kontroll:** ingen Telegram-, dashboard- eller kalibreringskod. Ingen schemalagd/`cron`-liknande live-loop (bara den rena `monitoring.py`-logiken — schemaläggning är Fas 5). Ingen historisk-data-pagineringsinfrastruktur (`replay.py` konsumerar handkonstruerade fixtures, inte en BingX-backfill-pipeline). Ingen ny AI-roll för SHORT-teser. `intelligence/` refereras inte någonstans.
