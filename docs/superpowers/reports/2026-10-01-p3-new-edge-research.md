# P3 – ny informationskälla för incremental edge (2026-10-01)

## Slutsats

**INGEN NY EDGE FUNNEN.**

| Källa | Utfall |
|---|---|
| Order flow (P3-B) | 0 av 120 hypoteser klarar multiple-testing-korrektionen |
| Likvidationer (P3-A) | Ingen fri historik finns. Proxyn klarar 0 av 72, och 1-dygnsurvalet (n = 23) är oanvändbart |
| Spot/perp (P3-C) | 0 av 84 på minutnivå. På sekundnivå finns en lead på ~1 bp som är borta inom 1–5 s, vilket gör den **operationellt oanvändbar** och dessutom under kostnaden |

- **Totalt:** 276 hypoteser, 22 med p < 0,05 i TRAIN (≈ 13,8 väntas av slump), 0 överlever BH, 0 överlever alla perioder.
- Inget ska byggas.
- LIVE har varit stoppat hela tiden. Ingen tradinglogik, SL/TP, riskmodell, max 4, inga %-tak och inget i GODFATHER har ändrats.

## 1. Baseline

**Data:** botens egen kvant-trigger rekonstruerad point-in-time på Binance 5m-arkivet. 135 symboler, 2025-08-01 → 2026-09-30.

**Trigger:** på varje stängd 30m-bar,
- |Δclose| > 2 %,
- eller RSI14 > 70,
- eller volym-z > 2,5 mot de föregående 20,
- eller |funding| > 0,05 %,

med botens cooldown på 60 min per symbol. Entry sker på open för första 5m-baren ≥ T + 5 min.

**Urval:** 335 502 händelser:

| Period | Datum | Händelser |
|---|---|---|
| TRAIN | 2025-08 → 2026-01 | 141 786 |
| VALID | 2026-02 → 2026-04 | 75 262 |
| TEST | 2026-05 → 2026-07 | 68 650 |
| HOLDOUT | 2026-08 → 2026-09 | 49 804 |

Fönster som når in i nästa period har purgats. Triggerfördelning: RSI 47 %, volym 37 %, pris 21 %, funding 13 %.

**Kostnader:**
- taker 0,05 % per sida + slippage 0,03 % per sida = **0,16 % tur och retur** för horisontavkastning;
- botens bracket (SL 4,3 % / TP 5 % / 6 h): +0,10 % stop-slippage och verklig funding.

| ALLA perioder | 5 m | 15 m | 30 m | 1 h | 2 h | 4 h |
|---|---|---|---|---|---|---|
| LONG brutto | +0,006 % | −0,003 % | 0,000 % | +0,011 % | +0,025 % | +0,034 % |
| LONG netto | −0,154 % | −0,163 % | −0,160 % | −0,149 % | −0,135 % | −0,126 % |
| SHORT netto | −0,166 % | −0,157 % | −0,160 % | −0,171 % | −0,185 % | −0,194 % |

Bruttot ligger på ≈ 0 över alla horisonter. Signalen saknar riktning, och nettoresultatet är i praktiken lika med kostnaden. Det bekräftar tidigare studier, nu på 335 k händelser och 14 månader.

## 2. Likvidationer (P3-A)

**Datatillgång**, kontrollerad 2026-10-01:

| Källa | Historik |
|---|---|
| Binance `data.binance.vision` (liquidationSnapshot) | Tom |
| Binance `allForceOrders` | Borttagen |
| OKX publik endpoint | Ca 24 h bakåt (BTC: 1 293 likvidationer, äldsta 2026-09-30 12:54Z) |
| Coinalyze | Kräver API-nyckel |
| CoinGlass | Betald |
| Bybit public | Saknar likvidationer |

**Följd:** P3-A går inte att testa OOS. Två ersättningar gjordes.

1. **Likvidationsproxy** (`p3_lab.py`): abrupta OI-fall (5/15 min, z mot 24 h) uppdelade på prisriktning (long- respektive short-likvidation) och OI-acceleration.
   - 72 hypoteser, 1 med p < 0,05 (≈ 3,6 väntas), 0 klarar BH (lägsta q 0,50).
   - Bästa i TRAIN: `oiz15` 15 m vid DOWN-händelser, ρ +0,049 → −0,002 / −0,005 / +0,035. Tecknet växlar.
   - Proxyn bygger på OI och pris, som baslinjen redan har, så den kan inte heller räknas som en ny källa.
2. **Deskriptivt OKX-urval** (`p3_liquidations.py`, 10 kontrakt, ~24 h): 23 likvidationskluster (topp 1 % per minut).
   - Klassisk "reversal efter likvidation": andelen reversal är 43 / 57 / 48 / 26 / 48 / 30 % vid 5 / 15 / 30 / 60 / 120 / 240 min.
   - Medel −0,09 / +0,06 / +0,01 / −0,41 / +0,01 / −0,20 %.
   - Ingen konsekvent riktning, n = 23 och 1 dygn: ingen slutsats är möjlig.

## 3. Order flow (P3-B)

**Features**, från taker-buy-volym i 5m-klines, beräknade vid T:
- taker-imbalance över 5, 15, 30 och 60 min;
- förändring (15 min) och acceleration;
- persistens (antal bars i följd med samma tecken som rörelsen);
- onormalt nettoaggressivt flöde (z mot 24 h);
- prisrespons per flödesenhet (relativ Kyle-lambda);
- divergens flöde mot pris (absorption).

**Test:** partiell korrelation mot framtida avkastning efter att feature och utfall residualiserats på allt signalen redan ser:
- 30m-, 1h-, 4h- och 24h-avkastning, |30m|;
- RSI, volym-z, funding, 1h-OI-förändring, ATR15;
- BTC 4h.

Klustring per dag. ~57–67 k händelser i TRAIN per riktning.

**Resultat:** 120 hypoteser, 16 med p < 0,05 i TRAIN (≈ 6 väntas), **0 klarar BH** (lägsta q 0,245).

De starkaste TRAIN-hypoteserna:

| Feature | Horisont | Riktning | ρ TRAIN | VALID | TEST | HOLDOUT |
|---|---|---|---|---|---|---|
| ti15 (imbalance 15 min) | 15 m | UP | −0,014 (p 0,002) | −0,005 (0,42) | +0,006 | +0,003 |
| flowz (onormalt flöde) | 5 m | UP | −0,018 (0,003) | −0,006 (0,49) | −0,001 | +0,004 |
| persist | 2 h | UP | −0,017 (0,004) | −0,004 | +0,005 | −0,006 |
| ti15 | 1 h | UP | −0,011 (0,008) | −0,014 (0,04) | **+0,011 (0,02)** | +0,006 |
| impact | 30 m | DOWN | +0,022 (0,02) | −0,019 (0,07) | +0,002 | +0,005 |

- |ρ| ≤ 0,02.
- Tecknet växlar mellan perioderna. I flera fall är TEST "signifikant" med *motsatt* tecken mot TRAIN, vilket är det urvalsbias kravet ska avvisa.
- Q5−Q1-spreaden för ti15 är −0,044 / −0,015 / +0,011 / +0,027 %, mot en kostnad på 0,16 %.

**Svar på huvudfrågan:** nej. Order flow tillför ingenting som pris och volym inte redan ger. Det gäller även när testet görs villkorat på våra egna signaler och på korta horisonter, vilket 09-30-studien (helt rutnät, 1 h/4 h) inte gjorde.

## 4. Spot mot perpetual (P3-C)

### Sekundnivå (`p3_leadlag.py`)

Underlag: 6 symboler (BTC, ETH, SOL, XRP, DOGE, LINK) × 8 dagar, två per period. 48 symbol-dagar, alla kompletta. Perp från aggTrades, spot från 1s-klines.

| Fönster | Samma fönster | spot → perp (spot leder) | perp → spot |
|---|---|---|---|
| 1 s | 0,694 | +0,075 | **+0,119** |
| 5 s | 0,855 | +0,049 | +0,101 |
| 10 s | 0,905 | +0,011 | +0,055 |
| 30 s | 0,953 | −0,013 | +0,012 |
| 1 min | 0,973 | −0,004 | +0,015 |
| 3 min | 0,989 | −0,020 | −0,014 |
| 5 min | 0,993 | −0,069 | −0,069 |
| 15 min | 0,997 | −0,091 | −0,085 |

Det finns en lead på några sekunder i båda riktningarna, men det är främst **perp som leder spot**. Efter 30 s är den ≈ 0. De negativa värdena vid 5–15 min är gemensam mean reversion, inte en lead.

**Händelsestudie:** 2 171 tillfällen där spot låg klart före perp (topp 0,1 % av gapet över 10 s, median 4,1 bp). Perps rörelse i gapets riktning, ALLA perioder:

| Latens | +5 s | +30 s | +60 s | +5 min | +15 min |
|---|---|---|---|---|---|
| 0 s | +1,0 bp | +1,3 | +1,2 | +1,0 | +0,9 |
| 1 s | +0,2 | +0,5 | +0,3 | +0,2 | +0,1 |
| 5 s | +0,3 | +0,3 | +0,2 | +0,1 | 0,0 |
| 1 min | +0,1 | +0,4 | +0,7 | −0,2 | +0,1 |
| 5 min | +0,1 | 0,0 | 0,0 | −0,4 | −0,1 |

- Kostnaden är 16 bp (taker) eller 4 bp (maker, optimistiskt). Effekten ligger under kostnaden **även vid noll latens**.
- Den är borta efter 1 s. Bara TRAIN visar något mer (+2–5 bp vid 0 s); VALID, TEST och HOLDOUT ≈ 0.
- → **OPERATIONELLT OANVÄNDBAR**, och inte heller statistiskt användbar efter kostnad.

### Botens tidsskala (`p3_spot.py`, 40 mest likvida symboler, 86 154 händelser)

**Features:** spot minus perp-avkastning över 5, 15 och 30 min, spotens taker-imbalance och gapet mot perp, basis och 1h-basisförändring.

**Resultat:** 84 hypoteser, 5 med p < 0,05 i TRAIN (≈ 4,2 väntas), 0 klarar BH (lägsta q 0,345).

Bästa: `spot_lead5` 5 m UP, ρ +0,0225 / +0,0225 / +0,001 / −0,000.
- Konsekvent i TRAIN och VALID, men borta i TEST och HOLDOUT.
- Q5−Q1-spreaden är 0,03 % mot en kostnad på 0,16 %.

## 5. LONG mot SHORT (P3-D, deskriptivt)

Botens bracket, netto per trade, för samma händelser i båda riktningar:

| Period | LONG medel | LONG vinst-% | SHORT medel | SHORT vinst-% | LONG PF | SHORT PF |
|---|---|---|---|---|---|---|
| TRAIN | −0,212 % (−0,049 R) | 44 % | **−0,124 %** | 48 % | 0,83 | 0,90 |
| VALID | **−0,136 %** | 44 % | −0,207 % | 48 % | 0,88 | 0,83 |
| TEST | **−0,154 %** | 43 % | −0,189 % | 48 % | 0,87 | 0,84 |
| HOLDOUT | **−0,006 %** | 46 % | −0,343 % | 46 % | 0,99 | 0,74 |
| ALLA | −0,152 % (−0,035 R) | 44 % | −0,189 % (−0,044 R) | 48 % | 0,87 | 0,84 |

**Övriga mått:**
- **MFE/MAE** (6 h, %): LONG +2,17 / −2,08, SHORT +2,13 / −2,10. Symmetriskt.
- **Snittvinst/-förlust:** LONG +2,37 / −2,15 %, SHORT +2,14 / −2,31 %.
- **Exits:** ~70 % går på tidsgräns, TP 14 % och SL 15 % för LONG; TP 11 % och SL 18 % för SHORT.

**Regimberoende** (BTC 4h-trend):
- ALLA perioder: LONG −0,19 / −0,17 / −0,13 % i BTC upp/ned/flat, SHORT −0,15 / −0,18 / −0,21 %.
- Det varierar mellan perioderna. I HOLDOUT gav LONG +0,20 % när BTC steg, och SHORT −0,54 %.

**Tolkning:**
- LONG har haft en svag fördel i tre av fyra perioder, eftersom marknaden 2026 steg.
- Båda riktningarna är negativa netto i alla perioder.
- Strategin har en strukturell long bias (LONG-only), men den bias:en är inte källan till förlusterna; signalen saknar riktning åt båda hållen.
- Detta är deskriptivt, ingen rekommendation.

## 6. OOS-resultat

Inga hypoteser gick vidare från urvalet, så TEST och HOLDOUT avgjorde inget beslut. TEST/HOLDOUT-siffrorna ovan visas bara för att illustrera tecken-växlingen. Utan förregistreringen hade TEST-"signifikanta" `ti15` 1 h (ρ +0,011, p 0,02) kunnat se ut som ett fynd, trots motsatt tecken mot TRAIN. Det är selection bias och avvisas.

## 7. Multiple testing

| Familj | Hypoteser | p < 0,05 TRAIN | väntat av slump | BH q < 0,10 | + VALID | + TEST & HOLDOUT | + ekonomi |
|---|---|---|---|---|---|---|---|
| Order flow | 120 | 16 | 6,0 | 0 | 0 | 0 | 0 |
| Likvidationsproxy | 72 | 1 | 3,6 | 0 | 0 | 0 | 0 |
| Spot/perp | 84 | 5 | 4,2 | 0 | 0 | 0 | 0 |
| **Totalt** | **276** | **22** | **13,8** | **0** | **0** | **0** | **0** |

Hypotes = feature × horisont (5 / 15 / 30 / 60 / 120 / 240 min) × händelseriktning (upp/ned). LONG och SHORT är samma hypotes med motsatt tecken. BH gjordes över alla 276 tillsammans.

## 8. Latens (P3-G)

**Botens verkliga pipeline,** uppmätt i DB 09-26 → 10-01:
- discovery startar i median var **21,8:e minut** (15 min paus + tick på median 6,5 min, q75 9 min);
- en signal upptäcks alltså i snitt ~11 min efter att den uppstår;
- AI-steget efter P1: ~16 s;
- historiskt låg det i median 17 min från signal till LIVE-fill (före P1).

**Bedömning per källa:**

| Källa | Bedömning |
|---|---|
| Spot → perp-lead | Borta inom 1–5 s → avvisad på latens, kostnad och OOS |
| Order flow och likvidationsproxy | Ingen information ens vid den optimistiska latensen 5 min (alla horisonter från 5 min testades). Latensen är inte orsaken: informationen finns inte |
| Likvidationer (äkta) | Okänd; data saknas |

## 9. Kostnader

- Alla horisontresultat redovisas brutto och netto (0,16 % tur och retur).
- Bracket-resultaten är netto med avgift, slippage (0,03 % entry, 0,10 % stop) och funding.
- Spread finns inte historiskt (bookTicker arkiveras inte efter 2024) och ingår i slippage-antagandet.
- Ingen hypotes har en bruttospread (Q5−Q1) över kostnaden i mer än en period:

| Hypotes | Bruttospread TRAIN / VALID / TEST / HOLDOUT |
|---|---|
| Order flow | −0,04 / −0,02 / +0,01 / +0,03 % |
| Spot/perp | +0,03 / +0,03 / 0,00 / +0,03 % |
| Likvidationsproxy | +0,44 / −0,07 / −0,03 / +0,04 % (ett enda TRAIN-utslag) |

## 10. Signalretention och frekvens

- Baslinjen på Binance-universumet är ~789 händelser per dag (135 symboler). LIVE begränsas av 4 platser och AI-budget.
- Den förregistrerade regeln "släpp sämsta kvintilen" (80 % retention) gav för de bästa hypoteserna:
  - TRAIN-satta kvintilgränser höll inte i OOS; retentionen sjönk till **74–79 %**;
  - netto förbättrades inte stabilt (exempel order flow LONG bracket: −0,207 → −0,201, −0,092 → −0,073, −0,228 → **−0,243**, +0,016 → +0,037);
  - inget blev positivt i alla perioder.
- Det fanns ingen kandidat att bevara frekvensen för.

## 11. Robusthet

- **Point-in-time:** alla features räknas på bars stängda vid T. Entry ligger 5 min efter T. Testat i `test_p3_features.py` och `test_p3_lab.py`, 8 tester.
- **Purging:** händelser vars utfallsfönster når in i nästa period är borttagna.
- **Förregistrering:** kriterierna står i `p3_analysis.py` och skrevs innan något resultat räknades. TEST och HOLDOUT användes inte för urval.
- **Klustring:** UTC-dagar.
- **Winsorisering:** 0,5 % i båda ändar för features och kontroller.
- **Begränsningar:**
  - Binance-data som proxy för BingX (09-30-studien visade synk inom samma minut);
  - survivorship (dagens 135 symboler);
  - spotfamiljen gäller bara de 40 mest likvida symbolerna;
  - sekundstudien är 48 symbol-dagar.

## 12. Avvisade hypoteser

1. Taker-imbalance (5 / 15 / 30 / 60 min), förändring, acceleration, persistens, onormalt flöde, prisrespons per flöde, divergens/absorption. Inget klarar BH, och tecknet växlar.
2. Likvidationsproxy (OI-fall × prisriktning, OI-z, acceleration): klarar inte BH och är inte en ny källa.
3. Äkta likvidationer: kan inte testas (ingen historik). 1-dygnsurvalet visar ingen konsekvent reversal.
4. Spot-leder-perp på minutnivå (spot_lead 5 / 15 / 30, spot-imbalance, imbalance-gap, basis, basisförändring): klarar inte BH, och spot_lead5 försvinner i TEST och HOLDOUT.
5. Spot-leder-perp på sekundnivå: finns i 1–5 s, är svagare än perp-leder-spot, ligger under kostnaden även vid noll latens, och är operationellt oanvändbar för en pipeline med minuters latens.
6. LONG mot SHORT som "lösning": båda negativa i alla perioder.

## 13. Överlevande hypoteser

**Inga.**

## 14. Vad som krävs för nästa steg

Nästa steg kräver ett **beslut**, inte mer kod.

1. **Likvidationer är den enda källan som inte kunnat prövas.** Två vägar:
   - (a) en API-nyckel till en historisk källa: Coinalyze har en gratisnyckel, CoinGlass är betald. Med den kan samma förregistrerade test köras på 12+ månader på någon timme;
   - (b) en insamlare (OKX var 30:e minut, Binance forceOrder-websocket) som körs i minst 3–6 månader innan ett OOS-test går att göra.

   Inget är startat.
2. **Alla prisderiverade källor är nu uttömda** för den här signalen och tidsskalan: pris, volym, RSI, funding, OI, order flow, likvidationsproxy, spot/perp och cross-exchange. Fortsatt research bör antingen
   - ha en ny *typ* av information: on-chain-flöden till och från börser, tidsstämplade nyheter/katalysatorer, optionsdata; eller
   - byta strategiklass: en tidsskala där vi har en fördel, och marknader där kostnaden är lägre i förhållande till rörelsen.
3. **Rekommendation:** LIVE förblir stoppat. Ingen implementationsuppgift skapas.

## Reproducera

```
python -m crypto_trading.entry_research.p3_lab scan            # 335 502 baslinjehändelser + features (~10 min)
python -m crypto_trading.entry_research.p3_data micro          # 1 s-urval spot/perp (6 × 8 dagar)
python -m crypto_trading.entry_research.p3_data spot           # spot 1m, 40 symboler
python -m crypto_trading.entry_research.p3_spot                # spot/perp-features per händelse
python -m crypto_trading.entry_research.p3_analysis            # baslinje, LONG/SHORT, 276 hypoteser
python -m crypto_trading.entry_research.p3_leadlag             # sekundnivå lead/lag
python -m crypto_trading.entry_research.p3_liquidations        # OKX 1-dygnsurval (deskriptivt)
```

Resultat sparas i `data/entry_research/p3_*.json`/`.db` (gitignored).
