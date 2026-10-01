# Trading-system review: förlusterna 10-01, exit-hantering, sena signaler (2026-10-01)

Boten är **stoppad**. Start-uppgifterna `crypto_trading_bot` och `crypto_trading_evidence_shadow` i Task Scheduler är **inaktiverade** (Disabled). Ingen tradinglogik har ändrats.

## Kort svar

1. **Förlusten 10-01 (−129,7 USDT) var en entry-förlust, inte en exit- eller driftförlust.** Fyra korrelerade LONG-positioner togs inom 21 minuter från samma discovery-batch, in i en marknad som föll. Ingen av dem var någonsin i nämnvärd vinst (högsta MFE +0,86 %). Boten hade gjort samma förlust om den varit igång: −136 USDT i replay mot −130 faktiskt.
2. **~90 % av alla förlust-USDT kommer från trades som aldrig nådde +1 %.** "Bra entry + dålig management" står för ~3 %. Det gäller bot-trades, LIVE och hela signalpopulationen.
3. **"+4,5 % av 5 % och sedan tillbaka" händer, men sällan:** 11 av 256 trades (4 %). Fem av dem slutade på break-even (−2 USDT).
   - Den vanligare vinstförlusten är den omvända: PP@+1 % stoppar ut på break-even, och sedan går priset till TP. Exempel: NIGHT 10-01, som gav −1,8 USDT i stället för TP på +87.
4. **Inget av 30 förregistrerade exit-modeller klarar OOS-kraven.** Profit-lock/trailing ger plus i de mean-reverterande perioderna och minus i trendperioderna. GODFATHER som manager ger samma tecken-växling.
5. **"Sena/översträckta" signaler är inte en dålig regim i vår data.** På botens egna trades är de tvärtom bättre. Ett "köp inte toppar"-filter skulle ta bort de bästa traderna.
6. **Signalen har ingen mätbar riktningsinformation.**
   - Efter en signal är medelavkastningen +0,04 % efter 30 min, −0,04 % efter 1 h och +0,12 % efter 4 h, med 50 % uppgångar.
   - Det är grundorsaken bakom både problem 1 och 2.
7. **Implementerat:** inget i tradinglogiken. Allt som testades föll på de förregistrerade kraven. Analysverktyget är committat så att samma test kan köras om på ny data.

---

## Del 1: Säkring och snapshot

| Kontroll | Resultat |
|---|---|
| Bot-process | Kör inte. Den dog 2026-10-01 08:10Z, exit 0x40010004 (processen dödad; samma kod som 09-30 "killed with the session"). Mellan 08:00 och 08:10Z misslyckades varje live-tick med `getaddrinfo failed` (nätverket låg nere). |
| Evidence shadow | Kör inte. Wrapper-loopen dog 08:10Z. |
| Task Scheduler | `crypto_trading_bot` och `crypto_trading_evidence_shadow` är satta till **Disabled**; de har ingen trigger. `crypto_trading_watchdog` kör var 5:e min men är alert-only (startar inget). |
| Exchange 11:02Z | **0 öppna positioner, 0 öppna ordrar, balance/equity 0,0000 USDT.** |
| Öppna positioner att skydda | Inga. Inga manuella ingrepp gjordes. |
| DB vs exchange | 4 `live_executions` står ACTIVE men är stängda på exchange. De stängs av `reconcile_active_executions` vid nästa start (befintligt testat flöde: `test_reconcile_active_executions_closes_out_positions_gone_flat_on_exchange`). |
| Snapshot | `data/entry_research/2026-10-01-exchange-snapshot.json` (gitignored eftersom den innehåller konto-ID). Även exchangens order- och income-historik är läst. |

Saldot 0 USDT betyder att kapitalet har flyttats från futures-kontot. Exchangens ledger visar inga fler trades efter 09:16Z.

## Del 2: Rotorsak till förlusten 10-01

Alla fem trades kom från discovery-run `f7580dc7`, som startade 06:59:56Z. Siffrorna nedan kommer från exchangens egen orderhistorik, i USDT efter avgifter.

| Trade | Entry (fill) | SL / TP | Trigger | Före entry | MFE / MAE | Exit | P/L |
|---|---|---|---|---|---|---|---|
| PUMP | 07:10:41 @0.005848 | −5,1 % / +6,0 % | bara volym (z 3,98) | 30m −1,2 %, RSI 44 | **+0,05 %** / −5,1 % | SL 08:36Z | **−52,07** |
| ADA | 07:14:40 @0.2528 | −4,3 % / +5,6 % | RSI 83 vid +0,08 % pris (RSI-artefakt) | 1h −0,08 % | **+0,16 %** / −3,9 % | manuell market 09:15Z | **−23,84** |
| NIGHT | 07:20:55 @0.04100 | −3,3 % / +8,8 % | prisrörelse 3,4 % | 24h +27,7 % | +1,63 % → BE | PP-stop 07:30Z | −1,79 (gick sedan till **+10,2 %**) |
| STX | 07:23:08 @0.4061 | −5,2 % / +7,1 % | pris 3,1 % + RSI 80/83 | 24h **+29 %**, 4h +5,9 %, 13,8 % över VWAP24 | **+0,86 %** / −5,4 % | SL 08:12Z | **−54,04** |
| GOOGL (tokeniserad) | 07:31:12 @351.42 | −3,4 % / +5,6 % | volym z 12,7 + RSI 74 | 4h −0,4 % | +0,41 % / −0,2 % | manuell market 09:16Z | +2,03 |
| **Summa** | | | | | | | **−129,71** |

**Gemensamt för de fyra stora:**
- **Marknaden:** BTC föll från 84 093 (07:00) till 83 172 (08:30), −1,1 %. När STX köptes steg bara 11 % av 40 large caps senaste timmen; när GOOGL köptes 3 %. Alla tre krypto-alts föll 3–5 % samtidigt.
- **AI-kedjan:** Forecast-agenten var mer bearish än bullish för ADA (0,38 mot 0,28), STX (0,40 mot 0,30) och GOOGL (0,35 mot 0,33). Bear-agenten pekade ut "överköpt utan volym" för STX och "RSI utan prisrörelse" för ADA. Gate gav ändå `CONFIRMED all_checks_passed`.
  - Tidigare research har visat att AI-rollerna inte separerar utfall (forecast Brier sämre än klimatologi), så det här är en observation, inte ett bevisat fel.
- **Risk:** Safety Kernel loggade en worst-case-portföljrisk på 196 USDT mot 391 USDT equity (50 %). Det var log-only enligt beslut 09-30.
- **Guardian:** HOLD hela tiden, med decay 0,01–0,31 (WATCH-tröskeln är 0,35). Decay-faktorerna reagerar inte på P/L eller marknadsfall. GODFATHER/Guardian Authority har **0 beslut någonsin** i `guardian_authority_decisions`. PP triggade aldrig för de fyra eftersom ingen nådde +1 %.

**Klassificering:**

| Kategori | USDT | Trades |
|---|---|---|
| A. Dålig entry | −129,9 | PUMP, STX, ADA (alla med MFE < +1 %) |
| B. Dålig exit/management | ≈ 0 | NIGHT: PP kapade en vinnare (−1,8 i stället för ≈ +87) – en *utebliven* vinst, inte en förlust |
| C. Downtime/drift | ≈ 0 | Exchange-SL fanns hela tiden. Med boten igång hade ADA/GOOGL stängts av 6 h-gränsen: −25,0 / −2,0 i replay mot −23,8 / +2,0 manuellt. |
| D. Execution/slippage/avgifter | ≈ −5 | avgifter ~1 USDT/trade; LIVE-fills var 0,7 % *bättre* än paper för STX |
| E. Svår att undvika givet info vid entry | delvis | Data visar att varken "översträckt", "svag marknadsbredd" eller "RSI utan rörelse" är robusta varningar (se del 4) |

**Hade samma förlust hänt med boten igång och GODFATHER/Guardian enligt nuvarande design?** Ja.
- Replayen av nuvarande regler ger −135,9 USDT mot −129,7 faktiskt.
- Ingen nuvarande mekanism agerar på en trade som går rakt ner till SL.
- Den enda mekanism som hade ändrat utfallet är att inte ta traderna, alltså entry-urvalet.

## Metod (del 3–9)

- **Populationer:**
  - **TRADES:** 256 positioner som boten öppnade 09-03 → 10-01, varav 103 LIVE. Entry-tid är LIVE-fill-tiden, annars gate-tiden; `opened_at` är cykelstart och för tidig.
  - **SIGNALS:** 1 135 oberoende kvant-kandidater, första per symbol och 6 h. BUDGET_LIMITED ingår. Entry sker vid signal + 10 min, med Risk Agentens SL/TP eller median-geometri (−4,3 % / +5,0 %).
- **Prisbanor:** riktiga BingX 1m-klines, 24 h före och 8 h efter varje kandidat; 180 symboler, cache `data/entry_research/trade_review.db`. 40 large caps på 5m används för BTC/ETH-regim och bredd.
- **Replay:**
  - En stop eller ett lås sätts vid en bar-close och gäller från **nästa** bar.
  - Inom en bar kontrolleras stop före TP.
  - Gap fylls på open.
  - Avgift 0,05 %/sida. Slippage 0,10 % på alla stop-typer och 0,03 % på market-exits.
  - Max hold 6 h. PP är +1 % → SL = entry, precis som i LIVE.
- **Validering mot verkligheten:**
  - Korrelationen simulerat/faktiskt är 0,89 på 89 bot-styrda LIVE-exits, skalat till 1000 notional.
  - Simulatorn är något *optimistisk*: −326 simulerat mot −452 faktiskt. Mark- och last-pris skiljer sig, och verklig slippage var större.
- **Perioder:**
  - TRAIN 09-01..14, VALID 09-15..21, TEST 09-22..27, HOLDOUT 09-28..10-01.
  - Antal SIGNALS 449/167/171/348; antal TRADES 110/35/34/77.
- **Förregistrerade godkännandekrav,** skrivna i `trade_review_run.py` innan något resultat räknades:
  - **Exit-modell:** SEL (TRAIN+VALID) > 0 på båda populationerna med BH q < 0,10, TEST och HOLDOUT > 0, och ett dag-klustrat bootstrap-CI för OOS som utesluter 0.
  - **Entry-flagga:** högst 20 % flaggade, flaggade sämre i SEL med q < 0,10, samma tecken i TEST, HOLDOUT och TRADES OOS, och högre OOS-expectancy *och* total P/L.

### Nuläge (BASE = nuvarande regler), USDT per 1000 notional

| | n | Total | Expectancy | Median | Vinst-% | Snittvinst | Snittförlust | PF | Max DD |
|---|---|---|---|---|---|---|---|---|---|
| TRADES | 256 | −90 | −0,35 | −2,0 | 26 % | +29,9 | −11,1 | 0,96 | −598 |
| LIVE (subset) | 107 | −197 | −1,84 | −2,0 | 22 % | +29,6 | −10,9 | 0,78 | −367 |
| SIGNALS | 1 135 | −2 153 | −1,90 | −2,0 | 27 % | +22,0 | −10,6 | 0,76 | −3 708 |

Verkliga LIVE-pengar vid 100×10: 62 trades sedan 09-26, **−210,7 USDT** (58 avstämda −82,7 plus de fyra från 10-01 −127,9).

## Del 3: Trade går +4–5 % och vänder

### Vad som händer efter en viss vinst (TRADES)

| Nådde ≥ | n (andel) | Når TP utan PP | Slutar i förlust utan PP | Slutar i förlust med PP (BASE) | Snitt BASE | Snitt utan PP |
|---|---|---|---|---|---|---|
| +1 % | 159 (62 %) | 40 % | 31 % | 65 % | +11,2 | +17,7 |
| +2 % | 120 (47 %) | 52 % | 22 % | 62 % | +14,6 | +26,7 |
| +3 % | 95 (37 %) | 63 % | 17 % | 61 % | +17,4 | +35,0 |
| +4 % | 67 (26 %) | 75 % | 12 % | 60 % | +20,8 | +45,4 |
| +4,5 % | 52 (20 %) | 81 % | 10 % | 54 % | +25,5 | +52,8 |
| +5 % | 39 (15 %) | 82 % | 8 % | 54 % | +27,6 | +58,1 |

Läs tabellen så här:
- En trade som nådde +4 % slutar sällan i förlust av själva prisrörelsen (12 %).
- Med PP slutar 60 % "i förlust", men det är nästan alltid break-even-stopp på −2 USDT (avgifter). De inträffar *innan* priset gick vidare till +4 %. PP@+1 % stoppar alltså ut många trades som senare hade nått TP.
- Mängden villkoras på framtida MFE, så siffrorna beskriver vad som hände och kan inte användas som strategi. Den ovillkorade jämförelsen står i tabellen nedan.

Användarens exakta scenario, alltså att nå minst 80 % av TP-avståndet utan att få TP, gäller **11 av 256 trades (4 %)**: 5 slutade på BE (−2) och 6 på tidsgräns med vinst. Det finns, men det är inte där pengarna förloras.

### Exit-modeller, samma entries (100 % signal-retention)

Värdena är medelskillnad mot BASE i USDT/trade. ✓ betyder att kravet uppfylls.

| Modell | TRADES SEL | TRADES TEST / HOLD | SIGNALS SEL (q) | SIGNALS TEST / HOLD | SIGNALS OOS CI90 | Kapade vinnare / undvikna förluster (TRADES) | Klarar kraven? |
|---|---|---|---|---|---|---|---|
| NO_PP (ta bort PP) | +4,04 | +1,93 / +5,01 | +1,64 (0,29) | −0,17 / −1,96 | [−4,0, +0,9] | 0 / 54 | Nej: minus OOS på signaler |
| BE_FEE (PP till entry+0,2 %) | −0,65 | +0,94 / +0,10 | −0,15 (0,73) | +0,03 / +0,39 | [−0,04, +0,70] | 2 / 0 | Nej |
| A_3_1.5 (lås +1,5 % vid MFE +3 %) | +0,27 | +1,32 / +1,20 | −0,03 (0,96) | +0,47 / +0,66 | [+0,49, +0,70] | 1 / 22 | Nej: SEL ≈ 0, q 0,96 |
| B_STEP gap 1,5 (trappsteg) | +0,65 | +1,32 / +0,51 | −0,25 (0,71) | +0,60 / +0,90 | [+0,58, +1,03] | 6 / 41 | Nej: SEL < 0 |
| C_ATR3 at 2 % | +0,19 | −0,21 / −0,83 | −0,35 (0,36) | +0,08 / +0,30 | [+0,11, +0,35] | 2 / 6 | Nej |
| D_MFE 0,5 at 3 % | +0,42 | +0,31 / +0,48 | −0,01 (0,96) | +0,48 / +0,77 | [+0,48, +0,86] | 5 / 22 | Nej: SEL ≈ 0 |
| E peak-retrace 40 % at 2 % | +1,34 | +2,53 / −0,67 | −0,29 (0,71) | +1,18 / +1,02 | [+0,75, +1,52] | 10 / 40 | Nej |
| F vol-adaptiv 0,3 | +0,56 | −0,28 / −0,92 | −0,43 (0,57) | +0,53 / +1,00 | [+0,51, +1,15] | 13 / 21 | Nej |
| G lås 50 % MFE efter 2 h | +0,10 | +0,26 / −0,09 | −0,28 (0,67) | +0,26 / +0,60 | [−0,02, +1,16] | 1 / 26 | Nej |
| G hold 3 h | +0,11 | +0,09 / −1,17 | −0,83 (0,29) | −1,27 / +0,33 | [−1,6, +1,3] | 6 / 16 | Nej |
| H near-TP lock 0,8 | +0,09 | −0,79 / −0,13 | +0,30 (0,42) | +0,30 / +0,26 | [+0,12, +0,39] | 8 / 5 | Nej: minus på TRADES OOS |
| J PP + lås 3/1,5 | −0,14 | +2,09 / +0,95 | −0,26 (0,71) | +0,41 / +0,94 | [+0,41, +1,23] | 3 / 19 | Nej |
| K TP × 0,9 | +0,06 | −0,86 / −0,12 | −0,04 (0,93) | −0,38 / −0,06 | [−0,31, +0,02] | 0 / 2 | Nej |

Totalt testades 30 modeller. Hela tabellen med perioder, vinst-%, snittvinst/-förlust, PF och DD finns i `data/entry_research/trade_review_results.json`.

**Mönstret är regimberoende.**
- Nästan alla lås- och trailing-modeller är negativa i TRAIN/VALID (trendigare marknad) och positiva i TEST/HOLDOUT (mean-reverterande, där även BASE var som sämst: −7,3/trade).
- Att trailing ser bra ut OOS är alltså inte en edge; det beror på marknadsläget. Samma tecken-växling sågs i exit-labbet 09-30 på 2,4 M syntetiska entries.

**Post-hoc PP-familj, tydligt märkt som efterhandstest:** PP vid 1–3 % kombinerat med lås 0–1 %, plus kombinationer.
- På botens egna trades är en senare PP genomgående bättre. PP 2 % → lås +0,5 % plus lås +1,5 % vid +3 % är positiv i 4/4 perioder, med OOS CI [+0,04, +3,48].
- På signalpopulationen är samma regel negativ i HOLDOUT. Varje senare PP-variant är sämre OOS på signaler.
- Inte robust. Inte implementerad.

## Del 4: Sen/översträckt signal

Flagga mot oflaggade, medel-P/L under BASE i USDT/trade.

| Hypotes | Andel flaggade (SIG) | SIGNALS SEL flagga / övr | SIGNALS TEST | SIGNALS HOLD | TRADES SEL | TRADES OOS | Klarar? |
|---|---|---|---|---|---|---|---|
| LATE_4H_EXTENDED (4h-avkastning i topp 20 %, inom 1 % av 4h-high) | 10 % | +0,8 / +1,4 | −0,3 / −2,5 | −1,0 / −7,8 | **+3,2 / −1,6** | **+13,3 / −1,1** | Nej: flaggade är *bättre* |
| PRICE_Z_EXTREME (1h z > 2,5) | 9 % | +2,1 / +1,2 | −0,7 / −2,5 | −11,0 / −6,9 | +5,5 / −1,5 | +9,3 / −0,4 | Nej |
| VWAP24_STRETCH (> 6 % över VWAP24) | 14 % | +4,2 / +0,8 | −2,9 / −2,4 | −6,6 / −7,4 | **+13,9 / −5,0 (p 0,005)** | **+6,5 / −3,0** | Nej: flaggade är *bättre* |
| BLOWOFF_WICK | 2 % | +8,5 / +1,1 | n=1 | n=4 | n=3 | n=0 | Nej: för få |
| ISOLATED_PUMP (coin +3 %, marknad ≤ 0) | 3 % | +4,0 / +1,2 | +1,6 / −2,5 | −14,1 / −7,1 | +44 / −1,7 (n=2) | +27 / −0,8 | Nej |
| EXHAUSTED_VOLUME (volym utan prisrörelse) | 6 % | −0,3 / +1,4 | +1,3 / −2,8 | −1,2 / −7,7 | −12,3 / −0,3 | −0,9 / +0,5 | Nej: tecknet vänder |
| NO_4H_MOMENTUM (≤ 0,17 %, tidigare hypotes) | 25 % | +2,4 / +1,0 | −1,5 / −3,4 | −4,3 / −9,0 | −4,4 / −0,1 | +3,8 / −1,8 | Nej |
| RSI_HOT_NO_MOVE (RSI ≥ 70, 1h < 1 %; ADA-fallet) | 33 % | +0,2 / +1,9 | −1,8 / −2,7 | −9,5 / −6,5 | −4,3 / +0,4 | −2,1 / +1,0 | Nej: TEST vänder tecken och andelen > 20 % |

Efterhandstest av marknadsläget (STX/GOOGL-fallet):
- Bredd < 15 %, BTC 1h ≤ −0,5 % och median-coin 1h ≤ −0,5 % byter alla tecken mellan perioder.
- Exempel, bredd < 15 % på TRADES: −2,5 / +24,7 / +10,9 / +1,5 mot övriga −3,0 / +3,2 / +0,4 / −2,6.

Feature-kvintiler (22 features: avstånd till high/VWAP/SMA, accel, ATR-expansion, gröna candles, wick, volym, RSI, bredd, BTC-regim, pos24h) visar inget monotont mönster. Alla kvintiler är plus i SEL och minus OOS.

**Slutsats D:** Det finns inget observerbart "late/exhaustion"-läge med robust sämre expectancy. Översträckta entries går tvärtom bättre på botens AI-valda trades, i linje med 09-28-fyndet att "overextension veto är en fälla". STX 10-01 är ett verkligt men inte representativt exempel. Ett filter skulle ha tagit bort ~10–15 % av signalerna och sänkt total P/L.

## Del 5: Entry-kvalitet mot management-kvalitet

**Definitioner:**
- God entry: MFE ≥ max(2 %, 40 % av TP-avståndet).
- Dålig entry: MFE < 1 %.
- God management: god entry som slutade ≥ 0, eller dålig entry som förlorade < 60 % av SL-avståndet.

| Grupp | TRADES n | TRADES förlust | Andel av förluster | LIVE andel | SIGNALS andel |
|---|---|---|---|---|---|
| A: bra entry + dålig management | 30 | −60 | **2,8 %** | 3,1 % | 2,8 % |
| B: dålig entry + bra management | 65 | −631 | 29,8 % | 22,2 % | 32,0 % |
| C: dålig entry + dålig management (full SL) | 32 | −1 275 | **60,3 %** | 66,1 % | 58,5 % |
| D: bra entry + bra management | 43 | 0 (+1 882 vinst) | 0 % | 0 % | 0 % |
| M: MFE 1–2 %, förlorad | 74 | −148 | 7,0 % | 8,5 % | 6,7 % |

**Entry står för ~90 % av förlusterna** (B+C) och management för ~3 % (A). Inget exit-system kan rädda en trade som aldrig går upp.

## Del 6: GODFATHER som manager (historisk replay, ingen future leakage)

**Upplägg:**
- GODFATHER (`claude-sonnet-5`) tillfrågades vid exakt de tillfällen problemet gäller:
  - första gången MFE når +2 %, +3 % och +4 %;
  - första 40 %-återgången från en topp ≥ +2 %;
  - efter 60 min om traden då låg ≤ −2 %.
- Den fick bara information fram till beslutsminuten: entry, SL/TP, stop, MFE/MAE, 2 h 5m-bana, RSI, ATR, BTC/bredd, signal och forecast.
- Beslut: HOLD / PROTECT / TIGHTEN_SL / EXIT.
- Kodens säkerhetsregler gällde: stop höjs bara, aldrig över priset, och sista SL tas aldrig bort.
- 233 beslut, 0 fel, kostnad **2,85 USD**. Kod: `entry_research/godfather_manager_replay.py`.

**Resultat:**

| Period | n | BASE | GODFATHER | Diff/trade |
|---|---|---|---|---|
| TRAIN | 110 | −313 | −205 | +0,98 |
| VALID | 35 | +156 | +137 | −0,52 |
| TEST | 34 | +160 | +235 | +2,23 |
| HOLDOUT | 77 | −110 | −131 | −0,27 |
| Alla | 256 | −108 | +37 | +0,57 (OOS CI90 [−1,75, +2,42]) |

- Besluten fördelade sig: PROTECT 204, HOLD 13, TIGHTEN_SL 9, EXIT 7.
- GODFATHER skyddar nästan alltid. Det är en profit-lock med varierande nivå, inte urskiljning.
- 49 trades blev bättre och 16 sämre; 38 förluster undveks och 7 vinnare kapades.
- Tecknet växlar mellan perioder, samma mönster som de deterministiska låsen. **Inte robust, så ingen LIVE-aktivering.**

Begränsning: replayen startade innan fill-tiderna för 10-01-traderna rättades, så de fyra korrigerade fill-tiderna används inte där. De nådde aldrig +2 % och påverkar därför bara EARLY_LOSS-punkten.

De befintliga GODFATHER-shadow-besluten (`evidence_shadow.db`) är för få för att användas: 62 management-svar, och resten föll på slut-krediten 09-30.

## Del 7: Vilken entry-hypotes har vi egentligen?

Pipelinen är en **riktningsneutral anomalidetektor**: |Δpris 30m| ≥ 2 %, RSI ≥ 70, volym-z ≥ 2,5 eller funding ≥ 0,05 %. Därefter ger AI ett **LONG-only**-beslut. I praktiken antas momentum/breakout-fortsättning ("volym före pris", "RSI-breakout").

Test av hypotesen: framåtavkastning från entry, signal + 10 min.

| Trigger, rörelse före | n | 30m | 1h | 2h | 4h (andel upp) |
|---|---|---|---|---|---|
| Alla, upp före | 718 | +0,04 % | −0,04 % | −0,16 % | +0,12 % (50 %) |
| Alla, ned före | 416 | +0,10 % | +0,05 % | +0,10 % | +0,16 % (45 %) |
| RSI-breakout, upp | 474 | −0,00 % | −0,05 % | −0,18 % | −0,03 % (49 %) |
| Volym, upp | 154 | +0,22 % | −0,09 % | −0,12 % | +0,46 % (54 %) |
| Prisrörelse, **ned** | 82 | +0,50 % | +0,75 % | +0,70 % | +1,34 % (57 %) |
| Funding, upp | 51 | −0,37 % | −0,63 % | −1,38 % | −1,01 % |

Continuation-hypotesen håller inte: efter en uppåt-signal är nästa timme ett myntkast. Den enda avvikelsen är en studs efter kraftigt prisfall, med n=82. Den är inte OOS-testad och ska behandlas som en ny hypotes, inte som ett fynd.

**Information som saknas** för att skilja fortsättning från utmattning:
- orderflöde: taker-imbalans och likvidationer i realtid;
- orderbok-djup;
- spot- mot perp-ledning;
- nyhetskatalysator med tidsstämpel.

Alla nuvarande features är härledda ur samma pris/volym/OI/funding-historik. Över fem studier (09-28 → 10-01) har de inte gett robust edge.

## Del 8: Signalfrekvens

| Förändring | Signal-retention | Trades/dag |
|---|---|---|
| Alla exit-modeller (del 3, 6) | 100 % | oförändrat (~9/dag paper) |
| Testade entry-flaggor (ingen implementerad) | 67–98 % | 2–33 % färre |
| **Implementerat** | **100 %** | **oförändrat** |

## Del 9: Counterfactual för 10-01

Siffror i USDT. "Förbättrad entry/exit" visar det bästa varje familj hade kunnat göra.

| Trade | Faktiskt | BASE replay | Förbättrad entry (bästa flagga) | Förbättrad exit (A_3_1.5 / E_RETR / H_NEARTP / K_TP) | NO_PP | Kombinerat |
|---|---|---|---|---|---|---|
| PUMP | −52,07 | −52,9 | ingen flagga | −52,9 (oförändrat) | −52,9 | −52,9 |
| ADA | −23,84 | −25,0 | RSI_HOT_NO_MOVE hade flaggat → 0 (men flaggan är inte robust) | −25,0 | −25,0 | 0 / −25 |
| NIGHT | −1,79 | −2,0 | VWAP24_STRETCH hade flaggat (fel: den gick till TP) | −2,0 | **+86,8** | — |
| STX | −54,04 | −53,9 | VWAP24_STRETCH flaggar → 0 (men flaggan är i snitt *positiv*) | −53,9 | −53,9 | 0 / −53,9 |
| GOOGL | +2,03 | −2,0 | NO_4H_MOMENTUM | −2,0 | −2,0 | — |
| **Summa** | **−129,7** | **−135,9** | | **−135,9** | **−47,1** | |

Ingen exit- eller management-modell hade hjälpt de fyra stora förlusterna. Två entry-flaggor hade träffat ADA och STX i efterhand, men samma flaggor tar bort lönsamma trades i historiken (VWAP24_STRETCH: +6,5 mot −3,0 OOS) och skulle ha stoppat NIGHT.

## Ytterligare fynd

1. **Manuella stängningar.**
   - Alla 17 `EXTERNAL_CLOSE` i DB är manuella market-order (tomt clientOrderId; boten använder alltid `lv…`).
   - I analysen ingår 18 manuella stängningar (16 med path-data plus ADA/GOOGL 10-01). Faktiskt utfall: +24 USDT skalat till 1000 notional. Botens egna regler i replay hade gett +128.
   - Exempel: AGT 09-30 stängdes manuellt på +10,8; TP på +52 nåddes 2 h senare. ARB 09-30 stängdes på −0,4 före TP.
   - Det är efterklokhet per trade, men summan visar att manuella ingrepp har kostat mer än de räddat.
2. **Klustring.** Flera trades i samma batch ändrar inte förväntat värde, men batch-summans spridning ökar från 27 till 83 USDT. 10-01 var den sämsta LIVE-batchen hittills. Förklaringen är att de fyra togs samtidigt, inte att de var mer korrelerade än normalt: 62 % samma tecken, vilket är exakt vad oberoende ger vid 26 % vinstandel.
3. **PP är avgiftsnegativ.** Varje BE-exit kostar cirka −2 USDT (33 LIVE-exits, −63,7 USDT inklusive avgifter). BE_FEE (entry+0,2 %) testades och klarade inte kraven.
4. **Riskgeometri.** SL ligger i median 4,3 % bort, alltså ~43 USDT per trade vid 1000 notional, mot equity ~400. Snittvinsten (+29,9) är under en SL-förlust, och PP@+1 % kapar vinstsidan ytterligare. Det här förklarar varför en vinstandel på 26 % ger negativt resultat, men det är sizing- och stopbeslut som ligger utanför uppdraget: 100×10 är fast.

## J. Vad som implementerades

Ingen tradinglogik ändrades. Inget klarade de förregistrerade kraven.

**Committat (research-only, importeras aldrig av en trading-path):**
- `crypto_trading/entry_research/trade_review_data.py`: BingX path-cache (publika endpoints).
- `crypto_trading/entry_research/trade_review.py`: populationer, entry-features (bara stängda bars före entry), exit-simulator med diskretionära händelser.
- `crypto_trading/entry_research/trade_review_run.py`: hela analysen med förregistrerade krav.
- `crypto_trading/entry_research/godfather_manager_replay.py`: GODFATHER-manager-replayen.
- `tests/crypto_trading/entry_research/test_trade_review.py`: 13 integritetstester. De täcker att stop kontrolleras först, att PP gäller först från nästa bar, gap-fill, tidsgräns, ratchet (sänks aldrig), retrace på nästa open, diskretionär exit/stop (aldrig över open, sänks aldrig), att lås aldrig ligger över close, resampling och periodgränser.

Operativt: `crypto_trading_bot` och `crypto_trading_evidence_shadow` är Disabled i Task Scheduler.

## K. Förkastade hypoteser

| Hypotes | Varför |
|---|---|
| Trailing/profit-lock (A, B, C, D, E, F, G, H, J) | Tecknet växlar mellan trend- och mean-reversion-perioder; ingen q < 0,10 i SEL |
| Ta bort PP / senare PP | Bättre på botens trades, sämre OOS på signaler |
| Avgiftstäckande BE | Negativ i SEL |
| Närmare TP (K) | Negativ OOS |
| GODFATHER-manager | Tecknet växlar, CI innehåller 0, nästan alltid PROTECT |
| "Köp inte toppar"-filter (översträckt, z-extrem, VWAP-stretch, blow-off) | Flaggade trades är *bättre* på TRADES |
| RSI utan rörelse, utmattad volym, ingen 4h-momentum | Tecknet växlar mellan perioder och populationer |
| Svag marknadsbredd / BTC-fall | Tecknet växlar (efterhandstest) |
| Klustring som orsak | Ingen extra korrelation; bara varians |

## L. Fortfarande osäkert

- Om studs-efter-fall (prisrörelse ned, n=82, +1,3 % på 4 h) är verklig. Den kräver ett eget förregistrerat test på ny data.
- Om en senare PP verkligen hjälper botens AI-valda trades (4/4 perioder på TRADES, n=256) eller bara speglar urvalsperioden. Det avgörs av mer TRADES-data, inte av fler varianter. Kör `trade_review_run` igen om 2–3 veckor och ändra ingenting innan dess.
- Simulatorn är optimistisk mot verkliga LIVE-exits (−326 mot −452); verklig slippage är större än antaget. Det gör slutsatsen "ingen förbättring" säkrare, inte svagare.
- Bortfall: 3 av 259 positioner saknade path-data och föll bort. Av signalerna föll några utan path-data eller med för kort historik bort.

## M. Krav innan boten kan startas igen

1. **Kapital:** futures-kontot har 0 USDT. Utan överföring tillbaka kan inga LIVE-trades tas (kapacitets-gaten kräver 101 USDT fri margin).
2. **Beslut av användaren:**
   - Data visar negativ expectancy efter kostnader i alla mått: TRADES −0,35, LIVE −1,84 och SIGNALS −1,90 USDT/trade.
   - Verkligt LIVE-resultat vid 100×10 är −210,7 USDT på 62 trades.
   - Ingen testad förändring vänder det.
   - Min rekommendation är att **inte** starta LIVE igen förrän en ny informationskälla (del 7) har visat edge. PAPER kan köras för att samla mer TRADES-data.
3. **Om start ändå önskas:**
   1. `Enable-ScheduledTask -TaskName crypto_trading_bot`, därefter start.
   2. Vid första ticken stänger `reconcile_active_executions` de 4 ACTIVE-raderna mot exchange.
   3. `verify_closed_live_exits` klassar dem från orderhistoriken.
   4. Evidence shadow startas separat på samma sätt, om den ska köras.
4. **Låt bli manuella stängningar,** eller logga dem som ett eget beslut. Utfallet av dem har hittills varit negativt mot botens regler.

## Reproducera

```
python -m crypto_trading.entry_research.trade_review_data      # path cache (~45 min)
python -m crypto_trading.entry_research.trade_review_run       # ~25 s -> data/entry_research/trade_review_results.json
python -m crypto_trading.entry_research.godfather_manager_replay   # ~2,9 USD
```
