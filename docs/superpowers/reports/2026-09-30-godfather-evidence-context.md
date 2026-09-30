# GODFATHER med historisk evidens som kontext (2026-09-30)

## Vad som implementerades
1. **Evidenskontext** (`crypto_trading/evidence/context.py`). Per signaltyp och sida innehåller den:
   - regimen vid beslutet;
   - OOS-förväntat utfall i R efter kostnader, med 95 %-intervall;
   - n, win rate, MFE/MAE och hålltid;
   - jämförelse mot slumpmässiga entries;
   - status, säkerhetsgrad och varning om att bara träningsdatan var positiv;
   - utfall per horisont;
   - `role: CONTEXT_NOT_RULE` och en text som säger att evidensen är kontext, inte regel.

   En klassning gjord för en senare tidpunkt än beslutet avvisas. Snapshoten är alltid den senaste med as_of ≤ beslutet.
2. **Självkritik** (`crypto_trading/evidence/self_critique.py`). GODFATHERs egna tidigare förutsägelser jämförs med faktiskt utfall: bias, medelabsolutfel, Brier-poäng för sannolikheten att vinna, och andel rätt per rekommendation.
   - Uppdelat totalt, per samma signaltyp, samma evidensstatus och samma Guardian-state.
   - Bara utfall med `known_at` ≤ beslutstiden används.
   - Under 5 fall visas inga siffror.
   - Också märkt `CONTEXT_NOT_RULE`.
3. **GODFATHER-shadow-beslut** (`crypto_trading/evidence_shadow/godfather_shadow.py` och två nya agenter). Varje beslut körs i två armar, WITHOUT (dagens kontext, baseline) och WITH (+ evidens och självkritik). På var 4:e beslut ställs dessutom WITHOUT-frågan en gång till för att mäta AI-brus.
   - **ENTRY:** varje CONFIRMED gate-entry som öppnade en position. Beslutstiden är gate-utvärderingen. GODFATHER anger en stance (rådgivande), förväntat R, sannolikhet att vinna och förväntad MFE/MAE. Traden tas oavsett svar.
   - **MANAGEMENT:** varje Guardian-övergång där AI:n anropas. GODFATHER anger HOLD/WATCH/PROTECT/EXIT, förväntat slutligt R och sannolikhet att vinna.
4. **Prediction-error-logg.** Tabellen `godfather_decisions` innehåller beslut, förväntan och exakt vilken evidens och självkritik som gavs. Tabellen `godfather_outcomes` innehåller verklig P/L, R, MFE/MAE efter beslutet, exit, kline-verifiering och investigatorns verdikt för entry och management, med `known_at` satt till stängningstiden. Prediction error räknas som faktiskt minus förväntat, per arm.
5. **Mätrapport** (`crypto_trading/entry_research/godfather_evidence_report.py`).
   - **Entry:** prediction error och Brier per arm, parat med KI; MFE/MAE-fel; stance mot utfall; hypotetisk effekt av att skippa DOUBTFUL, som aldrig tillämpas.
   - **Management:** förväntat utfall, P/L, rätt och felaktig EXIT, HOLD som borde ha varit EXIT, EXIT som borde ha varit HOLD, MFE och MAE efter beslutet, prediction error, AI-brus (A/A och McNemar) och skillnad mot baseline, även bara på verifierade exits.
   - **Attribution:** dålig entry, dåligt management efter rimlig entry, båda, eller bra, samt om traden gick som GODFATHER förväntade sig.
   - Varje vecka för sig.
6. **Live-loop** i shadow-tjänsten, med ett eget tak på 300 anrop per dygn.
   - Den spenderar ingenting när bottens AI-hälsa inte är OK, eftersom den delar API-nyckel med boten.
   - Vattenmärket flyttas aldrig förbi ett misslyckat beslut.
   - Backfillen stannar om mer än hälften av en dags beslut misslyckas och kan köras om för att fortsätta.
7. **Strategisten får ingen evidens längre** (`guardian/self_improvement.py`, −7 rader). Dess förslag kan bli PRE_ENTRY_VETO-heuristiker, så evidensen skulle annars indirekt kunna bli ett trade-filter.
8. **Bridge** (`evidence/bridge.py`). Guardian-AI:ns förklaring får samma rika kontext om flaggan slås på. Flaggan är fortfarande OFF.

## Vad som absolut inte påverkas
Följande har ingen diff:
- Safety Kernel, `live_execution_loop`, gate, paper_trading, orchestrator och run;
- Guardian `deterministic`, `authority` och `authority_live`;
- alla config-filer.

Därmed gäller:
- `context_enabled: false` och `risk_caps_enforced: false`, så 5/10 %-kapparna är fortfarande bara loggning;
- Guardian behåller sin deterministiska auktoritet;
- inget i evidens- eller shadow-koden importerar Safety Kernel, live execution, connectors, sizing, gate eller authority (AST-test);
- shadow skriver bara till sina egna tabeller, och bot-databasen öppnas `mode=ro`.

## Tester
- 21 nya tester i `test_godfather_evidence.py`, plus uppdaterade isoleringstester. Evidenstesterna: 55/55.
- Hela sviten: 2553 gröna. 8 fallerar, samma 8 config- och authority-tester som fallerade redan innan.

## Historisk verifiering: ofullständig
Backfillen körde alla 682 beslut sedan 1/9. **Anthropic-krediten tog slut 21:36Z mitt i körningen.** Bara besluten 3/9–12/9 fick svar (259 lyckade AI-rader, 2,08 USD). Boten delar API-nyckeln, och dess senaste lyckade AI-anrop var 21:27Z.

Resultat på den lilla delmängden. Inget förregistrerat minimum är uppnått, så ingen slutsats kan dras:

| | WITHOUT | WITH |
|---|---|---|
| Entry, mätbara | 33 | 33 |
| Förväntat R / faktiskt R | −0,17 / −0,17 | −0,12 / −0,17 |
| Medelabsolutfel (R) | 1,03 | 1,05 (paret +0,02, KI [−0,06; 0,11]) |
| Brier, sannolikhet att vinna | 0,260 | 0,258 |
| Management, mätbara | 48 (13 positioner) | 48 |
| Förväntat utfall (motfaktum) | +0,163 R | +0,163 R, identiskt (inga EXIT i någon arm) |
| Prediction error (R) | 0,92 | 1,08 (paret +0,17, KI [0,05; 0,28]), sämre med evidens |
| HOLD som borde ha varit EXIT | 18 | 18 |

**Attribution, 33 trades:**
- 17 slutade med vinst;
- 11 hade dålig entry (MFE < 0,5 R);
- 3 hade dålig entry och förlust bortom planen;
- 2 hade dåligt management efter en rimlig entry.

**Stance:** WITHOUT satte DOUBTFUL på 29 av 33 trades, WITH på 33 av 33. Evidensen (nästan alltid NEGATIVE_OOS) gör alltså GODFATHER mer pessimistisk utan att förbättra förutsägelserna. Det stämmer med det tidigare A/B-resultatet.

## Nästa steg
1. **Fyll på Anthropic-krediten.** Det är nödvändigt både för botens entry-analys och för att verifieringen ska kunna slutföras.
2. Återuppta backfillen: `python -m crypto_trading.evidence_shadow.backfill --skip-classify --godfather`. Den hoppar över redan lyckade beslut, och kostnaden är uppskattningsvis 6–8 USD.
3. Kör `python -m crypto_trading.entry_research.godfather_evidence_report` och bedöm mot de förregistrerade minimumen: 100 parade management-beslut, 30 A/A-par och 100 stängda trades.
