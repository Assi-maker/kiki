# Entry-edge-forskning: finns det en verklig, reproducerbar entry edge?

**Datum:** 2026-09-28
**Kod:** `crypto_trading/entry_research/` och `crypto_trading/godfather/entry_patterns.py`
**Reproducera:** `python -m crypto_trading.entry_research.run --snapshot` följt av `python -m crypto_trading.entry_research.registry`

**Märkning:** påståenden är märkta **[FAKTA]**, **[STATISTIK]** eller **[BEDÖMNING]**.

**Ingen LIVE-strategi har ändrats.** Det gäller P0, Guardian, hävstång och sizing. Ingen break-even eller trailing har slagits på, och inga testordrar har lagts.

---

## Kort svar

**[STATISTIK]**
- **Det finns ingen entry edge i datan.**
  - 2126 mönster testades i train: singlar, par och tripplar.
  - Inget överlever korrektionen för multipla test ens in-sample. Bästa train-q är 0,38 för edge-sidan och 0,26 för failure-sidan.
  - Av de 25 bästa train-mönstren blev 21 **DECAYING_EDGE**, det vill säga positiva i train och negativa i test.
  - 0 blev EDGE och 0 WEAK_EDGE.
- **Baseline är negativ i varje period**, räknat i R efter verkliga kostnader:

  | Period | Mean R |
  |---|---|
  | Train | −0,12 |
  | Validering | −0,21 |
  | Test (26–28/9, samma dagar som LIVE) | −0,71 |

- **Det som är stabilt är vad som gör det *sämre*, inte vad som gör det bra.** Signaler utan verklig prisrörelse (chg_1h låg) och signaler där priset står still mellan signal och fill är sämst i alla tre perioderna. Men även den bästa tredjedelen har negativt förväntat värde i test.

**[BEDÖMNING]**
- Systemet är i dag i praktiken en long-exponering mot altcoin-marknaden, med en fördröjning på 23 min och kostnader.
- När alts stiger (train, +0,68 % i 6 h-avkastning) ser allt bra ut. När de faller (test, −0,40 %) förlorar allt.
- Ingen av de 26 features vi har skiljer ut vinnare på ett sätt som håller över tid.

## Design (bestämd innan resultaten fanns)

- **Population:** alla 2840 kandidater 1–28/9.
  - Bara **886 är oberoende**, det vill säga första signalen per symbol per 6 h. Resten är samma rörelse upprepad.
  - 875 har klines och används: train 379 (1–12/9), validering 236 (13–19/9) och test 260 (26–28/9).
- **Kohorter (oberoende):**

  | Kohort | Antal |
  |---|---|
  | LIVE | 58 |
  | CONFIRMED men inte LIVE | 41 |
  | P1-blockerade | 23 |
  | AI_NOT_CONFIRMED | 217 |
  | Aldrig analyserade (BUDGET_LIMITED) | 544 |

  De aldrig analyserade är en kontrollgrupp för den deterministiska signalen helt utan AI.
- **Standardiserat utfall.** Bara en fjärdedel av kandidaterna har en egen SL/TP.
  - Entry sker vid signal + **23 min**, vilket är medianlatensen i verklig LIVE.
  - SL = 2×ATR15 och TP = 3×ATR15, med gräns på 6 h.
  - Avgift 0,10 % tur och retur, plus 0,15 % slippage på stoppen.
  - Som komplement redovisas nettoavkastning framåt över 1–6 h, samt riskagentens egen bracket där den finns.
- **Ingen look-ahead.** Alla features kommer från barer som var stängda före signalen. drift_to_fill kommer från barer som var stängda före fill. Detta är låst med tester.
- **Statistik:**
  - p-värden är klustrade på 4 h-block, eftersom trades i samma marknadsrörelse inte är oberoende.
  - Konfidensintervallen kommer från kluster-bootstrap.
  - Protokollet har tre steg. Upptäckt sker i train. BH-korrektion görs över de valda mönstren i validering och över de överlevande i test.
- **Klassning av EDGE kräver allt detta:**
  - positivt mean R i alla tre perioderna,
  - n ≥ 30 i test och q < 0,05,
  - PF > 1,
  - positivt även utan de bästa 5 % av tradesen,
  - positivt i minst 2/3 av dagarna.

  Win rate används aldrig.
- **Kontroll av protokollet.** Det har testats på syntetisk data. Det hittar en planterad kombination av två features och utser ingen EDGE på rent brus (3 seeds).

## 1. Topp 10 entry-mönster

Rangordnade efter mean R i OOS, det vill säga validering och test tillsammans.

| # | Klass | Mönster | Train n:R | Val n:R | Test n:R | OOS n / win / **mean R** / median / PF / DD | Train-fam.-q |
|---|---|---|---|---|---|---|---|
| 1 | HYPOTHESIS | BTC 4h ∈ (−0,01; 0,23] & ret_15m > 0,26 % & båda TF triggade | 30:+0,41 | 7:+0,20 | 9:+0,13 | 16 / 50 % / **+0,16** / −0,02 / 1,35 / −2,3 | 0,93 |
| 2 | HYPOTHESIS | ret_1h > 0,64 % & båda TF triggade & volz_1h > −0,14 | 31:+0,50 | 28:+0,11 | 17:+0,11 | 45 / 51 % / **+0,11** / +0,34 / 1,20 / −4,9 | 0,93 |
| 3 | DECAYING | volz_30m > 0,54 & ATR15 0,42–1,02 % | 51:+0,33 | 40:+0,07 | 67:−0,30 | 107 / 39 % / −0,16 / −1,13 / 0,77 / −24,4 | 0,93 |
| 4 | DECAYING | samma som #3 & ingen funding-trigger | 51:+0,33 | 40:+0,07 | 67:−0,30 | 107 / 39 % / −0,16 | 0,93 |
| 5 | DECAYING | samma som #3 & ingen price-vol-trigger | 47:+0,35 | 38:+0,07 | 62:−0,31 | 100 / 39 % / −0,16 | 0,48 |
| 6 | DECAYING | ATR15 0,42–1,02 % & volymtrigger & ingen price-vol-trigger | 41:+0,30 | 35:+0,11 | 58:−0,34 | 93 / 39 % / −0,17 | 0,93 |
| 7 | DECAYING | ATR15 0,42–1,02 % & volymtrigger | 44:+0,32 | 35:+0,11 | 61:−0,34 | 96 / 39 % / −0,18 | 0,98 |
| 8 | DECAYING | samma som #7 & volz_30m > 0,54 | 44:+0,32 | 35:+0,11 | 61:−0,34 | 96 / 39 % / −0,18 | 0,98 |
| 9 | DECAYING | samma som #7 & ingen funding-trigger | 44:+0,32 | 35:+0,11 | 61:−0,34 | 96 / 39 % / −0,18 | 0,98 |
| 10 | DECAYING | chg_30m 0,16–0,66 % & ret_4h 0,27–1,8 % & ingen funding-trigger | 43:+0,40 | 35:−0,15 | 24:−0,40 | 59 / 41 % / −0,25 | 0,98 |

**Kostnadsandel och fördröjning:**
- MFE/MAE i OOS: #1 +3,4 / −1,45 %, #2 +2,1 / −1,4 %, #3 till #9 ≈ +1,2 / −1,15 %.
- Avgiften kostar 0,06–0,12 R per trade.
- Med en snabb pipeline (1 min) blir #1 **−0,40** och #2 +0,17. #1 håller alltså inte ens mot en ändrad fördröjning.

## 2. Verklig edge och brus

- **EDGE: 0.** **WEAK_EDGE: 0.**
- **HYPOTHESIS: 2**, #1 och #2.
  - Båda handlar om kortsiktig fortsättning som är bekräftad på båda tidsramarna.
  - De har n = 7–28 per period, OOS-KI korsar noll och train-q är 0,93.
- **DECAYING_EDGE: 21.** Klassiskt överanpassat eller regimberoende.
- **NOISE: 2.**

## 3. OOS-resultat

- **[STATISTIK]** Inget mönster på edge-sidan klarar valideringssteget. Bästa valideringsq är 0,61. Därför testades inget edge-mönster formellt i steg 3.
- **Failure-sidan:** 4 mönster klarade valideringen (q = 0,04–0,09). I test gick de åt samma håll, men q var 0,10–0,28 och n i test 14–29. De blir därför **FAILURE_HYPOTHESIS**, inte FAILURE_PATTERN.
- **Walk-forward (dag för dag, en feature):** ingen enskild tredjedel är positiv på mer än 9 av 17 dagar.
- **Känslighet för bracket-valet** (används inte för att välja mönster):

  | Bracket | Train | Val | Test |
  |---|---|---|---|
  | SL 1,5 / TP 3 | −0,21 | −0,27 | −0,85 |
  | SL 2 / TP 3 (förregistrerad) | −0,12 | −0,21 | −0,71 |
  | SL 3 / TP 4,5 | +0,06 | −0,12 | −0,53 |
  | SL 3 / TP 6 | +0,09 | −0,14 | −0,52 |

  Slutsatsen beror alltså inte på bracketen. Det finns ingen variant som är positiv utanför train.

## 4–6. Expectancy, drawdown och n per kohort (oberoende kandidater)

Värdena är mean R med entry efter 23 min och bracketen 2/3 ATR.

| Kohort | n | Win | Mean R | Median | PF | Max DD (R) | KI | Egen bracket, mean R | 6 h netto, fwd % |
|---|---|---|---|---|---|---|---|---|---|
| LIVE | 58 | 31 % | **−0,46** | −1,09 | 0,43 | −30,1 | [−0,74; −0,18] | −0,09 | −0,55 |
| CONFIRMED men inte LIVE | 41 | 49 % | +0,06 | −0,14 | 1,11 | −8,5 | [−0,25; +0,43] | +0,09 | +0,59 |
| P1-blockerade | 23 | 30 % | −0,31 | −0,42 | 0,52 | −10,1 | [−0,67; +0,32] | +0,04 (n=14) | +0,62 |
| AI_NOT_CONFIRMED | 217 | 41 % | −0,25 | −0,92 | 0,66 | −62,2 | [−0,41; −0,07] | −0,03 | +0,56 |
| Aldrig analyserade | 544 | 39 % | −0,36 | −1,07 | 0,56 | −249 | [−0,68; −0,09] | – | −0,04 |

**Verklig LIVE (alla 83 verifierade trades, [FAKTA]):**
- Mean R_actual **−0,11**, netto **−184,66 USDT**.
- Avgifter 41,18 USDT. Funding var netto +8,59 USDT i vår favör.

**[BEDÖMNING]**
- AI-kedjan väljer inte bättre än kontrollgruppen på något statistiskt säkert sätt. Konfidensintervallen överlappar.
- CONFIRMED-men-inte-LIVE ser bättre ut än LIVE. Det är ett tidseffekt: de kommer från train-perioden, medan LIVE ligger i test-perioden. Det är inte ett urval som fungerar.
- **AI-features som prediktorer** (244 analyserade, oberoende): forecast-skillnaden, entropi, opportunity score, R:R, stoppavstånd, signalålder och GF EQ-score har alla q = 1,00.

## 7. Korrigerad statistisk signifikans

| Steg | Antal hypoteser | Bästa q |
|---|---|---|
| Train, hela familjen | 2126 | 0,38 (edge), 0,26 (failure) |
| Validering, 50 valda | 50 | 0,61 (edge), 0,04 (failure) |
| Test, 4 överlevande (failure) | 4 | 0,10 |
| Counterfactual-regler på LIVE | 8 | 0,58 |

## 8. Återkommande failure-mönster

Alla 13 FAILURE_HYPOTHESIS har samma tecken, sämre än resten, i train, validering och test. De faller i tre familjer:

1. **Signal utan verklig rörelse.** Villkoren är chg_1h ≤ 0,26 % och ett pris som står still mellan signal och fill (drift −0,08…+0,22 %).

   | Period | n | Mean R | Resten |
   |---|---|---|---|
   | Train | 71 | −0,79 | +0,03 |
   | Validering | 33 | −0,89 | −0,10 |
   | Test | 29 | −2,21 | −0,52 |

   Valideringsq 0,04, testq 0,10. **Det starkaste fyndet i hela analysen.**
2. **Överköpt på 1 h och stilla före fill.** RSI(1h) > 70,9 och ingen drift.
   - Train 41: −0,80.
   - Validering 19: −0,60.
   - Test 8: −1,69.
3. **Låg volatilitet** (ATR15 ≤ 0,42 %) utan volymtrigger. Train −1,44, test −3,87 (n=14).
   - Detta är delvis mekaniskt: med en tät stop blir avgiften 0,12 R eller mer.

**Enskilda features per tredjedel** (beskrivande, mean R per period train / val / test):

| Feature | Låg | Mitten | Hög |
|---|---|---|---|
| chg_1h | −0,44 / −0,60 / −1,17 | +0,07 / −0,14 / −0,72 | 0,00 / **+0,14** / −0,26 |
| drift_to_fill | +0,09 / −0,02 / −0,60 | **−0,46 / −0,56 / −1,34** | −0,01 / −0,13 / −0,33 |
| ATR15 | −0,34 / −0,76 / −1,51 | +0,08 / −0,06 / −0,46 | −0,11 / +0,02 / −0,40 |

**GODFATHER-kedjan** (signal → kontext → beslut → förväntat → faktiskt → prediktionsfel, n = 243):
- Forecastens Brier är 0,673, sämre än att gissa basfrekvensen (0,660).
- Förväntat R var +0,04 och faktiskt −0,04. Korrelationen mellan förväntat och faktiskt är **−0,05**.
- Utfallen fördelade sig: 39 % bearish, 34 % neutral och 28 % bullish.

## 9. Vad P1 stoppar och hur det hade gått

Totalt 59 kandidater hade stoppats av P1 som annars var CONFIRMED.

| P1-orsak | n | Egen bracket, mean R | Standardiserad, mean R |
|---|---|---|---|
| SIGNAL_STALE | 36 | +0,03 (win 56 %) | −0,18 |
| RR_BELOW_MINIMUM | 18 | +0,02 | −0,03 |
| REFERENCE_PRICE_MISSING | 10 | – (ingen bracket) | −0,79 |
| **Alla blockerade** | 59 | +0,00 | −0,30 |
| **Passerade CONFIRMED** | 162 | +0,04 | −0,20 |

**[BEDÖMNING]**
- P1 varken hjälper eller stjälper expectancy på ett sätt som går att mäta.
- Dess värde är konsistens och säkerhet, till exempel att det aldrig blir en entry utan referenspris. Det är ingen edge.
- P1 ska vara kvar. Det finns inget skäl att skärpa den för att få edge.

## 10. Mest lovande idéer för veto och filter (alla shadow-only)

1. **REJECT: "ingen rörelse".** chg_1h i lägsta tredjedelen och drift före fill i mittersta tredjedelen.
   - Har hållit tecknet i tre perioder, med valideringsq 0,04 och testq 0,10.
   - Behöver cirka 30 framåtobservationer per sida.
2. **REJECT: "RSI(1h) > 71 utan uppföljning".** Samma tecken i tre perioder, men test-n är bara 8.
3. **ACCEPTABLE: "kortsiktig fortsättning, bekräftad på båda tidsramarna"** (mönster #2).
   - Den enda positiva kandidaten med rimligt n (45 OOS).
   - q är långt från signifikans.
4. **Kostnadsfilter (ATR15 låg)** skulle i LIVE motsvara att riskagentens stopp inte får vara för tätt i förhållande till avgiften.
   - Det hör hemma i kostnadsmodellen, inte i ett edge-påstående.

**[BEDÖMNING]** Inget av detta ger positivt förväntat värde. Som bäst gör det förlusten mindre. Utan en verklig edge är ett veto i LIVE bara ett sätt att handla mindre. Det kan vara bra, men det är inte en strategi.

## 11. Fortsätter i shadow

**Entry Quality Layer** (`crypto_trading/entry_research/quality.py`) går nu i en egen tråd i boten, var 30:e minut.
- **Register:** `config/entry_quality_registry.json`, version `20260928T2132Z`, fryst 2026-09-28 21:32 UTC.
- **Registret innehåller:**
  - 13 FAILURE_HYPOTHESIS, som ger REJECT,
  - 2 HYPOTHESIS, som ger ACCEPTABLE,
  - 0 EDGE, vilket betyder att STRONG inte kan ges i dag.

  Allt annat blir WEAK, det vill säga baseline.
- **Tabellen `entry_quality_shadow`** loggar per kandidat:
  - klass och evidensnivå,
  - skäl (`why`),
  - de features som användes,
  - den historiska evidensen för de mönster som matchade,
  - standardiserat utfall, fwd-avkastning, egen bracket och verkligt LIVE-resultat,
  - hela GODFATHER-kedjan.
- **Forward OOS:** bara kandidater efter frysningen räknas som `FORWARD_OOS`. De historiska fylls i som `HISTORICAL_IN_SAMPLE`.
- **Rapport:** `python -m crypto_trading.entry_research.quality`.
- **GODFATHER** (`godfather/entry_patterns.py`) kör var 24:e timme.
  - Samma protokoll körs om på allt som har samlats, med en rullande delning 50/25/25 i tid.
  - Kategorierna sparas i `godfather_entry_patterns` tillsammans med föregående kategori, så att en edge som bleknar syns som DECAYING_EDGE.
  - Ingen kategori sätts om n < 300 poster totalt eller < 30 per steg.
  - GODFATHER ändrar aldrig registret.
- **Isolering:**
  - Tester låser att ingen modul på LIVE-vägen importerar eller namnger lagret eller tabellerna.
  - Lagret importerar inga handelsmoduler.
  - Det kan inte blocka, sizea, öppna eller stänga något.
- **Befordran till LIVE** kräver alla dessa steg: historiskt → OOS → shadow → minst 30 framåtobservationer per klass → stabilt tecken → ett uttryckligt beslut av dig. Inget i koden gör det automatiskt, och P0 ligger alltid överst.

## 12. Ska absolut inte implementeras

| Idé | Varför inte |
|---|---|
| **Veto mot överextension (ret_4h hög)** | Ser bra ut på de 83 LIVE-tradesen: +8,5 R, p = 0,07. Men på hela populationen var överextenderade signaler **mindre dåliga** än resten i alla tre perioderna. Resultatet på LIVE är en slump i ett litet urval, med q 0,58. |
| **Försenad entry** | +15 min ≈ 0 (+0,6 R totalt). +30 min ger −0,9 R och +60 min −2,5 R. |
| **Filter på forecast eller AI-sannolikheter** | Negativ förmåga mot basfrekvensen, q = 1,00 genomgående. |
| **Något av de 21 DECAYING-mönstren** | Positiva i train, negativa i test. |
| **Break-even och trailing i LIVE** | Tidigare shadow-resultat: negativa eller brus. |
| **Högre hävstång eller större positioner** | Förväntat värde är negativt i varje period. Större position betyder snabbare förlust. |
| **Filter på BTC-regim (BTC 4h svag)** | Sämre i train (−0,16 mot −0,10) men *bättre* i test (−0,48 mot −0,88). Tecknet vänder. |

## Kontrafaktisk analys av förlorande LIVE-trades

**[FAKTA]** 83 verifierade LIVE-trades, varav 55 förlorare med sammanlagt −24,4 R.
- 51 av de 55 förlorarna hade minst en varningsflagga före entry.
- Samma flaggor satt också på vinnarna: överextension på 9 av 28 vinnare och låg volym på 10 av 28.

Därför bedöms varje regel på **alla** trades:

| Regel (tredjedel från train) | Blockerar | Förlorare i det blockerade | Netto R | Slump, samma antal | p (permutation) | q |
|---|---|---|---|---|---|---|
| OVEREXTENDED_4H | 32 | 23 | +8,49 | +3,63 | 0,07 | 0,58 |
| LOW_VOLUME_Z | 30 | 20 | +4,77 | +3,40 | 0,34 | 0,68 |
| BTC_4H_WEAK | 25 | 18 | +4,56 | +2,84 | 0,29 | 0,68 |
| WEAK_SIGNAL_SCORE | 9 | 7 | +3,20 | +1,02 | 0,15 | 0,59 |
| AT_4H_HIGH | 22 | 15 | +2,57 | +2,50 | 0,49 | 0,78 |
| LOW_RR | 21 | 13 | +0,05 | +2,38 | 0,77 | 0,88 |
| STALE_SIGNAL | 8 | 3 | −0,02 | +0,91 | 0,68 | 0,88 |
| RAN_BEFORE_FILL | 24 | 12 | −2,95 | +2,72 | 0,97 | 0,97 |

**[BEDÖMNING]** Förlusterna i LIVE beror i första hand på *när* vi handlade (26–28/9, en fallande altmarknad). Något enskilt entry-fel som går att förutse förklarar dem inte.

## Kvarvarande begränsningar

- **OI finns inte historiskt.** Varken DB eller BingX publika API ger det bakåt i tiden. `funding_oi` innehåller i praktiken bara funding.
- **Spread vid entry loggas inte.** Slippage vid entry mäts bara indirekt, som fill mot referenspris i LIVE.
- **Datan täcker 18 dagar** med tre marknadsregimer. Test-perioden är tre dagar i en fallande marknad, så det är tunt för slutsatser om regim.
- **Forward OOS har n = 0 i dag.** Det behövs uppskattningsvis 1–3 veckors drift för 30 observationer per sida, beroende på signalfrekvensen.
- **Två definitioner av oberoende.** Historiskt oberoende är "första per 6 h-fönster". I shadow-lagret är det strängare: "ingen kandidat på symbolen de senaste 6 h".
