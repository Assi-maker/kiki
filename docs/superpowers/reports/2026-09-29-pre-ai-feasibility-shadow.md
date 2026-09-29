# Riskkontroll före AI (SHADOW): kalibrering, historisk utvärdering och beslutsträd

**Datum:** 2026-09-29
**Status:** enbart shadow. Kontrollen loggar och blockerar ingenting.

**Kod:**

| Fil | Roll |
|---|---|
| `crypto_trading/shadow/pre_ai_feasibility.py` | Ren bedömning |
| `shadow/pre_ai_feasibility_hook.py` | Koppling in i discovery |
| `performance/pre_ai_feasibility_report.py` | Framåtrapport |
| `entry_research/pre_ai_calibration.py` | Kalibrering och historik |

**Kalibrering:** `crypto_trading/config/pre_ai_feasibility.json`, låst på TRAIN.

**Reproducera:**
- `python -m crypto_trading.entry_research.pre_ai_calibration`
- Framåt: `python -m crypto_trading.performance.pre_ai_feasibility_report`

**Ordning i pipelinen:** screening → **riskkontroll före AI (shadow)** → AI → Gate → Safety Kernel (oförändrad) → Guardian → execution.

## Metod

- **Upplösning:** samma som discovery. 30m-bars som stängt före signalen, ATR över 14 bars. Historiskt byggs de upp från 1m-cachen på UTC-halvtimmar, som BingX 30m. I LIVE används cykelns egna 30m-klines, bara stängda bars.
- **Stoppavståndet är okänt före Risk Agent.** Kontrollen räknar därför aldrig fram ett exakt riskvärde.

**Metoden `bound`** är den enda som får säga INFEASIBLE:
- Stoppavståndet sätts till intervallet [max(q01 × ATR30, tightaste stoppet i TRAIN), q99 × ATR30].
- INFEASIBLE bara om **även det tightaste stoppet** bryter kvarvarande portfölj- eller grupputrymme, eller om en exakt strukturell regel gäller: max 4, 1 per symbol, eller likvidation redan vid tightaste stopp.
- FEASIBLE bara om även det vidaste stoppet ryms.
- Allt annat blir **UNKNOWN**. Saknad equity, pris eller ATR ger också UNKNOWN.

**Metoden `estimate`** loggas bara, märks som skattning och avgör aldrig något:
- Punktskattning: TRAIN-median × ATR30.

**Kalibrering (TRAIN 1–12/9):**
- Kvoten stopp/ATR30: q01 0,45, median 2,72, q99 25,2.
- Tightaste stoppet: 0,47 %.
- Korrelation ATR30 ↔ stopp: 0,39. ATR säger alltså lite om vilket stopp Risk Agent väljer.

**Facit:** Safety Kernels egen `size_entry` med Risk Agents faktiska stopp.
- Historiskt: equity 420 och tom portfölj.
- Nattens kärnbeslut: den loggade equityn och den loggade portfölj- och grupprisken.

## Resultat

### Hela historiken (445 AI-analyserade kandidater med riskplan)

- Bara **4 av 445** hade fått plats med full storlek, även med tom portfölj.
- **441 analyser, ≈ 58,7 USD, var i efterhand meningslösa för LIVE.** En full position kunde aldrig få plats.

| Metod | INFEASIBLE | UNKNOWN | FEASIBLE | Precision INFEASIBLE | Felaktigt avvisade (fick plats) | AI-kostnad sparad |
|---|---|---|---|---|---|---|
| **bound** | 112 (25 %) | 330 | 3 | **100 %** | **0** | ≈ 14,9 USD |
| estimate | 362 (81 %) | 4 | 79 | 100 % | 0 | ≈ 48,2 USD |

### Per period (bound)

| Period | n | INFEASIBLE | Fick faktiskt plats | Felaktigt avvisade | Sparat |
|---|---|---|---|---|---|
| TRAIN | 192 | 45 | 4 | 0 | 6,0 USD |
| VALID | 148 | 45 | 0 | 0 | 6,0 USD |
| OOS | 105 | 22 | 0 | 0 | 2,9 USD |

### Alla CONFIRMED (210 med ATR)

| Metod | INFEASIBLE | Felaktigt avvisade | Når fortfarande Safety Kernel |
|---|---|---|---|
| bound | 54 | 0 | 156 |
| estimate | 179 | 0 | 31 |

### Nattens 21 kärnbeslut (verklig equity och exponering)

| Metod | INFEASIBLE | UNKNOWN | Felaktigt avvisade |
|---|---|---|---|
| **bound** | 3 (WLD, CRV, QNT). Alla 3 avvisades också av kärnan. | 18 | **0**. PEOPLE blev UNKNOWN. |
| estimate | 17 | | **1: PEOPLE**, nattens enda godkända trade (stopp 0,79 %) |

**[BEDÖMNING]**
- `bound` korrelerar fullt med kärnan: varje INFEASIBLE avvisades också av kärnan, historiskt 112/112 och i natt 3/3.
- Men den är försiktig. Med tom portfölj kan den nästan aldrig bevisa att en ensam trade inte ryms, eftersom Risk Agent ibland sätter stopp ned till 0,47 %.
- `estimate` sparar 3 gånger mer, men kastade i natt bort **den enda trade som gick att öppna**.
- Det är exakt den typ av fel som principen "gissa inte fram ett exakt riskvärde" ska förhindra.
- **Positivt utfall bland INFEASIBLE** (52 av 112 för bound): de hade inte heller fått plats i kärnan, så det är ingen förlorad möjlighet under nuvarande regler. Siffran redovisas för fullständighetens skull.

## Isolering (låst med tester)

- Bedömningsmodulen importerar bara config och kärnans rena aritmetik.
- Hooken anropar bara `get_balance` (GET) och en läsande exponeringshjälpare. Den anropar ingen order, ingen `set_leverage` och skriver inget kärnbeslut.
- `run_single_cycle` kastar hookens returvärde. Ett "allt INFEASIBLE"-utfall eller en krasch ändrar inget: AI, Gate och position körs som vanligt.
- Ingen beslutsväg läser verdiktet: execution, Gate, kärnan, Guardian och sizing.
- UNKNOWN avvisar aldrig något, eftersom ingenting avvisas i shadow.
- Kärnan är fortfarande auktoritativ. En FEASIBLE från `bound` hindrar inte att kärnan avvisar på det faktiska stoppet.

## Beslutsträd för nästa steg

```
1. Samla framåt-shadow (boten igång, inget beteende ändrat)
   └─ efter >= 100 kandidater med kärnbeslut: kör pre_ai_feasibility_report
2. Är bound-INFEASIBLE:s precision mot kärnan 100 % och 0 kärn-APPROVE bland INFEASIBLE?
   ├─ NEJ -> behåll shadow, utred avvikelserna (kalibrering, pris/ATR-skillnad) - ingen aktivering
   └─ JA  -> 3.
3. Aktivera bound-INFEASIBLE som "hoppa över AI" (kräver ditt beslut)
   - sparar ~25 % av AI-kostnaden, noll kända felaktiga avvisningar
   - UNKNOWN och FEASIBLE går till AI som idag; kärnan har sista ordet
4. estimate-metoden: aktivera INTE
   - kastade bort nattens enda handlingsbara trade
   - kan bara bli aktuell om Risk Agents stopp kan förutsägas bättre (t.ex. en billig
     deterministisk stopp-regel före AI), och då bara efter egen OOS-validering
5. Grundfrågan kvarstår (se 2026-09-29-safety-kernel-bottleneck.md):
   ~99 % av AI-analyserna kan aldrig bli en full-size trade med 10/5-taken vid ~420 USDT
   equity. Den verkliga hävstången är inte pre-AI-filtret utan valet mellan
   (a) nuvarande regler + låg frekvens, (b) setups med tightare stopp, eller
   (c) annan storleksregel - och det beslutet ska tas på bevisad entry-edge, inte på frekvens.
```

## Begränsningar

- Historiskt facit bygger på referenspriset och tom portfölj. I LIVE används senaste pris och verklig exponering.
- Det finns bara 4 historiska kandidater som faktiskt fick plats. Det gör falsk-avvisningsmåttet statistiskt tunt, och därför är framåt-shadow ett krav före aktivering.
- De 21 kandidaterna från natten 28–29/9 finns inte i entry-datasetet. Det byggdes före natten. De utvärderas därför separat mot DB.
