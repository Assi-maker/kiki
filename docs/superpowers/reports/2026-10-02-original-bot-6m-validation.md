# Ursprungliga AI-boten: 6-månadersvalidering med PP1 AV (2026-10-02)

**Fråga:** har den ursprungliga boten med PP1 avstängt en verklig positiv edge efter realistiska kostnader, eller var de senaste +393 USDT slump eller en regim?

**Läge:** LIVE är stoppad och `CRYPTO_TRADING_SAFE_MODE=1` ligger kvar. Inga order skickas. Ingen botkod, prompt, Gate, Guardian, GODFATHER, sizing eller riskgräns ändras. Detta är enbart historisk simulering och uppspelning.

---

## FÖRREGISTRERING
Skriven och committad tillsammans med `entry_research/original_bot_6m.py` och dess tester innan första körningen.

### Testad version och konfiguration
- **Kodbas:** commit `db50684` (HEAD när testet förregistrerades). Botens beslutskod har inte ändrats sedan P1 (`bf47ebd`).
- **Screener** (`config/pipeline.yaml`):
  - 30m-staplar, inklusive den pågående stapeln som BingX returnerar den.
  - Prisändring > 2,0 %, RSI(14) > 70, volym-z (20 staplar) > 2,5, |senaste funding| > 0,05 %.
- **Discovery:** var 15:e minut på de 80 största coinsen efter 24 h-volym (minst 5 M USDT). Cooldown 60 minuter, om inte poängen ändrats ≥ 0,15.
- **Budget** (`budget_limits.yaml`): högst 10 kandidater per körning. Sedan `0840ff8` analyseras bara så många som det finns lediga LIVE-platser.
- **Gate** (`gate.yaml`): SL < pris < TP, R:R ≥ 1, signalålder ≤ 30 minuter, kapacitet.
- **Safety Kernel:** likvidationsskydd (SL < cirka 9 % vid 10x). Procentgränserna loggas bara (`risk_caps_enforced: false`).
- **LIVE-regler:** 1 000 USDT notional (100 × 10), högst 4 positioner, högst 1 per coin, SL/TP på börsen, 6 timmars tidsgräns.
- **PP1:** AV är huvudfallet. PÅ körs bara som jämförelse (+1 % flyttar stoppet till inköpspriset).
- **Guardian:** utan heuristiker i Guardian Authority kan Guardian i den här konfigurationen inte stänga eller strama åt något. Guardian ger därför inga utgångar.

### Vad som INTE kan rekonstrueras exakt (markeras, ersätts inte optimistiskt)
AI-stegen kan inte köras historiskt utan läckage eller påhittad indata:
- **Stegen som berörs:** AI opportunity screening, analysen med 7 roller eller bara Risk-agenten efter P1, och QA.
- **Varför:**
  1. Nyhetsflödet (RSS) vid varje historisk tidpunkt finns inte sparat.
  2. Modellernas träningsdata sträcker sig till juni 2026, vilket ger läckage för april–juni.
  3. AI-svar går inte att reproducera exakt.
  4. Det skulle kräva tusentals betalda AI-anrop.

**Därför delas testet i två delar.**

**DEL R (rekonstruktion, 2026-04-01 till 2026-09-29, Binance 5m, 133 coins):**
- Allt deterministiskt följer botens regler, som ovan.
- **AI-ersättning (neutral, inte optimistisk):**
  - Varje analyserad kandidat behandlas som CONFIRMED.
  - Stopp och mål dras med fast seed ur fördelningen av den **verkliga** botens Risk-agent-SL/TP (alla 259 positioner).
  - Gate- och Kernel-reglerna tillämpas på dem.
- **Inträde:** 15 minuter efter utvärderingen (analystid), på nästa 5m-öppning plus 0,05 % slippage.
- **Utgång:** SL före TP inom samma stapel, 0,10 % slippage vid stopp och 0,03 % vid tidsutgång. Avgift 0,05 % per sida. Long betalar verklig funding.

**DEL A (verkliga AI-beslut, 2026-09-01 till 10-01, botens databas):**
- Alla 3 656 kvant-kandidater spelas upp med samma utgångsmodell på BingX 1m.
- De grupperas efter vad AI-kedjan faktiskt gjorde: CONFIRMED, NO_TRADE, REJECTED (Gate) eller BUDGET_LIMITED (aldrig analyserad).
- Detta mäter AI-urvalets värde utan läckage.

### Perioder
TRAIN apr–maj, VALID jun, TEST jul–aug, HOLDOUT sep 2026.

Inga parametrar väljs eller ändras i någon period: boten är fryst. Indelningen används bara för att visa stabilitet. HOLDOUT används aldrig för något val.

### Mått och statistik
- Alla mått som efterfrågats: per affär, per dag, per vecka, per månad, drawdown, kostnader och kostnadsandel.
- **Osäkerhet:** block-bootstrap per dag (5 000 dragningar) för totalt, expectancy och vinstandel, alltså klustrat per dag och inte som oberoende affärer.
- **Nollhypoteser:** noll edge före kostnader (brutto) och efter kostnader (netto).
- **Robusthet:**
  - per månad, regim (BTC 7 d > +5 % = UPP, < −5 % = NED), coin, poängkvintil och trigger-typ;
  - långt och kort: boten handlar bara long, så kort finns inte;
  - borttagning av de 1, 3, 5 och 10 största vinnarna.
- **+393-frågan:** fördelningen av rullande 30-dagarsresultat och av slumpvis dragna 17 aktiva dagar i rekonstruktionen. Var hamnar +393?
- **Sanity checks:**
  - framtidsinformation (enhetstest av screenern);
  - fyllnader och candle-artefakter (5m mot BingX 1m på verkliga septemberaffärer);
  - survivorship (133 coins som fortfarande är listade);
  - kostnader (redovisas separat).

### Förhandskriterier för klassificering (gäller DEL R, PP1 AV)
- **A. Tydlig robust positiv edge:** ALLA följande måste gälla:
  - netto > 0;
  - nedre gränsen för 95 %-CI av totalen (dagsbootstrap) > 0;
  - netto > 0 i minst 3 av 4 perioder, inklusive HOLDOUT;
  - netto > 0 även utan de 5 största vinnarna;
  - profit factor ≥ 1,15.
- **B. Lovande men otillräckligt bevis:** netto > 0 men minst ett A-villkor uppfylls inte. Ett positivt resultat som bara finns i DEL A (en månad, verklig AI) räcker högst till B.
- **C. Ingen påvisad edge:** netto ≤ 0, eller CI omfattar 0 och punktskattningen saknar stöd i flera perioder, utan att vara signifikant negativ.
- **D. Negativ edge:** övre gränsen för 95 %-CI av totalen < 0.

**DEL A kan inte ensam ge A.** Om AI-valda affärer är tydligt bättre än AI-avvisade under september noteras det som ett möjligt AI-värde. Det kräver en egen framåtriktad validering.

---

## RESULTAT

**Körning:** förregistrering `9deb540`.
- Rådata: `2026-10-02-original-bot-6m-validation.json` och `…-checks.json` (kompletterande kontroller, `original_bot_6m_checks.py`).
- Tester: hela sviten 2 679 passed, 4 skipped. De överhoppade, som kräver numpy och pandas, kördes separat: 14 passed, varav 6 nya motortester (ingen framtidsinformation i screenern, pågående stapel, funding-trigger, SL före TP, PP från nästa stapel, borttagning av toppvinnare).
- `SAFE_MODE=1` ligger kvar. Inga order skickades.

### 1. Huvudresultat, DEL R (rekonstruktion 2026-04-01 till 09-29, 1 000 USDT per position)

| Mått | **PP1 AV** | PP1 PÅ |
|---|---|---|
| **Totalt netto** | **−3 856 USDT** | −12 402 USDT |
| Avkastning på konto om 1 000 USDT | −386 % (kontot hade tömts redan i april) | −1 240 % |
| Affärer | 4 737 | 6 849 |
| Vinnare / förlorare | 2 059 / 2 678 | 1 620 / 5 229 |
| Andel vinnande affärer | 43,5 % | 23,7 % |
| Snittvinst / snittförlust | +38,9 / −31,3 | +33,6 / −12,8 |
| Median per affär | −6,0 | −2,0 |
| Expectancy per affär | **−0,81** | −1,81 |
| Profit factor | 0,95 | 0,82 |
| Största vinst / förlust | +147 / −85 | +147 / −84 |
| Max drawdown | −4 810 USDT | −12 910 USDT |
| Längsta drawdown | 171 dagar | 169 dagar |
| Positiva dagar | 84 av 182 (46 %) | 62 (34 %) |
| Snitt positiv / negativ dag | +160 / −176 | +129 / −170 |
| Median dag | −26 | −60 |
| Bästa / sämsta dag | +878 / −783 | +463 / −558 |
| **Brutto före kostnader** | **+3 134** | +1 182 |
| Avgifter | 4 737 | 6 848 |
| Slippage | 4 592 | 8 427 |
| Funding (+ = betalt) | −2 340 (longarna *fick* funding) | −1 690 |
| Kostnad per affär (inkl. funding) | 1,48 | 1,98 |
| Kostnader som andel av brutto | **223 %** | 1 149 % |
| Utgångar TP / SL / BE / TID | 1 267 / 1 689 / 0 / 1 781 | 911 / 1 177 / **3 425** / 1 336 |

**Tratten** (PP1 AV):

| Steg | Antal |
|---|---|
| Utlösta screenerträffar | 223 779 |
| Kandidater efter cooldown | 89 042 |
| Utanför budget / lediga platser | 82 094 |
| Analyserade | 5 867 |
| Avvisade av Gate eller Kernel | 1 130 |
| Coin redan öppen | 1 081 |
| **Affärer** | **4 737** |

### 2. Per period och månad (PP1 AV)

| Period | Affärer | Netto | Vinstandel | PF |
|---|---|---|---|---|
| TRAIN (apr–maj) | 1 577 | −827 | 43,6 % | 0,97 |
| VALID (jun) | 811 | −550 | 42,8 % | 0,96 |
| TEST (jul–aug) | 1 534 | −2 119 | 43,2 % | 0,92 |
| HOLDOUT (sep) | 815 | −360 | 44,4 % | 0,98 |

| Månad | Apr | Maj | Jun | Jul | Aug | Sep |
|---|---|---|---|---|---|---|
| PP1 AV | −2 016 | **+1 041** | −463 | −382 | −1 703 | −333 |
| PP1 PÅ | −1 510 | −869 | −3 284 | −2 810 | −1 790 | −2 141 |

**Veckor med PP1 AV** (27 veckor): 9 positiva och 18 negativa. Bästa vecka +1 180 (4/5), sämsta −1 404 (10/8). Alla veckor finns i JSON-filen.

### 3. Jämförelse över samma 6 månader (per 1 000 USDT)

| Strategi | Netto | Positiva dagar | Största nedgång |
|---|---|---|---|
| **Originalbot, PP1 AV** | **−3 856** (1 000 per position, upp till 4 samtidigt) | 46 % | −4 810 USDT |
| Originalbot, PP1 PÅ | −12 402 | 34 % | −12 910 USDT |
| Trendstrategin | **+234** | 30 % | −9,9 % |
| Köp och håll BTC/ETH 1x | +234 | 51 % | −31 % |
| Kontanter | 0 | – | 0 |

**PP1 försämrar botens resultat kraftigt över 6 månader:** −12 402 mot −3 856. PP1 ger 3 425 break-even-utgångar som alla förlorar avgifterna, och vinstandelen halveras.

### 4. Robusthet (PP1 AV)
- **Utan de största vinnarna:**

| Borttagna | 0 | 1 | 3 | 5 | 10 |
|---|---|---|---|---|---|
| Totalt | −3 856 | −4 003 | −4 297 | −4 554 | −5 072 |

  Resultatet är negativt redan innan något tas bort.
- **Coins:** de bästa var LAB +1 020, BR +610, ALLO +522, USELESS +436 och BOME +346. De sämsta var BEAT −743, MYX −618, SKYAI −520, RIVER −466 och SOON −427. Utan de 5 bästa coinsen blir totalen −6 790.
- **Regim** (BTC 7 d vid inträdet):

| Regim | Affärer | Totalt | Per affär |
|---|---|---|---|
| UPP | 867 | −896 | −1,03 |
| SIDLEDES | 3 317 | −2 854 | −0,86 |
| NED | 553 | −106 | −0,19 |

  Ingen regim är positiv.
- **Signalstyrka** (poängkvintil 1→5): −3,8 / +1,6 / −1,7 / −1,9 / +1,7 per affär. Inget monotont samband.
- **Trigger:** RSI +0,34, volym −0,25, funding −0,26, pris −0,48 per affär. Alla ligger nära noll.
- **Long/short:** boten handlar bara long. Short finns inte i konfigurationen.

### 5. Statistik (dagsklustrad block-bootstrap, 5 000 dragningar, PP1 AV)

| | Punkt | 95 %-CI | P(≤ 0) |
|---|---|---|---|
| Totalt netto | −3 856 | **[−9 540; +1 892]** | 0,90 |
| Expectancy per affär | −0,81 | [−2,01; +0,40] | |
| Vinstandel | 43,5 % | [42,0 %; 45,0 %] | |
| **Brutto** (före kostnader) totalt | +3 134 | [−2 660; +8 909] | 0,14 |
| Brutto expectancy | +0,66 | [−0,56; +1,89] | |

- **Före kostnader:** förenligt med noll edge (CI omfattar 0).
- **Efter kostnader:** punktskattningen är negativ, men CI omfattar 0. Resultatet är alltså inte signifikant negativt, men det finns inget stöd för positiv edge.
- **PP1 PÅ:** CI [−17 307; −7 373]. Signifikant negativ.
- Statistisk signifikans hade inte i sig betytt användbar edge. Här finns ingen.

### 6. +393-frågan

| Underlag | Andel ≥ +393 | Fördelning |
|---|---|---|
| Rullande 30-dagarsfönster i rekonstruktionen (PP1 AV) | **16 %** | median −685, p95 +769, max +1 607 |
| Slumpvis dragna 17 dagar (lika många som boten var aktiv) | **20 %** | median −364, p95 +1 123 |

**Slutsats:** de +393 USDT ligger väl inom vad slump och regimvariation ger för ett system med negativ eller noll edge. Ungefär var femte period av den längden blir så bra av en slump.

### 7. DEL A: de verkliga AI-besluten (september, BingX 1m, PP1 AV)

**Samma geometri för alla** (SL 4,3 % / TP 5,0 %), dagsklustrade CI:

| Vad AI-kedjan gjorde | Antal | Snitt per affär | Vinstandel | 95 %-CI snitt |
|---|---|---|---|---|
| AI **godkände** (CONFIRMED) | 280 | **+0,30** | 45 % | [−5,2; +4,9] |
| AI **avvisade** (NO_TRADE) | 501 | **+8,97** | 64 % | [−1,5; +12,7] |
| Gate avvisade (REJECTED) | 263 | −1,87 | 43 % | [−6,3; +4,0] |
| Aldrig analyserad (BUDGET_LIMITED) | 2 605 | −2,78 | 43 % | [−9,5; +2,8] |

- **Med Risk-agentens egna SL/TP:** CONFIRMED gav +3,50 per affär (detta är källan till de +393). Gate-avvisade gav −1,06.
- **AI:s urval förbättrade inte resultatet.** De affärer AI avvisade gick *bättre* än de AI godkände (CI överlappar). Analyserade kandidater gick bättre än aldrig analyserade, men det kan bero på rankning eller tidpunkt (vilka kandidater som råkade få en ledig plats) och är inte AI-värde i sig.

### 8. Sanity checks

| Kontroll | Utfall |
|---|---|
| **Framtidsinformation** | Enhetstest: screenern ger identiskt utfall när framtida staplar ändras. Funding = senaste *betalda* räntan. Inträde 15 minuter efter beslutet. Ingen framtidsinformation. |
| **Candle-artefakter / upplösning** | Samma BingX-affärer som 1m och som sammanslagen 5m: **−0,32 USDT per affär**. 5m-motorn är nästan rättvisande (91 % samma utgångsorsak). |
| **Fyllnader, börs och tidpunkt** | Binance 5m-motorn mot BingX 1m på verkliga septemberaffärer: −2,44 per affär till. Det kommer från 0,05 % inträdes-slippage, något senare inträde och skillnaden mellan börserna. Det är modellantaganden som gör resultatet **osäkert i nivå**. Även om hela skillnaden (+2,76 per affär) läggs tillbaka hamnar expectancy på cirka +1,95, med CI som fortfarande omfattar 0. Klassificeringen ändras inte (som bäst B, aldrig A). |
| **Survivorship** | 133 coins som fortfarande är listade. Avlistade coins saknas, vilket ger ett något för positivt resultat för en bot som bara köper. Utfallet är ändå negativt. |
| **Kostnader** | Avgifter, slippage och funding redovisas separat (se tabell 1). |
| **AI-ersättning** | Inte optimistisk. Del A visar att AI-godkända inte var bättre än AI-avvisade, så "godkänn allt" ger ingen överskattning. **Osäker men neutral.** |

### 9. Begränsningar
- **AI-kedjan** (opportunity screening, 7 roller eller Risk-agenten, QA) kunde inte köras historiskt utan läckage. Den ersattes neutralt och utvärderades separat på verklig data (en månad).
- **Volym** mäts som quote-volym i stället för BingX basvolym. Spread-filtret (≤ 0,2 %) kunde inte tillämpas historiskt.
- **Discovery-tiderna** (:05/:20/:35/:50) och analystiden (15 min) är antaganden.
- **Fast kapital:** 1 000 USDT notional per affär. I verkligheten hade ett konto på 1 000 USDT tömts i april, och boten hade stannat.
- **Den verkliga boten** var bara aktiv 17 av 30 dagar i september. Rekonstruktionen kör kontinuerligt.

---

## SLUTSATS

### Klassificering: **C. Ingen påvisad edge.** (PP1 PÅ: **D. Negativ edge.**)

**Avstämning mot förhandskriterierna (PP1 AV):**

| Kriterium | Utfall | |
|---|---|---|
| Netto > 0 | −3 856 | ❌ |
| CI-nedre > 0 | −9 540 | ❌ |
| ≥ 3 av 4 perioder positiva | 0 av 4 | ❌ |
| > 0 utan topp 5 | −4 554 | ❌ |
| PF ≥ 1,15 | 0,95 | ❌ |

- **Inte A eller B:** netto är inte större än 0.
- **Inte D:** övre CI-gränsen (+1 892) är större än 0.
- **Alltså C.**

**Svar på frågan:**
- **Boten med PP1 AV har ingen påvisad positiv edge** över 6 månader efter realistiska kostnader. Före kostnader är den förenlig med noll edge (+0,66 per affär, CI omfattar 0). Kostnaderna (cirka 1,5 USDT per affär) gör den negativ i alla fyra perioder och alla regimer.
- **De senaste +393 USDT ligger inom slump och regimvariation:** 16–20 % av motsvarande perioder blir så bra även i ett system utan edge. AI-urvalet i september var inte bättre än det AI avvisade.
- **PP1 försämrar resultatet tydligt** och signifikant.
- **Det som skulle kunna ändra slutsatsen:** ett framåtriktat test där den verkliga AI-kedjan körs utan PP1. Del A ger ingen anledning att förvänta sig att AI-urvalet lyfter resultatet över kostnaderna.

**Ingenting har byggts eller ändrats i tradingboten. LIVE förblir stoppad.**
