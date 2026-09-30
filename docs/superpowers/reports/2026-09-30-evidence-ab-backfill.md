# Evidence A/B – historisk backfill (2026-09-30)

Endast research och shadow. Evidence-flaggan är fortfarande OFF. LIVE-logiken, Guardian authority, Safety Kernel och 5/10 %-kapparna (log-only) är orörda. Den körande boten har inte rörts.

## Vad som gjordes
- **Lokal point-in-time-källa** (`evidence_shadow/local_data.py`):
  - klines och OI från `archive.db`, funding för september från `derivs.db`;
  - glappet efter 29/9 hämtas från samma Binance-endpoints (938 rader);
  - samma kontrakt som API-vägen: bar stängd ≤ T, funding settlad ≤ T, OI ≤ T. Klassificeraren kapar dessutom en gång till.
  - Klassning: cirka 180 s → 0,7 s per kandidat.
  - Kontroll mot de 55 kandidater som API-vägen klassade: regimer identiska 55/55, signaltyper 54/55. Skillnaden är en extra `OI_PRICE`, som beror på OI-källan.
- **Backfill** (`evidence_shadow/backfill.py`):
  - alla 1000 kandidater sedan 1/9;
  - alla 431 Guardian-övergångar sedan 4/9, med samma kod som live-loopen (`prepare_ab` / `ask_ab` / `write_ab`), 6 trådar;
  - A/A-replikat på var 4:e övergång i tidsordning, fastlagt innan något svar sågs;
  - 970 AI-anrop, 5,23 USD, 12 minuter.
- **Två rättade fel i rapporten** (`evidence_ab_report.py`):
  - `positions.size` är notional i USDT, och P/L = size × prisavkastning. Rapporten räknade (exit − entry) × size och gav därför orimliga värden (+16 000 USDT, −24 R).
  - Positioner med storlek 0 exkluderas.
  - Nytt: utfall som verifierats mot exchange-klines (MATCH, `godfather.book`), McNemar A/A och Wilson-intervall.

## Temporal isolation
- **Kontexten för varje A/B-rad** bygger bara på Guardian-observationen själv: state, faktorer, decay, progress, orealiserad P/L och den ursprungliga tesen.
- **Evidensen** tas från den senaste snapshoten med as_of ≤ observationen, och kandidatens klassning måste ha beslutstid ≤ observationen.
- **Utfall** läses först i rapporten, efter att rekommendationen loggats.
- **Kvarvarande förbehåll:** research-designen (vilka signalfamiljer, protokoll) togs fram på data som överlappar september. Snapshotarnas statistik är tidskorrekt, men valet av mått är det inte helt.

## Resultat
| | värde |
|---|---|
| Återanvänd data | 1000 kandidater (+ 938 gap-rader), 431 Guardian-övergångar / 115 positioner, 116 kline-verifierade positioner |
| Giltiga A/B-observationer | 431 (0 fel), varav 312 med utfall (65 positioner) |
| Brus (A/A-par) | 108 |
| Stängda trades med klassning | 134 (94 verifierade) |
| Förväntat utfall utan / med evidence | +0,030 R / +0,047 R per beslut (2,60 / 3,28 USDT) |
| Med − utan (95 % KI, klustrat per position) | +0,017 R [−0,001; +0,036], p = 0,069 |
| Samma, bara verifierade exits | +0,006 R [−0,004; +0,015], p = 0,27 (256 rader / 47 positioner) |
| Felaktig EXIT (EXIT fast hålla hade tjänat) utan / med | 1,9 % / 1,3 % |
| Missad EXIT (ingen EXIT fast hålla förlorade) utan / med | 53,8 % / 51,3 % |
| Diskordanta fel: bara utan fel / bara med fel | 16 / 6 (p = 0,053) |
| MFE som gavs bort efter EXIT, utan / med | 2,92 / 4,12 USDT |
| MAE som satt kvar efter hold, utan / med | −10,60 / −9,91 USDT |
| AI-brus (A/A) | 11,1 % [6,5; 18,4] |
| Ändrade beslut med evidence | 20,9 % [17,3; 25,0] |
| Effekt bortom brus (McNemar på A/A-raderna) | evidens ändrar 20, replikat ändrar 6, p = 0,009 |
| Riktning | WATCH→PROTECT 61, PROTECT→EXIT 16, EXIT→PROTECT 8 |

## "Negativ på ny data" (NEGATIVE_OOS)
- **Täckning:** 732 av 776 klassbara kandidater (94 %) får NEGATIVE_OOS. 44 får NO_EDGE och 0 får något positivt. 253 har ingen evidens, främst BingX aktie- och råvarukontrakt som inte finns på Binance.
- **Bokade paper-utfall:** NEGATIVE_OOS −0,110 R (n = 107) mot övriga −0,025 R (n = 27, inkl. utan evidens). Intervallen överlappar kraftigt.
- **Verifierade utfall:** NEGATIVE_OOS −0,010 R (n = 72) mot övriga −0,215 R (n = 22). Etiketten skiljer inte ut sämre trades.
- **Hypotetiskt filter (tillämpas aldrig):** att skippa NEGATIVE_OOS skulle ta bort 81 % av LIVE-trades (87 av 108). På verifierad data blir förväntat utfall sämre (−0,215 mot −0,058 R), på bokad data bättre (−0,025 mot −0,093 R). Resultatet är inte robust, och kvar blir en mycket liten mängd trades.

## Vad som fortfarande kräver ny LIVE-data
1. **Utfall på börsfills.** A/B-motfaktumet bygger på paper-priser. 137 av raderna gäller 44 LIVE-positioner, men EXIT-vid-observation är inte prissatt på börsen.
2. **Effekten av PROTECT/WATCH.** Designen behandlar dem som att hålla positionen. Att mäta dem kräver en modell för vad PROTECT gör, inte mer data.
3. **Verklig effekt med flaggan ON.** Den går inte att backfilla, och det görs inte utan beslut.
4. **Positiva evidenscellers diskriminering.** Det finns 0 positiva celler, så mer LIVE-data ändrar inte det. Det kräver ny research.

## Rekommendation
**Behåll evidence OFF. Gå inte vidare till en test med flaggan ON.**
- Evidensen påverkar AI:n mer än bruset gör (p = 0,009). Den gör den mer defensiv: fler PROTECT, fler EXIT.
- Utfallsförbättringen är inte signifikant: +0,017 R, där KI:t innehåller 0. På verifierade exits är den bara +0,006 R.
- Etiketten NEGATIVE_OOS täcker nästan allt och skiljer inte vinnare från förlorare.
- Shadow-tjänsten får gå vidare, med lokal källa och billig drift, som oberoende uppföljning utanför provet.
