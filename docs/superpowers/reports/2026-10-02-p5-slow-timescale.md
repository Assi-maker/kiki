# P5: långsam tidsskala, trendstrategin (2026-10-02)

## Varför P5
Sju studier (P1–P4, regim-, exit- och edge-labben) testade intradagsentréer med 5 minuter till 24 timmars hålltid, på cirka 14 månaders data. Ingen hittade någon robust edge. Kostnaden för en tur och retur (~0,16 %) är lika stor som de rörelser som går att förutsäga.

P5 byter tidsskala till dagar och veckor. Där är kostnaden liten jämfört med rörelsen, och litteraturen beskriver trend-, momentum- och carry-premier i krypto. Testet gjordes på 7 års dagsdata (Binance USDT-M perps 2019-09 till 2026-10), inklusive avlistade kontrakt.

## Metod (förregistrerad, commit `bf2f510` före första resultatet)
- 55 fasta varianter i fem familjer:
  - F1: trend på BTC/ETH.
  - F2: bred trend.
  - F3: tvärsnittsmomentum och reversal.
  - F4: funding-carry.
  - F5: cash-and-carry.
- Handel sker dagen efter signalen, med verklig funding. Kostnaden är 0,08 % per omsättningsenhet för likvida kontrakt och 0,15 % för övriga.
- Perioderna är TRAIN 2020–2022, VALID 2023–06/2024, TEST 07/2024–09/2025 och HOLDOUT 10/2025–09/2026.
- För att bli godkänd krävs alla tre villkor:
  1. BH q < 0,10 på TRAIN+VALID (block-bootstrap);
  2. positiv i alla fyra perioder var för sig;
  3. största nedgång (MDD) mindre än 50 %.
- Begränsning: nedladdningen stoppades av minnesbrist efter 261 av 659 kontrakt (allt som listades fram till 2024-04). Det påverkar bara F2–F4, som behöver det breda universumet. BTC och ETH är kompletta.

## Resultat
Sex av 55 varianter godkändes:

| Variant | Årsavkastning 2020–26 | Sharpe | MDD | HOLDOUT |
|---|---|---|---|---|
| F1 BTC L20 long/flat | +32 % | 1,02 | −47 % | +3 % |
| F1 ETH L50 long/flat | +32 % | 0,98 | −41 % | +10 % |
| F1 BTC+ETH L20 long/flat | +28 % | 0,99 | −43 % | +6 % |
| F1 BTC+ETH L50 long/short | +25 % | 0,75 | −48 % | +23 % |
| F5 BTC cash-and-carry | +5 % | 8,4 | −1 % | +1 % |
| F5 ETH cash-and-carry | +7 % | 8,7 | −1 % | +1 % |
| *Köp och håll BTC* | *+44 %* | *0,91* | *−77 %* | *−27 %* |
| *Köp och håll ETH* | *+57 %* | *0,97* | *−79 %* | *−36 %* |

- **Tvärsnittsmomentum, reversal och funding-carry:** inga varianter klarade alla fyra perioderna. Reversal förlorade kraftigt i alla perioder.
- **Cash-and-carry:** hög träffsäkerhet (90 % vinstdagar) men bara cirka 1 % per år sedan 2025, eftersom funding har pressats ned. Den kräver också ett spotben som botten saknar. Den byggs inte.

## Vald strategi och robusthet (`p5_robust.py`, post hoc, kan bara förkasta)
Vald strategi:
- **Ensemble BTC+ETH, lookback 20 och 50 dagar, long/flat, vol-target 40 %.**
- Den består av två godkända eller BH-överlevande celler. Jag valde den i stället för den enskilt bästa cellen, så att parametervalet inte blir ett plockat utfall.

| Kontroll | Årsavk. | Sharpe | MDD |
|---|---|---|---|
| Bas (2020–09/2026) | +28 % | 1,07 | −32 % |
| Kostnad ×2 | +26 % | 1,00 | −33 % |
| Kostnad ×3 | +24 % | 0,93 | −34 % |
| En extra dags fördröjning | +21 % | 0,86 | – |
| Rebalanseringsband 5 % (det som körs) | +29 % | 1,10 | −32 % |

Avkastning per år, jämfört med köp och håll av BTC:

| År | Strategin | Köp och håll BTC |
|---|---|---|
| 2020 | +112 % | +303 % |
| 2021 | +25 % | +60 % |
| 2022 | −22 % | −64 % |
| 2023 | +71 % | +156 % |
| 2024 | +28 % | +121 % |
| 2025 | +5 % | −6 % |
| 2026 | +14 % | −5 % |

BingX-data mot Binance-data: dagliga stängningskurser skiljer cirka 0,02 % (median). Strategin är i samma läge (in eller ute) 99,9 % av dagarna.

## Ärlig tolkning
- Detta är inte en hemlig fördel. Det är en välkänd riskpremie: trendföljning.
- Värdet ligger i riskkontrollen. Strategin följer med i uppgångar men kliver av i långa nedgångar: −22 % i stället för −64 % under 2022, och plus under 2025–26 när köp och håll låg på minus.
- I starka tjurår ligger den klart efter köp och håll.
- Träffsäkerheten per dag är cirka 50 %. Vinsten kommer från att vinsterna är större än förlusterna, inte från att den har rätt ofta.
- 2022-liknande förluster på −20 till −30 % kommer att hända igen.
- Siffrorna är backtest. Riktiga resultat syns först efter månader av PAPER eller LIVE.

## Det som byggdes
- **Strategin:** `crypto_trading/trend/`, med `signal.py` (ren funktion, paritetstestad mot forskningskoden), `engine.py`, `store.py` och `__main__.py`. Konfigurationen ligger i `config/trend_strategy.yaml`.
- **Körning:** en ombalansering per stängt UTC-dygn. Den är idempotent och vägrar handla på inaktuell data. Uppgiften `crypto_trading_trend` i Task Scheduler körs varje timme, så en dator som har sovit kommer ikapp.
- **LIVE-läge:**
  - Börsen är facit.
  - Säljorder läggs före köp.
  - Hävstången är 3x och bruttoexponeringen högst 2x kapitalet.
  - Efter varje ombalansering läggs ett nödstopp på börsen (STOP_MARKET 20 % under priset).
  - Motorn vägrar köra under SAFE_MODE, utan nycklar, med en SHORT-position i BTC/ETH eller med 0 i eget kapital.
  - Order-ID:t är unikt per försök.
- **Den gamla intradagsmotorn är pensionerad för LIVE:** `CRYPTO_TRADING_LIVE_EXECUTION_ENABLED` och `CRYPTO_TRADING_GUARDIAN_ENABLED` är bortkommenterade i `.env`. Koden finns kvar.
- **Tester:** 19 nya. Hela sviten ger 2661 passed, 2 skipped.
- **Startläge 2026-10-02:** PAPER med 1 000 USDT. Första ombalanseringen: 48 % BTC och 46 % ETH. LIVE-kontot har 0 USDT.

## Så slås LIVE på (användarens beslut)
1. För över kapital till BingX-kontot för USDT-M perpetual futures. Kontot måste vara i hedge mode, som tidigare.
2. Sätt `mode: LIVE` i `crypto_trading/config/trend_strategy.yaml`. Sätt eventuellt `capital_cap_usdt` om bara en del av kontot ska användas.
3. Ta bort raden `CRYPTO_TRADING_SAFE_MODE=1` i `.env`. Den gamla motorn är avstängd, så det slår inte på den igen.
4. Kontrollera med `python -m crypto_trading.trend status` och följ `logs/trend.log`.
