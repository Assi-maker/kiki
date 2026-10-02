# Slutvalidering: Trend, Carry, Trend + Carry och baslinjer (2026-10-02)

Status: **FÖRREGISTRERING.** Det här avsnittet skrevs och committades tillsammans med koden (`entry_research/final_validation.py`, tester i `tests/crypto_trading/test_final_validation.py`) **innan** första körningen. Resultaten läggs till under "RESULTAT" och ändrar inget här.

LIVE är stoppad, `SAFE_MODE=1` ligger kvar, och execution, GODFATHER, riskgränser och trendstrategin är oförändrade. Ingen carry byggs. Detta är simulering.

---

## FÖRREGISTRERING

### Mål som prövas
En automatisk strategi med **positiv netto-P/L över tid**, gärna **många positiva dagar**, och **utan dolda stora svansrisker**.

### Data och tidsregler
- **Data:** Binance USDT-M perps och Binance spot, dagliga candles, september 2019 till 2026-10-01 UTC. Full funding-historik (varje betalning).
  - Universumet omfattar 261 perps som listades före 2024-04, inklusive 66 avlistade (SETTLING).
  - Perps som listades efter 2024-04 saknas, eftersom nedladdningen stoppades av minnesbrist. Det påverkar bara TOP5/TOP10-varianterna 2024–2026.
- **Ingen framtidsinformation:**
  - Beslut tas vid dagens stängning d med data som är känd då. Resultatet räknas över dag d+1.
  - Funding som tillhör dag d+1 är den som betalas under d+1.
  - Signaler använder bara funding som redan är betald.
  - Likviditetsrankning är bakåtblickande (90 dagar) och uppdateras den första i varje månad.
- **Perioder** (samma som sedan P5, inte valda nu):

| Period | Datum |
|---|---|
| TRAIN | 2020-01 till 2022-12 |
| VALID | 2023-01 till 2024-06 |
| TEST | 2024-07 till 2025-09 |
| HOLDOUT | 2025-10 till 2026-09 |
| FULL | 2020-01 till 2026-09 |

- **Regimer** (beskrivande): BTC:s 60-dagarsavkastning vid föregående stängning. Över +15 % = UPP, under −15 % = NED, annars SIDLEDES.

### Kostnader (BingX, per ben och sida)
- **Perp:** taker 0,05 %.
- **Spot:** taker 0,10 %.
- **Halva spreaden:** 0,01 % på BTC/ETH och 0,03 % på övriga.
- **Slippage:** 0,02 % på BTC/ETH och 0,05 % på övriga.
- **Funding:** verklig. En long betalar positiv funding, och en short tar emot den.

### Strategier
1. **CASH:** 0 % (USDT på kontot ger ingen avkastning).
2. **BUY&HOLD 1x:** 50/50 BTC/ETH-perps på 1x kapital, ombalanseras månadsvis, betalar funding.
   - Dessutom **BUY&HOLD exponeringsmatchad:** samma, men skalad till trendens genomsnittliga bruttoexponering. Den visar om trendens avkastning bara är exponering.
3. **TREND:** den frysta produktionsregeln, oförändrad.
   - BTC+ETH, signal = medel av sign(20 d) och sign(50 d), avklippt vid 0 (long eller flat).
   - Storleken styrs mot 40 % årlig volatilitet (30 dagar), högst 2x per tillgång, delat på 2.
   - Ett 5 %-band, och en utgång (till noll) verkställs alltid.
   - Kapitalbindning: marginal = brutto / 3.
4. **CARRY**, nio hypoteser som behandlas som separata och multipeltestkorrigeras:
   - **Universum:** MAJORS (BTC+ETH, 2 platser), TOP5 eller TOP10, de senare efter 90-dagars perp-volym, kräver spotpar och minst 90 dagars historik, exkluderar stablecoins och guld.
   - **Inträde:** när 7-dagars snittfunding × 365 överstiger T_in, med T_in ∈ {0 %, 5 %, 10 %}.
   - **Utträde:** under T_in/2 (vid T_in = 0 % under 0 %).
   - **CARRY_MAJORS_T0 är primärvarianten.** Den definierades redan i P5 (F5_thr0) och har alltså setts förut. Den räknas därför inte som ett nytt fynd.
5. **TREND + CARRY:**
   - **C-A, växling (primär):** trenden körs oförändrad. De dagar trenden inte har någon position används hela kapitalet till CARRY_MAJORS_T0. När trenden går in stängs carry-positionen.
   - **C-C, overlay (sekundär):** trenden körs i full storlek. Carry använder det kapital som trendens marginal inte binder, minus en buffert på 20 %: (0,8 − brutto/3) × kapital. Positionerna ändras först när det tillgängliga kapitalet har ändrats mer än 25 %.
6. **Generaliseringstest av trendregeln:** samma frysta regel, men på topp 10 altcoins efter volym vid varje tidpunkt, med andelen 1/10 per coin. Coinsen är aldrig valda efter resultat. Testet visar om mekanismen fungerar utanför BTC/ETH.

### CARRY: exakt mekanik
- **Positioner:** för varje innehavd coin long Q spot och short Q perp, i samma antal coins.
  - Notional per plats = platsens kapital / (1 + 1/L_perp), med L_perp = 2.
  - Kapital per notional: 1 i spot plus 0,5 i perp-marginal, alltså 1,5.
- **Dagligt resultat:** Q × (ΔSpot − ΔPerp) [basis] + Q × perp-pris × dagens funding.
  - Positiv funding ger short-benet en inkomst. Negativ funding är en kostnad, tills utträdesregeln stänger positionen.
- **Låg funding:** utträde under T_in/2.
- **Negativ funding:** betalas tills 7-dagarssnittet passerar utträdesgränsen. Det finns ingen omvänd carry (short spot kräver lån, som inte modelleras).
- **Kostnad:** varje öppning och stängning betalar båda benen (spot 0,13 % plus perp 0,08 % på BTC/ETH, 0,18 % + 0,13 % på övriga).
- **Hedgeunderhåll:** när perp-priset har rört sig mer än 10 % sedan senaste ombalansering ställs båda benen om till målstorleken. Det kostar avgifter på den handlade mängden.
- **Likvidation:** om perp-HÖGSTA stiger mer än 0,9/L_perp (45 %) över priset vid senaste ombalansering. Då stängs perp vid den nivån, spot säljs vid stängning, och 1 % av notional plus utgångskostnad dras.
- **Basisrisk:** spot och perp prissätts separat (dagliga stängningar).
  - Datakvalitet: ingen öppning om |spot/perp − 1| > 2 %, och tvångsutträde om > 5 % (avlistade eller omnamnade tokens).
- **Kapitalbindning:** spotbenet plus perp-marginalen. Exponeringen är delta-neutral, så kvarvarande risk är basis, funding och likvidation.

### Mått (alla strategier, FULL och varje period)
- Netto-P/L per 1 000 USDT (sammansatt och enkel summa). Brutto före kostnader och totala kostnader.
- Årsavkastning och Sharpe.
- Antal och andel positiva dagar (av alla dagar och av aktiva dagar).
- Genomsnittlig positiv och negativ dag, median, största plus- och minusdag.
- Max drawdown och längsta drawdown i dagar.
- Kapitalutnyttjande (bundet kapital) och exponering. Antal affärer och ombalanseringar.
- Resultat per regim och per år.
- p-värde för medel > 0 (block-bootstrap med 20-dagarsblock).

### Trend + Carry: isolering
- **Bidrag:** trendens bidrag, carry-delens bidrag (brutto, kostnad, netto) och interaktion = kombinationen − trend − carry-delen.
- **Skillnaden mot trend ensam:** Δ dagsmedel med 95 % block-bootstrap-CI, ΔSharpe, ΔMDD (procentenheter) och Δ andel positiva dagar.
- **Samvariation:** korrelationen mellan trendens och carry-delens dagar.

### Känslighetsanalys
Scenarierna körs på TREND, CARRY_MAJORS_T0 och TREND + CARRY C-A:
- avgifter ×2;
- slippage ×2;
- slippage ×3 och spread ×2;
- sämre fyllnader (+0,05 % per ben);
- funding ×0,5;
- funding ≈ 0;
- negativ förskjutning av funding (−0,01 % per 8 h);
- stressad basis (0,2 % / 0,5 % ogynnsamt vid varje utträde);
- sämre kapitalutnyttjande (perp 1x);
- allt kombinerat (avgifter ×2, slippage ×2, funding ×0,5, basis).

### Hypoteser
- **H-T:** TREND har positiv netto-expectancy. Den har setts förut (P5), så den räknas som bekräftelse, inte som upptäckt.
- **H-Tgen:** den frysta trendregeln är positiv även på altcoins som aldrig använts i urvalet.
- **H-C1 till H-C9:** CARRY-varianten har netto-medel > 0. Benjamini-Hochberg q < 0,10 på TRAIN+VALID.
- **H-CA:** C-A förbättrar trend utan att försämra expectancy.

### Förhandskriterier: CARRY anses värd att bygga endast om ALLA uppfylls
1. Minst en carry-variant klarar BH (q < 0,10) på TRAIN+VALID.
2. Den varianten har netto-medel > 0 i **alla fyra** perioder.
3. **Materialitet:** netto-årsavkastning på carry-kapitalet ≥ **3 %** i både TEST och HOLDOUT. Annars är den inte värd risken och komplexiteten jämfört med kontanter.
4. I HOLDOUT är netto fortfarande > 0 i scenariot "allt kombinerat".
5. MDD bättre än −5 % över FULL, och **0 likvidationer** i basscenariot.
6. **För att bygga den som del av Trend + Carry (C-A):** över FULL och i HOLDOUT gäller ΔSharpe ≥ 0, ΔMDD inte sämre än −2 procentenheter, Δ andel positiva dagar ≥ +5 procentenheter, och Δ dagsmedel inte signifikant negativ (95 %-CI:s övre gräns > 0).

Om villkor 1–5 inte uppfylls byggs ingen fristående carry. Om villkor 6 inte uppfylls byggs inte heller C-A.

---

## RESULTAT

Körning: commit `5798bc5` (förregistreringen) och den här commiten. Rådata finns i `2026-10-02-final-trend-carry-validation.json`.

**Tester:**
- Hela sviten: 2 679 passed, 3 skipped (numpy- och pandas-beroende).
- De överhoppade testerna körda separat med numpy och pandas: 14 passed, varav 8 nya motortester.

**Läge efter körningen:** `SAFE_MODE=1` ligger kvar och LIVE är stoppad.

### 1. Huvudtabell (1 000 USDT, sammansatt, avgifter, spread, slippage och funding inräknade)

| Strategi | FULL 2020–26 | Årsavk. | Sharpe | MDD | Längsta DD | Positiva dagar (av alla / av aktiva) | Kapital bundet |
|---|---|---|---|---|---|---|---|
| CASH | 0 | 0 % | – | 0 % | 0 d | 0 % | 0 |
| BUY&HOLD 1x (BTC/ETH 50/50) | +6 796 | 35,5 % | 0,80 | **−77,3 %** | 1 370 d | 52 % | 0,33 |
| BUY&HOLD exponeringsmatchad (0,30x) | +1 610 | 15,3 % | 0,80 | −33,2 % | 846 d | 52 % | 0,10 |
| **TREND** | **+4 651** | **29,2 %** | **1,10** | −32,4 % | **715 d** | 26 % / 48 % | 0,10 |
| TREND + CARRY C-A (växling) | +3 165 | 23,5 % | 0,92 | −34,1 % | 793 d | 55 % / 56 % | 0,52 |
| TREND + CARRY C-C (overlay) | +6 168 | 33,9 % | 1,23 | −31,1 % | 714 d | 57 % / 58 % | 0,74 |
| CARRY_MAJORS_T0 (primär) | +509 | 6,3 % | 1,92 | −8,1 % | 316 d | 76 % / 79 % | 0,90 |
| Trendregeln på altcoins (generalisering) | +415 | 5,3 % | 0,46 | −28,3 % | 1 117 d | 31 % / 47 % | 0,04 |

**Per period** (årsavkastning / positiva dagar / MDD):

| Strategi | TRAIN | VALID | TEST | HOLDOUT |
|---|---|---|---|---|
| BUY&HOLD 1x | +42 % / 52 % / −77 % | +102 % / 51 % / −25 % | +31 % / 52 % / −49 % | **−32 %** / 49 % / −61 % |
| BUY&HOLD 0,30x | +20 % / 52 % / −33 % | +27 % / 51 % / −8 % | +12 % / 52 % / −17 % | −8,5 % / 49 % / −23 % |
| **TREND** | +28,9 % / 26 % / −31 % | +61,2 % / 30 % / −22 % | +18,3 % / 26 % / −13 % | **+4,4 %** / 18 % / −21 % |
| C-A | +21,4 % / 53 % / −34 % | +57,1 % / 59 % / −23 % | +13,9 % / 55 % / −14 % | +0,5 % / 53 % / −23 % |
| C-C | +36,7 % / 55 % / −31 % | +66,9 % / 61 % / −21 % | +20,1 % / 58 % / −13 % | +3,5 % / 54 % / −22 % |
| CARRY_MAJORS_T0 | +9,3 % / 73 % / −8,1 % | +6,8 % / 87 % / −0,4 % | +3,4 % / 80 % / −0,5 % | **+0,3 %** / 64 % / −1,4 % |
| Trend på altcoins | +6,2 % | +3,9 % | +3,2 % | +7,3 % |

**Dagsstatistik FULL** (USDT per 1 000):

| Strategi | Snitt + dag | Snitt − dag | Median | Bästa | Sämsta | Brutto | Kostnader | Netto (enkel summa) | Affärer / ombal. |
|---|---|---|---|---|---|---|---|---|---|
| BUY&HOLD 1x | +24,7 | −23,3 | +0,76 | +217 | **−427** | 3 640 | 8 | 3 632 | 85 |
| TREND | +14,4 | −10,5 | 0,00 | +175 | −112 | 2 090 | 122 | 1 968 | 434 |
| C-A | +6,9 | −7,2 | +0,04 | +175 | −113 | 2 128 | 460 | 1 668 | 1 032 |
| C-C | +6,7 | −7,2 | +0,04 | +174 | −113 | 2 450 | 241 | 2 208 | 1 062 |
| CARRY_MAJORS_T0 | +0,33 | −0,40 | +0,11 | +4,5 | **−78,9** | 530 | 115 | 415 | 450 |

**Per regim** (BTC 60 d; snitt per 1 000 och dag, andel positiva dagar):

| Strategi | UPP (910 d) | SIDLEDES (1 035 d) | NED (520 d) |
|---|---|---|---|
| BUY&HOLD 1x | +3,21 / 53 % | −0,33 / 50 % | +2,02 / 53 % |
| TREND | +1,42 / 45 % | +0,64 / 21 % | +0,02 / 3 % (står mest i kontanter) |
| C-C | +1,73 / 55 % | +0,68 / 58 % | −0,13 / 56 % |
| CARRY_MAJORS_T0 | +0,46 / 88 % | +0,08 / 75 % | −0,18 / 58 % |

**Per år:**

| Strategi | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 (jan–sep) |
|---|---|---|---|---|---|---|---|
| TREND | +112 % | +28 % | −21 % | +71 % | +27 % | +6 % | +14 % |
| C-C | +118 % | +50 % | −22 % | +75 % | +33 % | +7 % | +12 % |
| CARRY_MAJORS_T0 | +6,4 % | +23,6 % | −0,6 % | +4,7 % | +7,8 % | +2,5 % | −0,2 % |
| BUY&HOLD 1x | +297 % | +113 % | −66 % | +106 % | +62 % | −11 % | −8 % |

### 2. Carry: mekanik och fakta (BTC/ETH)
- **Funding för BTC per år (årstakt):** 17 % (2020), 31 % (2021), 4 % (2022), 8 % (2023), 12 % (2024), 5 % (2025), **3 % (2026)**. ETH var 27 %, 38 %, 1 %, 8 %, 13 %, 5 % respektive **2 %**.
  - Snittet de senaste 7 dagarna låg under 0 cirka 10 % av dagarna och under 5 % i årstakt cirka 34 % av dagarna.
- **Basis:** dagsförändringen i spot/perp var i median 1,3–1,4 bp, p99 12–17 bp och som mest 114–177 bp (mars 2020).
- **Svansrisk (verklig och inräknad):**
  - **12–13 mars 2020:** BTC-perp −40 % på en dag. ETH-perpen har ett högsta pris på 323 mot en stängning på 134, alltså en extrem spik eller en felnotering som inte går att verifiera. BTC-funding låg på −0,44 % på en dag.
  - **Följd:** short-benet likvideras (1 gång) och sämsta dagen blir **−79 USDT per 1 000** för en strategi vars snittdag är +0,11. En sådan dag äter cirka 2 års normal carry-inkomst i dagens fundingläge.
- **Omsättning:** 450 affärer och ombalanseringar på 6,75 år. Positionen är aktiv 96 % av dagarna.
- **Kapital:** cirka 0,90 av kapitalet är bundet (spot plus perp-marginal vid 2x). Med perp 1x halveras avkastningen och MDD blir −17 %.
- **När funding är låg eller negativ:** utträde enligt regeln, men först efter 7-dagarssnittet. Under tiden betalas negativ funding.

### 3. Multipeltest (9 carry-hypoteser)
- **BH på TRAIN+VALID:** alla 9 klarar q < 0,10 (p 0,000–0,006). Det fanns alltså en verklig carry-premie historiskt.
- **Positiv i alla fyra perioder:** bara MAJORS_T0. Alla TOP5- och TOP10-varianter och MAJORS T5/T10 är negativa i HOLDOUT, och TOP10 är negativa redan i TEST.
- **Fler coins:** TOP5 hade 8 likvidationer och TOP10 22, utan högre avkastning.

### 4. Trend + Carry: isolering

| | Trendens bidrag | Carry-delens bidrag (brutto / kostnad) | Interaktion | Δ dagsmedel mot trend [95 %-CI] | ΔSharpe | ΔMDD | Δ positiva dagar | Korr. |
|---|---|---|---|---|---|---|---|---|
| **C-A FULL** | +1 968 | **−300** (+38 / −339) | 0 | **−0,122 [−0,196; −0,075]** | −0,18 | −1,7 pp | +28,6 pp | +0,03 |
| C-A HOLDOUT | +78 | −38 (+12 / −49) | 0 | −0,103 [−0,165; −0,046] | −0,14 | −2,2 pp | +34,5 pp | +0,08 |
| **C-C FULL** | +1 968 | **+240** (+360 / −120) | 0 | **+0,097 [+0,024; +0,158]** | +0,13 | +1,3 pp | +30,8 pp | 0,00 |
| C-C TRAIN / VALID / TEST | | +178 / +52 / +18 | | alla > 0 | | | | |
| C-C HOLDOUT | +78 | **−8** (+15 / −23) | 0 | −0,023 [−0,049; +0,005] | −0,03 | −0,7 pp | +36,2 pp | +0,02 |

**Slutsatser om kombinationerna:**
- **C-A (växling) försämrar trenden signifikant.** Orsaken är strukturell: trenden står i kontanter när marknaden faller, och då är funding som lägst eller negativ. Carry tjänar alltså minst just när C-A använder den. Varje växling kostar dessutom båda benen (cirka 0,4 %). Carry-delen hade brutto +38 men kostnader på −339.
- **C-C (overlay) förbättrade trenden historiskt:** +0,10 USDT per dag och 1 000, med CI över 0. Sharpe steg 0,13, MDD blev något bättre och positiva dagar ökade med 31 pp. Ingen interaktion och ingen korrelation.
- **Men förbättringen har försvunnit i HOLDOUT:** −0,02 per dag, CI omfattar 0. Samma mönster syns i carry ensam. Carry gör alltså inte trenden bättre längre. Den ger fler positiva dagar men inte mer pengar.

### 5. Känslighet (FULL / HOLDOUT, årsavkastning)

| Scenario | TREND | CARRY_MAJORS_T0 | C-A |
|---|---|---|---|
| Bas | 29,2 % / 4,4 % | 6,3 % / 0,3 % | 23,5 % / 0,5 % |
| Avgifter ×2 | 27,8 % / 2,9 % | 5,0 % / **−1,1 %** | 17,9 % / −4,3 % |
| Slippage ×2 | 28,7 % / 3,8 % | 5,9 % / −0,1 % | 21,8 % / −1,0 % |
| Slippage ×3 + spread ×2 | 27,8 % / 2,9 % | 5,5 % / −0,6 % | 19,3 % / −3,2 % |
| Sämre fyllnader (+0,05 %/ben) | 27,8 % / 2,9 % | 5,5 % / −0,6 % | 19,3 % / −3,2 % |
| Funding ×0,5 | 33,0 % / 4,9 % | 1,7 % / −0,8 % | 26,1 % / 0,3 % |
| Funding ≈ 0 | 36,9 % / 5,4 % | 0 % (handlar aldrig) | = trend |
| Funding negativ (−0,01 %/8 h) | 33,6 % / 7,7 % | 1,1 % / 0,0 %, MDD −10 % | 30,0 % / 7,7 % |
| Stressad basis | 29,2 % / 4,4 % | 5,2 % / −0,7 % | 20,5 % / −2,0 % |
| Perp 1x (sämre kapitalutnyttjande) | 29,2 % / 4,4 % | 2,7 % / 0,2 %, MDD −17 % | 22,6 % / 1,4 % |
| Allt kombinerat | 30,9 % / 2,9 % | **−0,8 % / −3,5 %** | 15,8 % / **−8,2 %** |

- Trenden betalar funding som long. Lägre funding *hjälper* därför trenden, och dess resultat håller i alla kostnadsscenarier.
- Carry blir negativ i HOLDOUT redan med dubbla avgifter eller dubbel slippage.

### 6. Förhandskriterierna, avstämda

| Kriterium | Utfall | |
|---|---|---|
| 1. Någon variant klarar BH | Alla 9 | ✅ |
| 2. Positiv i alla fyra perioder | Bara MAJORS_T0 (HOLDOUT +0,3 %) | ✅ (knappt) |
| 3. Minst 3 % årsavk. i TEST och HOLDOUT | TEST 3,4 %, **HOLDOUT 0,3 %** | ❌ |
| 4. HOLDOUT > 0 med allt kombinerat | −3,5 % | ❌ |
| 5. MDD bättre än −5 % och 0 likvidationer | −8,1 %, 1 likvidation | ❌ |
| 6. C-A förbättrar trend | ΔSharpe −0,18, CI under 0 | ❌ |

**Carry är inte värd att bygga, varken fristående eller som C-A.**

---

## SVAR

### 1. Vad vet vi med rimligt stöd?
- **Trendregeln har positiv netto-expectancy** i alla fyra perioder. Den klarar alla kostnadsscenarier. FULL p = 0,016.
  - Den slår exponeringsmatchad köp och håll: 29 % mot 15 % per år, Sharpe 1,10 mot 0,80, vid samma MDD.
  - Fördelen är alltså timing, inte bara exponering.
- **Carry fanns historiskt** (alla 9 klarar BH), men den var koncentrerad till 2020–2021 och 2024 och är nära noll sedan 2025.
- **C-A är sämre än trend ensam**, signifikant och av strukturella skäl.

### 2. Vad vet vi inte?
- **Om trendens svaga HOLDOUT** (+4,4 %, MDD −21 %, 18 % positiva dagar) är början på en sämre era eller normal variation. Ett år räcker inte för att avgöra det.
- **Om funding kommer tillbaka** till nivåerna från 2021 eller 2024.
- **Riktiga fyllnader på BingX spot.** Modellen bygger på Binance-data plus ett kostnadspåslag.
- **Universum och överlevnad:** TOP5/TOP10 saknar listningar efter 2024-04, med okänd riktning på felet. BTC/ETH påverkas inte.
- **Generaliseringen till altcoins** är positiv i alla perioder men inte signifikant (p = 0,13).
- **Trendens val av ensemble (20/50)** gjordes efter P5. Bekräftelsen här är därför inte helt oberoende för BTC/ETH. Alt-testet är det oberoende stödet, och det är svagt.

### 3. Bäst robust evidens
**TREND ensam.** Den enda strategi som är positiv i alla perioder, håller i alla stresscenarier, slår relevant baslinje och inte har dolda svansrisker utöver de synliga: MDD −32 % och 715 dagar under tidigare topp.

### 4. Är carry värd att bygga?
**Nej, inte nu.** Kriterierna 3, 4, 5 och 6 fallerar.

Carry ger många positiva dagar (76–87 %) men mycket liten avkastning idag (0,3 % i HOLDOUT). Den har en verklig svansrisk (mars 2020: −79 per 1 000 på en dag) och blir negativ med normal kostnadsstress.

### 5. Om Trend + Carry vore kandidaten: regler implementationen måste följa
Kandidaten är inte aktuell nu. Om den någon gång ska prövas igen gäller **C-C** (inte C-A), exakt som testat:
1. Trenden körs oförändrad och har alltid företräde.
2. Carry endast på BTC och ETH: long spot och short perp i samma antal coins, perp-marginal 2x.
3. Carry-kapitalet är (0,8 − trendens brutto/3) × konto. Det ändras bara vid mer än 25 % förändring.
4. Inträde när 7-dagars funding × 365 > 0, utträde när det är < 0, beslut vid dagsstängning.
5. Båda benen ombalanseras när perp-priset rört sig mer än 10 % sedan senaste ombalansering. Ingen öppning om |spot/perp − 1| > 2 %, tvångsutträde om > 5 %.
6. Benen ska alltid ha samma antal coins. Om ett ben misslyckas stängs det andra.
7. Ett eget stopp krävs på short-benet. Likvidationsrisken vid extrema spikar (mars 2020) ska hanteras med lägre hävstång eller ett börsstopp.
8. **Startvillkor:** en ny förregistrerad forward-test, där funding för BTC och ETH i 30-dagarssnitt ligger över cirka 8 % i årstakt, eftersom carry under det inte täcker kostnaderna. Tröskeln är en slutsats ur denna analys och därmed efterhandsbestämd. Den måste prövas på ny data innan den används.

### 6. Risker som återstår för trenden, trots positiv historisk expectancy
- **Långa förlustperioder:** längsta drawdown var 715 dagar (cirka 2 år) och MDD −32 %. 2022 gav −21 %.
- **Få vinstdagar:** 26 % av alla dagar (48 % av de aktiva). Vinsten kommer från få stora uppgångsdagar.
- **Svag senaste period:** HOLDOUT +4,4 %. Sämre framtid än historiken är möjlig.
- **Koncentration:** bara BTC och ETH, som rör sig nästan likadant.
- **Hävstång 3x på börsen:** felhantering eller börsavbrott vid en krasch kan ge större förlust än modellen. Nödstoppet på 20 % finns men är inte prövat LIVE.
- **Spik-risk:** som i mars 2020, där börsstoppet kan fyllas långt från triggerpriset.

### Rekommendation
- **Bygg ingenting nytt.** Varken carry, C-A eller C-C uppfyller kraven.
- **Behåll trendstrategin i pappershandel**, oförändrad, som den enda kandidaten med robust stöd.
- **LIVE förblir stoppad.** Ett beslut om LIVE bör först tas efter minst 2–3 månaders pappershandel, och med full förståelse för att trenden inte ger daglig vinst.
- **Rättelse av min tidigare rekommendation:** jag rekommenderade "Trend + carry" (C-A, växling) i chatten. Med realistiska spotavgifter, spread och slippage på båda benen gör växlingen strategin sämre. Den rekommendationen dras tillbaka.
