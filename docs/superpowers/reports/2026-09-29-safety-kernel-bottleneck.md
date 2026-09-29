# Varför Safety Kernel släpper igenom 1 av 21: forensik och counterfactual-replay av riskpolicyer

**Datum:** 2026-09-29
**Kod** (enbart läsning, ingen LIVE-påverkan):
- `crypto_trading/entry_research/risk_policy.py`
- `risk_replay_run.py`
- `lifecycle_run.py`

**Data:**
- `data/entry_research/risk_replay.json`
- `lifecycle.json`

**Reproducera:**
- `python -m crypto_trading.entry_research.risk_replay_run --snapshot`
- `python -m crypto_trading.entry_research.lifecycle_run`

**LIVE är oförändrat.** Det gäller 100 USDT × 10, max 4, 1 per symbol, 10 %/5 %, Guardian, P1, Profit Protection, SL-regler och execution.

## Metod

**Möjligheter:**
- Alla 227 CONFIRMED med riskagentens egen SL/TP och fullständig prisbana.
- Fördelning: train 1–12/9 (83), validering 13–19/9 (72) och OOS 26–29/9 (72).

**Beslutstid:**
- Den verkliga väggklockan för sista QA-anropet, taget från AI_CALL_MADE-eventet.
- De historiska gate-tiderna har cykelklocksbuggen och kan inte användas.
- Median signal → CONFIRMED är 14 min. Efter 19/9 ligger bara 3 % över 30 min.

**Utfall:**
- Entry sker 1 min efter beslut.
- LIVE-reglerna gäller: SL/TP, Profit Protection flyttar stoppen till break-even vid +1 %, 6 h tidsgräns.
- Kostnader: 0,10 % avgift och 0,15 % stop-slippage.

**Policy A** är Safety Kernels egen kod (`size_entry`), anropad med olika konfigurationer.

**Ingen look-ahead:**
- Varje beslut vid tiden t använder bara öppna positioner, deras aktuella stop och realiserad equity vid t.
- Break-even räknas först från baren efter att +1 % nåddes.
- Policy D:s kvalitetsfeature väljs på TRAIN och tillämpas oförändrad på VALID och TEST.

## 1. Flaskhalsen är storleken per trade, inte portföljen

**[FAKTA]** Med **tom portfölj** och equity 420 USDT skulle Safety Kernel ha avvisat **221 av 227** CONFIRMED:

| Utfall med tom portfölj | Antal |
|---|---|
| Bara grupptaket (5 % = 21 USDT) | 72 |
| Både portfölj- och grupptaket (> 42 USDT) | 130 |
| Likvidationsvakten | 19 |
| Godkänd | **6** |

- 1000 notional med riskagentens stopavstånd ger en worst-case-risk på i median **31–52 USDT**, alltså 7–12 % av equity.
- Stoppen ligger i median 3,8 × ATR15 och är typiskt 3–8 %.
- En enskild trade ryms bara om stoppen ligger högst ≈ 1,3 % under priset.

## 2. Vad kärnan avvisade: hur det hade gått med full storlek

Varje trade räknas för sig med 100 × 10.

| Kategori | n | Win | Exp. USDT | Exp. R | Summa USDT | PF | p(exp > 0) |
|---|---|---|---|---|---|---|---|
| A godkände | 10 | 30 % | +10,68 | +0,90 | **+107** (+113 kommer från 1 trade) | 8,9 | 0,18 |
| Avvisad: grupptak | 68 | 28 % | −3,40 | −0,11 | **−231** | 0,59 | 0,95 |
| Avvisad: likvidation | 19 | 16 % | −8,30 | −0,11 | **−158** | 0,19 | 0,95 |
| Avvisad: portfölj och grupp | 130 | 25 % | +0,36 | +0,01 | +47 | 1,06 | 0,46 |

- Kärnan avvisade 217 trades som tillsammans hade gett **−342 USDT**.
- Cirka 55 av dem var vinnare i efterhand (≈ 25 %), men förlorarna dominerar.
- Ingen avvisad delgrupp har signifikant positiv expectancy. "Portfölj och grupp" byter tecken mellan perioderna: +0,09 / −0,15 / +0,08 R.

## 3. Counterfactual-policyer (sekventiell portfölj-replay, equity 420)

| Policy | Trades (per dag) | P/L ALL | TRAIN | VALID | **OOS (TEST)** | Max DD | Värsta samtidiga risk |
|---|---|---|---|---|---|---|---|
| **A, nuvarande 10/5, full storlek** | 10 (0,6) | +107 | +3,5 | +108,5 | **−2,5** | −1,9 % | 4,4 % |
| B1, grupp 7,5 %, full storlek | 28 (1,8) | +66 | +10 | +107 | **−34** | −11,2 % | 9,0 % |
| B2, grupp 10 %, full storlek | 31 (1,9) | −39 | +4 | +42 | **−82** | −24,9 % | 9,1 % |
| C, allokera inom 10/5 (reduce) | 84 (5,3) | −56 | −30 | +11 | **−38** | −22,0 % | 9,95 % |
| C2, fast 2,5 % risk per trade inom 10/5 | 110 (6,9) | −20 | +16 | −21 | **−15** | −12,9 % | 9,4 % |
| D, kvalitetsviktad (volz_30m, vald på TRAIN) | 113 (7,1) | +21 | +47 | −23 | **−0,3** | −10,9 % | 9,9 % |

**[BEDÖMNING]**
- **Fler trades ger fler förluster.** Möjligheterna i sig har ingen positiv expectancy, vilket stämmer med entry-forskningen från 28/9.
- Att lätta på grupptaket ökar drawdown upp till 13 gånger, med negativt OOS-resultat.
- Riskallokering (C, C2, D) håller den totala risken ≤ 10 %, men sprider den på fler trades som går med förlust.
- D:s kvalitetsfeature fungerar bara in-sample.
- A:s positiva siffra är i praktiken **en** trade (+113 i VALID). Utan den står A på cirka −6.

## 4. Signalens livscykel (episoder: samma symbol med glapp < 2 h)

| Mean R (std-utfall) | TRAIN | VALID | TEST |
|---|---|---|---|
| NEW (första i episoden) | −0,17 | −0,25 | **−0,63** |
| ACTIVE (andra) | +0,04 | −0,19 | −0,38 |
| AGING (tredje och senare) | +0,22 | −0,17 | −0,39 |

- **Upprepade signaler är inte sämre.** Den första signalen är sämst i alla perioder (p(upprepning sämre) = 0,98, åt motsatt håll).
- Regeln "en ny möjlighet kräver en ny händelse" stöds inte av datan. Den skulle ta bort de *bättre* kandidaterna.
- 52 % av AI-analyserna (407 av 775, ≈ 54 USD) gick till upprepningar. Kostnaden ska minskas på andra sätt, se punkt 5.

## 5. AI-effektivitet: kompatibilitetskontroll före AI

- Proxy: stopavstånd ≈ k × ATR15, med k = 3,79 som median på TRAIN och därefter låst.
- Predikterad worst-case-risk för 1000 notional jämförs med grupptaket.

| Period | Analyserade med plan | Faktiskt inom grupptaket | Predikterat omöjliga | **Felaktigt blockerade** | Sparad AI-kostnad |
|---|---|---|---|---|---|
| TRAIN | 188 | 4 | 149 | **0** | ≈ 19,8 USD |
| VALID | 148 | 0 | 118 | **0** | ≈ 15,7 USD |
| TEST | 105 | 0 | 92 | **0** | ≈ 12,2 USD |

- Med nuvarande regler kan ≈ 98 % av AI-kostnaden aldrig leda till en trade.
- En deterministisk kontroll före AI skulle ha sparat ≈ 80 % av den, utan en enda felaktig blockering i någon period.
- Korrelationen ATR ↔ stop är måttlig (0,36–0,46), så kontrollen är konservativ. Den blockerar bara när risken tydligt ligger över taket.

## 6. Rekommenderad arkitektur (baserad på datan)

```
billig deterministisk screening
  -> RISK-KOMPATIBILITET före AI (ny, shadow först)
  -> AI (7 roller)
  -> Gate/P1
  -> Safety Kernel (oförändrad: full storlek eller REJECT)
  -> Guardian
  -> Execution
```

- **Behåll policy A.**
  - Den skyddar kapitalet: DD −1,9 %, värsta samtidiga risk 4,4 %.
  - Den avvisade en grupp trades som totalt hade gått med förlust.
  - Ingen testad alternativ policy slår den OOS.
- **Bygg ingen riskallokerare nu.** Den kan inte skapa edge. Den skalar exponeringen mot en möjlighetsström som saknar positiv expectancy.
- **Implementera risk-kompatibilitetskontrollen före AI, först i SHADOW.** Den loggar "skulle ha hoppat över AI" per kandidat och verifieras framåt. Först därefter kan den få styra. Den ändrar ingen riskgräns och ingen tradingregel, bara var AI-pengarna går.
- **Grundproblemet är en obalans:** fast 1000 notional (2,4× equity per position) mot 5 %-grupptak vid 420 USDT equity, och riskagentens stopp på 3–8 %.
  - Fler trades kräver antingen tightare stopp i uppsättningarna, en annan storleksregel eller andra tak.
  - Datan visar att fler trades med dagens möjligheter hade kostat pengar.
  - Rätt ordning: bevisad entry edge först (Entry Quality framåt-OOS), riskregler sedan.
- **GODFATHER** kan följa detta genom den befintliga kedjan hypotes → replay → OOS → shadow. Inget av det kringgår kärnan.

## Begränsningar

- Datan täcker 16 handelsdagar och 227 möjligheter, varav 72 i OOS.
- A:s resultat vilar på väldigt få trades.
- Utfallen är simulerade på 1m-klines. Verkliga fills kan skilja med några tiondels procent.
- Kompatibilitetskontrollens proxy använder referenspriset. Kärnan använder senaste priset vid ordern.
