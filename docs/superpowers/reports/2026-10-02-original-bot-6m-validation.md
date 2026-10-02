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

*(fylls i efter körningen)*
