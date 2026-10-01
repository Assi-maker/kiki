# Kritisk granskning av crypto_trading – svagheter, onödiga delar, åtgärder (2026-10-01)

Granskningen bygger på data, inte på åsikter. Underlaget:
- botens DB (3 656 kandidater, 259 positioner, 123 LIVE-exekveringar);
- exchangens orderhistorik;
- replayen från samma dag (`2026-10-01-trading-system-review.md`) på riktiga BingX 1m-banor;
- kodbasen (52 500 rader produktionskod, 57 400 rader tester, 22 agenter).

Belopp anges i USDT per 1000 notional om inget annat sägs.

## Sammanfattning

Systemet är byggt som ett säkerhetsmaskineri runt en strategi som inte fungerar. Säkerhetsdelen är gedigen: add-then-remove SL, exchange som source of truth, restart-recovery och Safety Kernel. Felet sitter inte där.

1. **Köpbeslutet är i praktiken inget beslut.** Gaten bekräftar allt där sju AI-agenter svarat, schemat är giltigt och R:R ≥ 1. CONFIRMED-traderna är den *sämsta* gruppen av alla kandidater.
2. **Signalen har ingen riktningsinformation**, men boten köper alltid (LONG-only).
3. **Avgifter och slippage äter hela bruttot.** Botens trades är +325 USDT brutto och −90 netto.
4. **Riskgeometrin är fel dimensionerad** för ett konto på ~400 USDT och för hur stora rörelser signalen faktiskt förutser.
5. **Mycket kod, AI och shadow-lager påverkar inte ett enda beslut** men kostar pengar, latens och felyta.

Åtgärderna står i prioritetsordning sist.

---

## 1. Strategi och beslut (kritiskt)

### 1.1 Gaten har ingen beslutsfunktion

| Gate-utfall | Antal | Orsak |
|---|---|---|
| NO_TRADE | 3 153 | 3 106 `missing_or_failed_assessment` (budget, timeout, agentfel), 31 fulla platser, 16 innehållskontroll |
| REJECTED | 265 | `qa_gate_rejected`: QA-agenten kontrollerar *schema och konsistens*, inte innehåll |
| CONFIRMED | 280 | `all_checks_passed`: alla agenter svarade, SL < pris < TP, R:R ≥ 1, signalålder ≤ 30 min |

- Forecastens riktning är bara shadow (`shadow_min_bullish_probability`). **222 av 280 CONFIRMED (79 %) hade bearish ≥ bullish.**
- Bull- och Bear-agenternas argument vägs aldrig av något.
- Vilka kandidater som blir trades avgörs alltså av: (a) vilka som hann analyseras före budgettaket och (b) om sju LLM-svar gick att tolka.

**Utfall per status,** oberoende signaler, samma exitregler (BASE), USDT/trade:

| Status | n | TRAIN | VALID | TEST | HOLDOUT | Alla |
|---|---|---|---|---|---|---|
| CONFIRMED | 152 | −2,91 | −1,72 | −3,40 | −1,31 | **−2,43** |
| BUDGET_LIMITED (aldrig analyserad) | 701 | +3,80 | +0,89 | −2,80 | −9,25 | −2,20 |
| REJECTED (QA) | 152 | +2,43 | −2,02 | −1,16 | −10,66 | −1,28 |
| NO_TRADE | 128 | −0,98 | +10,7 (n=4) | +18,8 (n=4) | +1,59 | **+0,99** |

CONFIRMED är negativ i alla fyra perioder och sämre än både oanalyserade och avvisade kandidater. Urvalet tillför ingenting; om något är det svagt negativt.

**Kostnad:** 5 037 loggade AI-anrop i analyskedjan (75,9 USD). Totalt 81 USD loggat sedan 09-01, inklusive Guardian, Detective och GODFATHER. Det motsvarar ~0,70 USD per LIVE-trade, plus olagd kostnad för timeouts (09-29 uppskattades ~4,4 USD i en natt). Till det kommer i median **17 minuters latens** från signal till fill (kvartil 3: 28 min).

**Dyrast är:**
- QA-agenten, **19,03 USD**, som gör en kontroll kod kan göra gratis;
- News, **15,08 USD**, som för de flesta symboler bara konstaterar att inget symbolspecifikt finns.

### 1.2 Signalen förutser inte riktning, men boten köper alltid

- Detektorn triggar på anomalier åt båda hållen: |Δpris 30m| ≥ 2 %, RSI ≥ 70, volym-z ≥ 2,5 eller funding ≥ 0,05 %. Exekveringen är LONG-only.
- Framåtavkastning efter signal är +0,04 % (30 min), −0,04 % (1 h) och +0,12 % (4 h), med 50 % uppgångar. Det är ett myntkast.
- Per trigger:

| Trigger | n | USDT/trade |
|---|---|---|
| RSI ≥ 70 ("momentum_breakout", vanligast) | 611 | **−2,99** (sämst) |
| Prisrörelse | 208 | −1,88 |
| Funding | 92 | −1,02 |
| Volym | 331 | −0,85 |

- Fler samtidiga triggers är *sämre*, inte bättre: en trigger −1,78, två −2,07, tre −9,35. Kandidat-scoringen premierar fler triggers.
- Det är samma slutsats som fem tidigare studier (09-28 → 10-01): informationen i pris, volym, OI och funding räcker inte. Varken parametrar eller AI-tolkning kan skapa edge ur en källa som saknar den.

### 1.3 Tidshorisonterna passar inte ihop

- **Signal:** en 30m-anomali. Typisk 15m-volatilitet (ATR15) är ~0,5 %.
- **Entry:** 17 min efter signalen, i median. Mycket av rörelsen är redan förbrukad.
- **SL/TP:** sätts av en LLM (Risk Agent) utan volatilitetsmodell. Median −4,3 % / +5,0 %, alltså ~8–10 × ATR15.
- **Hold:** max 6 h. Bara 36 av 256 trades (14 %) når TP; 88 går på tidsgräns och 104 på break-even.
- **Bredast SL är sämst:** kvartilen med 5,1–10,2 % SL ger −3,89 USDT/trade med lägst R:R (1,06).

Signalen beskriver en rörelse på minuter till en timme, men traden kräver en rörelse på 5 % inom 6 h.

### 1.4 Kostnaderna är den enda helt mekaniska förlusten

- Botens 256 trades: brutto **+324,7 USDT**, netto **−89,9 USDT**. Kostnaderna är ~1,62 USDT/trade (taker 0,05 % per sida plus slippage).
- **104 break-even-exits** kostar ~2 USDT vardera, ≈ −208 USDT. PP flyttar SL till exakt entry, så varje BE-exit förlorar avgifter och slippage.
- Verklig LIVE-slippage är större än modellen: faktiskt −452 mot simulerat −326 på 89 exits.
- Allt är taker, både entry och exit. Maker-entry (BingX standard 0,02 % mot 0,05 %) är inte testat.

Det brutto-positiva resultatet kan vara brus (n=256). Men kostnaden är säker, och den kan sänkas utan att ändra signalen.

## 2. Risk och kapital (kritiskt)

- **Storlek:** 100 × 10 = 1000 notional per trade, mot equity ~400 USDT, och 4 samtidiga platser.
- **Stoppavstånd:** median-SL på 4,3 % ger ~43 USDT, alltså ~11 % av equity per trade.
- **Samtidig risk:** Safety Kernel loggade 50 % av equity i worst-case-risk 10-01 (log-only).
- **Asymmetri:** snittvinst +29,9 mot en full SL-förlust på ~−43. PP kapar vinstsidan ytterligare.
- **Följd:** vid 26 % vinstandel krävs ett R:R som strategin inte levererar. En dålig batch (10-01: −130) tar ~30 % av kontot.

Fast sizing är ditt beslut och respekteras; inga %-tak föreslås. Konsekvensen måste däremot vara känd: **med nuvarande geometri räcker 3–4 dåliga batcher för att tömma ett konto på 400 USDT**, även om strategin hade varit neutral före avgifter. Om storleken ska vara fast måste SL-avståndet bestämmas av volatilitet och signalens horisont, inte av en LLM.

## 3. Drift och process (hög)

| Problem | Fakta |
|---|---|
| Upptid | live-loopen hade 34 avbrott > 10 min sedan 09-06, totalt ~469 h (inkl. avsiktliga stopp). Två oförklarade dödningar med 0x40010004 (09-30, 10-01): boten lever i en användarsession. |
| Oskötta positioner vid avbrott | SL på exchange skyddar, men tidsgräns, PP och avstämning stannar. 10-01 stod 4 rader kvar som ACTIVE. |
| Manuella ingrepp | 18 manuella market-stängningar. Utfall +24 mot +128 enligt botens regler. De loggas som `EXTERNAL_CLOSE` utan orsak och förorenar statistiken. |
| Testsviten | 8 tester har fallerat i flera dagar (gamla default-värden). Gröna tester betyder då mindre, och nya fel kan döljas. |
| Simulator och paper | `opened_at` = cykelstart (känt sedan 09-29). Paper-fyllning ≠ LIVE-fyllning. Simulatorn är optimistisk mot LIVE. |
| Research-loopen | fem studier i rad med "0 edge" på samma informationskälla. Varje ny runda kostar tid men ändrar inte källan. |

## 4. Onödiga delar (komplexitet utan effekt)

| Del | Storlek | Påverkan på beslut | Kritik |
|---|---|---|---|
| GODFATHER | 11 900 rader | **0** LIVE-beslut (`guardian_authority_decisions` tom). Lärtabeller tomma eller NOISE. | Störst modul, ingen effekt. Replay 10-01: säger PROTECT nästan alltid. |
| Guardian (decay/AI) | 5 400 rader, 343 AI-anrop | HOLD/WATCH; tilldelningen ändrar ingenting i LIVE | Decay-faktorerna ser inte P/L eller marknadsfall. 10-01 låg decay på 0,01–0,31 medan positionerna föll till SL. |
| QA-agent (LLM) | 709 anrop, 19 USD | Schemakontroll | Kan ersättas av deterministisk validering: gratis, snabbare, utan falska avslag. |
| News-agent | 728 anrop, 15 USD | Ingen mätbar | Sällan symbolspecifik information. |
| Forecast-agent | 720 anrop, 4 USD | Ingen (shadow) | "v1-heuristic-low-confidence"; Brier sämre än klimatologi (09-28). |
| Bull/Bear | 1 436 anrop, 19 USD | Ingen | Argumenten vägs aldrig. |
| Shadow-lager | evidence_shadow, entry_quality_shadow, pre_ai_feasibility_shadow, PP-experiment, guardian_authority_shadow, godfather_shadow | Inga | Var och en har egna tabeller, trådar och felytor. Ingen har gett ett godkänt fynd. |
| Detective / strategist | 21 anrop, 3,4 USD | Inga godkända heuristiker | — |

Totalt bidrar ~20 000 rader och 22 agentdefinitioner inte till något beslut som når exchange. Varje tråd i samma process är ytterligare en väg till fel (frysningar 09-27, AI-timeoutloop 09-29).

## 5. Det som fungerar (behåll)

- LIVE-exekveringen: deterministiska client-ids, add-then-remove SL, restart-recovery, exit-verifiering mot exchangens orderhistorik.
- Safety Kernel (likvidationsavstånd, stoppar aldrig bakåt), max 4 / 1 per symbol, SL på exchange.
- Watchdog och heartbeat. Utan dem hade dödningarna inte syntes.
- Research-verktygen med förregistrerade krav. De har hindrat flera överanpassade "förbättringar".

---

## Åtgärdsplan i prioritetsordning

### P0: innan LIVE startas igen
1. **Erkänn att entry-strategin saknar edge** och kör den inte med riktiga pengar. Fem oberoende studier och dagens granskning ger samma svar. PAPER kan fortsätta för att samla data.

### P1: billigt, mekaniskt, utan edge-antaganden
2. **Ersätt QA-agenten med kod.** Samma schema- och konsistensregler, deterministiskt. Sparar ~19 USD/månad och latens, och tar bort falska avslag.
3. **Stäng av News, Forecast och Bull/Bear i den kritiska vägen** tills någon av dem visat mätbart informationsvärde. Latensen från signal till fill sjunker från ~17 min.
4. **Gör beslutet explicit:** en enda beslutsfunktion med en loggad sannolikhet, mätt på kalibrering (Brier mot klimatologi) innan den får avgöra något. Idag finns ingen sådan funktion.
5. **Kostnader:**
   - Testa maker-entry (post-only, limit vid bid) på PAPER och mät missade fills och adverse selection.
   - Mät verklig slippage per exittyp.
6. **Drift:**
   - Kör boten som Windows-tjänst (t.ex. NSSM eller Task Scheduler "kör oavsett inloggning") så att den inte dör med sessionen.
   - Larma när en LIVE-position saknar avstämning i mer än 5 min.
7. **Manuella stängningar:** gör dem inte, eller logga dem med orsak (ett kommando som skriver `MANUAL_CLOSE` och anledning). Då förorenar de inte statistiken.
8. **Rätta eller ta bort de 8 inaktuella testerna** så att en grön svit betyder något igen.

### P2: förenkla
9. **Frys GODFATHER, evidence shadow och alla shadow-lager.** Stoppa trådarna, behåll koden i git. De återaktiveras bara mot en konkret hypotes med förregistrerade krav. Det ger mindre felyta, färre AI-kostnader och enklare drift.
10. **Guardian:** antingen ta bort AI-delen eller låt decay inkludera P/L och marknadsläge. Mät det i så fall som en exitmodell med samma krav som i dagens granskning.

### P3: strategi, det enda som kan ge lönsamhet
11. **Ny informationskälla** som inte härleds ur samma pris/volym-historik: likvidationer, taker-flöde i realtid, orderboksdjup, spot-perp-ledning, tidsstämplade katalysatorer. Testa var och en mot förregistrerade krav innan någon kod byggs in i boten.
12. **Matcha horisonten:** om signalen gäller 30 min ska SL/TP och hold sättas efter volatilitet för den horisonten (t.ex. ATR15-multiplar), inte av en LLM på 4–5 %.
13. **Riktning:** en riktningsneutral detektor bör antingen få en riktningsmodell eller tillåta SHORT. Studs-efter-fall (n=82, +1,3 % på 4 h) är den enda öppna hypotesen; den kräver ett eget OOS-test.

## Vad som inte rekommenderas

- Fler varianter av exit, trailing eller PP på samma entries. Dagens 30 modeller, och exit-labbet på 2,4 M entries, visar regimberoende och ingen edge.
- Filter mot "sena/översträckta" signaler. På botens trades är de bättre, inte sämre.
- Att återinföra 5 %/10 %-taken. Det är ditt beslut, och de löser inte edge-problemet; de minskar bara storleken på förlusterna.
