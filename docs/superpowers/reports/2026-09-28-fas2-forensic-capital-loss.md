# Fas 2: forensisk utredning av kapitalförlusten i LIVE 26–28/9

**Period:** 2026-09-26 17:27 till 2026-09-28 11:15 UTC. Den omfattar alla 37 LIVE-trades med 100 USDT margin, 10x och ≈1 000 USDT notional.

**Hur utredningen gjordes (endast läsning, inga ändringar):**
- Databasen öppnades i `mode=ro`.
- Börsen lästes bara med GET: income-ledger, orderhistorik och publika 1m-klines.
- Ingen strategi, config, risk eller heuristik har ändrats.

**Märkning i rapporten:** varje påstående är märkt **[FAKTA]**, **[BERÄKNING]**, **[STATISTIK]**, **[HYPOTES]** eller **[REKOMMENDATION]**.

**Tre resultatkällor hålls isär genom hela rapporten:**

| Källa | Innehåll |
|---|---|
| **Faktiskt** | BingX income-ledger och fills: realiserad P/L, avgifter och funding per order |
| **DB (lokalt)** | Det systemet självt bokförde före Fas 1-avstämningen: exitpris, modellerad avgift 0,04 % × notional och ingen funding |
| **Modellerat** | 1m-kline-simulering av den konfigurerade policyn: SL/TP, PP-BE vid +1 %, 6 h, verklig avgift 0,1 % tur och retur, stop-slippage 0,075 %. Det är den nivå där simuleringen stämmer mot verkligheten, se §11. |

---

## 1. Sammanfattning

1. **[FAKTA]** Handelsresultatet enligt börsen är **−164,91 USDT** på 37 trades, alltså **−35,8 %** av startkapitalet 461,09 USDT.
   - Siffran "−148" var läget innan tre öppna positioner stängdes manuellt 28/9 11:14:58 UTC, vilket kostade −16,47.
   - Saldot är nu 420,00 USDT. Skillnaden förklaras av en insättning eller överföring på +123,82 USDT som inte finns i futures-kontots income-ledger. Den är härledd, se §2.
2. **[FAKTA/BERÄKNING]** Förlusten kommer i första hand från **entries som aldrig fungerade**:
   - 11 trades gick aldrig +1 % och stängdes av boten (6 full SL, 5 tidsgräns), totalt **−213,75 USDT**.
   - Alla andra grenar tillsammans gav +48,84.
3. **[FAKTA]** Gaten som gör en kandidat till CONFIRMED har **ingen innehållströskel**. Den kräver bara att alla 7 AI-roller svarat med `status="ok"`, att QA godkänt (form och konsistens) och att det finns kapacitet. Följden:
   - Systemet gick long i alla 37 fall trots att den egna prognosen gav bullish **högst 35 %** (median 25 %).
   - I 26 av 37 fall bedömde prognosen bearish som mer sannolikt än bullish.
   - Riskagenten varnade för överköpt läge i 17 fall.
   - 8 trades hade R:R < 1.
4. **[STATISTIK]** Det finns **ingen påvisbar selektions-edge**:
   - Ingen av cirka 70 testade egenskaper vid beslutstidpunkten skiljer vinnare från förlorare efter multipeltestning (minsta BH-q = 1,0).
   - 26 av 37 entries gick −1 % innan de gick +1 %.
   - Tagna trades gick inte bättre än kandidater som aldrig analyserades (förra rapporten).
5. **[FAKTA]** **Kapitalförstörelsen var snabb och korrelerad.** Under 27/9 22:00–28/9 09:00 UTC gjordes 9 fills som gav **−150,3 USDT, 91 % av hela förlusten**, samtidigt som BTC föll 1,96 %.
   - Sämsta 12 h-fönster: −174,9 USDT (−37,9 %).
   - Max drawdown: −182,5 USDT (−39,6 %).
6. **[FAKTA]** Risken per trade varierade **7×** (2,8–20 % av eget kapital). Orsaken är fast notional kombinerat med ett SL-avstånd som AI-riskagenten valde fritt (−1,3 % till −9,0 %).
   - Den största planerade risken som var öppen samtidigt var **208 USDT = 45 % av eget kapital**.
7. **[FAKTA]** Kostnader och exekvering:
   - Avgifter −36,83 (1,0 USDT per trade).
   - Slippage vid triggade exits −22,78 netto (stop-fills −31,48, TP +8,70).
   - Funding +7,35.
   - PP:s break-even-stopp: 16 trades gav −36,59, varav −25,18 i slippage och −15,96 i avgifter.
8. **[STATISTIK/HYPOTES]** Management var **inte** huvudorsaken: PP var netto bättre än den ursprungliga planen. Två förbättringar är dock väl underbyggda, men båda är in-sample:
   - **Avgiftstäckt BE** (+23,9 USDT; 16 trades bättre, 0 sämre; positivt i båda tidshalvorna).
   - **Trailing stop på 50 % av MFE** efter +1 % (+155 USDT; 16 bättre, 1 sämre; positivt i båda halvorna).
9. **[FAKTA]** GODFATHER påverkade ingenting (0 ingrepp). I urvalet var dess entry-bedömning **sämre än att inte filtrera alls**: filtret "EQ ≠ TRADE" hade gjort resultatet 29 USDT sämre.
10. **[FAKTA]** Operativa fel kostade nästan inget direkt i P/L. De tre stoppen (kreditstopp, tyst död och frysning), felbokförda exits och manuella stängningar gjorde däremot att data och övervakning var opålitliga. Det är åtgärdat i Fas 1 (57afe15).

**Svar på huvudfrågan:** förlusten beror i första hand på **entry**: signalkvalitet, en Gate utan innehållskrav och en korrelerad long-only-bok i en fallande marknad. I andra hand på **exekvering och kostnader i PP-mekanismen**. I tredje hand på **management**, där det fanns förbättringspotential men management inte var orsaken. Operativa fel hade nästan ingen direkt P/L-effekt.

---

## 2. Exakt P/L-avstämning

### Konto (börsen)

| Post | USDT | Källa |
|---|---|---|
| Startsaldo 26/9 16:00 UTC | **461,09** | [BERÄKNING] Saldo före överföringen (296,18) minus summan av alla income-poster |
| REALIZED_PNL | −135,43 | [FAKTA] income |
| TRADING_FEE | −36,83 | [FAKTA] income |
| FUNDING_FEE | +7,35 | [FAKTA] income |
| **Handelsresultat** | **−164,91** | [FAKTA] |
| Saldo efter sista trade | 296,18 | [BERÄKNING] |
| Överföring in (syns inte i futures-income) | +123,82 | [BERÄKNING] 420,00 − 296,18. Kontrollera gärna i BingX Assets → Transfer. |
| Saldo nu | 420,00 | [FAKTA] live-anrop, endast läsning |

### Per trade mot kontot

- **[FAKTA]** Summan av alla 37 trades från orderhistoriken (brutto + entry-avgift + exit-avgift + funding i hålltiden) blir **−164,91**. Det stämmer **exakt** mot income-ledgern. Inga oförklarade poster finns.

### De tre resultatkällorna

| Källa | Värde |
|---|---|
| **Faktiskt** (37 trades) | −164,91 |
| **DB före avstämning** | 30 trades bokförda som verifierade = −131,09. 7 trades var UNVERIFIABLE (TICKER-pris): f02740, e0c240, 509546, e30337, UNI, DOT och PEOPLE. |
| Börsen, samma 30 trades | −146,27 |
| **Modellerat** (32 icke-manuella) | −151,9, mot faktiskt −145,9 för samma trades |

- **[BERÄKNING]** DB **underskattade förlusten med 15,18 USDT** på sina 30 "verifierade" trades. Det beror på modellerade avgifter (0,04 % × notional, alltså ≈0,40 USDT per trade mot faktiska ≈1,00) och på att funding ignorerades.
- **[FAKTA]** Den modellerade avgiften räknas en gång på notional (0,04 %), medan den verkliga är 0,05 % per sida, alltså **≈0,10 % tur och retur**. Allt som bygger på modellerade avgifter underskattar därför kostnaden **≈2,5 gånger**. Det gäller paper-boken och GODFATHER:s kontrafaktiska motor.

---

## 3. Tabell trade för trade (alla 37, börsverifierade)

Kolumnförklaringar:
- **SL/TP avsedd/börs:** riskagentens förslag jämfört med de order som faktiskt lades på börsen. De stämde överens i 37 av 37 fall.
- **TP flyttad:** aldrig.
- **Slippage:** fill jämfört med triggerpris, bara för SL-, PP- och TP-order.
- **Verifiering:** alla 37 är nu VERIFIED via Fas 1-avstämningen.
- **Entry-klass**, bedömd i efterhand på prisbanan:
  - GOOD: första ±1 %-rörelsen gick upp.
  - BAD: MFE < 1 %.
  - BORDERLINE: första rörelsen gick ner, men MFE blev minst 1 %.

| # | Symbol | pid | Signal (UTC) | Fill (UTC) | Ålder signal→fill (min) | Rörelse ref→fill % | Fill | SL avsedd/börs | TP avsedd/börs | Notional | Lev | Avgifter | Funding (kostnad; − = erhållen) | Slippage vs triggerpris | Hålltid (min) | MFE % | MAE % | Exit (verifierad) | Exitpris | SL flyttad | TP flyttad | Manuell | GF EQ | Guardian | Verifiering | Faktiskt USDT | Faktiskt R | DB-resultat USDT (före avstämning) | Entry-klass |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | KAS | f8009c | 09-26T17:01 | 09-26T17:27 | 25 | +1.18 | 0.04901 | 0.0465/0.0465 | 0.051/0.051 | 1001 | 10 | 1.00 | +0.00 | -5.52 | 49 | 1.86 | -1.02 | PP-BE-stopp | 0.04873 | ja (BE) | nej | nej | WAIT | HOLD | VERIFIED | -6.60 | -0.13 | -6.12 | GOOD |
| 2 | JTO | 5a25ba | 09-26T17:01 | 09-26T17:27 | 25 | -1.72 | 0.6041 | 0.585/0.585 | 0.655/0.655 | 1000 | 10 | 1.00 | +0.00 | -0.33 | 53 | 1.67 | -0.56 | PP-BE-stopp | 0.6038 | ja (BE) | nej | nej | WAIT | HOLD | VERIFIED | -1.50 | -0.05 | -0.90 | GOOD |
| 3 | NCSKCRCL2USD | b23ea0 | 09-26T17:01 | 09-26T17:27 | 25 | -0.26 | 88.78 | 82.8/82.8 | 96.1/96.1 | 999 | 10 | 1.00 | +0.00 | – | 360 | 0.21 | -0.81 | TIME_LIMIT (bot) | 88.92 | nej | nej | nej | TRADE | HOLD | VERIFIED | +0.58 | +0.01 | +1.18 | BAD |
| 4 | 2Z | 795949 | 09-26T17:01 | 09-26T17:27 | 26 | +0.96 | 0.06952 | 0.0655/0.0655 | 0.0725/0.0725 | 1006 | 10 | 1.01 | +0.00 | -0.87 | 73 | 1.97 | -0.60 | PP-BE-stopp | 0.06945 | ja (BE) | nej | nej | TRADE | HOLD | VERIFIED | -1.98 | -0.03 | -1.42 | GOOD |
| 5 | APE | 8fcc5f | 09-26T18:40 | 09-26T18:48 | 8 | -0.13 | 0.1555 | 0.149/0.149 | 0.165/0.165 | 998 | 10 | 0.99 | +0.10 | – | 352 | 0.39 | -2.44 | TIME_LIMIT (bot) | 0.1539 | nej | nej | nej | TRADE | HOLD→WATCH | VERIFIED | -11.40 | -0.27 | -10.66 | BAD |
| 6 | 2Z | 8dc611 | 09-26T18:40 | 09-26T18:48 | 8 | -0.23 | 0.06958 | 0.0673/0.0673 | 0.0732/0.0732 | 997 | 10 | 1.00 | -1.75 | -0.43 | 89 | 2.85 | -1.09 | PP-BE-stopp | 0.06954 | ja (BE) | nej | nej | WAIT | HOLD | VERIFIED | +0.22 | +0.01 | -0.97 | BORDERLINE |
| 7 | DOT | 8afefe | 09-26T19:24 | 09-26T19:31 | 7 | -0.27 | 1.2384 | 1.205/1.205 | 1.285/1.285 | 996 | 10 | 0.99 | +0.10 | – | 353 | 0.51 | -1.99 | TIME_LIMIT (bot) | 1.2322 | nej | nej | nej | TRADE | HOLD→WATCH | VERIFIED | -6.08 | -0.23 | -5.39 | BAD |
| 8 | TRX | d206e1 | 09-26T22:13 | 09-26T22:19 | 6 | -0.09 | 0.33397 | 0.3243/0.3243 | 0.3445/0.3445 | 998 | 10 | 1.00 | -0.26 | – | 355 | 0.04 | -0.70 | TIME_LIMIT (bot) | 0.33271 | nej | nej | nej | TRADE | HOLD→WATCH | VERIFIED | -4.50 | -0.16 | -4.16 | BAD |
| 9 | 2Z | f02740 | 09-26T23:34 | 09-26T23:40 | 6 | -0.15 | 0.07107 | 0.0691/0.0691 | 0.0735/0.0735 | 997 | 10 | 1.00 | -3.11 | -1.82 | 21 | 1.56 | -0.72 | PP-BE-stopp | 0.07094 | ja (BE) | nej | nej | TRADE | HOLD | VERIFIED | +0.27 | +0.01 | UNVERIFIABLE | GOOD |
| 10 | 2Z | 9cf346 | 09-27T00:09 | 09-27T00:18 | 9 | -0.63 | 0.0689 | 0.0673/0.0673 | 0.0728/0.0728 | 993 | 10 | 0.99 | +0.00 | -0.86 | 150 | 3.40 | -1.71 | PP-BE-stopp | 0.06883 | ja (BE) | nej | nej | TRADE | HOLD | VERIFIED | -1.94 | -0.08 | -1.41 | BORDERLINE |
| 11 | QNT | 9ff72a | 09-27T02:37 | 09-27T02:47 | 10 | -0.12 | 168.52 | 155.0/155.0 | 188.0/188.0 | 998 | 10 | 1.00 | +0.00 | -4.56 | 7 | 2.92 | -1.35 | PP-BE-stopp | 167.75 | ja (BE) | nej | nej | TRADE | HOLD | VERIFIED | -5.61 | -0.07 | -4.96 | GOOD |
| 12 | BOME | e0c240 | 09-27T03:01 | 09-27T03:12 | 11 | -0.16 | 0.0010219 | 0.0009725/0.0009725 | 0.001125/0.001125 | 997 | 10 | 1.00 | +0.10 | – | 454 | 2.47 | -1.19 | EXTERN (manuell) | 0.0010334 | nej | nej | JA | TRADE | HOLD→WATCH | VERIFIED | +10.12 | +0.21 | UNVERIFIABLE | BORDERLINE |
| 13 | SOON | 290f60 | 09-27T03:50 | 09-27T03:59 | 9 | +2.06 | 0.2924 | 0.266/0.266 | 0.315/0.315 | 1020 | 10 | 1.02 | +0.23 | +1.39 | 33 | 3.76 | -2.09 | PP-BE-stopp | 0.2927 | ja (BE) | nej | nej | TRADE | HOLD | VERIFIED | -0.01 | -0.00 | +0.64 | BORDERLINE |
| 14 | 2Z | 509546 | 09-27T06:55 | 09-27T07:06 | 11 | +0.35 | 0.06906 | 0.0668/0.0668 | 0.0715/0.0715 | 1002 | 10 | 1.00 | -1.90 | – | 221 | 0.56 | -2.49 | EXTERN (manuell) | 0.06813 | nej | nej | JA | TRADE | HOLD | VERIFIED | -12.64 | -0.39 | UNVERIFIABLE | BAD |
| 15 | BNB | 7d5afa | 09-27T11:57 | 09-27T12:25 | 28 | +0.17 | 781.66 | 748.0/748.0 | 820.0/820.0 | 1001 | 10 | 1.00 | +0.10 | – | 466 | 0.14 | -1.05 | TIME_LIMIT (bot) | 778.79 | nej | nej | nej | WAIT | HOLD→WATCH | VERIFIED | -4.77 | -0.11 | -4.07 | BAD |
| 16 | SOON | 3a31c5 | 09-27T11:57 | 09-27T12:25 | 28 | +3.60 | 0.2879 | 0.264/0.264 | 0.292/0.292 | 1035 | 10 | 1.05 | +0.00 | +8.99 | 63 | 2.40 | -4.20 | TP (börsen) | 0.2945 | nej | nej | nej | TRADE | HOLD | VERIFIED | +22.78 | +0.27 | +23.31 | BORDERLINE |
| 17 | 2Z | 4bfb00 | 09-27T11:57 | 09-27T12:25 | 28 | -0.35 | 0.06761 | 0.0658/0.0658 | 0.0705/0.0705 | 995 | 10 | 0.98 | +0.00 | -1.91 | 159 | 0.09 | -2.88 | SL (börsen) | 0.06567 | nej | nej | nej | TRADE | HOLD | VERIFIED | -29.49 | -1.11 | -28.96 | BAD |
| 18 | AAVE | 061bee | 09-27T13:28 | 09-27T13:40 | 12 | -0.27 | 154.36 | 148.5/148.5 | 162.0/162.0 | 988 | 10 | 0.99 | +0.10 | – | 392 | 1.03 | -1.74 | TIME_LIMIT (bot) | 155.29 | nej | nej | nej | WAIT | HOLD→WATCH | VERIFIED | +4.86 | +0.13 | +5.56 | BORDERLINE |
| 19 | CRV | 3b47c4 | 09-27T14:09 | 09-27T14:20 | 11 | -0.23 | 0.3455 | 0.335/0.335 | 0.362/0.362 | 997 | 10 | 1.01 | +0.10 | – | 351 | 2.66 | -1.24 | TIME_LIMIT (bot) | 0.3517 | ja (BE) | nej | nej | TRADE | HOLD | VERIFIED | +16.66 | +0.55 | +17.49 | BORDERLINE |
| 20 | NEAR | 505221 | 09-27T20:12 | 09-27T20:40 | 28 | -0.64 | 5.45 | 5.32/5.32 | 5.75/5.75 | 992 | 10 | 0.99 | +0.00 | -2.18 | 90 | 2.39 | -0.51 | PP-BE-stopp | 5.438 | ja (BE) | nej | nej | REJECT | HOLD | VERIFIED | -3.17 | -0.13 | -2.58 | GOOD |
| 21 | QNT | e30337 | 09-27T20:12 | 09-27T20:40 | 28 | +3.55 | 194.58 | 179.5/179.5 | 199.0/199.0 | 1033 | 10 | 1.03 | +0.00 | +1.17 | 16 | 1.24 | -1.60 | PP-BE-stopp | 194.79 | ja (BE) | nej | nej | REJECT | HOLD | VERIFIED | +0.07 | +0.00 | UNVERIFIABLE | BORDERLINE |
| 22 | JUP | ef9b71 | 09-27T20:12 | 09-27T20:40 | 28 | -1.83 | 0.3438 | 0.336/0.336 | 0.367/0.367 | 981 | 10 | 1.01 | +0.00 | -0.29 | 69 | 6.95 | -0.58 | TP (börsen) | 0.3669 | ja (BE) | nej | nej | REJECT | HOLD | VERIFIED | +64.88 | +2.92 | +65.50 | GOOD |
| 23 | ZRO | 83d0f0 | 09-27T20:12 | 09-27T20:40 | 28 | -2.59 | 1.6668 | 1.645/1.645 | 1.81/1.81 | 973 | 10 | 0.97 | +0.00 | -2.16 | 56 | 0.93 | -1.80 | SL (börsen) | 1.6413 | nej | nej | nej | REJECT | HOLD | VERIFIED | -15.85 | -1.25 | -15.28 | BAD |
| 24 | POL | b317a9 | 09-27T20:12 | 09-27T20:56 | 44 | -1.82 | 0.12221 | 0.1195/0.1195 | 0.131/0.131 | 981 | 10 | 0.97 | +0.05 | -1.77 | 293 | 0.85 | -2.57 | SL (börsen) | 0.11928 | nej | nej | nej | REJECT | HOLD→WATCH | VERIFIED | -24.52 | -1.13 | -23.91 | BAD |
| 25 | JUP | 844dbd | 09-27T21:47 | 09-27T21:57 | 11 | +0.14 | 0.3664 | 0.348/0.348 | 0.395/0.395 | 1000 | 10 | 1.00 | +0.00 | -3.82 | 29 | 1.64 | -2.78 | PP-BE-stopp | 0.365 | ja (BE) | nej | nej | TRADE | HOLD | VERIFIED | -4.81 | -0.10 | -4.22 | BORDERLINE |
| 26 | JUP | 8819c7 | 09-27T22:37 | 09-27T22:50 | 13 | +0.69 | 0.3666 | 0.351/0.351 | 0.382/0.382 | 1006 | 10 | 1.01 | +0.05 | -0.27 | 80 | 2.26 | -0.44 | PP-BE-stopp | 0.3665 | ja (BE) | nej | nej | REJECT | HOLD | VERIFIED | -1.33 | -0.03 | -0.68 | GOOD |
| 27 | COW | a5bff2 | 09-27T23:05 | 09-27T23:16 | 11 | -0.06 | 0.1582 | 0.152/0.152 | 0.166/0.166 | 998 | 10 | 1.00 | -1.37 | -2.52 | 52 | 1.20 | -0.25 | PP-BE-stopp | 0.1578 | ja (BE) | nej | nej | TRADE | HOLD | VERIFIED | -2.39 | -0.06 | -2.92 | GOOD |
| 28 | ONDO | 58f1a3 | 09-27T23:54 | 09-28T00:02 | 8 | +0.05 | 0.5888 | 0.558/0.558 | 0.62/0.62 | 1000 | 10 | 0.97 | +0.00 | +0.00 | 75 | 0.88 | -5.47 | SL (börsen) | 0.558 | nej | nej | nej | TRADE | HOLD→WATCH | VERIFIED | -53.26 | -1.02 | -52.68 | BAD |
| 29 | LTC | ddb332 | 09-28T00:17 | 09-28T00:27 | 10 | -0.38 | 70.98 | 68.9/68.9 | 74.5/74.5 | 994 | 10 | 0.99 | +0.00 | – | 350 | 0.56 | -1.63 | TIME_LIMIT (bot) | 70.82 | nej | nej | nej | TRADE | HOLD→WATCH | VERIFIED | -3.23 | -0.11 | -2.64 | BAD |
| 30 | NIL | 9fc852 | 09-28T01:29 | 09-28T01:41 | 12 | -2.83 | 0.08776 | 0.08386/0.08386 | 0.09755/0.09755 | 971 | 10 | 0.97 | +0.00 | +0.66 | 77 | 4.84 | -1.57 | PP-BE-stopp | 0.08781 | ja (BE) | nej | nej | TRADE | HOLD→WATCH | VERIFIED | -0.40 | -0.01 | +0.16 | BORDERLINE |
| 31 | AVAX | 17d519 | 09-28T02:43 | 09-28T02:51 | 8 | -0.73 | 10.774 | 10.55/10.55 | 11.3/11.3 | 991 | 10 | 0.98 | +0.00 | -0.46 | 160 | 0.99 | -2.26 | SL (börsen) | 10.545 | nej | nej | nej | TRADE | HOLD | VERIFIED | -22.05 | -1.07 | -21.46 | BAD |
| 32 | NIL | c69971 | 09-28T02:19 | 09-28T02:58 | 38 | -3.10 | 0.08803 | 0.0845/0.0845 | 0.0954/0.0954 | 968 | 10 | 0.97 | +0.05 | -0.55 | 259 | 3.03 | -2.22 | PP-BE-stopp | 0.08797 | ja (BE) | nej | nej | TRADE | HOLD | VERIFIED | -1.66 | -0.04 | -1.05 | GOOD |
| 33 | VIRTUAL | 5120b5 | 09-28T06:17 | 09-28T06:28 | 11 | +0.31 | 0.8097 | 0.775/0.775 | 0.855/0.855 | 1002 | 10 | 0.98 | +0.05 | +0.00 | 155 | 0.57 | -4.53 | SL (börsen) | 0.775 | nej | nej | nej | TRADE | HOLD | VERIFIED | -43.94 | -1.02 | -43.35 | BAD |
| 34 | UNI | 6102be | 09-28T08:09 | 09-28T08:18 | 8 | +0.44 | 9.138 | 8.45/8.45 | 9.55/9.55 | 996 | 10 | 0.99 | +0.00 | – | 177 | 0.02 | -4.09 | EXTERN (manuell) | 8.945 | nej | nej | JA | TRADE | HOLD→WATCH | VERIFIED | -22.02 | -0.29 | UNVERIFIABLE | BAD |
| 35 | HBAR | f3494b | 09-28T09:19 | 09-28T09:31 | 12 | -0.74 | 0.11278 | 0.107/0.107 | 0.122/0.122 | 992 | 10 | 0.99 | +0.00 | -4.66 | 26 | 2.26 | -2.56 | PP-BE-stopp | 0.11224 | ja (BE) | nej | nej | WAIT | HOLD | VERIFIED | -5.74 | -0.11 | -5.14 | BORDERLINE |
| 36 | DOT | 33a842 | 09-28T09:46 | 09-28T09:54 | 9 | -0.18 | 1.1871 | 1.155/1.155 | 1.235/1.235 | 997 | 10 | 1.00 | +0.00 | – | 80 | 1.27 | -0.15 | EXTERN (manuell) | 1.1955 | ja (BE) | nej | JA | WAIT | HOLD→WATCH | VERIFIED | +6.10 | +0.23 | UNVERIFIABLE | GOOD |
| 37 | PEOPLE | 5d2b5f | 09-28T10:09 | 09-28T10:17 | 8 | +0.44 | 0.00875 | 0.00845/0.00845 | 0.00905/0.00905 | 1003 | 10 | 1.00 | +0.00 | – | 57 | 0.78 | -0.33 | EXTERN (manuell) | 0.008754 | nej | nej | JA | WAIT | HOLD→WATCH | VERIFIED | -0.54 | -0.02 | UNVERIFIABLE | BAD |

**Anteckningar:**
- **[FAKTA]** Guardian nådde aldrig PROTECT eller EXIT. GODFATHER har 0 verkställda beslut. Ingen stale signal öppnades: TTL 1 800 s hölls, och ålder signal→fill var 6–44 min, räknat från candidate `created_at`.
- **[FAKTA] Manuella stängningar (5):**
  - BOME och 2Z 509546 stängdes 27/9 10:46:53 UTC när boten var nere.
  - UNI, DOT och PEOPLE stängdes 28/9 11:14:58 UTC.
  - Alla fem var MARKET-order utan botens client-id.

---

## 4. Entry-analys

### 4.1 Varför blev signalerna CONFIRMED?

- **[FAKTA]** `gate/risk_signal_gate.py` bekräftar när tre villkor är uppfyllda: (a) alla 7 roller har `status == "ok"`, (b) `qa.passed` är sant och (c) det finns kapacitet. Inget värde från någon roll påverkar beslutet: varken prognosens sannolikheter, riskagentens varningar, R:R, bear-argument eller opportunity-score.
- **[FAKTA]** QA-agenten kontrollerar enligt sin egen definition bara att schemat är komplett och internt konsistent, inte själva innehållet.
- **[FAKTA]** SL och TP är riskagentens fritt valda förslag (`position_opening.py`). Det finns ingen R:R-regel och ingen gräns för SL-avstånd.
- **[FAKTA]** LIVE öppnar varje CONFIRMED-signal som ryms inom TTL och kapacitet, och som inte krockar med en redan öppen position i samma symbol.

### 4.2 Förlorande trades och de beslutsdata som fanns vid beslutet (24 trades med netto < −1 USDT)

| Symbol | pid | Netto USDT | Exit | Trigger | score | RSI 30m | volZ | Opp-score | Prognos bull/bear | Riskagent: överköpt | R:R | BTC 1h / 4h % | Ålder min | Ref→fill % | Återinträde | GF EQ | Vad hade stoppat den (info som fanns då) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ONDO | 58f1a3 | -53.26 | SL | price_volatility+momentum_breakout | 0.32 | 90 | 2.2 | 6.8 | 0.25/0.40 | ja | 1.01 | +0.33 / -0.26 | 8 | +0.05 | nej | TRADE | prognos bear>bull, RSI≥80 |
| VIRTUAL | 5120b5 | -43.94 | SL | volume | 0.25 | 42 | 7.7 | 3.2 | 0.30/0.25 | nej | 1.31 | +0.09 / -0.63 | 11 | +0.31 | nej | TRADE | BTC 4h<−0,5%, endast volym-trigger |
| 2Z | 4bfb00 | -29.49 | SL | funding_oi | 0.25 | 55 | -0.8 | 2.8 | 0.30/0.25 | nej | 1.60 | +0.06 / +0.23 | 28 | -0.35 | ja | TRADE | återinträde <6h |
| POL | b317a9 | -24.52 | SL | momentum_breakout | 0.02 | 76 | -2.8 | – | 0.28/0.38 | ja | 3.24 | -0.06 / +0.16 | 44 | -1.82 | nej | REJECT | prognos bear>bull |
| AVAX | 17d519 | -22.05 | SL | volume | 0.25 | 37 | 5.7 | 3.2 | 0.25/0.30 | nej | 2.35 | -0.38 / -0.89 | 8 | -0.73 | nej | TRADE | prognos bear>bull, BTC 4h<−0,5%, endast volym-trigger |
| UNI | 6102be | -22.02 | EXTERN | volume | 0.25 | 15 | 12.3 | 3.2 | 0.35/0.35 | nej | 0.60 | -0.07 / -0.48 | 8 | +0.44 | nej | TRADE | R:R<1, endast volym-trigger |
| ZRO | 83d0f0 | -15.85 | SL | momentum_breakout | 0.01 | 72 | -3.6 | – | 0.25/0.35 | ja | 6.57 | -0.11 / +0.05 | 28 | -2.59 | nej | REJECT | prognos bear>bull |
| 2Z | 509546 | -12.64 | EXTERN | funding_oi | 0.25 | 36 | -0.9 | 3.2 | 0.20/0.30 | nej | 1.08 | +0.04 / +0.07 | 11 | +0.35 | ja | TRADE | prognos bear>bull, återinträde <6h |
| APE | 8fcc5f | -11.40 | TIME | volume | 0.25 | 49 | 19.5 | 3.2 | 0.30/0.25 | nej | 1.46 | -0.11 / +0.00 | 8 | -0.13 | nej | TRADE | endast volym-trigger |
| KAS | f8009c | -6.60 | PP | momentum_breakout | 0.04 | 82 | -1.8 | 4.2 | 0.20/0.45 | ja | 0.79 | -0.05 / +0.10 | 25 | +1.18 | nej | WAIT | prognos bear>bull, RSI≥80, R:R<1 |
| DOT | 8afefe | -6.08 | TIME | volume | 0.25 | 53 | 6.2 | 3.2 | 0.32/0.25 | nej | 1.40 | -0.05 / -0.07 | 7 | -0.27 | nej | TRADE | endast volym-trigger |
| HBAR | f3494b | -5.74 | PP | price_volatility+momentum_breakout+volume | 0.37 | 85 | 3.1 | 7.2 | 0.30/0.40 | ja | 1.60 | -0.01 / -0.25 | 12 | -0.74 | nej | WAIT | prognos bear>bull, RSI≥80 |
| QNT | 9ff72a | -5.61 | PP | price_volatility+momentum_breakout+funding_oi | 0.36 | 77 | -0.5 | 7.2 | 0.25/0.45 | ja | 1.44 | +0.05 / +0.23 | 10 | -0.12 | nej | TRADE | prognos bear>bull |
| JUP | 844dbd | -4.81 | PP | price_volatility+momentum_breakout | 0.26 | 85 | -0.5 | 6.2 | 0.20/0.45 | ja | 1.55 | +0.16 / +0.16 | 11 | +0.14 | ja | TRADE | prognos bear>bull, RSI≥80, återinträde <6h |
| BNB | 7d5afa | -4.77 | TIME | momentum_breakout | 0.05 | 84 | -0.5 | – | 0.25/0.40 | ja | 1.14 | +0.06 / +0.18 | 28 | +0.17 | nej | WAIT | prognos bear>bull, RSI≥80 |
| TRX | d206e1 | -4.50 | TIME | volume | 0.25 | 15 | 24.4 | 3.2 | 0.32/0.28 | nej | 1.09 | +0.17 / +0.26 | 6 | -0.09 | nej | TRADE | endast volym-trigger |
| LTC | ddb332 | -3.23 | TIME | volume | 0.25 | 58 | 19.9 | 3.2 | 0.32/0.25 | nej | 1.69 | +0.62 / +0.24 | 10 | -0.38 | nej | TRADE | endast volym-trigger |
| NEAR | 505221 | -3.17 | PP | momentum_breakout | 0.01 | 73 | -1.2 | – | 0.30/0.30 | ja | 2.31 | -0.04 / +0.33 | 28 | -0.64 | nej | REJECT | inget testat kriterium |
| COW | a5bff2 | -2.39 | PP | funding_oi | 0.25 | 46 | -16.4 | 2.8 | 0.20/0.30 | nej | 1.26 | -0.13 / -0.68 | 11 | -0.06 | nej | TRADE | prognos bear>bull, BTC 4h<−0,5% |
| 2Z | 795949 | -1.98 | PP | funding_oi | 0.25 | 28 | -5.4 | 2.8 | 0.20/0.35 | ja | 0.74 | -0.05 / +0.12 | 26 | +0.96 | nej | TRADE | prognos bear>bull, R:R<1 |
| 2Z | 9cf346 | -1.94 | PP | price_volatility+funding_oi | 0.40 | 50 | -5.0 | 4.2 | 0.25/0.35 | nej | 2.44 | -0.10 / +0.31 | 9 | -0.63 | ja | TRADE | prognos bear>bull, återinträde <6h |
| NIL | c69971 | -1.66 | PP | price_volatility | 0.25 | 24 | -0.4 | 3.2 | 0.35/0.32 | nej | 2.09 | -0.50 / -1.03 | 38 | -3.10 | ja | TRADE | BTC 4h<−0,5%, återinträde <6h |
| JTO | 5a25ba | -1.50 | PP | momentum_breakout | 0.04 | 80 | -2.3 | – | 0.25/0.40 | ja | 2.66 | -0.07 / +0.06 | 25 | -1.72 | nej | WAIT | prognos bear>bull, RSI≥80 |
| JUP | 8819c7 | -1.33 | PP | momentum_breakout | 0.04 | 80 | -1.5 | 3.2 | 0.30/0.35 | ja | 0.99 | -0.55 / -0.66 | 13 | +0.69 | ja | REJECT | prognos bear>bull, RSI≥80, R:R<1, BTC 4h<−0,5%, återinträde <6h |

**Vad hade behövt vara annorlunda?**
- **[FAKTA]** 23 av 24 förlorare hade minst en av flaggorna i tabellen.
- **[FAKTA] Men vinnarna hade samma flaggor.** 5 av 6 vinnare (BOME, SOON 3a31, AAVE, JUP ef9b och DOT 33a8) hade också prognos bear>bull, och 4 av 6 hade endast volym-trigger. Flaggorna är vanliga i hela urvalet och skiljer inte ut förlorarna. Att de finns hos förlorarna bevisar därför ingenting.

### 4.3 Statistiska tester

Upplägg:
- Tre utfall testades: "entry fungerade" (första ±1 % uppåt, 11 mot 26 trades), "vinst > 0,1 R" (6 mot 31) och "full SL" (6 mot 31).
- 23 kontinuerliga egenskaper testades med permutationstest på medianer (4 000 permutationer) och Benjamini–Hochberg-korrigering över 69 tester. Dessutom 11 binära egenskaper med Fishers exakta test och BH.

| Resultat | Detaljer |
|---|---|
| **[STATISTIK]** Starkaste råa signaler | Entries som fungerade hade *längre* tid mellan CONFIRMED och fill: median 4,4 mot 1,4 min, p = 0,034. Vinnare hade *högre* volym-z: median 8,3 mot −0,5, p = 0,06. Full-SL-förlorare hade *snävare* SL: −2,4 % mot −4,0 %, p = 0,08. |
| Efter BH | **Alla q = 1,0. Inget överlever.** |
| **[STATISTIK]** Binära tester | Endast volym-trigger: fungerade 1 av 12 mot 10 av 25, p = 0,064, q = 0,54. Prognos bear>bull: q = 0,70. RSI ≥ 80: q = 0,82. BTC 1h < 0: q = 0,54. GF EQ = TRADE: q = 0,54. |
| **Slutsats** | **INSUFFICIENT_DATA.** Hypoteserna "momentum-utmattning (hög RSI)", "BTC faller medan vi går long på alts", "för gammal signal", "priset har redan rört sig", "låg kandidat-score", "svag R:R" och "motstridiga agentbedömningar" kan varken bekräftas eller förkastas statistiskt vid n = 37. Fördröjd entry blev tydligt *sämre* (§11). Det talar **emot** hypotesen "signal för gammal". |

### 4.4 Fanns bättre kandidater samtidigt?

**[STATISTIK]** Enligt förra rapporten, och oförändrat: tagna trades fick −0,26 R med en generisk bracket, medan aldrig analyserade BUDGET_LIMITED-kandidater (n = 339) fick −0,18 R. AI-kedjan valde alltså inte bättre än slumpen, och ingen kandidat kan identifieras som bättre med den information som fanns då.

---

## 5. Position management

| Fråga | Svar | Evidens |
|---|---|---|
| Positioner som först låg på plus men blev förluster | 13 trades nådde ≥ +1 % och stängde med förlust, **alla via PP-BE** (−0,01 till −6,60 USDT). **Ingen** av dem blev en full förlust. | [FAKTA] |
| Nästan-TP som vände | QNT 9ff72a (MFE 2,9 % mot TP 11,6 %), 2Z 795949 (1,97 % mot 4,3 %): nej, långt ifrån TP. SOON 290f60 nådde 3,76 % mot TP 7,7 %. Ingen trade som inte nådde TP kom längre än 60 % av TP-avståndet. | [FAKTA] |
| Borde SL ha flyttats enligt objektiva regler? | PP flyttade SL i 20 fall, varje gång korrekt enligt regeln (+1 % mark). Regeln lägger BE = entry **utan avgifter**, och stoppet fylls i snitt **−0,16 %** under triggern (mark-trigger, market-fill). | [FAKTA] |
| TP för långt bort? | Median-TP +4,9 %, median-MFE 1,6 %. Bara 2 av 37 nådde TP. Om TP låg på 50 % av avståndet: +64,7 USDT (5 bättre, 2 sämre). | [HYPOTES] |
| Exit för sent? | Tidsgräns: 8 bot-stängningar varav 2 för sent (+0,09 USDT, försumbart). Exit vid första thesis WEAKENING/INVALID: +120 USDT, men CI korsar 0 och effekten ligger i andra halvan (12,8 / 107,2). | [HYPOTES] |
| Exit för tidigt? | PP klippte 5 trades som enligt planen hade gett +241 USDT, men räddade 11 som hade gett −254. Netto var PP bättre än planen: +24,3 USDT (simulerat) / +12 USDT (faktiskt mot plan i förra rapporten). | [BERÄKNING] |
| Reagerade GODFATHER inte alls? | Korrekt: 455 thesis-rekommendationer, 0 verkställda, by design. | [FAKTA] |
| Kunde Guardian inte reagera? | Guardian observerade inom ≤ 1 min efter fill (förbättring). Tillståndet nådde aldrig PROTECT eller EXIT. Guardian WATCH som exit-regel: −0,5 USDT (ingen nytta). | [FAKTA] |
| Missade data eller övervakning utvecklingen? | Ja, två gånger: 27/9 07:13–11:57 (processen död) och 16:07–20:11 (frusen). Effekten var 2 manuella stängningar och 2 för sena tidsgränser. P/L-effekt ≈ 0 (§8). | [FAKTA] |

**Kvantifierad ansvarsfördelning [BERÄKNING]** (summerar till −164,91; se trädet i §8):

| Kategori | USDT |
|---|---|
| **Entry** (misslyckad entry, stängd av boten) | **−213,75** (11 trades) |
| **Management** (fungerande entries: PP-scratch, tidsgräns och TP) | **+67,74** (19 trades). Brutto från PP-scratch ≈ −1,3; kostnaden är avgifter, slippage och funding. |
| **Operativt och manuellt** | **−18,89** (7 trades) |

---

## 6. Exekveringsanalys

| Mått | Värde |
|---|---|
| **[FAKTA]** Stop-fills mot triggerpris | PP-BE-stopp i snitt −0,158 %, totalt −25,18 USDT (16 st). Ursprungliga SL −0,109 %, −6,30 USDT (6 st). |
| **[FAKTA]** TP-fills | +8,70 USDT (SOON 3a31 +8,99 genom en gap-fill över TP) |
| **[FAKTA]** Entry-fills | Market-order. Candidate `reference_price` → fill varierade −3,1 till +3,6 % på grund av discovery-latens (6–44 min), inte på grund av orderbokens påverkan. |
| **[FAKTA]** Orsak till PP-slippagen | Stoppet triggas på **mark price** och fylls som market på last price. På tunna alts (KAS −0,55 %, QNT −0,46 %, HBAR −0,47 %) blir BE-stoppet en förlust. |
| **[FAKTA]** Avgifter | 0,995 USDT per trade (0,05 %/sida taker). Det motsvarar 0,028 R per trade i snitt. |
| **[FAKTA]** Latens | Signal → gate 5–27 min, gate → claim 0–30 min, claim → fill < 1 s |
| **[STATISTIK]** Påverkade latens utfallet? | Nej, inte påvisbart. Fördröjning med ytterligare 15/30 min gav **−25 / −152 USDT** (§11). |

---

## 7. Kapital- och riskanalys: hur kunde 36 % försvinna på 42 timmar?

| Mekanism | Evidens | Klass |
|---|---|---|
| **Risk per trade** | Fast notional ≈ 1 000 USDT och AI-valt SL-avstånd −1,3 % till −9,0 % gav planerad risk **12,7–92 USDT = 2,8–20 % av eget kapital per trade** (median 8,4 %). En enda full SL kan ta 20 %. | [FAKTA] |
| **Samtidig exponering** | Max 4 samtidiga positioner = ≈ 4 000 USDT notional ≈ **8,7× eget kapital**. Max samtidig planerad risk **208 USDT = 45 % av eget kapital** (26/9 17:27, fyra fills i samma sekund). | [FAKTA] |
| **Korrelation** | Enbart longs på alts. Parvis 1m-korrelation mellan samtidigt öppna positioner: median 0,27 (n = 47 par). | [FAKTA] |
| **Förlustkluster** | 27/9 22:00–28/9 09:00 UTC: 9 fills, **−150,3 USDT (91 % av förlusten)**, 3 full SL (ONDO −53, AVAX −22, VIRTUAL −44) plus UNI −22 manuellt. BTC −1,96 % i samma fönster. | [FAKTA] |
| **Hastighet i drawdown** | Sämsta fönster: 1 h −52,8 (−11,4 %), 3 h −83,2 (−18,1 %), 6 h −104,8 (−22,7 %), 12 h −174,9 (−37,9 %). Per 6 h-block: 28/9 00–06 −103,5, 06–12 −70,0. | [FAKTA] |
| **Koncentration** | 2Z togs 6 gånger (−45,6 USDT), JUP 3, NIL 2, QNT 2, SOON 2, DOT 2. 8 återinträden inom 6 h gav −51,4 USDT, med 0 vinnare bland dem. | [FAKTA] |
| **Effektiv hävstång** | 10× per position och ≈ 8–10× på kontonivå vid 3–4 öppna positioner. Likvidation ligger vid ≈ −9,2 till −9,8 %. SOON 290f60 hade SL −9,03 %, bara ≈ 0,2–0,7 % från likvidation. Ingen likvidation inträffade. | [FAKTA] |
| **Gap och slippage** | Stop-slippage −0,11 till −0,16 % i snitt, som mest −0,55 %. Inga gap genom SL. | [FAKTA] |
| **Avgifter** | −36,83 = 22 % av nettoförlusten, 13,7 % av bruttoförlusterna | [FAKTA] |
| **Funding** | +7,35 (longs fick betalt, alltså en fördel) | [FAKTA] |
| **Upprepade förluster** | 6 fulla SL, varav 4 inom 8 h (28/9 00:02–09:03) | [FAKTA] |

**Mekanismen [BERÄKNING]:** en signal utan påvisad edge (≈ −0,13 R per trade) med **hög frekvens** (37 trades på 42 h, i snitt 2,4 samtidiga) och **stor, varierande risk per trade** (8 % av kapitalet i median) ger i snitt **−4,46 USDT per trade**. En korrelerad nedgång samlade dessutom förlusterna till ett enda kluster.

Snabbheten beror inte på någon enskild katastrof. Den är produkten av **frekvens × risk per trade × negativ edge × korrelation**. Med samma edge (−4,69 R totalt) och 1 % risk per trade hade samma serie kostat ungefär **−22 USDT** i stället för −165. Det är en skalningsberäkning, inte en rekommendation att ändra.

---

## 8. Rotorsaksträd

Summan är −164,91 USDT. Procentsatser anges mot **bruttoförlusterna** (summan av negativa grenar, −269,32), eftersom vinsterna (+104,42) annars ger andelar över 100 %.

```text
−164,91 USDT  (faktiskt, börsen; 37 trades)
│
├── ENTRY-FÖRLUSTER  −213,75  (79,4 %)   [11 trades]
│   ├── misslyckad entry → full SL          −189,12  (70,2 %)  6 trades: 2Z 4bfb, ZRO, POL, ONDO, AVAX, VIRTUAL
│   │     brutto −176,87 · slippage −6,30 · avgifter −5,85 · funding −0,10
│   │     MFE 0,09–0,99 % · alla gick −1 % först · 4 av 6 inom BTC-nedgången 28/9
│   └── misslyckad entry → tidsgräns         −24,63  (9,1 %)   5 trades: NCSK, APE, DOT 8afe, TRX, LTC
│         brutto −19,72 · avgifter −4,97
│
├── MANAGEMENT  netto +67,74   [19 trades med fungerande eller BORDERLINE entry]
│   ├── PP-BE-scratch                        −36,59  (13,6 %)  16 trades
│   │     brutto ≈ −1,34 · SLIPPAGE −25,18 · AVGIFTER −15,96 · funding +5,89
│   ├── tidsgräns (fungerade)                +16,66           CRV
│   └── TP                                   +87,67           SOON 3a31, JUP ef9b (varav TP-slippage +8,70)
│
├── EXEKVERING (tvärgående, redan fördelad ovan)
│   ├── stop-slippage (mark-trigger → market-fill)   −31,48
│   └── TP-gap-fill                                   +8,70
│
├── KOSTNADER (tvärgående, redan fördelad ovan)
│   ├── avgifter                              −36,83   (≈1,0 USDT per trade)
│   └── funding                                +7,35
│
└── OPERATIVT / MANUELLT  −18,89  (7,0 %)   [7 trades]
    ├── manuella stängningar                 −18,98  5 trades. Botens egen policy hade gett −30,16, alltså var manuellt +11,18 bättre.
    └── för sena tidsgränser (bot frusen)     +0,09  BNB, AAVE
```

| Gren | n | USDT | Andel av bruttoförluster | Evidens | Confidence | Åtgärdbar? |
|---|---|---|---|---|---|---|
| Misslyckad entry → SL | 6 | −189,12 | 70,2 % | Börsens fills + klines | Hög (fakta) | Ja: entry-veto, riskgräns per trade, regim och korrelation. Effekten är obevisad (§10). |
| Misslyckad entry → tidsgräns | 5 | −24,63 | 9,1 % | Samma | Hög | Samma |
| PP-scratch | 16 | −36,59 | 13,6 % | Fills | Hög | **Ja: avgiftstäckt BE** (mekaniskt, +24 USDT i simulering) |
| Manuellt och operativt | 7 | −18,89 | 7,0 % | Orderhistorik | Hög | Övervakning åtgärdad i Fas 1. Manuella stängningar är ditt beslut. |
| Avgifter (tvärgående) | 37 | −36,83 | – | Income | Hög | Bara genom färre trades eller maker-entries. Ingen förändring nu. |
| Slippage (tvärgående) | 24 | −22,78 | – | Fills | Hög | Delvis: BE-offset som täcker slippage |

---

## 9. Påvisade sårbarheter

| # | Sårbarhet | Svårighet | Evidensnivå | Evidens |
|---|---|---|---|---|
| V1 | **Gaten har ingen innehållströskel.** Alla 7 "ok" + QA-form ger CONFIRMED, oavsett prognos, riskvarningar eller R:R. | **CRITICAL** | **Påvisat** (kod + data) | 37 av 37 hade bullish ≤ 35 %, 26 av 37 bear > bull, 17 riskvarningar om överköpt, 8 med R:R < 1. Alla öppnades. |
| V2 | **Ingen påvisad signal-edge.** Entries går −1 % först i 70 % av fallen, och tagna trades är inte bättre än oanalyserade. | **CRITICAL** | **Starkt stödd** (CI inkluderar 0) | −0,13 R/trade (−4,69 R på 37), CI från förra rapporten (n = 34): −0,35 till +0,12. 0 av 69 tester överlever BH. |
| V3 | **Risk per trade styrs inte.** Fast notional med AI-valt SL-avstånd ger 2,8–20 % av kapitalet per trade och 45 % samtidigt. | **HIGH** | **Påvisat** (mekanism) | §7 |
| V4 | **Ingen regim- eller korrelationskontroll** i en long-only-bok på alts | **HIGH** | Påvisad mekanism; orsakssambandet är en **stark hypotes** | 91 % av förlusten i ett BTC-nedgångsfönster, korrelation 0,27 |
| V5 | **PP-BE blir en förlust efter kostnader** (BE = entry, mark-trigger och market-fill) | **HIGH** | **Påvisat** (mekanism) | 16 av 16 PP-exits ger i snitt −2,29 USDT. Avgiftstäckt BE +23,9 USDT, 16 bättre, 0 sämre. |
| V6 | **SL kan ligga nära likvidationen** (ingen spärr) | **HIGH** (latent) | Påvisat, ännu inte inträffat | SOON −9,03 % mot likvidation ≈ −9,2 % |
| V7 | **Upprepade återinträden i samma symbol** | MEDIUM | Svag hypotes | 8 återinträden, −51,4 USDT, 0 vinnare. Positivt i båda halvorna men n = 8. |
| V8 | **Modellerade avgifter är 2,5× för låga** (0,04 % × notional mot faktiska 0,10 % tur och retur). Paper-boken och GF:s kontrafaktiska motor ser därför bättre ut än verkligheten. | MEDIUM | **Påvisat** | §2 |
| V9 | **GODFATHER:s EQ har ingen eller negativ prediktiv kraft** | MEDIUM | Påvisat för urvalet | Filter på EQ: −22 till −29 USDT |
| V10 | Operativt: tyst död, frysning, AI-kredit, TICKER-exits | MEDIUM → **åtgärdat** (57afe15) | Påvisat | Fas 1 |
| V11 | Discovery-latens 6–44 min | LOW | Hypotesen falsifierad i urvalet | Fördröjd entry var sämre |
| V12 | Avgifter som andel av en noll-edge | LOW–MEDIUM | Påvisat | 0,028 R per trade |

---

## 10. Hypoteser som kräver mer data

| Hypotes | Data idag | Behov |
|---|---|---|
| Trailing stop på 50 % av MFE efter +1 % förbättrar resultatet | +155 USDT in-sample, båda halvorna positiva, 16/1 | OOS på nya trades, n ≥ 100 |
| BE-trigger vid +0,5 % (i stället för +1 %) | +198 USDT, men 161 av dem i andra halvan (kraschen) | Regimberoende. Kräver OOS och trades i uppåtmarknad. |
| Veto för RSI ≥ 80 | +70 USDT (8 borttagna, 0 sämre), Fisher q = 0,82 | n ≥ 30 med RSI ≥ 80 |
| Veto vid BTC 4h < −0,5 % | +69 USDT, allt i andra halvan | Fler regimbyten |
| Veto för endast volym-trigger | +73 USDT, fungerade 1 av 12 (q = 0,54) | n ≥ 40 |
| Veto vid prognos bear > bull | +75 USDT, men 5 av 6 vinnare också bear > bull | Prognosen är inte kalibrerad. Kalibrera den först. |
| Veto för återinträde < 6 h | +73 USDT, 8/0 | n ≥ 30 återinträden |
| Exit vid första thesis WEAKENING | +120 USDT, CI korsar 0, andra halvan dominerar | OOS |
| TP på 50 % av avståndet | +65 USDT, 5/2 | OOS |

Dessa hypoteser testades på samma 37 trades som genererade dem. Ungefär 27 varianter prövades, så minst ett par "positiva" utfall väntas av ren slump.

---

## 11. Kontrafaktiska resultat (utan lookahead)

**Metod:** varje regel använder bara information som fanns vid beslutstidpunkten, och simuleringen går minut för minut framåt på börsens 1m-klines.
- Om SL och TP träffas i samma candle räknas SL först.
- Stopp fylls 0,075 % under triggern.
- Avgift 0,10 % tur och retur.
- Horisont 6 h.
- Kalibrering: simuleringen av den faktiska policyn ger −151,9 mot faktiska −145,9 (32 icke-manuella trades, genomsnittligt absolut fel 1,69 USDT per trade).
- **Baslinjen** är den simulerade faktiska policyn, −182,0 för alla 37. Den skiljer sig från −164,91 eftersom de manuella stängningarna här simuleras som botens policy.

| Variant | Δ USDT mot baslinjen | 95 % CI för Δ per trade | Bättre/sämre | Halvor (tid) | Bedömning |
|---|---|---|---|---|---|
| Ingen PP (original-SL/TP) | −24,3 | −8,7 … +8,8 | 5 / 11 | +94,9 / −119,1 | PP netto bättre, osäkert |
| **BE + 0,15 % efter +1,0 %** | **+23,9** | **+0,40 … +0,89** | **16 / 0** | **+12,0 / +11,9** | **Robust (mekaniskt)** |
| BE + 0,15 % efter +0,5 % | +198,0 | +1,5 … +10,0 | 24 / 2 | +36,9 / +161,0 | Stark men regimberoende |
| BE + 0,15 % efter +1,5 % | +6,0 | −2,6 … +2,4 | 15 / 1 | +12,0 / −6,0 | Oklart |
| BE + 0,15 % efter +2,0 % | −72,7 | −5,9 … +1,3 | 12 / 4 | – | Sämre |
| **Trailing 50 % av MFE efter +1 %** | **+155,3** | **+2,4 … +6,2** | **16 / 1** | **+88,7 / +66,6** | **Starkt stödd (in-sample)** |
| TP på 50 % av avståndet | +64,7 | −1,4 … +4,9 | 5 / 2 | +49,6 / +15,1 | Hypotes |
| TP på 75 % av avståndet | −20,2 | −1,5 … 0 | 0 / 2 | – | Sämre |
| Tidsexit 2 h | +34,6 | −1,3 … +3,2 | 9 / 7 | +7,2 / +27,4 | Oklart |
| Fördröjd entry 15 min | −25,2 | −5,0 … +3,7 | 11 / 8 | – | Sämre |
| **Fördröjd entry 30 min** | **−152,4** | −9,8 … +0,4 | 10 / 12 | −69,7 / −82,8 | **Sämre**: signalen är inte "för gammal" |
| Exit vid thesis WEAKENING/INVALID | +120,0 | −1,7 … +8,1 | 14 / 8 | +12,8 / +107,2 | Hypotes |
| Exit vid thesis INVALID | +65,6 | −1,8 … +5,4 | 11 / 6 | −17,2 / +82,8 | Hypotes |
| Exit vid Guardian WATCH | −0,5 | – | 4 / 7 | – | Ingen nytta |
| Veto: prognos bear > bull (26 bort) | +74,7 | −3,3 … +7,2 | 20 / 5 | +25,9 / +48,8 | Hypotes |
| Veto: R:R < 1 (8 bort) | +15,5 | −0,9 … +2,0 | 6 / 2 | – | Svag |
| Veto: RSI ≥ 80 (8 bort) | +69,8 | +0,19 … +5,0 | 8 / 0 | +10,5 / +59,2 | Hypotes |
| Veto: BTC 1h < 0 (24 bort) | +48,9 | −3,4 … +5,0 | 19 / 4 | +28,5 / +20,4 | Hypotes |
| Veto: BTC 4h < −0,5 % (7 bort) | +68,9 | −0,1 … +4,9 | 6 / 1 | 0 / +68,9 | Regimberoende |
| Veto: signalålder > 20 min (13 bort) | ≈ +9 | – | – | – | Ingen effekt |
| Veto: pris upp > 1 % före fill (4 bort) | −6,1 | – | 3 / 1 | – | Sämre |
| Veto: endast volym-trigger (12 bort) | +73,3 | −0,8 … +5,4 | 7 / 5 | +8,3 / +65,0 | Hypotes |
| Veto: GF EQ ≠ TRADE (14 bort) | **−29,1** | – | 10 / 4 | – | **Sämre**: använd inte EQ |
| Veto: GF EQ = REJECT (6 bort) | −22,0 | – | 5 / 1 | – | Sämre |
| Veto: återinträde < 6 h (8 bort) | +73,3 | +0,19 … +4,6 | 8 / 0 | +68,1 / +5,2 | Hypotes |

**Större förluster var för sig [BERÄKNING]:**

| Trade | Vad hade hjälpt (in-sample) | Vad hade inte hjälpt |
|---|---|---|
| ONDO (−53) | Veto för RSI ≥ 80 eller prognos bear > bull. MFE 0,88 %, så BE vid +0,5 % hade gett ≈ +0,5. | Trailing, som kräver +1 % |
| VIRTUAL (−44) | Veto för BTC 4h eller volym-trigger. MFE 0,57 %, så BE vid +0,5 % hade hjälpt. | – |
| 2Z 4bfb (−29) | Veto för återinträde. MFE 0,09 %. | Varken BE eller trailing |
| POL (−25) och ZRO (−16) | Veto för prognos bear > bull. MFE 0,85 / 0,93 %, så BE vid +0,5 % hade hjälpt. | – |
| AVAX (−22) | Flera veton. MFE 0,99 %, så BE vid +0,5 % hade hjälpt. | – |

**Mönstret:** 5 av 6 fulla förluster rörde sig +0,5–1,0 % innan de föll. De dödades alltså inte omedelbart, men nådde aldrig PP:s +1 %.

---

## 12. Förebyggande och åtgärdsplan (inget är implementerat)

| Problem | Evidens | Påverkan | Förebyggande åtgärd | Risk | Test |
|---|---|---|---|---|---|
| V5: PP-BE är en förlust efter kostnader | 16 av 16 exits, snitt −2,29 | ≈ +24 USDT per 37 trades | BE-pris = entry × (1 + avgifter + ≈ 0,1 % slippagebuffert) | Låg: små trades blir scratch med ± 0 i stället för −2 | Replay på alla LIVE-trades hittills + OOS på kommande 30 trades |
| V3: risk per trade styrs inte | 2,8–20 % per trade | Förlusternas storlek | Riskbaserad sizing (samma USDT-risk per trade) eller ett tak för planerad risk i % av eget kapital. Sizing ändras bara efter ditt uttryckliga beslut. | Medel: färre eller mindre positioner | Replay: samma trades i R. USDT-varians och max drawdown ska minska. |
| V6: SL nära likvidation | SOON −9,03 % | Latent | Spärr: SL-avstånd ≤ likvidationsavstånd − 2 % (eller skippa entry) | Låg | Enhetstest och replay |
| V1: Gaten saknar innehållskrav | 37 av 37 bullish ≤ 35 % | Hela entry-grenen | Deterministiska veto-regler, **först i shadow mode**: loggas men blockerar inte. Kandidater: R:R-golv, prognos-bull ≥ bear, RSI-tak. | Medel: kan ta bort vinnare (5 av 6 hade bear > bull) | Shadow i ≥ 100 kandidater, förregistrerat OOS-test med BH |
| V4: regim och korrelation | 91 % av förlusten i ett BTC-fönster | Klustringen | Shadow-mätning av BTC-regim och samtidig korrelation. Senare ett tak för antal samtidiga long på alts vid BTC-nedgång. | Medel | Shadow + OOS |
| V7: återinträde | 8/0 | −51 USDT | Shadow-flagga för återinträde < 6 h | Låg | OOS |
| V2: signal-edge | −0,14 R | Allt | Mät edge per triggertyp i paper med **verkliga avgifter** (V8) innan LIVE-risken ökas | – | ≥ 200 paper-trades per triggertyp |
| V8: fel avgiftsmodell | 0,04 % mot 0,10 % | Förvränger paper och GF | Rätta `fee_pct` till tur och retur 0,10 % (eller per sida) | Låg: bara rapportering och learning | Enhetstest mot börsens avgifter |
| Trailing / MFE-skydd | +155 in-sample | Potentiellt stort | Shadow-simulering per trade, loggad live | Låg i shadow | OOS ≥ 50 trades |
| Övervakning | Fas 1 klar | – | Håll kvar | – | Klart |

---

## 13. Prioriteringsordning [REKOMMENDATION]

1. **Riskgräns per trade och spärr mot likvidation** (V3, V6). Detta minskar förlustens *storlek* oavsett om vi har edge. Kräver ditt beslut om sizing.
2. **Avgiftstäckt BE i PP** (V5). Mekaniskt bevisad, låg risk.
3. **Rätta avgiftsmodellen** (V8). Utan det lär sig paper och GODFATHER på för optimistiska siffror.
4. **Veto-regler för Gaten i shadow mode** (V1), plus regim och korrelation (V4) och återinträde (V7). Bara loggning, OOS-test förregistreras.
5. **Trailing/MFE-skydd i shadow mode.**
6. **Utvärdera edge per triggertyp** i paper med verkliga kostnader (V2) innan LIVE-risken ökas igen.

---

## 14. Vad som inte ska ändras än

- **Inga veto-regler i LIVE** baserade på detta urval: n = 37, cirka 27 varianter testade, 0 överlever BH.
- **GODFATHER:s EQ får inte styra** entries. Den var sämre än inget filter.
- **Inget skifte till fördröjd entry**: det var tydligt sämre.
- **Guardian WATCH ska inte bli en exit-signal**: ingen nytta.
- **BE-trigger vid +0,5 %** ska inte införas trots +198 USDT. Effekten är regimberoende, och 161 av 198 kom från en enda nattlig krasch.
- **PP ska inte tas bort.** Utan PP hade det blivit −24 USDT sämre, och 13 trades hade annars riskerat att bli fulla förluster.
- **Ingen heuristik ska promotas**, och ingen GODFATHER-authority ska aktiveras.

---

## 15. Vad som bör testas härnäst

1. **Replay-test (endast läsning):** avgiftstäckt BE, riskbaserad sizing och likvidationsspärr på alla verifierade LIVE-trades hittills, uttryckt både i USDT och R.
2. **Shadow-loggning** vid varje CONFIRMED: prognos bull/bear, R:R, RSI, BTC 1h/4h, antal samtidiga long på alts, återinträde samt "hade vetot slagit till" (utan att blockera).
3. **Förregistrerat OOS-test** efter ≥ 100 nya LIVE- eller paper-trades med verkliga avgifter. Samma hypoteser som i §10, BH över alla tester, och train/test-uppdelning i tid.
4. **Kalibrering av prognosagenten:** Brier-poäng för "bullish" mot faktiskt 6 h-utfall på alla analyserade kandidater (inte bara de tagna).
5. **Edge per triggertyp** i paper (≥ 200 per typ) med rättad avgiftsmodell.

---

*Underlaget kommer från läsbara skript i sessionens scratchpad (`build.py`, `analysis2.py`, `ledger.py`, `compete.py`, `dryrun_reconcile.py`), som bara läser. Alla börsdata hämtades med GET. Siffrorna kan reproduceras från databasen (endast läsning) och från BingX orderhistorik och income-ledger.*
