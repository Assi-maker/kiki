# LIVE 100 USDT × 10x: forensisk status- och post-trade-rapport

**Period:** 2026-09-26 17:27 UTC (första fill med 100 USDT margin) till 2026-09-28 ca 10:50 UTC (≈41 h)

**Typ:** endast läsning (READ-ONLY).
- Databasen öppnades med `mode=ro`.
- Börsen anropades bara med GET: balance, positions, income, allOrders och publika klines.
- Inga ordrar, inga kod- eller configändringar och ingen commit har gjorts.

**Sanningskälla:**
- BingX orderhistorik (`allOrders`) och income-historik (REALIZED_PNL, TRADING_FEE, FUNDING_FEE) för P/L, fills och exits.
- Databasen för signaler, Guardian, PP och GODFATHER.
- 1m-klines från börsen för MFE/MAE och kontrafaktiska scenarier.

**Konventioner:**
- R = netto-USDT dividerat med planerad risk, där planerad risk = qty × (entry − initial SL på börsen).
- "BE" = break-even.
- "Plan utan PP" betyder original-SL/TP plus 6 h time limit, simulerat på 1m-klines. Om SL och TP träffas i samma candle räknas SL först (konservativt). Avgifter är modellerade till 0,1 % round-trip.

---

## 1. LIVE-status

### Konto just nu (live-anrop, endast läsning)

| Post | Värde |
|---|---|
| Balance | **311,14 USDT** |
| Equity | **301,78 USDT** |
| Orealiserad P/L | **−9,36 USDT** |
| Använd margin | 299,66 USDT |
| **Ledig margin** | **11,48 USDT**: ingen fjärde position kan öppnas, eftersom den kräver ≥101 |

### Resultat sedan övergången

| Post | Värde |
|---|---|
| Startsaldo | **461,08 USDT**. Härlett exakt: nuvarande balance minus summan av alla income-poster sedan 26/9 16:00 UTC. Inga transfers finns i perioden. |
| Förändring av equity | **−159,30 USDT (−34,5 %)** |
| Realiserad P/L brutto (börsen) | −121,95 USDT |
| Trading fees | −35,34 USDT. Varav 33,84 på stängda trades och 1,50 är entry-avgift på öppna. |
| Funding | **+7,35 USDT**, erhållen |
| **Realiserad netto (34 stängda)** | **−148,44 USDT** |
| Antal LIVE-trades | 37: 34 stängda och 3 öppna |

**Utfall, W/L/BE:**

| Gräns | Vinster | Scratch | Förluster |
|---|---|---|---|
| ±0,1 R | 5 | 14 | 15, varav 6 fulla SL på ≈ −1 R |
| ±1 USDT | 5 | 6 | 23 |

**Nyckeltal:**

| Mått | Värde |
|---|---|
| Expectancy per trade | **−4,37 USDT** (95 % bootstrap-CI −10,65 till +2,16) |
| Expectancy i R | **−0,136 R** (CI −0,35 till +0,12) |
| Profit factor | **0,45** |
| Snittvinst / medianvinst | +23,86 / +16,66 USDT |
| Snittförlust / medianförlust | −11,67 / −5,61 USDT |
| Största vinst | +64,88 USDT (JUP ef9b71, TP, +2,92 R) |
| Största förlust | −53,26 USDT (ONDO 58f1a3, SL, −1,02 R) |
| Max drawdown | **−167,5 USDT (−36,3 % av start)**, på den realiserade income-strömmen |
| Hålltid | Snitt 170 min, median 90 min |
| Exponering | Max 4 samtidiga positioner (≈4 000 USDT notional ≈ 8,7× startequity). I snitt 2,43 samtidiga. |
| Planerad risk per trade | Snitt 42,5 USDT, spann **12,7–92,1 USDT** |

### Öppna positioner (vid kontrolltillfället)

| Symbol | Entry | SL | TP | Planerad risk | Orealiserat | Status |
|---|---|---|---|---|---|---|
| UNI | 9,138 | 8,45 (−7,5 %) | 9,55 | 75,0 | −21,5 | MAE −4,1 %, Guardian WATCH |
| DOT | 1,1871 | BE (PP flyttat) | 1,235 | – | +10,3 | PP aktivt |
| PEOPLE | 0,00875 | 0,00845 | 0,00905 | 34,4 | +1,4 | – |

### Verifieringsklass

| Klass | Antal | Kommentar |
|---|---|---|
| Verifierad av systemet (`EXCHANGE_ORDER`/`MARKET_CLOSE`) | 30 | DB-exit matchar börsen |
| **UNVERIFIABLE i systemet (`TICKER`)** | **4** | f02740, e0c240, 509546, e30337. Alla fyra går att **rekonstruera exakt** från börsens orderhistorik, vilket jag har gjort i den här rapporten. |
| – varav fel exit-orsak i DB | 3 | BOME e0c240 och 2Z 509546 står som `stop_loss` men stängdes av en **extern MARKET-order** utan bot-clientOrderId, 27/9 10:46:53 UTC, medan boten var nere. QNT e30337 står som `target` men stängdes av PP-BE-stoppet. |
| – varav fel exit-pris i DB | 3 | Avvikelse −0,13 % till −0,24 % |
| Ofullständig observation | 2 | BOME och 2Z 509546: Guardian låg 213 min utan observation före exit, på grund av avbrottet. Deras MFE/MAE från klines är fullständiga. |
| Ofullständig kontrafaktisk data | 1 | HBAR: 6 h-fönstret var inte passerat när datan hämtades |

---

## 2. Trade för trade

`sig→fill` = minuter från candidate `created_at` till fill på börsen. PP "BE @Xm" = minuter efter fill då SL flyttades till break-even. Guardian visar tillståndssekvensen. Thesis visar första WEAKENING/INVALID i minuter efter fill.

**Entry-klass, bedömd efter entry på prisbanan:**
- GOOD: första ±1 %-rörelsen gick upp.
- BAD: MFE < 1 % under hela traden.
- BORDERLINE: första rörelsen gick ner, men MFE blev minst 1 %.

**Tidsstämplar:**
- Signal- och gate-tider finns per trade i underlaget.
- GODFATHER EQ `assessed_at` är satt till gate-tiden. Den skrivs i efterhand av supervisorn med as-of-data, och `enforced=0` gäller alla.
- Claim-tiden ligger 0–30 min efter gate.
- Fill-tiden kommer från börsen.

| # | Symbol | pid | Trigger | score | RSI | volZ | sig→fill (min) | GF EQ | Entry | SL % | TP % | Risk USDT | Exit | Net USDT | R | MFE % | MAE % | t→MFE (min) | Första ±1 % | PP | Guardian | Thesis första WEAK/INVALID (min) | Entry-klass | Plan utan PP R |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | KAS | f8009c | momentum_breakout | 0.04 | 82 | -1.8 | 25 | WAIT | 0.04901 | -5.1 | 4.1 | 51.3 | PP-BE-stop | -6.60 | -0.13 | 1.86 | -1.02 | 38 | UP | BE @16m | HOLD | –/– | GOOD | -0.88 (TIME) |
| 2 | JTO | 5a25ba | momentum_breakout | 0.04 | 80 | -2.3 | 25 | WAIT | 0.6041 | -3.2 | 8.4 | 31.6 | PP-BE-stop | -1.50 | -0.05 | 1.67 | -0.56 | 32 | UP | BE @32m | HOLD | 5/5 | GOOD | -0.16 (TIME) |
| 3 | NCSKCRCL2USD | b23ea0 | momentum_breakout+funding_oi | 0.11 | 79 | -11.3 | 25 | TRADE | 88.78 | -6.7 | 8.2 | 67.3 | TIME_LIMIT | 0.58 | 0.01 | 0.21 | -0.81 | 354 | NONE | – | HOLD | 127/– | BAD | 0.01 (TIME) |
| 4 | 2Z | 795949 | funding_oi | 0.25 | 28 | -5.4 | 26 | TRADE | 0.06952 | -5.8 | 4.3 | 58.2 | PP-BE-stop | -1.98 | -0.03 | 1.97 | -0.60 | 21 | UP | BE @20m | HOLD | –/– | GOOD | 0.72 (TP) |
| 5 | APE | 8fcc5f | volume | 0.25 | 49 | 19.5 | 8 | TRADE | 0.1555 | -4.2 | 6.1 | 41.7 | TIME_LIMIT | -11.40 | -0.27 | 0.39 | -2.44 | 17 | DOWN | – | HOLD↔WATCH ×8 | 15/31 | BAD | -0.30 (TIME) |
| 6 | 2Z | 8dc611 | funding_oi | 0.25 | 42 | -3.5 | 8 | WAIT | 0.06958 | -3.3 | 5.2 | 32.7 | PP-BE-stop | 0.22 | 0.01 | 2.85 | -1.09 | 39 | DOWN | BE @26m | HOLD | 31/31 | BORDERLINE | -0.39 (TIME) |
| 7 | DOT | 8afefe | volume | 0.25 | 53 | 6.2 | 7 | TRADE | 1.2384 | -2.7 | 3.8 | 26.9 | TIME_LIMIT | -6.08 | -0.23 | 0.51 | -1.99 | 34 | DOWN | – | HOLD↔WATCH ×3 | 33/33 | BAD | -0.18 (TIME) |
| 8 | TRX | d206e1 | volume | 0.25 | 15 | 24.4 | 6 | TRADE | 0.33397 | -2.9 | 3.2 | 28.9 | TIME_LIMIT | -4.50 | -0.16 | 0.04 | -0.70 | 91 | NONE | – | HOLD→WATCH | 48/– | BAD | -0.17 (TIME) |
| 9 | 2Z | f02740 | funding_oi | 0.25 | 61 | -7.1 | 6 | TRADE | 0.07107 | -2.8 | 3.4 | 27.6 | PP-BE-stop | 0.27 | 0.01 | 1.56 | -0.72 | 8 | UP | BE @7m | HOLD | –/– | GOOD | -1.04 (SL) |
| 10 | 2Z | 9cf346 | price_volatility+funding_oi | 0.40 | 50 | -5.0 | 9 | TRADE | 0.0689 | -2.3 | 5.7 | 23.1 | PP-BE-stop | -1.94 | -0.08 | 3.40 | -1.71 | 91 | DOWN | BE @82m | HOLD | 112/143 | BORDERLINE | -1.04 (SL) |
| 11 | QNT | 9ff72a | price_volatility+momentum_breakout+funding_oi | 0.36 | 77 | -0.5 | 10 | TRADE | 168.52 | -8.0 | 11.6 | 80.0 | PP-BE-stop | -5.61 | -0.07 | 2.92 | -1.35 | 1 | UP | BE @2m | HOLD | –/– | GOOD | 1.43 (TP) |
| 12 | BOME | e0c240 | volume | 0.25 | 41 | 15.7 | 11 | TRADE | 0.0010219 | -4.8 | 10.1 | 48.2 | EXTERN market* | 10.12 | 0.21 | 2.47 | -1.19 | 284 | DOWN | – | HOLD↔WATCH ×2 | 60/152 | BORDERLINE | 0.21 (TIME) |
| 13 | SOON | 290f60 | price_volatility+momentum_breakout | 0.28 | 84 | 0.5 | 9 | TRADE | 0.2924 | -9.0 | 7.7 | 92.1 | PP-BE-stop | -0.01 | -0.00 | 3.76 | -2.09 | 14 | DOWN | BE @14m | HOLD | –/– | BORDERLINE | 0.45 (TIME) |
| 14 | 2Z | 509546 | funding_oi | 0.25 | 36 | -0.9 | 11 | TRADE | 0.06906 | -3.3 | 3.5 | 32.8 | EXTERN market* | -12.64 | -0.39 | 0.56 | -2.49 | 5 | DOWN | – | HOLD | –/– | BAD | -1.03 (SL) |
| 15 | BNB | 7d5afa | momentum_breakout | 0.05 | 84 | -0.5 | 28 | WAIT | 781.66 | -4.3 | 4.9 | 43.1 | TIME_LIMIT (+106 min sen) | -4.77 | -0.11 | 0.14 | -1.05 | 20 | DOWN | – | HOLD→WATCH | 34/34 | BAD | -0.12 (TIME) |
| 16 | SOON | 3a31c5 | funding_oi | 0.05 | 27 | 1.9 | 28 | TRADE | 0.2879 | -8.3 | 1.4 | 85.9 | TP | 22.78 | 0.27 | 2.40 | -4.20 | 62 | DOWN | ABORTED (race) | HOLD | 51/51 | BORDERLINE | 0.16 (TP) |
| 17 | 2Z | 4bfb00 | funding_oi | 0.25 | 55 | -0.8 | 28 | TRADE | 0.06761 | -2.7 | 4.3 | 26.6 | SL | -29.49 | -1.11 | 0.09 | -2.88 | 2 | DOWN | – | HOLD | 65/– | BAD | -1.04 (SL) |
| 18 | AAVE | 061bee | volume | 0.25 | 39 | 11.5 | 12 | WAIT | 154.36 | -3.8 | 4.9 | 37.5 | TIME_LIMIT (+32 min sen) | 4.86 | 0.13 | 1.03 | -1.74 | 338 | DOWN | – | HOLD↔WATCH ×5 | 21/21 | BORDERLINE | 0.10 (TIME) |
| 19 | CRV | 3b47c4 | volume | 0.25 | 42 | 5.0 | 11 | TRADE | 0.3455 | -3.0 | 4.8 | 30.3 | TIME_LIMIT | 16.66 | 0.55 | 2.66 | -1.24 | 243 | DOWN | BE @351m (samma sekund som time-close) | HOLD | 73/– | BORDERLINE | 0.35 (TIME) |
| 20 | NEAR | 505221 | momentum_breakout | 0.01 | 73 | -1.2 | 28 | REJECT | 5.45 | -2.4 | 5.5 | 23.7 | PP-BE-stop | -3.17 | -0.13 | 2.39 | -0.51 | 54 | UP | BE @41m | HOLD | 74/89 | GOOD | -1.04 (SL) |
| 21 | QNT | e30337 | momentum_breakout | 0.02 | 76 | -1.9 | 28 | REJECT | 194.58 | -7.8 | 2.3 | 80.1 | PP-BE-stop (DB: "target") | 0.07 | 0.00 | 1.24 | -1.60 | 14 | DOWN | BE @14m | HOLD | –/– | BORDERLINE | 0.28 (TP) |
| 22 | JUP | ef9b71 | momentum_breakout | 0.03 | 78 | -1.2 | 28 | REJECT | 0.3438 | -2.3 | 6.7 | 22.3 | TP | 64.88 | 2.92 | 6.95 | -0.58 | 69 | UP | BE @43m | HOLD | –/– | GOOD | 2.93 (TP) |
| 23 | ZRO | 83d0f0 | momentum_breakout | 0.01 | 72 | -3.6 | 28 | REJECT | 1.6668 | -1.3 | 8.6 | 12.7 | SL | -15.85 | -1.25 | 0.93 | -1.80 | 5 | DOWN | – | HOLD | –/– | BAD | -1.08 (SL) |
| 24 | POL | b317a9 | momentum_breakout | 0.02 | 76 | -2.8 | 44 | REJECT | 0.12221 | -2.2 | 7.2 | 21.7 | SL | -24.52 | -1.13 | 0.85 | -2.57 | 29 | DOWN | – | HOLD↔WATCH ×5 | 27/– | BAD | -1.05 (SL) |
| 25 | JUP | 844dbd | price_volatility+momentum_breakout | 0.26 | 85 | -0.5 | 11 | TRADE | 0.3664 | -5.0 | 7.8 | 50.2 | PP-BE-stop | -4.81 | -0.10 | 1.64 | -2.78 | 24 | DOWN | BE @25m | HOLD | 11/– | BORDERLINE | -0.76 (TIME) |
| 26 | JUP | 8819c7 | momentum_breakout | 0.04 | 80 | -1.5 | 13 | REJECT | 0.3666 | -4.3 | 4.2 | 42.8 | PP-BE-stop | -1.33 | -0.03 | 2.26 | -0.44 | 32 | UP | BE @23m | HOLD | –/– | GOOD | -1.02 (SL) |
| 27 | COW | a5bff2 | funding_oi | 0.25 | 46 | -16.4 | 11 | TRADE | 0.1582 | -3.9 | 4.9 | 39.1 | PP-BE-stop | -2.39 | -0.06 | 1.20 | -0.25 | 34 | UP | BE @35m | HOLD | –/– | GOOD | -1.03 (SL) |
| 28 | ONDO | 58f1a3 | price_volatility+momentum_breakout | 0.32 | 90 | 2.2 | 8 | TRADE | 0.5888 | -5.2 | 5.3 | 52.3 | SL | -53.26 | -1.02 | 0.88 | -5.47 | 20 | DOWN | – | HOLD↔WATCH ×3 | 9/71 | BAD | -1.02 (SL) |
| 29 | LTC | ddb332 | volume | 0.25 | 58 | 19.9 | 10 | TRADE | 70.98 | -2.9 | 5.0 | 29.1 | TIME_LIMIT | -3.23 | -0.11 | 0.56 | -1.63 | 317 | DOWN | – | HOLD↔WATCH ×2 | 15/15 | BAD | -0.09 (TIME) |
| 30 | NIL | 9fc852 | price_volatility+volume | 0.31 | 11 | 7.9 | 12 | TRADE | 0.08776 | -4.4 | 11.2 | 43.1 | PP-BE-stop | -0.40 | -0.01 | 4.84 | -1.57 | 49 | DOWN | BE @25m | HOLD↔WATCH ×2 | 34/34 | BORDERLINE | -0.24 (TIME) |
| 31 | AVAX | 17d519 | volume | 0.25 | 37 | 5.7 | 8 | TRADE | 10.774 | -2.1 | 4.9 | 20.6 | SL | -22.05 | -1.07 | 0.99 | -2.26 | 27 | DOWN | – | HOLD | 41/41 | BAD | -1.05 (SL) |
| 32 | NIL | c69971 | price_volatility | 0.25 | 24 | -0.4 | 38 | TRADE | 0.08803 | -4.0 | 8.4 | 38.8 | PP-BE-stop | -1.66 | -0.04 | 3.03 | -2.22 | 171 | UP | BE @169m | HOLD | –/– | GOOD | -0.44 (TIME) |
| 33 | VIRTUAL | 5120b5 | volume | 0.25 | 42 | 7.7 | 11 | TRADE | 0.8097 | -4.3 | 5.6 | 42.9 | SL | -43.94 | -1.02 | 0.57 | -4.53 | 3 | DOWN | – | HOLD | 10/10 | BAD | -1.02 (SL) |
| 34 | UNI | 6102be | volume | 0.25 | 15 | 12.3 | 8 | TRADE | 9.138 | -7.5 | 4.5 | 75.0 | ÖPPEN | – | – | 0.02 | -4.09 | 0 | DOWN | – | HOLD→WATCH | 8/8 | OPEN | – |
| 35 | HBAR | f3494b | price_volatility+momentum_breakout+volume | 0.37 | 85 | 3.1 | 12 | WAIT | 0.11278 | -5.1 | 8.2 | 50.8 | PP-BE-stop | -5.74 | -0.11 | 2.26 | -2.56 | 22 | DOWN | BE @19m | HOLD | –/– | BORDERLINE | 0.41 (TIME, ofullst.) |
| 36 | DOT | 33a842 | volume | 0.25 | 33 | 20.3 | 9 | – | 1.1871 | -2.7 | 4.0 | 27.0 | ÖPPEN | – | – | 1.27 | -0.15 | 44 | UP | BE @41m | HOLD→WATCH | 20/20 | OPEN | – |
| 37 | PEOPLE | 5d2b5f | volume | 0.25 | 32 | 14.9 | 8 | – | 0.00875 | -3.4 | 3.4 | 34.4 | ÖPPEN | – | – | 0.67 | -0.33 | 19 | NONE | – | HOLD | –/– | OPEN | – |

\* "EXTERN market" betyder en MARKET-sell utan bot-clientOrderId, 27/9 10:46:53 UTC, för båda positionerna i samma sekund medan boten var nere. Det ser ut som en manuell stängning i BingX-appen, eller något annat utanför boten. **Boten öppnade eller stängde ingenting då.** Kan du bekräfta om det var du?

**Om leverage:** alla trades körde 10x isolated. Entry-avgiften var ≈0,50 USDT och exit-avgiften ≈0,50 USDT per trade, vilket ger en faktisk taker-avgift på ≈0,05 %/sida. Systemets modellerade `fee_pct` är 0,04 %.

---

## 3. Entry-analys

### Var förlusten ligger

| Entry-klass | n | Net USDT | R | Plan utan PP (R) | MFE med. | MAE med. |
|---|---|---|---|---|---|---|
| GOOD (första ±1 % upp) | 10 | **+40,9** | +2,38 | −0,52 | 2,12 % | −0,59 % |
| BORDERLINE (ner först, sedan MFE ≥ 1 %) | 11 | **+41,8** | +0,86 | −0,48 | 2,47 % | −1,71 % |
| **BAD (MFE < 1 % hela traden)** | **13** | **−231,2** | **−7,85** | −8,14 | 0,56 % | −2,26 % |

**Kärnfynd:**
- De 13 trades som aldrig gick med oss står för **−231 USDT**, vilket är mer än hela nettoförlusten på −148.
- De övriga 21 trades gav tillsammans **+82,7 USDT**.
- **22 av 34 trades (65 %) gick −1 % innan de gick +1 %.**
- Alla 6 fulla SL-förluster gick rakt ner, med MFE 0,09–0,99 %. Management kunde inte ha räddat dem på något annat sätt än att inte ta dem.

Klassningen är gjord i efterhand på prisbanan och är delvis definitionsmässig. Att BAD förlorar är alltså inte i sig ett fynd. Fyndet är **storleken**: förlusten förklaras nästan helt av entries som aldrig fungerade, inte av management.

### Gick det att se vid beslutstidpunkten? (endast data från då)

| Egenskap vid beslut | GOOD | BAD |
|---|---|---|
| Median-RSI (30m) | 75 | 55 |
| Median volume z | −1,65 | +2,22 |
| Vanligaste trigger | momentum_breakout (5/10) | **volume (6/13)** |
| GF EQ verdict | 5 TRADE / 3 REJECT / 2 WAIT | **10 TRADE** / 2 REJECT / 1 WAIT |

**Per trigger** (R-snitt; alla n < 10):

| Trigger | n | R-snitt | USDT |
|---|---|---|---|
| volume | 9 | −0,22 | −59,6 |
| momentum_breakout | 9 | +0,01 | +7,2 |
| funding_oi | 7 | −0,19 | −23,2 |
| price_volatility+momentum | 3 | −0,37 | −58,1 |

Slutsats: **INSUFFICIENT_DATA.** Jag testade ett dussintal grupperingar: trigger, RSI, volZ, score, funding, BTC, forecast, SL-avstånd, RR, EQ och symbol. Inget överlever multipeltestning vid n=34. Hypotesen "volume-spike-entries är sämre än momentum-entries" är intressant men **obevisad**. GODFATHER:s större bok (n=139) klassar samma features som NOISE.

### Fanns bättre kandidater samtidigt?

Jag jämförde grupperna med en identisk generisk bracket (−3,9 % / +5 %, medianen av de verkliga LIVE-bracketerna) över 6 h efter candidate-skapande:

| Grupp | n | Generisk R-snitt (95 % CI) | TP/SL/TIME |
|---|---|---|---|
| **LIVE tagna** | 32 | **−0,26** (−0,55 till +0,04) | 6/13/13 |
| Gate-REJECTED (QA) | 39 | −0,15 (−0,41 till +0,13) | 8/12/19 |
| NO_TRADE | 17 | −0,07 (−0,49 till +0,37) | 4/6/7 |
| **BUDGET_LIMITED** (aldrig AI-analyserade, dedup symbol/timme) | 339 | **−0,18** (−0,25 till −0,11) | 35/91/213 |
| CONFIRMED men ej tagna LIVE | 7 | +0,77 (+0,30 till +1,16) | 4/0/3 |

**Kärnfynd:**
- **AI-pipelinen plus gaten valde inte bättre än de kandidater som aldrig analyserades.** Tagna trades är i punktestimat sämre, men konfidensintervallen överlappar. Det finns alltså ingen påvisbar selektions-edge.
- De 7 CONFIRMED som inte togs var nästan alla **omsignaler på redan hållna symboler** (2Z, SOON, QNT), som blockerades av max 1 per symbol eller TTL, samt INX. De gick bra, men n=7 är **bara hypotes**: momentum-fortsättning på samma symbol.
- Någon "bättre kandidat med information som fanns DÅ" kan inte identifieras. candidate_score låg under 0,5 för alla, och 0,2–0,5 gick sämre än under 0,2 (−0,22 R mot +0,03 R). GF EQ var inte bättre: REJECT gav +0,06 R och TRADE −0,22 R, alltså **omvänt**, med n=6 mot 22.

### Latens och entry-avvikelse

| Mått | Värde |
|---|---|
| Signal → gate | 5–27 min, median ≈9 |
| Gate → claim | 0–30 min |
| **Signal → fill** | **6–44 min, median 11**. 13 trades hade 25–44 min. |
| Candidate `reference_price` → faktisk fill | −3,1 % till +3,6 % |

Latensen korrelerar inte synligt med utfallet (n för litet).

---

## 4. Position management

### Profit Protection (BE vid +1 %)

- 20 PP-aktiveringar totalt: 18 på stängda trades och 1 på den öppna DOT. Dessutom avbröts 1 (SOON 3a31c5), eftersom positionen redan hade stängts på TP 5 sekunder tidigare. Det var en ofarlig race, fångad korrekt.
- PP-flytten sker i median ≈25 min efter fill (2–351 min).
- 16 trades stängdes på själva PP-stoppet.

| PP-stoppade (n=16) | Faktiskt | Plan utan PP |
|---|---|---|
| R summa / snitt | **−0,83 / −0,052** (CI −0,075 till −0,030) | −4,75 / −0,30 (CI −0,63 till +0,09) |
| USDT summa | **−36,6** | −48,6 |
| Utfallsmix utan PP | – | 3 TP · 5 SL · 8 TIME |

**Var varje PP-flytt rationell med den information som fanns då?**
- Ja, mekaniskt: triggern var +1 % mark-price, och regeln följdes exakt.
- **Men BE-priset = entry-priset exakt.** Avgifterna (≈1,0 USDT round-trip) och skillnaden mellan mark- och last-pris ingår inte.
- Stoppfill mot stopPrice låg mellan **−0,55 % och +0,14 %** (KAS −0,55 %, HBAR −0,47 %, QNT −0,46 %, JUP −0,38 %).
- Resultat: **varje "break-even" är en liten förlust, i snitt −2,3 USDT (−0,05 R)**, robust (CI helt under 0).

### Jämförelse av management-scenarier (alla 34 stängda, samma bana, ingen lookahead)

| Scenario | USDT | R | Kommentar |
|---|---|---|---|
| **Faktiskt** | **−148,4** | **−4,61** | |
| Original-SL/TP + 6 h (ingen PP) | −191,8 | −9,14 | PP hjälpte med ≈ +43 USDT / +4,5 R |
| BE + 0,15 % (avgiftstäckt BE) vid +1 % | −135,2 | −4,19 | ≈ +13 USDT bättre än faktiskt. Hypotes, in-sample. |
| Exit vid första thesis WEAKENING/INVALID (n=21) | – | +0,235 R/trade mot faktiskt | CI −0,00 till +0,48. **Motsäger** GF:s större bok (NOISE). Hypotes. |
| Exit vid första thesis INVALID (n=15) | – | +0,158 R/trade | CI −0,03 till +0,35. Hypotes. |

### Hade ett annat utfall varit bättre?

| Fråga | Svar |
|---|---|
| Original-SL | **Sämre**, både totalt och för GOOD-entries: −0,52 R mot +2,38 R |
| Break-even | Ja, men **avgiftstäckt BE** hade varit marginellt bättre än dagens exakta BE |
| Profit-lock | GODFATHER:s PROFIT_LOCK_HALF_MFE demoterades till **NOISE** 27/9 20:19 på större data. Stöds inte. |
| Tidigare EXIT | Möjligen, se thesis-raderna ovan. Inte bevisat. |
| HOLD | Sämre, se original-planen |

### Gav vinnarna tillbaka mycket?

- Ja, i procent: GOOD-entries hade MFE median 2,1 %, men 7 av 10 slutade på BE-stopp.
- **13 av 34 trades nådde minst +1 % och stängde ändå med förlust.** Alla 13 var PP-scratches på −0,1 till −6,6 USDT, inga stora förluster.
- MFE capture median för vinnare: 0,63.
- Inga fulla förluster efter +1 % tack vare PP: 0 av 16 icke-PP-trades hade gått upp +1 %.

**Tidigare observation, "bra entry men dålig management":** den syns fortfarande i LIVE, men i en **annan och mindre form** än väntat.
- PP gör inte att vinnare vänder till stora förluster. Utan PP hade det blivit värre.
- Det som syns är att vinnare **klipps till en liten fee-förlust** innan de hinner till TP.
- Kostnaden är ≈ −0,05 R per PP-trade plus uteblivna TP i 3 fall: 2Z 795949, QNT 9ff72a och QNT e30337. Dessa hade gett +0,72 / +1,43 / +0,28 R.
- Den stora förlusten ligger i entries, inte i management.

### Guardian

- 37 av 37 trades nådde bara **HOLD/WATCH**. Ingen trade nådde någonsin PROTECT eller EXIT.
- Guardian gjorde **0 interventioner**.
- `guardian_authority_decisions` = 0 rader totalt, och `guardian_authority_live_sl_actions` = 0. Authority är påslagen, men det finns inga promotade heuristiker att verkställa.

---

## 5. GODFATHER under perioden

| Mått | Värde |
|---|---|
| Entry Quality-bedömningar | **45**: 29 TRADE / 9 WAIT / 7 REJECT. **enforced = 0 på alla.** |
| EQ edge class | 45/45 INSUFFICIENT_DATA |
| EQ experience-verdict | 45/45 **NO_EVIDENCE** |
| Selection/portfolio | 23 TAKE+SKIP_CONCENTRATION, 6 TAKE+KEEP, 7 REJECT, 5 WAIT, 4 saknas. Detta är rådgivande: 2Z togs 6 gånger i rad. |
| Pattern matches på LIVE-trades | 622 matchningar: **454 NOISE + 168 INSUFFICIENT_DATA**. **Alla med vikt 0.** |
| Position Quality/Thesis | 455 rader över 40 positioner: 242 INVALID→EXIT, 72 WEAKENING→REDUCE, 35 WEAKENING→TIGHTEN_SL, 1 VALID→TIGHTEN_SL, 94 VALID→HOLD, 11 STRONG→HOLD |
| – varav verkställda | **0**. HOLD / PROTECT / TIGHTEN_SL / REDUCE / EXIT har aldrig verkställts. |
| **Faktiska interventioner** | **0**. GODFATHER avstod i 100 % av fallen, vilket är korrekt enligt design när inget är promotat. |
| Experience Memory | 50 patterns, räknade om 28/9 10:31: **30 NOISE, 20 INSUFFICIENT_DATA**. 0 FDR-signifikanta, max confidence 0,0, minsta p = 0,093. 8 överlevde walk-forward, men alla är NOISE. |
| Bokens storlek | 139 trades i den största patternen (var 96–98 i fas 2). calibration 97 / OOS 42 / live_n 84. |
| Policy-registret | 12 policies: 0 promotade, 0 ACTIVE. 1 transition i perioden: PROFIT_LOCK_HALF_MFE gick INSUFFICIENT_DATA → **NOISE** (27/9 20:19). TIGHTEN_SL_AFTER_FAVORABLE är fortfarande SUSPECT. |
| Heuristiker ändrade / promotade / demoterade | 0 / 0 / 1 (se raden ovan) |
| Strategiändring som påverkade LIVE | **Ingen** |
| Strategist | 21 `assessment_unusable`, 22 "already proposed today", 27 misslyckade anrop på grund av Anthropic-kreditstopp. 0 heuristik-kandidater producerade. |
| Trade-investigations (LIVE) | 30 av 34 klassas **UNKNOWN**. Audits: 16 POSITION_MANAGEMENT, 5 BOTH, 5 NEITHER, 7 UNKNOWN, 1 SIGNAL_SELECTION. |

**Lärde sig GODFATHER något?**

| Nivå | Svar |
|---|---|
| A. Ny data insamlad | **Ja.** Boken gick från ~98 till ~139 trades. Guardian- och thesis-banor, PP-händelser och 1m-klines arkiverades. |
| B. Erfarenhet klassificerad | **Ja.** 50 patterns räknades om, och 1 policy klassades ner till NOISE. |
| C. Erfarenhet som påverkade beslut | **Nej.** Varje matchning hade vikt 0, EQ är NO_EVIDENCE och ingenting verkställdes. |
| D. Tillräckligt stark för promotion | **Nej.** 0 edges och 0 FDR-signifikanta. |

GODFATHER:s *diagnos* säger att felet oftast ligger i POSITION_MANAGEMENT (16 audits). Min rekonstruktion på börsdata säger tvärtom att **förlusten ligger i entries**. GODFATHER:s investigations klassar dessutom 30 av 34 som UNKNOWN. Diagnosmotorn är alltså inte tillförlitlig på LIVE-data ännu.

---

## 6. Vad förbättrades?

| Område | Bedömning | Evidens |
|---|---|---|
| Datakvalitet | **Förbättrat** | `exit_fill_source` sätts på alla exits. Guardians första observation kommer ≤1 min efter fill, mot median 27 min i fas 2. Klines arkiveras. |
| LIVE exit verification | **Förbättrat men ofullständigt** | 30 av 34 verifierade, mot 3 av 10 tidigare. 4 är TICKER-UNVERIFIABLE, fast börsens orderhistorik har sanningen. 3 har fel orsak eller pris i DB. |
| P/L reconciliation | **Delvis** | Verifierade exits matchar börsen exakt. `realized_fees_usdt` och `realized_funding_usdt` är fortfarande NULL. Fees modelleras på 0,04 % mot verkliga 0,05 %. Funding på +7,35 saknas i DB. |
| Notify/daily report | **Förbättrat** | 25 `None - Decimal`-krascher, alla före omstarten 26/9 17:26. Noll efter fixen. |
| GODFATHER learning | **Oförändrat** | 0 edges, 0 påverkade beslut |
| Entry quality | **Oförändrat / otillräckligt med data** | Ingen selektions-edge mot slumpmässigt urval av icke-analyserade candidates |
| Position management | **Oförändrat** | PP är nettopositiv mot planen, men BE är avgiftsnegativt |
| Profit Protection | **Fungerar mekaniskt** | 19 lyckade SL-byten och 1 korrekt hanterad race. 0 UNCERTAIN i perioden, mot 2 den 14/9. |
| Guardian | **Oförändrat** | Aldrig PROTECT/EXIT, 0 interventioner |
| Stale signal handling | **Fungerar** | TTL 1800 s respekterades. Men 1 226 skip-loggrader på 20 h (spam). |
| Capacity control | **Fungerar** | Max 4 samtidiga, 0 överlapp på samma symbol, 128 dubblettblock, margin-check blockerade 11 gånger |
| Duplicate-process prevention | **Fungerar** | En instans, lockfil finns |
| Exchange reconciliation | **Svag vid avbrott** | Externa stängningar under nertid bokfördes som `stop_loss` till ticker-pris |
| Slippage | **Ny observation** | PP-stopp (mark-trigger) fylls upp till −0,55 % under BE-priset |
| Execution quality | Oförändrat | Market-entries; TP-fills −0,03 % till +0,86 % |
| AI reliability | **Försämrat** | Anthropic-krediten tog slut 27/9 04:13–06:37 UTC: 399 anrop avvisades (400 "credit balance too low"). Dessutom 37 timeouts och 6 JSON-fel. |

---

## 7. Root-cause (34 stängda trades)

| # | Kategori | n | USDT | R | MFE/MAE med. | Evidens | Robust? |
|---|---|---|---|---|---|---|---|
| 1 | Dåliga entries (MFE < 1 %) | 13 | −231,2 | −7,85 | 0,56 / −2,26 % | Stark som beskrivning | **Riktning robust.** Orsaken ej identifierbar vid beslut. |
| 2 | Bra entries, dålig management | 10 GOOD | +40,9 (plan: +17) | +2,38 | 2,12 / −0,59 % | Management slog planen | **Nej**, management var inte orsaken |
| 3 | Bra trades drabbade av execution/slippage | 16 PP-stopp | −36,6 totalt = −16,0 avgifter + −20,6 fill under entry | −0,83 | – | Börsens fills | Robust men liten |
| 4 | Bra trades som gav tillbaka profit | 13 (+1 % → förlust) | −37,2 | −0,85 | – | Klines | Robust. Liten per trade. |
| 5 | Dålig TP | 6 med RR < 1 (t.ex. SOON 3a31c5 +1,4 %/−8,3 %, QNT e30337 +2,3 %/−7,8 %) | +12,9 | – | – | Setup-data | Hypotes: gaten släpper igenom RR < 1 |
| 6 | Dålig SL | 6 fulla SL | −189,1 | −6,40 | 0,85 / −2,6 % | Alla gick rakt ner | SL träffades korrekt. Problemet var entryn. |
| 7 | PP skyddade korrekt | 11 (planen hade varit sämre) | +266,5 USDT mot planen | +7,43 R räddat | – | Kontrafaktiskt | Robust inom urvalet |
| 8 | PP för tidigt | 5 (2Z 795949, QNT 9ff72a, SOON 290f60, QNT e30337, HBAR) | −13,2 faktiskt mot +241,2 i plan | −3,50 R | – | Kontrafaktiskt | Robust inom urvalet. Gick inte att förutse vid flytten. Tre av dem hade stor planerad risk (80–92 USDT), därför stora USDT-belopp. |
| 9 | Marknadsregim | 4 av 6 fulla SL | −143,8 | – | – | BTC −2,8 % från 27/9 22:00 till 28/9 08:00. ONDO, POL, AVAX och VIRTUAL stoppades 01:17–09:03. | Trolig, n=4 |
| 10 | Korrelerade positioner | Samma 4 | – | – | – | Enbart longs på alts, 4 samtidiga | Trolig |
| 11 | För låg signal-edge | Alla | – | −0,136 R/trade | – | Tagna ≈ slumpurval (§3) | **Starkaste förklaringen.** Konsistent med all tidigare data. |
| 12 | Discovery-latens | 13 trades > 25 min signal→fill | – | – | – | Ingen synlig effekt | INSUFFICIENT_DATA |
| 13 | AI/agentfel | 0 direkt | – | – | – | Kreditstoppet blockerade nya trades (säkert läge) | Inga förluster direkt |
| 14 | Data/observability | 4 (UNVERIFIABLE) + 2 (externa) | – | – | – | Se §1 och §11 | Robust |
| 15 | Slump | Allt ovan | – | CI för R-expectancy −0,35 till +0,12 | – | n=34 | **Nollhypotesen, att expectancy = 0, kan inte förkastas** |

---

## 8. Förväntning mot utfall

| Förväntning | Utfall | Fel | Orsak | Lärdom |
|---|---|---|---|---|
| Bättre data → bättre learning | Mer och bättre data, men 0 edges | Learning ≠ data | n=34 per 41 h, och effekter är små i förhållande till bruset | Tusentals trades eller större effekter krävs. Data är nödvändig men inte tillräcklig. |
| GODFATHER förstår entry quality | EQ var NO_EVIDENCE 45/45 och i punktestimat omvänd mot utfallet | EQ har ingen prediktiv kraft ännu | Alla patterns är NOISE eller INSUFFICIENT | EQ bör inte ges vikt förrän något pattern når FDR/OOS |
| Bättre position management | PP räddade netto +3,9 R (+12 USDT) mot planen på de 16 PP-stoppade trades. Guardian och GF gjorde ingenting. | Guardian och GF påverkar inte LIVE | Inget promotat, by design | Management är inte flaskhalsen |
| PP skyddar kapital | Ja: 0 av 16 PP-trades blev fulla förluster | "BE" kostar −0,05 R per gång | BE = entry utan avgifter, mark→last-slippage | Avgiftstäckt BE är värd att testa (beslut för dig) |
| Systemet förstår varför trades vinner eller förlorar | 30 av 34 investigations UNKNOWN. Audits pekar på management, data pekar på entry. | Diagnosen stämmer inte | Diagnosmotorn är kalibrerad på paper-banor | Diagnos på LIVE ska byggas på börsens fills och klines |
| LIVE-data ger Experience Memory användbar info | live_n = 84 i patterns, men inget signifikant | – | Låg edge plus låg n | Fortsätt samla. Förvänta dig inte edge från några dussin trades. |

---

## 9. MFE / MAE

| Grupp | n | MFE snitt/med | MAE snitt/med | MFE capture med | Giveback med | t→MFE med | +1 % först, sedan förlust |
|---|---|---|---|---|---|---|---|
| Alla | 34 | 1,80 / 1,60 % | −1,78 / −1,62 % | −0,10 | 1,10 | 32 min | 13/34 |
| Vinnare | 5 | 3,10 / 2,47 % | −1,79 / −1,24 % | 0,63 | 0,37 | 243 min | 0/5 |
| Förlorare | 23 | 1,35 / 0,99 % | −1,91 / −1,80 % | −0,32 | 1,32 | 27 min | 11/23 |
| Breakeven | 6 | 2,41 / 2,20 % | −1,31 / −1,33 % | 0,01 | 0,99 | 26 min | 2/6 |
| PP-trades | 18 | 2,69 / 2,32 % | −1,27 / −1,17 % | −0,06 | 1,06 | 33 min | 13/18 |
| Icke-PP | 16 | 0,79 / 0,57 % | −2,36 / −2,13 % | −1,75 | 2,75 | 28 min | 0/16 |

- +1 % nåddes i 21 av 34 trades, efter i median ≈24 min (1–283 min).
- Vinnarna tar lång tid: MFE efter i median 243 min. PP-stoppen slår till efter i median ≈50 min. **PP klipper tidiga svängningar i trades som annars kunde ha löpt**, men i 11 av 16 fall hade planen varit sämre. Netto i USDT är det bara +12, eftersom de 5 klippta vinnarna hade stor planerad risk.

---

## 10. Risk och kapital (10x större positioner)

| Område | Observation | Risk |
|---|---|---|
| Förlustens storlek | Fast notional (1 000) med olika SL-avstånd (−1,3 % till −9,0 %) ger **planerad risk 12,7–92 USDT per trade (7×)**. Två 1R-förluster (ONDO −53 och VIRTUAL −44) är lika stora som 6–8 PP-scratches tillsammans. | **Ny och viktig.** USDT-utfallet styrs av SL-avståndet, inte av signalen. |
| Andel av equity | En enskild trade kan riskera **~20–30 % av nuvarande equity** (UNI 75 USDT = 25 %). 4 samtidiga med snittrisk ≈ 170 USDT ≈ 56 % av equity. | Hög |
| **Liquidationsavstånd** | 10x isolated ger likvidation vid ≈ −9,2 till −9,8 % (öppna positioner: UNI −9,7 %, DOT −9,8 %, PEOPLE −9,2 %). **SOON 290f60 hade SL −9,03 %**, SOON 3a31c5 −8,3 %, QNT −8,0 % och −7,8 %, UNI −7,5 %. Observerad stop-slippage upp till −0,55 %. | **Farlig men ännu inte inträffad.** En SL nära likvidationspriset kan bli en likvidation, med annan exit-väg och avgift. Det saknas en spärr för SL-avstånd mot likvidationsavstånd. |
| Kapitaleffektivitet | Ledig margin 11,48. **Den fjärde platsen är i praktiken stängd.** Vid ytterligare cirka −100 USDT blir det bara 2 platser. | Kapaciteten krymper när equity faller. Det är korrekt beteende, men "max 4" gäller inte längre i praktiken. |
| Avgifter | 33,84 USDT = **23 % av nettoförlusten**. ≈1,0 USDT per round-trip = ≈ 0,024 R per trade vid snittrisk. | Betydande mot en edge på noll |
| Funding | +7,35 (longs fick betalt) | Positiv hittills |
| Slippage/impact | Entry market-fills. Stopp −0,55 % till +0,14 %. Ingen tecken på impact vid 1 000 notional. | Låg |
| Beter sig 100 × 10 som designat? | Ja: margin 99,6–100,3 och notional ≈ 974–1 007 enligt börsen. Leverage 10, isolated. | OK |

---

## 11. Operativa problem

| Problem | Evidens | Farligt? |
|---|---|---|
| **Anthropic-krediten slut** | 399 × 400 "credit balance is too low", 27/9 04:13–06:37 UTC, i alla 10 agenter inklusive Guardian och strategist | Säker fail (inga nya trades), men systemet stod blint i 2,4 h utan larm |
| **Processen dog tyst** | Loggen slutar 27/9 07:13 UTC utan traceback. Allt stod still i 4 h 44 min tills omstarten 11:57. | Ja. TIME_LIMIT, PP och Guardian var ur drift. Bara börsens SL/TP skyddade. |
| **Andra stoppet utan omstart** | Alla loopar stod still 27/9 16:07–20:11 UTC (≈4 h) i samma process. DNS `getaddrinfo failed` ×31 och ConnectError ×22 13:43–21:20. Troligen sömn eller nätverksbortfall i datorn. | **Ja.** BNB TIME_LIMIT kom **106 min sent** och AAVE 32 min sent. CRV fick PP och TIME_LIMIT i samma sekund. |
| **Loggar sparas inte längre** | `start_bot.bat` skickar stderr till fönstret. Sedan 27/9 11:57 finns inga persistenta strukturerade loggar. | Ja, för forensik. Jag kunde bara granska loggar fram till 07:13. |
| Externa stängningar felbokförda | BOME och 2Z 509546: MARKET utan bot-id bokfört som `stop_loss`/TICKER vid omstart | Data. De exkluderas från learning (UNVERIFIABLE), men etiketten är fel. |
| QNT e30337 som "target" | Börsen: PP-stopp | Data. Påverkar TP-statistik. |
| Klocka | 3 × "timestamp is invalid" (109400) | Låg. Tick misslyckades och kördes om. |
| Stale-skip-spam | 1 226 rader på 20 h | Brus, som gömmer riktiga fel |
| AI-timeouts / JSON | 37 timeouts, 6 JSON-fel (retry lyckades) | Låg |
| Strategist | 21 unusable, 0 output | GF-learning via strategisten står still |
| Monitoring partial_error | 30 (ticker ConnectError under DNS-avbrottet), 3 tomma kline-listor | Låg |
| EQ saknas för DOT 33a842 och PEOPLE | Supervisorn skriver EQ i efterhand | Låg, men "beslutstid" är en rekonstruktion |
| PP/TP-race | SOON 3a31c5: PP avbröts eftersom positionen redan var stängd | Hanterat korrekt |
| Kapacitets- och symbol-race | 0 fall | – |
| Dubblettobservationer | 0: Guardian-PK innehåller tidsstämpel, max 1 process | – |

---

## 12. Data quality audit (användbarhet för learning)

| Krav | Uppfyllt | Undantag |
|---|---|---|
| Observerbar | 32/34 | BOME e0c240 och 2Z 509546: ingen Guardian/thesis på 213 min före exit (nertid) |
| Rätt tidsstämplar | 31/34 | e0c240, 509546 och f02740: DB `closed_at` och pris ≠ börsen |
| Rätt storlek | 34/34 | – |
| R från planerad risk | 34/34 | Kräver initial SL från börsens sub-order. Det fanns för alla. |
| Rätt exit | 31/34 | 2 externa stängningar och 1 PP-stopp bokförd som target |
| Ingen lookahead | Ja | Kontrafaktiska scenarier använder bara prisbanan fram till respektive tidpunkt. Thesis-exit prissätts på nästa 1m-close. |
| Inga dubbletter | Ja | – |

**Exkluderat från slutsatserna om management:** BOME e0c240 och 2Z 509546, eftersom de stängdes av en extern stängning och inte av botens logik. Deras P/L (+10,12 och −12,64) ingår däremot i kontosummorna, eftersom det är riktiga pengar. Utan dem blir summan −145,9 USDT och −4,43 R på 32 trades. Slutsatserna ändras inte.

---

## 13. Statistisk säkerhet

| Slutsats | n | Effekt | 95 % CI | OOS / multipeltest | Status |
|---|---|---|---|---|---|
| Expectancy < 0 | 34 | −0,136 R | −0,35 till +0,12 | – | **INSUFFICIENT_DATA** (0 ingår) |
| PP-BE kostar avgifter | 16 | −0,052 R/trade | −0,075 till −0,030 | Mekanisk, inte statistisk | **CONFIRMED** (mekanism) |
| PP bättre än original-planen | 16 | +0,245 R/trade (+12,1 USDT totalt) | Ej separat bootstrap. Parvis 11 bättre, 5 sämre. | GF:s större bok: TIGHTEN_SL_AFTER_FAVORABLE SUSPECT | **PROBABLE** |
| Avgiftstäckt BE bättre än exakt BE | 16 | +0,064 R/trade | 0,01–0,02 mot −0,075 till −0,03 | In-sample | **PROBABLE** (mekaniskt nästan säker vid samma bana) |
| Tagna LIVE ≠ bättre än BUDGET_LIMITED | 32 / 339 | −0,26 mot −0,18 R | Överlappar | – | **Ingen selektions-edge påvisad** |
| Exit vid thesis-försvagning | 21 | +0,235 R | −0,00 till +0,48 | 1 av ≈15 test; motsägs av större bok | **Hypotes** |
| Volume-trigger sämre | 9 | −0,22 R | – | ≈12 grupperingar testade, inget klarar BH | **INSUFFICIENT_DATA** |
| Omsignaler på hållen symbol bra | 7 | +0,77 R (generisk) | +0,30 till +1,16 | Post hoc | **Hypotes** |

Train/test-uppdelning och walk-forward körs i GODFATHER:s egen pipeline: 50 patterns, 0 FDR-signifikanta. Jag har inte hittat någon edge och har inte konstruerat någon.

---

## 14. Vad har GODFATHER (och vi) lärt oss?

### CONFIRMED KNOWLEDGE
- PP-BE vid +1 % förhindrar att trades som nått +1 % blir fulla förluster (0 av 16).
- PP-BE i nuvarande form realiserar en **avgifts- och slippage-förlust på ≈ −0,05 R** per BE-exit (mekaniskt, börsverifierat).
- Mark-triggade stopp fylls upp till −0,55 % under stopPrice på tunna alts.
- Förlusten 26–28/9 kommer nästan helt från entries som aldrig gick med oss.
- Planerad USDT-risk varierar 7× vid fast notional.
- Inga GODFATHER-, Guardian- eller heuristikbeslut påverkade LIVE.

### PROBABLE
- PP är nettopositiv mot original-SL/TP: +3,9 R (+12 USDT) på 16 PP-stoppade trades. Hela avvikelsen från planen på alla 34 trades är +4,5 R och +43 USDT.
- Avgiftstäckt BE (+0,1–0,15 %) vore bättre än exakt BE.
- Förlusterna 28/9 natt var korrelerade med BTC:s nedgång (−2,8 %) i en long-only alt-bok.

### UNKNOWN
- Om systemet har någon positiv signal-edge alls, oavsett om den mäts i LIVE eller paper-korrigerat.
- Om latens (signal→fill 25–44 min) kostar något.
- Vem eller vad som stängde BOME och 2Z 27/9 10:46:53 UTC.

### FAILURE PATTERNS
- Entries som går −1 % först (65 %). De står för hela förlusten.
- RR < 1-setups passerar gaten.
- Upprepade entries på samma symbol (2Z ×6: −45,6 USDT, planen −0,64 R/trade).
- Processen stannar (krediter, tyst död, sömn/nät) utan larm.

### NOISE
- PROFIT_LOCK_HALF_MFE (demoterad till NOISE i perioden)
- EXIT/REDUCE_ON_THESIS_* och THESIS_TIGHTEN på den större boken
- 30 av 50 experience patterns
- TIGHTEN_SL_AFTER_FAVORABLE (SUSPECT)

### INSUFFICIENT DATA
- Trigger-typ, RSI, volZ, candidate_score, BTC-regim, forecast-bucket och SL-avstånd som prediktorer
- Thesis-baserad tidig exit på LIVE
- Omsignal/momentum-fortsättning på samma symbol
- DELAY_ENTRY, SAFE_TP, ENTRY_SELECTION_TOP_HALF, PORTFOLIO_THEME_CAP

---

## 15. Rekommendation (inga ändringar gjorda)

**A. Lämna exakt som det är**
- Sizing 100×10, max 4, max 1 per symbol, TTL 1800 s
- PP-mekanismen (trigger och flöde)
- Guardian/GF som observatörer utan authority
- Policy-registrets NOISE-klassningar

**B. Övervaka**
- Equity mot kapacitet: den fjärde platsen är redan stängd.
- UNI 6102be (−4 % MAE, SL −7,5 %, risk 75 USDT)
- SL-avstånd nära likvidation
- Anthropic-kreditsaldo
- Loopstopp och sömn
- Upprepade 2Z-entries

**C. Fixa NU** (drift och data; kräver ditt beslut, inga strategiändringar)
1. Persistent loggning i `start_bot.bat`: stderr till fil.
2. Larm eller watchdog när loopar står still, plus förhindra att datorn sover.
3. Larm för Anthropic-kredit och 400-fel.
4. LIVE-exit-reconcile från börsens `allOrders` istället för TICKER, med rätt orsak: PP-stopp, extern stängning. Då blir 4 UNVERIFIABLE verifierade.
5. Spara riktiga fees och funding från income-endpointen i `live_executions`.
6. Minska stale-skip-spam.

**D. Kan vänta** (kräver separat, uttryckligt beslut från dig eftersom det berör PP, SL eller sizing)
- Avgiftstäckt BE-pris i PP
- Spärr för SL-avstånd mot likvidationsavstånd
- Risk-normaliserad sizing
- RR-golv i gaten

**E. Data att samla härnäst**
- Fler LIVE-trades med verifierad exit, minst 100 för att R-CI ska smalna av till ungefär ±0,1.
- Riktiga fills och avgifter per trade.
- BTC-regim vid varje tick, inte bara vid entry.
- Utfall för blockerade omsignaler (CONFIRMED men ej tagna).

**F. Hypoteser värda att testa senare** (out-of-sample, förregistrerade)
- Avgiftstäckt BE
- Exit vid första thesis WEAKENING på LIVE
- Volume-trigger mot momentum-trigger
- Omsignal på hållen symbol
- BTC-nedtrend som filter för longs på alts

**G. Hypoteser att förkasta**
- "Problemet är främst management" (stöds inte: management slog planen)
- "GF EQ-verdict förutsäger utfall" (NO_EVIDENCE, omvänd i punktestimat)
- "Högre candidate_score ger bättre trades" (0,2–0,5 sämre än under 0,2)
- PROFIT_LOCK_HALF_MFE och TIGHTEN_SL_AFTER_FAVORABLE

**Är resultatet förenligt med dålig entry selection, dålig management, båda eller för lite data?**

Resultatet är **mest förenligt med dålig entry selection, det vill säga låg eller ingen signal-edge**, plus regim och korrelation. Till det kommer en **liten, mekanisk management-kostnad**: avgifter och slippage i PP-BE samt några uteblivna TP.

Riktningen (entry > management) är robust inom urvalet. Om den totala expectancyn är negativ eller bara noll kan **inte avgöras** med n=34.
