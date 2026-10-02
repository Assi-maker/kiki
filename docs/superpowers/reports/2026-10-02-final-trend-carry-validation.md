# Slutvalidering: Trend, Carry, Trend + Carry och baslinjer (2026-10-02)

Status: **FÖRREGISTRERING.** Det här avsnittet skrevs och committades tillsammans med koden (`entry_research/final_validation.py`, tester i `tests/crypto_trading/test_final_validation.py`) **innan** första körningen. Resultaten läggs till under "RESULTAT" och ändrar inget här.

LIVE är stoppad, `SAFE_MODE=1` ligger kvar, och execution, GODFATHER, riskgränser och trendstrategin är oförändrade. Ingen carry byggs. Detta är simulering.

---

## FÖRREGISTRERING

### Mål som prövas
En automatisk strategi med **positiv netto-P/L över tid**, gärna **många positiva dagar**, och **utan dolda stora svansrisker**.

### Data och tidsregler
- **Data:** Binance USDT-M perps och Binance spot, dagliga candles, september 2019 till 2026-10-01 UTC. Full funding-historik (varje betalning).
  - Universumet omfattar 261 perps som listades före 2024-04, inklusive 66 avlistade (SETTLING).
  - Perps som listades efter 2024-04 saknas, eftersom nedladdningen stoppades av minnesbrist. Det påverkar bara TOP5/TOP10-varianterna 2024–2026.
- **Ingen framtidsinformation:**
  - Beslut tas vid dagens stängning d med data som är känd då. Resultatet räknas över dag d+1.
  - Funding som tillhör dag d+1 är den som betalas under d+1.
  - Signaler använder bara funding som redan är betald.
  - Likviditetsrankning är bakåtblickande (90 dagar) och uppdateras den första i varje månad.
- **Perioder** (samma som sedan P5, inte valda nu):

| Period | Datum |
|---|---|
| TRAIN | 2020-01 till 2022-12 |
| VALID | 2023-01 till 2024-06 |
| TEST | 2024-07 till 2025-09 |
| HOLDOUT | 2025-10 till 2026-09 |
| FULL | 2020-01 till 2026-09 |

- **Regimer** (beskrivande): BTC:s 60-dagarsavkastning vid föregående stängning. Över +15 % = UPP, under −15 % = NED, annars SIDLEDES.

### Kostnader (BingX, per ben och sida)
- **Perp:** taker 0,05 %.
- **Spot:** taker 0,10 %.
- **Halva spreaden:** 0,01 % på BTC/ETH och 0,03 % på övriga.
- **Slippage:** 0,02 % på BTC/ETH och 0,05 % på övriga.
- **Funding:** verklig. En long betalar positiv funding, och en short tar emot den.

### Strategier
1. **CASH:** 0 % (USDT på kontot ger ingen avkastning).
2. **BUY&HOLD 1x:** 50/50 BTC/ETH-perps på 1x kapital, ombalanseras månadsvis, betalar funding.
   - Dessutom **BUY&HOLD exponeringsmatchad:** samma, men skalad till trendens genomsnittliga bruttoexponering. Den visar om trendens avkastning bara är exponering.
3. **TREND:** den frysta produktionsregeln, oförändrad.
   - BTC+ETH, signal = medel av sign(20 d) och sign(50 d), avklippt vid 0 (long eller flat).
   - Storleken styrs mot 40 % årlig volatilitet (30 dagar), högst 2x per tillgång, delat på 2.
   - Ett 5 %-band, och en utgång (till noll) verkställs alltid.
   - Kapitalbindning: marginal = brutto / 3.
4. **CARRY**, nio hypoteser som behandlas som separata och multipeltestkorrigeras:
   - **Universum:** MAJORS (BTC+ETH, 2 platser), TOP5 eller TOP10, de senare efter 90-dagars perp-volym, kräver spotpar och minst 90 dagars historik, exkluderar stablecoins och guld.
   - **Inträde:** när 7-dagars snittfunding × 365 överstiger T_in, med T_in ∈ {0 %, 5 %, 10 %}.
   - **Utträde:** under T_in/2 (vid T_in = 0 % under 0 %).
   - **CARRY_MAJORS_T0 är primärvarianten.** Den definierades redan i P5 (F5_thr0) och har alltså setts förut. Den räknas därför inte som ett nytt fynd.
5. **TREND + CARRY:**
   - **C-A, växling (primär):** trenden körs oförändrad. De dagar trenden inte har någon position används hela kapitalet till CARRY_MAJORS_T0. När trenden går in stängs carry-positionen.
   - **C-C, overlay (sekundär):** trenden körs i full storlek. Carry använder det kapital som trendens marginal inte binder, minus en buffert på 20 %: (0,8 − brutto/3) × kapital. Positionerna ändras först när det tillgängliga kapitalet har ändrats mer än 25 %.
6. **Generaliseringstest av trendregeln:** samma frysta regel, men på topp 10 altcoins efter volym vid varje tidpunkt, med andelen 1/10 per coin. Coinsen är aldrig valda efter resultat. Testet visar om mekanismen fungerar utanför BTC/ETH.

### CARRY: exakt mekanik
- **Positioner:** för varje innehavd coin long Q spot och short Q perp, i samma antal coins.
  - Notional per plats = platsens kapital / (1 + 1/L_perp), med L_perp = 2.
  - Kapital per notional: 1 i spot plus 0,5 i perp-marginal, alltså 1,5.
- **Dagligt resultat:** Q × (ΔSpot − ΔPerp) [basis] + Q × perp-pris × dagens funding.
  - Positiv funding ger short-benet en inkomst. Negativ funding är en kostnad, tills utträdesregeln stänger positionen.
- **Låg funding:** utträde under T_in/2.
- **Negativ funding:** betalas tills 7-dagarssnittet passerar utträdesgränsen. Det finns ingen omvänd carry (short spot kräver lån, som inte modelleras).
- **Kostnad:** varje öppning och stängning betalar båda benen (spot 0,13 % plus perp 0,08 % på BTC/ETH, 0,18 % + 0,13 % på övriga).
- **Hedgeunderhåll:** när perp-priset har rört sig mer än 10 % sedan senaste ombalansering ställs båda benen om till målstorleken. Det kostar avgifter på den handlade mängden.
- **Likvidation:** om perp-HÖGSTA stiger mer än 0,9/L_perp (45 %) över priset vid senaste ombalansering. Då stängs perp vid den nivån, spot säljs vid stängning, och 1 % av notional plus utgångskostnad dras.
- **Basisrisk:** spot och perp prissätts separat (dagliga stängningar).
  - Datakvalitet: ingen öppning om |spot/perp − 1| > 2 %, och tvångsutträde om > 5 % (avlistade eller omnamnade tokens).
- **Kapitalbindning:** spotbenet plus perp-marginalen. Exponeringen är delta-neutral, så kvarvarande risk är basis, funding och likvidation.

### Mått (alla strategier, FULL och varje period)
- Netto-P/L per 1 000 USDT (sammansatt och enkel summa). Brutto före kostnader och totala kostnader.
- Årsavkastning och Sharpe.
- Antal och andel positiva dagar (av alla dagar och av aktiva dagar).
- Genomsnittlig positiv och negativ dag, median, största plus- och minusdag.
- Max drawdown och längsta drawdown i dagar.
- Kapitalutnyttjande (bundet kapital) och exponering. Antal affärer och ombalanseringar.
- Resultat per regim och per år.
- p-värde för medel > 0 (block-bootstrap med 20-dagarsblock).

### Trend + Carry: isolering
- **Bidrag:** trendens bidrag, carry-delens bidrag (brutto, kostnad, netto) och interaktion = kombinationen − trend − carry-delen.
- **Skillnaden mot trend ensam:** Δ dagsmedel med 95 % block-bootstrap-CI, ΔSharpe, ΔMDD (procentenheter) och Δ andel positiva dagar.
- **Samvariation:** korrelationen mellan trendens och carry-delens dagar.

### Känslighetsanalys
Scenarierna körs på TREND, CARRY_MAJORS_T0 och TREND + CARRY C-A:
- avgifter ×2;
- slippage ×2;
- slippage ×3 och spread ×2;
- sämre fyllnader (+0,05 % per ben);
- funding ×0,5;
- funding ≈ 0;
- negativ förskjutning av funding (−0,01 % per 8 h);
- stressad basis (0,2 % / 0,5 % ogynnsamt vid varje utträde);
- sämre kapitalutnyttjande (perp 1x);
- allt kombinerat (avgifter ×2, slippage ×2, funding ×0,5, basis).

### Hypoteser
- **H-T:** TREND har positiv netto-expectancy. Den har setts förut (P5), så den räknas som bekräftelse, inte som upptäckt.
- **H-Tgen:** den frysta trendregeln är positiv även på altcoins som aldrig använts i urvalet.
- **H-C1 till H-C9:** CARRY-varianten har netto-medel > 0. Benjamini-Hochberg q < 0,10 på TRAIN+VALID.
- **H-CA:** C-A förbättrar trend utan att försämra expectancy.

### Förhandskriterier: CARRY anses värd att bygga endast om ALLA uppfylls
1. Minst en carry-variant klarar BH (q < 0,10) på TRAIN+VALID.
2. Den varianten har netto-medel > 0 i **alla fyra** perioder.
3. **Materialitet:** netto-årsavkastning på carry-kapitalet ≥ **3 %** i både TEST och HOLDOUT. Annars är den inte värd risken och komplexiteten jämfört med kontanter.
4. I HOLDOUT är netto fortfarande > 0 i scenariot "allt kombinerat".
5. MDD bättre än −5 % över FULL, och **0 likvidationer** i basscenariot.
6. **För att bygga den som del av Trend + Carry (C-A):** över FULL och i HOLDOUT gäller ΔSharpe ≥ 0, ΔMDD inte sämre än −2 procentenheter, Δ andel positiva dagar ≥ +5 procentenheter, och Δ dagsmedel inte signifikant negativ (95 %-CI:s övre gräns > 0).

Om villkor 1–5 inte uppfylls byggs ingen fristående carry. Om villkor 6 inte uppfylls byggs inte heller C-A.

---

## RESULTAT

*(fylls i efter körningen)*
