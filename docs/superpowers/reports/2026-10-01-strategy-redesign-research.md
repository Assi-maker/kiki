# P4 – Strategy redesign research (2026-10-01)

## Slutsats

**NO ROBUST EDGE FOUND.**

- 1 484 förregistrerade celler (8 mekanismer, 104 händelsemängder; cell = familj × parametrar × sida × horisont). Cellerna är inte oberoende hypoteser: horisonterna är nästlade, FADE/FOLLOW är spegelbilder och trösklar/delmängder är nästlade.
- **0 celler klarade urvalet ens utan multipeltestskorrigering** (TRAIN p < 0,05 och VALID netto > 0 med p < 0,05). Slutsatsen beror alltså inte på hur testerna grupperas.
- **0 klarar** multiple-testing-korrektionen på TRAIN som också håller i VALID. 0 når TEST/HOLDOUT-steget. 0 SURVIVING EDGE.
- Inget har byggts. Ingen tradingkod, LIVE-execution, Safety Kernel, Guardian, max 4, max 1/symbol, %-gränser, leverage eller sizing har ändrats. GODFATHER är orörd.
- Boten är stoppad hela tiden. Task Scheduler: `crypto_trading_bot` och `crypto_trading_evidence_shadow` står kvar som **Disabled**.

| Steg | Antal |
|---|---|
| Celler testade (8 mekanismer, 104 händelsemängder) | 1 484 |
| TRAIN: netto > 0 (initialt positiva) | 326 |
| TRAIN: netto p < 0,05 | 8 |
| TRAIN: brutto p < 0,05 (riktningsinformation före kostnad) | 64 (≈ 74 väntas av ren slump om cellerna vore oberoende, vilket de inte är – jämförelsen är grov) |
| Efter Benjamini–Hochberg (q < 0,10) på TRAIN | 3 (alla F6, äkta likvidationer, **med timkluster**; se §16) |
| Efter Bonferroni (p < 0,05) på TRAIN | 2 (samma F6-celler) |
| + VALID (netto > 0, p < 0,05) | **0** (0 även utan någon korrektion) |
| + TEST och HOLDOUT | 0 |
| Håller i alla fyra perioderna och är signifikant | 0 |
| SURVIVING EDGE | **0** |

Urvalet frystes innan TEST/HOLDOUT bedömdes: `p4_selection.json` innehåller tom lista, sha256 `4f53cda1…b945`.

---

## 1. Varför nuvarande strategi förkastas

Det är redan visat i P1–P3, regime lab, exit lab och system review:

- **Ingen riktningsinformation.** 335 502 historiska signaler gav brutto ≈ 0 på 5m–4h och netto ≈ −0,13 till −0,19 % per trade.
- **Båda sidor förlorar.** LONG och SHORT är negativa i alla perioder.
- **Inga filter hjälper.** 0 av 710 regimfilter, 0 av 1 628 exitvarianter och 0 av 276 nya informationskällor klarar korrektion.
- **Förlusterna kommer från entry.** ~90 % av LIVE-förlusten kommer från trades som aldrig nådde +1 % MFE.

Signalen är i praktiken en slumpmässig entry som betalar 0,16 % per tur och retur. Att lägga fler features ovanpå den är därför meningslöst. P4 testar i stället helt andra marknadsmekanismer.

## 2. Ekonomisk hypotes för varje ny strategi

Varje familj har en förregistrerad mekanism, nedskriven i `p4_lab.py`/`p4_external.py` innan något utfall beräknades.

| # | Familj | Vilken aktörs beteende vi försöker exploatera | Test |
|---|---|---|---|
| F1 | **XS_REL** – cross-sectional relativ styrka (marknadsneutral) | **MOM:** trögt informationsflöde/uppmärksamhet ger att relativa vinnare fortsätter.<br>**REV:** oinformerat flöde trycker enskilda coins för långt, och likviditetsgivare får betalt när det reverterar. | Rangordna på avkastning 1h/4h/24h/72h/7d varje timme. Long topp-kvintil och short botten-kvintil (och omvänt). Benen LONG, SHORT och LS |
| F2 | **IDIO_SHOCK** – idiosynkratisk 15m-chock (mean reversion vs continuation) | **OI faller under chocken:** positioner tvingas ut, priskänslös likviditetsefterfrågan ger överskjutning, och FADE bör vinna.<br>**OI stiger:** ny positionering/information, och FOLLOW bör vinna. | Residualavkastning mot marknadsmedianen med \|z\| ≥ 3/4/6 och volym ≥ 3× 7d-snitt. Uppdelat på OI_DOWN/OI_UP/ALL, FADE och FOLLOW |
| F3 | **MKT_CASCADE** – marknadsbred tvångsavveckling | Kaskadlikvidationer i hela marknaden ger överskjutning, och market makers lager måste avvecklas | Universums median 1h ≤ −X eller ≥ +X (1,5/2,5/4 %), breadth ≥ 80 %, aggregerad OI −1 % eller ALL. Korgen handlas, FADE och FOLLOW |
| F4 | **BREAKOUT** – strukturell breakout med bekräftelse | Stopporder och trendföljare klustrar bortom range-extremer. Bekräftelse ska skilja verklig omprissättning från stop-runs | N-dagars range (3/7/14). Bekräftelse NONE, HOLD (1h stannar utanför + volym) eller RETEST (inom 0,3 %, invalideras vid 1 % tillbaka). Regim ALL/BTC_ALIGNED. FOLLOW samt FADE (falsk breakout) |
| F5 | **CROWDING** – positionering, carry | Trångt belånat läge betalar carry och är den sida som squeezas | Vid varje funding-settlement: funding, top-trader L/S, konto-L/S och OI-uppbyggnad × pris. CONTRA och FOLLOW, funding-P/L inkluderad |
| F6 | **REAL_LIQ** – äkta Binance-likvidationer | Tvångsorder är priskänslös efterfrågan och borde revertera | Likviderad notional i en 5m-bar ≥ θ × symbolens 24h-snittvolym (1/3/10 %, ≥ 10 kUSDT). FADE och FOLLOW |
| F7 | **OPTIONS_IV** – Deribit DVOL (extern) | Implicit vol är vad optionshandlare tar betalt för risk. Spike eller hög VRP betyder rädsla och hedgeefterfrågan (riskpremie), crush betyder självbelåtenhet | 24h DVOL-förändring som z ±1,5/2,5, eller VRP (IV − RV) i topp/botten 10 %. BTC/ETH egen tillgång samt korg, LONG och SHORT |
| F8 | **ONCHAIN_FLOW** – exchange in/out-flöden (extern) | Coins som skickas till börser ska säljas (supply), uttag är ackumulation | Coin Metrics dagliga flöden BTC/ETH, z ≥ 2 mot 90 d. LONG och SHORT |

Det som inte testades, och varför:

- **Nyheter:** DATA GAP, se §19.
- **Intradag on-chain:** DATA GAP.
- **BTC→alt lead-lag:** redan uttömt i P3 cross-exchange/spot-perp. Korrelationen i samma minut är 0,97–0,99 och laggen ≈ 0 efter 1 minut, vilket är under vår latens. Ingen mekanism kvar som vi kan nå.

## 3. Datakällor

| Källa | Timestamp / latens | Tillgänglighet | Historik | Kvalitet / brister |
|---|---|---|---|---|
| Binance USD-M 5m klines (data.binance.vision) | Bar känd vid stängning. Entry 5 min senare | Gratis | 2025-08-01 → 2026-09-30, 133 symboler (> 50 000 bars) | 3,1 % saknade bars. **Survivorship:** universum = dagens BingX-listade coins, vilket gynnar LONG. Negativa fynd blir därför starkare, inte svagare |
| Binance 5m metrics (OI, top-trader/konto-L/S) | Stämplad vid barens start och används först vid barens slut (1 bars lag) | Gratis | Samma | 3,8 % saknas |
| Binance funding | Används från settlement-tidpunkten | Gratis | Samma | Verklig funding ingår i varje trades netto |
| **Tardis.dev `binance-futures/liquidations`** (forceOrder) | ms-stämpel | **Gratis för den 1:a dagen varje månad** | 14 dagar (2025-08-01 … 2026-09-01), 312 079 likvidationer, 1,31 mdr USD | Binance sänder bara den **senaste** likvidationen per symbol och sekund, så summorna är en undre gräns i kaskader. Bara 14 dagar ger låg statistisk styrka |
| Deribit DVOL (public API) | Timbar används från timmens slut | Gratis | 2025-06 → 2026-09-30, BTC och ETH | Bra. Bara BTC/ETH |
| Coin Metrics community (exchange flows) | Dag D används från D+1 04:00 UTC (flash ~03:00) | Gratis **endast dagligt**. 1h ger HTTP 403 | 2025-04 → 2026-09 | Grov upplösning, 36–37 händelser |
| CryptoPanic (nyheter) | – | HTTP 403 utan nyckel | – | DATA GAP |

Inga syntetiska data har använts.

## 4. Testdesign (förregistrerad)

- **Entry-edge isolerat.** Fasta hållperioder 5m, 15m, 30m, 1h, 2h, 4h, 8h och 24h, utan SL, TP eller exitlogik. XS-familjerna testas på 30m–24h och F5 på 1h–24h.
- **Point-in-time.** Signal på bar k (känd vid barens slut), entry vid stängningen av bar k+1 (**5 min latens**). Känslighet: 15 min. Alla rullande fönster är bakåtblickande och exkluderar den aktuella baren. Testat i `tests/.../test_p4_lab.py` (11 tester): framåtfönstret läser aldrig entry-baren, DVOL-timmen används först efter stängning, och trailing z exkluderar sig själv.
- **Perioder** (efter entry-tid):

  | Period | Datum |
  |---|---|
  | TRAIN | 2025-08 → 2026-01 |
  | VALID | 2026-02 → 2026-04 |
  | TEST | 2026-05 → 2026-07 |
  | HOLDOUT | 2026-08 → 2026-09-29 |

  Entries vars 24h-fönster når in i nästa period är purgade.
- **Statistik.** Klustrad t (UTC-dag för horisont < 8h, vecka för ≥ 8h, timme för F6). Ensidigt test av NETTO > 0.
- **Multiple testing.** Benjamini–Hochberg över **alla 1 484 celler i alla familjer samtidigt**, plus Bonferroni som rapporteras.
- **Urval** sker enbart på TRAIN+VALID:
  - S1: BH q < 0,10 på TRAIN, med n ≥ 30 och ≥ 20 kluster.
  - S2: VALID netto > 0 och p < 0,05.
  - Urvalet frystes därefter (sha256).
- **SURVIVING EDGE kräver dessutom:**
  - S3: TEST > 0, HOLDOUT > 0 och poolad OOS p < 0,05.
  - S4: > 0 i alla fyra perioderna.
  - S5: ≥ 1 trade/dag och topp-5 symboler ≤ 50 % av P/L.
  - S6: > 0 vid 15 min latens.
  - S7: > 0 OOS vid 0,25 % kostnad.
  - S8: mekanism.

## 5–8. TRAIN, VALID, TEST, HOLDOUT

Genomsnittligt NETTO (% per trade) över alla celler i familj × sida, i ordningen TRAIN / VALID / TEST / HOLDOUT:

| Familj | Sida | TRAIN | VALID | TEST | HOLDOUT | Andel celler > 0 (T/V/T/H) |
|---|---|---|---|---|---|---|
| F1 XS_REL | LONG | −0,14 | −0,11 | −0,13 | +0,12 | 0,05 / 0,10 / 0,08 / 0,42 |
| F1 XS_REL | SHORT | −0,18 | −0,22 | −0,19 | −0,44 | 0,02 / 0 / 0,02 / 0 |
| F1 XS_REL | LS | −0,16 | −0,16 | −0,16 | −0,16 | 0,03 / 0 / 0,03 / 0,10 |
| F2 IDIO_SHOCK | LONG | +0,81 | +0,38 | −0,09 | +0,23 | 0,85 / 0,63 / 0,27 / 0,53 |
| F2 IDIO_SHOCK | SHORT | −1,13 | −0,70 | −0,23 | −0,55 | 0,01 / 0 / 0,08 / 0,10 |
| F3 MKT_CASCADE | LONG | +1,40 | +0,24 | +0,29 | +0,07 | 0,38 / 0,34 / 0,42 / 0,35 |
| F3 MKT_CASCADE | SHORT | −1,72 | −0,48 | −0,53 | −0,28 | 0,31 / 0,07 / 0,12 / 0,10 |
| F4 BREAKOUT | LONG | −0,13 | −0,11 | −0,06 | +0,18 | 0,10 / 0,29 / 0,26 / 0,51 |
| F4 BREAKOUT | SHORT | −0,13 | −0,33 | −0,20 | −0,35 | 0,14 / 0,07 / 0,09 / 0,02 |
| F5 CROWDING | LONG | −0,19 | −0,09 | −0,13 | +0,18 | 0,03 / 0,07 / 0,03 / 0,60 |
| F5 CROWDING | SHORT | −0,13 | −0,23 | −0,19 | −0,50 | 0,10 / 0 / 0 / 0 |
| F5 CROWDING | LS | −0,16 | −0,16 | −0,16 | −0,16 | 0,07 / 0,03 / 0,03 / 0,05 |
| F6 REAL_LIQ | LONG | +0,23 | −0,32 | +0,41 | +0,15 | 0,48 / 0,27 / 0,54 / 0,48 |
| F6 REAL_LIQ | SHORT | −0,55 | −0,00 | −0,73 | −0,47 | 0,08 / 0,23 / 0 / 0,08 |
| F7 OPTIONS_IV | LONG | −0,37 | −0,05 | −0,24 | +0,30 | 0,08 / 0,33 / 0,18 / 0,53 |
| F7 OPTIONS_IV | SHORT | +0,05 | −0,27 | −0,08 | −0,62 | 0,36 / 0,21 / 0,26 / 0,03 |
| F8 ONCHAIN_FLOW | LONG | −0,28 | −0,44 | −0,38 | −0,27 | 0,06 / 0,44 / 0,25 / 0,12 |
| F8 ONCHAIN_FLOW | SHORT | −0,04 | +0,12 | +0,06 | −0,06 | 0,38 / 0,38 / 0,62 / 0,38 |

**Läsanvisning:**

- **LS-benen** (F1 och F5) ligger på exakt −0,16 = −kostnaden. Den marknadsneutrala spreaden är alltså ≈ 0 brutto.
- **Positiva LONG-siffror i HOLDOUT** är marknadsdrift, inte signal: en slumpmässig LONG på varje symbol och timme gav brutto +0,94 % per 24h i HOLDOUT, mot −0,22 % i TRAIN (`p4_benchmark.json`).
- **F2 och F3 LONG i TRAIN** är medelvärden över FADE- och FOLLOW-celler med stor varians och få oberoende kluster. Ingen av dem är signifikant efter korrektion.

**De tre BH-överlevarna i TRAIN** (signifikanta bara med F6:s timkluster, se §16) är alla F6 och samma mekanism: *shorts likviderade → FOLLOW LONG, 24h*.

| θ | TRAIN | VALID | TEST | HOLDOUT |
|---|---|---|---|---|
| 1 % | +3,07 | −3,24 | +3,54 | −2,45 |
| 3 % | +4,36 | −2,46 | +3,51 | −0,48 |
| 10 % | +4,08 | −2,30 | +4,60 | +2,64 |

Netto i % per trade. Alla tre faller i VALID (p = 0,92–0,99). Per provdag (θ 3 %) varierar resultatet mellan −5,3 % och +8,4 %. Det är en dag-effekt med 14 dagar, inte en mekanism.

## 9. Fees / slippage / funding / latens

- **Primär kostnad:** taker 0,05 % + slippage 0,03 % per sida = **0,16 % tur och retur**, plus verklig funding under innehavet (betald eller mottagen).
- **Stresstest:** 0,25 % tur och retur.
- **Spread** ingår i slippageantagandet. Bid/ask-historik finns inte gratis efter 2024.
- **Execution miss** har inte modellerats separat. Market-entry antas, vilket är optimistiskt för de illikvida alts som dominerar F2/F6.
- **Latens:** primärt 5 min (en deterministisk 5-minutersmotor), känslighet 15 min. Dagens bot har ~22 min discovery-cykel, så 5 min är **optimistiskt**. Trots det klarar ingenting.
- **GROSS och NET:** rapporteras för varje cell i CSV:n. Brutto p < 0,05 i 64 av 1 484 celler, där ≈ 74 väntas under nollhypotesen. Det finns alltså inte ens brutto någon riktningsinformation i aggregatet.

## 10. LONG / SHORT

- SHORT är negativt eller ≈ 0 i nästan varje familj och period.
- LONG ser bättre ut enbart när marknaden steg (HOLDOUT).
- Efter justering mot universums samtida medelavkastning (`p4_inspect.py`) försvinner nästan alla LONG-"fynd":
  - **Short-squeeze-continuation** (F2 UP + OI_DOWN, LONG 30m–2h) är netto +1,1/+0,6/+0,03/+0,02 %, men **marknadsjusterat negativt i alla fyra perioderna** (−0,45/−0,38/−0,16/−0,17 %). Det är ren beta.
  - **F6 FADE av long-likvidationer, 24h** ger netto +1,0/+1,3/+1,2/+2,2 %, men marknadsjusterat +0,40/−0,09/−0,31/+1,13 %. Inte konsekvent.

## 11. Holding periods

Andel celler med netto > 0 per horisont (alla familjer):

| Horisont | TRAIN | VALID | TEST | HOLDOUT | Median \|brutto\| TRAIN |
|---|---|---|---|---|---|
| 5m | 0,11 | 0,05 | 0,04 | 0,02 | 0,07 % |
| 15m | 0,14 | 0,12 | 0,09 | 0,09 | 0,07 % |
| 30m | 0,19 | 0,18 | 0,08 | 0,15 | 0,10 % |
| 1h | 0,16 | 0,20 | 0,12 | 0,24 | 0,09 % |
| 2h | 0,22 | 0,20 | 0,15 | 0,26 | 0,11 % |
| 4h | 0,22 | 0,21 | 0,19 | 0,34 | 0,11 % |
| 8h | 0,27 | 0,23 | 0,22 | 0,39 | 0,16 % |
| 24h | 0,36 | 0,31 | 0,39 | 0,44 | 0,31 % |

- **Korta horisonter:** typisk bruttorörelse är 0,07–0,11 %, under kostnaden på 0,16 %. Där kan ingen strategi med taker-execution vinna.
- **Längre horisonter:** fler celler blir positiva enbart för att variansen och marknadsdriften växer. Andelen positiva brutto är fortfarande ≈ 0,5 (myntkast).

## 12. Trades/dag

| Familj | Trades/dag (TRAIN, per cell) | Kommentar |
|---|---|---|
| F1 XS_REL | 559–1 117 | Kvintil-portföljer. Implementerbart med max 4 bara som topp-2/botten-2, vilket inte behövdes eftersom kvintilerna själva saknar edge |
| F2 IDIO_SHOCK | 0,9–48 | |
| F3 MKT_CASCADE | 0,7–97 (korg) | 1–273 händelser på 14 mån |
| F4 BREAKOUT | 1,5–30 | |
| F5 CROWDING | 69–139 | |
| F6 REAL_LIQ | 22–212 per provdag | |
| F7 OPTIONS_IV | 0,1–24 | |
| F8 ONCHAIN_FLOW | 0,1 | |

Frekvens var aldrig flaskhalsen. Ingen cell nådde frekvenskriteriet med positiv, robust expectancy.

## 13. Drawdown

Max drawdown på en daglig equity-kurva (varje dags trades delar dagens kapital lika, utan ränta på ränta) för de mest lovande cellerna (`p4_inspect.json`):

| Cell | MDD TRAIN / VALID / TEST / HOLDOUT |
|---|---|
| F2 z4 DOWN OI_DOWN FADE LONG 4h | 31 / 12 / 8 / 8 % |
| F2 z4 DOWN OI_DOWN FADE LONG 24h | 45 / 45 / 40 / 11 % |
| F4 14d HOLD breakout LONG 24h | 126 / 137 / 100 / 44 % |
| F1 7d-momentum topp-kvintil LONG 24h | 35 / 19 / 19 / 8 % |

Även de "bästa" cellerna har drawdowns som är oförenliga med 10× leverage.

## 14. Expectancy

Ingen cell har positiv expectancy efter kostnad som är både signifikant och stabil över perioderna. Högsta poolade OOS-netto bland celler som klarade S1 är negativt i VALID (F6 ovan). Per-cell expectancy, hit rate, MFE och MAE finns i CSV:n.

## 15. Robusthet

Två svaga **ledtrådar** hittades post hoc, genom att titta på alla perioder. Det är data snooping, de är **inte** godkända och rapporteras bara för fullständighetens skull.

**1. "Köp idiosynkratisk krasch med fallande OI, håll 4h"** (F2 z4 DOWN OI_DOWN FADE LONG 4h). Mekanismen är likviditetsgivning åt tvångssäljare.

| Mått | TRAIN | VALID | TEST | HOLDOUT |
|---|---|---|---|---|
| Netto | +0,36 | +0,23 | +0,05 | +0,31 |
| Marknadsjusterat | +0,19 | +0,19 | +0,06 | +0,23 |
| Marknadsjusterat p | 0,06 | 0,09 | 0,22 | 0,03 |

- I huvudtestet: TRAIN p = 0,14, BH q = 1,0.
- TEST faller under noll vid 0,25 % kostnad.
- ~22–36 trades/dag.

**2. "14-dagars breakout bekräftad av 1h hold + volym, håll 24h"** (F4).

- Netto: +1,35 / +0,40 / +1,90 / +2,40 %.
- Marknadsjusterat: +1,16 / +0,51 / +2,03 / +1,73 %, p 0,12 / 0,28 / 0,045 / 0,046.
- Bara 1,5–5 trades/dag, MDD 44–137 %.
- TRAIN p = 0,13, BH q = 1,0.

Bland 1 484 celler väntas ett tiotal vara positiva i alla fyra perioderna av ren slump (och beta). Vi såg 22, varav nästan alla var LONG 24h i en uppåtmarknad.

Övrigt:

- **F7 (optioner):** VRP_LO SHORT 24h var +0,75/+0,59/+0,79 % men −2,12 % i HOLDOUT, och bara 0,2–0,4 trades/dag. **F8 (on-chain):** för få händelser (36–37) för att säga något alls.
- **F3 (kaskader):** "uppåt-kaskad + fallande aggregerad OI" inträffade bara 1 gång på 14 månader. Shortlikvidationer syns inte i marknads-OI på 1h-nivå.

## 16. Multiple-testing correction

- **BH över 1 484 celler** (8 mekanismer, 104 händelsemängder; cellerna är beroende):
  - TRAIN: 3 överlevare (F6 θ = 1/3/10 % SHORTS FOLLOW LONG 24h, q = 0,003/0,000/0,043).
  - **Kvalificering:** F6 klustrades på **timme**, mot 4h i forskningsregistret (`stats.BLOCK_HOURS`) och dag/vecka i resten av P4. Timkluster underskattar standardfelet när likvidationskaskader varar i flera timmar, så de tre TRAIN-överlevarna är sannolikt en klustringseffekt. En grov omräkning med dagkluster (θ = 3 %: 6 dagkluster i TRAIN, t = 3,2) ger p ≈ 0,01, vilket inte klarar BH över 1 484 (minsta tröskel ≈ 0,00007).
  - VALID: 0 överlevare.
- **Bonferroni:** 2 överlevare på TRAIN (samma timklustrade F6-celler), 0 efter VALID.
- **Utan multipeltestskorrigering:** 0 celler har TRAIN p < 0,05 och VALID netto > 0 med p < 0,05. BH per familj och BH över 202 grupper (familj × parametrar × sida) ger samma resultat: 0 i VALID.
- **Naiva p < 0,05 (netto) på TRAIN:** 8 st. Med brutto 64 st, mot ≈ 74 väntade av slump om cellerna vore oberoende (grov jämförelse).

## 17. Förkastade hypoteser

Alla:

- **F1 XS_REL.** Varken 1h–7d-momentum eller -reversal ger en marknadsneutral spread över kostnad (LS = −kostnaden).
- **F2 IDIO_SHOCK.** FADE efter tvångssälj har rätt tecken men är inte signifikant efter korrektion. FOLLOW efter squeeze är beta.
- **F3 MKT_CASCADE.** För få oberoende händelser, och tecknet flippar mellan perioder.
- **F4 BREAKOUT.** Ingen variant (NONE/HOLD/RETEST, BTC-alignment, FADE) klarar BH.
- **F5 CROWDING.** Funding-carry, L/S-ratio-contrarian och OI-uppbyggnad ger alla ≈ 0 spread.
- **F6 REAL_LIQ.** Signifikant på TRAIN men faller i VALID. Resultatet är dagberoende.
- **F7 OPTIONS_IV.** Inget signifikant. Det bästa vänder i HOLDOUT.
- **F8 ONCHAIN_FLOW.** För få händelser och inget signifikant.

## 18. Överlevande hypoteser

**Inga.**

## 19. Data gaps

| Källa | Status | Vad som krävs |
|---|---|---|
| **Kontinuerlig likvidationshistorik ≥ 12 mån** | Inte gratis. Testad på 14 gratis provdagar (Tardis) utan robust effekt | Se nedan |
| Tidsstämplade nyheter | DATA GAP. CryptoPanic ger 403 utan nyckel | Betald nyhets-API med historik, ej utrett vidare |
| Intradag on-chain | DATA GAP. Coin Metrics 1h kräver betald nyckel. Glassnode och CryptoQuant är betalda | Betald plan |
| Options-skew/flöde | Endast DVOL är gratis (testat) | Deribit tick/Tardis (betald) |
| Bid/ask-spread-historik | Saknas efter 2024 | Forward-inspelning |

**Likvidationsdata: vad det kostar.** Inget är köpt eller anslutet.

| Källa | Pris | Historik / upplösning | Symboler | Latens |
|---|---|---|---|---|
| Tardis.dev Solo | 350 USD/mån | Månadsfakturering ger 4 mån historik. Kvartalsfakturering ger 12 mån, årsfakturering 4 år. Tick-nivå (forceOrder) sedan 2020 | Alla Binance-perps | Historisk CSV/replay |
| CoinGlass API | Hobbyist 29 USD/mån (intervall ≥ 4h), Startup 79 USD/mån (≥ 30m), Standard 299 USD/mån (alla intervall inkl. 1m/5m) | Aggregerat per intervall. Hobbyist/Startup gäller endast privat bruk | Multi-exchange | REST |
| Coinalyze | Gratis nyckel | Intradag bara 1 500–2 000 punkter per symbol (5m ≈ 5–7 d, 1h ≈ 2–3 mån). Dagligt: hela historiken | Många | REST, 40 anrop/min |
| Tardis gratis provdagar | Gratis | 1:a dagen varje månad (använt här: 14 dagar) | Alla | – |

**Forward collector** (designad, **inte byggd**):

- **Källa:** Binance `!forceOrder@arr`-websocket (gratis, alla symboler, ≤ 1 per sekund och symbol) till en egen SQLite-tabell.
- **Förregistrerad testhypotes:** F6/F2 "FADE efter long-likvidation / tvångssälj, LONG 4h", testad med samma protokoll efter ≥ 90 dagar.
- **Varför den inte byggdes:** evidensen från 14 provdagar och från OI-proxyn (P3) är svag. Enligt instruktionen byggs den bara på uttryckligt beslut.

## 20. Nästa steg

1. **Bygg ingenting.** Ingen ny strategi ska till LIVE eller PAPER. LIVE förblir stoppad.
2. **Pris-, volym-, OI-, funding-, L/S- och flödesbaserade hypoteser är nu uttömda** på 5m–24h-skalan för det här universumet. Det gäller båda klasserna:
   - P1–P3: features ovanpå signalen.
   - P4: åtta helt nya strategiklasser, inklusive äkta likvidationer, optioner och on-chain.

   Ytterligare permutationer av samma data är data mining, inte research.
3. **Om research ska fortsätta** finns bara vägar som kräver ett beslut från dig:
   - (a) köp av data (likvidationer 12 mån: 79–350 USD/mån; nyheter/on-chain: okänt pris);
   - (b) forward collection av likvidationer eller spread under 3–6 månader;
   - (c) en annan tidsskala (dagar–veckor) eller en annan marknad.

   Ingen av dem har i dag evidens som motiverar kostnaden.
4. **Ett nollresultat är giltigt och förväntat.** Medianen för en retail-algo på kryptoperps är netto negativ efter kostnader, och resultaten här är förenliga med en effektiv marknad på den här tidsskalan.

---

**Filer:**

| Typ | Filer |
|---|---|
| Kod | `crypto_trading/entry_research/p4_data.py`, `p4_liq_fill.py`, `p4_lab.py`, `p4_external.py`, `p4_inspect.py` |
| Tester | `tests/crypto_trading/entry_research/test_p4_lab.py` (11 integritetstester) |
| Alla celler | `docs/superpowers/reports/2026-10-01-strategy-redesign-cells.csv` (1 484 rader) |
| Rådata (git-ignorerat) | `data/entry_research/p4_final.json`, `p4_selection.json`, `p4_benchmark.json`, `p4_inspect.json`, `p4_liq.db`, `p4_cache/` |
