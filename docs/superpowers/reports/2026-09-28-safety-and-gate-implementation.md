# P0–P9: Safety Kernel, innehållskrav i Gaten, ekonomisk modell och shadow-utvärdering

**Datum:** 2026-09-28
**Commits:**

| Del | Commit | Innehåll |
|---|---|---|
| P0 | `4b3d729` | Safety Kernel |
| P1 | `1d30e8f` | Innehållskrav i Gaten |
| P2 | `5c1f44d` | Kostnadsmodell |
| P3–P6 | `fa22597` | Shadow-utvärdering |
| P9 | denna rapport | Samt flytten av `oos_start` |

**Driftsättning:**
- P0 har varit LIVE sedan 2026-09-28 12:24 UTC.
- P1, P2 och shadow-tråden har varit LIVE sedan boten startades om **2026-09-28 20:58:32 UTC**.
- Boten var nere från 13:28 till 20:58 UTC eftersom datorn var avstängd.

**Märkning i rapporten:** påståenden är märkta **[FAKTA]**, **[BERÄKNING]**, **[STATISTIK]** eller **[BEDÖMNING]**.

---

## A. Safety Kernel (P0)

Kärnan ligger i `crypto_trading/safety_kernel.py` och konfigureras i `config/safety_kernel.yaml`. Den är ren kod utan AI.

**Var den sitter:** den ligger på den *enda* vägen till en LIVE-entry (`process_pending_positions`) och före varje stop-flytt, både från PP och från Guardian Authority.

### Regler för en entry

1. **Värsta förlust per trade:** högst min(1 % av *aktuellt* equity, 10 USDT). Beräkningen räknar med:
   - stopavståndet,
   - 0,3 % slippage på stoppen,
   - 0,1 % avgift tur och retur,
   - en entry på senaste pris + 0,3 %.
2. **Tak för öppen risk:**
   - portfölj 3 %,
   - korrelerad grupp 2,5 % (grupperna är crypto_major, crypto_alt, tokenized_equity och commodity_fx),
   - notional högst 3× equity.
3. **Möjliga beslut:** bara APPROVE, REDUCE eller REJECT.
   - Kärnan förstorar aldrig en position.
   - Den ändrar aldrig hävstång, SL eller TP.
   - Saknas equity eller pris blir det ingen entry (fail-closed).
4. **Likvidationsvakt:** stoppen måste ligga minst 2 % av entry ovanför det konservativt uppskattade likvidationspriset (mmr 1 %). Annars blir beslutet REJECT.

### Stop-flytt (`check_stop_move`)
- En LONG-stop får bara flyttas uppåt, och aldrig in i likvidationsbufferten.
- **Hittat och stängt:** PP:s break-even-flytt kunde tidigare *sänka* en stop som redan hade dragits upp över entry.

### Resultat
- **[BERÄKNING] Replay** på de 37 verifierade LIVE-trades från 26–28/9:

  | Mått | Före | Efter P0 |
  |---|---|---|
  | P/L | −164,91 USDT | −13,84 USDT |
  | Max drawdown | −39,6 % | −3,2 % |
  | Högsta samtidiga risk | 45 % | 3,1 % |

  Likvidationsvakten hade stoppat 6 av tradesen.
- **[FAKTA] Verklig drift 12:24–13:28:** 6 beslut, varav 1 REJECT (`LIQUIDATION_TOO_CLOSE`, GRASS) och 5 REDUCE.
  - 3 av REDUCE-besluten begränsades av `PER_TRADE_RISK` till ≈4,2 USDT risk.
  - 2 begränsades av `GROUP_RISK_CAP`.
  - Den största verkliga förlusten efter P0 är PUMP-USDT med −3,75 USDT, alltså inom taket på 4,2.
- **[BEDÖMNING]** P0 begränsar skadan. Den skapar ingen edge. Förväntat värde är fortfarande negativt.

## B. Innehållskrav i Gaten (P1)

Tidigare räckte det för CONFIRMED att alla 7 roller svarade ok, att QA godkände formen och att det fanns kapacitet. Nu **krävs dessutom** följande, konfigurerat i `config/gate.yaml`:

| Kontroll | Avvisningskod |
|---|---|
| Referenspris finns och stop < referens < target för en LONG | `CONFLICT_RISK_LEVELS_CONTRADICT_LONG` |
| QA får inte godkänna samtidigt som den listar överträdelser | `CONFLICT_QA_PASSED_WITH_VIOLATIONS` |
| R:R ≥ 1 | `RR_BELOW_MINIMUM` |
| Signalen är högst 30 min gammal vid Gaten. 30 min är den primära tidsramen och den är låst med ett test. | `SIGNAL_STALE` |

**Forecast-villkoren** loggas men **tillämpas aldrig**. Det var ditt beslut 2026-09-28: forecasten har ingen prediktiv kraft (korrelation 0,01–0,04).

**Loggning och låsning:**
- Varje utvärdering sparas i `gate_evaluations`.
- Ett strukturellt test låser att bara Gaten kan producera CONFIRMED.

**Resultat:**
- **[BERÄKNING] Replay på 403 kandidater:** P1 blockerar 24 %.
  - Out-of-sample (från 20/9): 7 kandidater med R:R < 1 blockeras, med medel −0,46 R.
  - På LIVE hade 8 trades blockerats, sammanlagt −9,63 USDT.
- **[STATISTIK]** Konfidensintervallen överlappar. Det här är en regel för datakvalitet och konsistens, ingen bevisad edge.

## C. Ekonomisk modell (P2)

**Problemet:** papperskontot drog 0,04 % i avgift. En verklig tur-och-retur hos BingX kostar 0,05 % taker per sida, alltså **0,10 %**. Kostnaderna var alltså underskattade 2,5× i:
- pappers-P/L,
- GODFATHER-kontrafaktiskt,
- kostnadskontroller för entry-kvalitet,
- lärande.

**Ändringar:**
- `config/cost_model.yaml` (v2): 0,05 % in, 0,05 % ut och 0,15 % uppmätt slippage på stoppen.
  - Loadern vägrar starta om `risk_limits.fee_pct` avviker från modellens tur-och-retur.
- `risk_limits.yaml fee_pct` ändrades från 0,0004 till 0,0010. Hashen registrerades om med motivering. Inget risk-, storleks- eller exponeringsvärde ändrades.
- **[BERÄKNING]** Historiska v1-paper-closes skrivs aldrig om. De räknas om vid läsning och märks `PAPER_MODEL_V1_RECOSTED`.
  - 109 av 150 closes: P/L går från −510,03 till −559,84 USDT.
  - Antalet vinster är oförändrat, 55.
- LIVE-avstämningen sparar nu faktisk entry-avgift, faktisk exit-avgift och stoppens fill-vs-trigger-slippage separat.
  - UNVERIFIABLE-utfall hålls fortsatt utanför lärandet.

## D. Shadow-utvärdering (P3–P6)

Koden ligger i `crypto_trading/shadow/` (`evaluation.py`, `history.py`, `report.py`). Den körs som en tråd i `run.py` var 30:e minut.

**Isolering:** modulen har ingen handelskonnektor och skriver bara till `shadow_evaluations`. Den rör aldrig en kandidat, position eller order.

**Vad som sparas per kandidat, när dess 6-timmarsfönster har passerat:**
- **Features vid beslutet.** De beräknas enbart från barer som var stängda före Gatens beslut:
  - BTC-regim,
  - symbolens momentum och volatilitet,
  - volymbekräftelse,
  - forecast-flaggor,
  - re-entry i samma symbol,
  - GODFATHER:s entry-kvalitetsdom.
- **Vetoregler (P3):** om varje shadow-regel hade blockerat eller inte.
- **Utfall:** kandidatens egen SL/TP-bracket på 1m-barer med den verkliga kostnadsmodellen.
- **Break-even (P4):** trigger 0,75, 1, 1,5 eller 2 %, med offset +0,25 %. +0,5 % är avsiktligt utelämnat.
- **Trailing (P5):** fyra varianter.
- **GODFATHER entry-kvalitet (P6):** endast observation.
- **LIVE-resultat:** det verkliga utfallet, när kandidaten faktiskt handlades.

**Statistisk regel i `report.py`:** för att något ska kallas EDGE måste allt detta gälla:
- n ≥ 30,
- Benjamini–Hochberg q < 0,05 över **alla** hypoteser,
- samma tecken i TEST.

Kontinuerliga features delas i tertiler på TRAIN, och gränserna tillämpas oförändrade på TEST.

**OOS-kohort:** `oos_start` är satt till **2026-09-28T21:30Z**, efter den faktiska driftsättningen 20:58Z.
- Värdet flyttades från 16:00Z. Det var planerat till en driftsättning som aldrig blev av eftersom datorn var avstängd.
- Det flyttades *senare*, aldrig tidigare. Allt före den tidpunkten är IN_SAMPLE_HISTORICAL.

## E. Historiskt resultat och OOS

Replayen gjordes på en kopia av produktionsdatabasen, med publika BingX-klines.

**[FAKTA] Underlaget:**
- 473 Gate-utvärderingar återskapades (backfill).
  - 14 kandidater hoppades över eftersom deras sparade data är korrupt. De loggas som `shadow_backfill_candidate_skipped`.
- 471 kandidater utvärderades.
- **235 har ett utfall.** De övriga 236 saknar en körbar bracket:
  - 207 REJECTED,
  - 13 NO_TRADE,
  - 16 CONFIRMED utan en användbar stop/target.
- 2 kandidater kan inte utvärderas: symbolen NCCOGASOLINE2USD-USDT är pausad hos BingX.

**[STATISTIK] Resultat, n = 235 (TRAIN 117 / TEST 118, OOS-kohort 0):**
- Baseline: medel **+0,018 R**, KI (−0,084; 0,12). TEST: −0,020 R.
- **39 hypoteser. 0 EDGE.** Alla är NOISE eller INSUFFICIENT_DATA. Minsta q är 0,52.

| Område | Mest utmärkande (endast beskrivande) | Klass |
|---|---|---|
| P3 veto | BULLISH_NOT_DOMINANT: de 12 som *inte* blockerades hade −0,44 R | NOISE (q 0,52) |
| P3 veto | ALT_LONG_WHILE_BTC_FALLING: effekt −0,08 R, men +0,11 i TEST (tecknet vänder) | NOISE |
| P3 re-entry | SAME_SYMBOL_REENTRY_6H: effekt +0,01 R | NOISE |
| P3 trigger | price_volatility: −0,23 R (n=30) | NOISE (q 0,58) |
| P3 feature | candidate_score, nedersta tredjedelen: −0,16 R, TEST −0,50 | NOISE (q 0,52) |
| P4 break-even | Alla fyra triggers ger *negativt* delta R (−0,02 till −0,06), både i TRAIN och TEST | NOISE |
| P5 trailing | Alla fyra ger delta −0,01 till −0,03 R, tecknet vänder mellan TRAIN och TEST | NOISE |
| P6 GF EQ | TRADE mot icke-TRADE: +0,02 R | NOISE |

**[BEDÖMNING]**
- Inget av detta motiverar en regeländring.
- P4 ligger i linje med tidigare fynd (TIGHTEN_SL, PP-BE): break-even ger ingen hjälp, och om något lutar det mot skada. Det bör inte aktiveras.
- Den enda riktiga prövningen blir OOS-kohorten från 21:30Z. Den kan klassas först när den har minst 10 poster. För EDGE krävs dessutom n ≥ 30 per grupp.
- Rapporten skapas med `python -m crypto_trading.shadow.report`.

## F. LIVE-status (2026-09-28 ~21:05 UTC)

**[FAKTA] Omstart 20:58:**
- Avstämningen mot börsen stängde och verifierade de tre positioner som var öppna under driftstoppet:

  | Position | Utfall | P/L |
  |---|---|---|
  | FLOCK-USDT | TP | +1,98 USDT |
  | PUMP-USDT | SL | −3,75 USDT |
  | HBAR-USDT | Tidsgräns | +0,05 USDT |

- Alla tre är VERIFIED via exchange-order eller market close.
- **0 öppna LIVE-positioner** efter omstarten.
- Inga ERROR-rader efter omstarten.

**[FAKTA] Sedan 26/9:**
- 42 stängda LIVE-trades.
- Summan av `exchange_realized_pnl_usdt` är −134,67 USDT.
- Den siffran går inte att jämföra direkt med den ledger-avstämda siffran −164,91 från Fas 2-forensiken. Fälten har olika täckning av avgifter och funding.

**[FAKTA] Tester:**
- 2316 passerar.
- 8 fallerar. Det är **samma 8** som i baseline före P0: config-tester som förutsätter att flaggor är avstängda, men de är medvetet påslagna (`authority_shadow_enabled`, `profit_protection`).

## G. Kvarvarande sårbarheter

1. **Entry-edge saknas.** Varken P0–P6 eller något annat har hittat en positiv förväntan. P0 gör förlusterna små. Det gör dem inte till vinster.
2. **Boten dör när datorn stängs av.** Positionerna låg utan tillsyn i 7,5 h. Börsens SL/TP skyddade dem, men tidsgränsen (6 h) och PP kunde inte agera. Det behövs en värd som alltid är på, eller att man inte stänger av datorn med öppna positioner.
3. **Pausad symbol.** Två kandidater på NCCOGASOLINE2USD-USDT prövas om i varje shadow-tick och fallerar. Det är ofarligt men ger brus i loggen.
4. **Korrupt kandidatdata.** 14 historiska kandidater har korrupt `assessment:forecast`. De hoppas över. Orsaken är inte utredd.
5. **De 8 config-testerna** är inaktuella och döljer riktiga regressioner i samma filer.
6. **Forecast-agenten** kostar AI-anrop men saknar prediktiv kraft. Den används bara i shadow.
7. **Likvidationsestimatet** är konservativt (mmr 1 %). Det använder inte BingX:s faktiska trappsteg.

## H. Rollback

Varje del kan backas oberoende av de andra med `git revert`, följt av en omstart av boten via `start_bot.bat`.

| Del | Rollback |
|---|---|
| P0 | `git revert 4b3d729`, eller höj taken i `config/safety_kernel.yaml`. Det rekommenderas inte. |
| P1 | `git revert 1d30e8f`, eller stäng av enskilda krav i `config/gate.yaml`. |
| P2 | `git revert 5c1f44d`. Kräver då också `fee_pct` 0,0004 och en ny hash i `risk_limits.yaml`. Loadern vägrar starta vid avvikelse. |
| P3–P6 | `git revert fa22597` och denna commit. Shadow påverkar inte handeln. Tabellen `shadow_evaluations` kan ligga kvar. |

Tabellerna `safety_kernel_decisions`, `gate_evaluations` och `shadow_evaluations` är tillägg. Ingen befintlig data har skrivits om.
