# Gridbot: validering på tickdata (2026-10-02)

**Klassificering: grid-hypotesen är EJ VERIFIERAD. Ingenting byggs vidare på den.**

## Fråga
Har griddarna som bara köper (buy-only) en verklig fördel när fyllnaderna bestäms av faktiska Binance-affärer i stället för av candle-wicks? Det gäller de fyra varianter som 14-månaderslabbet godkände: LONG ±1σ, steg 0,10/0,20 σ, med och utan trendfilter.

## Viktig rättelse först
Under arbetet hittades en bugg som fanns i båda gridmotorerna. Den rättades i commit `e6aaa79`, med regressionstester som fallerar på den gamla koden.

- **Felet:** lagret räknades som ett flyttal.
  - På coins med lågt pris (PUMP: 17 320 coins per nivå) kunde en försäljning lämna en rest på cirka 7e-12. Det var mer än toleransen 1e-12.
  - En grid som bara köper sålde då en nivå för mycket och kunde senare hålla fler nivåer än den har. Lagret blev upp till 1 461 USDT i en grid på 1 000 USDT.
- **Rättelsen:** lagret räknas nu som ett heltal antal nivåer (`held`), och `pos = held * q` exakt.
- **Följden:** alla tidigare gridresultat har körts om.
  - **14 månader (`grid_lab14`):** med rättad motor godkänns 0 av 24 varianter, mot 4 av 24 före rättelsen. De fyra LONG-griddarna med ±1σ är negativa i TRAIN, VALID och TEST, och gridens egen skörd (resultat minus lagrets kursrörelse) är negativ i alla fyra perioder.
  - **Användarens LIVE-fönster:** den bästa griden som bara köper gav +129 USDT med 4 platser, mot +1 379 för köp och håll. Gridens egen skörd är negativ i alla LONG-varianter.
  - **Slutsats om orsaken:** det tidigare "godkännandet" berodde på buggen, inte på verklig fördel.

Tickvalideringen nedan kördes ändå enligt förregistreringen (`01fc0f0`), och med den rättade motorn.

## Förregistrerad design (`grid_tick.py`, commit `01fc0f0` före första resultatet)
- **Coins (15):** användarens LIVE-coins i Binance-universumet, utan BTC och ETH. De valdes på högst handelsvolym i 2025-10, aldrig på gridresultat:
  - SOL, BNB, XRP, XPL, SUI, 1000PEPE, LINK, ENA, AVAX, PUMP, LTC, TAO, PAXG, WLD, AAVE.
- **Perioder:**
  - FALL 2025-10-01 till 2025-11-30. Mediancoinen föll 25 % respektive 23 %.
  - MIXED 2026-04-01 till 2026-05-31. Mediancoinen rörde sig +8 % respektive −2 %.
- **Data:** varje Binance aggTrade (data.binance.vision). Konsekutiva affärer med samma pris slogs ihop. Det var 1–5 MB per coin och dag.
- **Exakt fyllnad:**
  - En köporder på nivå L fylls bara om en affär sker strikt under L (nivån har svepts, köplacering spelar ingen roll). En säljorder fylls bara strikt över L.
  - En motorder blir aktiv 1 s efter att föregående order fyllts.
  - Likvidation kontrolleras på varje affär: hela marginalen plus 0,5 % går förlorad.
- **Samma gridregler som i grid-labben:**
  - σ beräknas från de föregående 1 440 stängningarna på 1m, så ingen framtida data används.
  - Intervallstopp och marginalskydd (−90 %) kontrolleras på 1m-stängningen, och efter ett stopp väntar griden 60 minuter.
  - Lagret är högst 1 000 USDT.
- **Avgifter:** maker 0,02 % per fyllnad. Stopp betalar taker 0,05 % plus 0,05 % slippage. Verklig Binance-funding räknas in.
- **Jämförelser på exakt samma affärer:** candle-motorn på 1m- och 5m-staplar byggda från affärerna, samt köp och håll med 1 000 USDT per coin.
- **Beslutsregel:** hypotesen överlever bara om minst en variant har positivt resultat efter avgifter i BÅDA perioderna på tickdata. Robusthetskontroll (touch-fyllnad, 5 s latens) körs bara om den överlever.

## Resultat (15 coins × 1 000 USDT, USDT)

### FALL (2025-10 till 2025-11)
| Motor | Resultat | Före avgifter | Avgifter | Fyllnader | MDD | Vinstdagar | Coins + |
|---|---|---|---|---|---|---|---|
| Köp och håll | −5 612 | – | – | – | −7 410 | 43 % | 1/15 |
| tick LONG_R1_S0.1_noTF | −744 | −382 | 362 | 13 152 | −1 366 | 54 % | 5/15 |
| tick LONG_R1_S0.1_TF | −1 032 | −722 | 310 | 11 284 | −1 507 | 54 % | 4/15 |
| tick LONG_R1_S0.2_noTF | −651 | −412 | 239 | 3 408 | −1 351 | 54 % | 5/15 |
| tick LONG_R1_S0.2_TF | −863 | −655 | 208 | 3 039 | −1 402 | 56 % | 5/15 |
| candle 1m (samma 4) | −720 till −1 115 | | | | | | |
| candle 5m (samma 4) | −1 363 till −1 652 | | | | | | |

### MIXED (2026-04 till 2026-05)
| Motor | Resultat | Före avgifter | Avgifter | Fyllnader | MDD | Vinstdagar | Coins + |
|---|---|---|---|---|---|---|---|
| Köp och håll | +89 | – | – | – | −3 028 | 48 % | 7/15 |
| tick LONG_R1_S0.1_noTF | −809 | +43 | 852 | 31 419 | −1 371 | 59 % | 4/15 |
| tick LONG_R1_S0.1_TF | −1 126 | −353 | 772 | 28 270 | −1 443 | 59 % | 1/15 |
| tick LONG_R1_S0.2_noTF | −479 | +74 | 552 | 8 123 | −1 362 | 59 % | 4/15 |
| tick LONG_R1_S0.2_TF | −980 | −487 | 493 | 7 101 | −1 476 | 59 % | 2/15 |
| candle 1m (samma 4) | −650 till −1 414 | | | | | | |
| candle 5m (samma 4) | −892 till −1 754 | | | | | | |

**Kapitalbindning (tick):**
- Snittlager 154–204 USDT per grid på 1 000 USDT. Det högsta lagret var 994–996 USDT, alltså inom gränsen, vilket bekräftar att buggen är borta.
- Vid fullt lager binds cirka 100 USDT marginal per coin.

**Per coin, tick LONG_R1_S0.2_noTF:**
- FALL: 6 av 15 coins var positiva, mest PUMP +120, LTC +83 och SUI +31. Sämst var XPL −320 och AVAX −154. Alla utom PAXG slog köp och håll, eftersom köp och håll föll 46–805 USDT per coin.
- MIXED: 4 av 15 coins var positiva, mest ENA +126, WLD +114 och TAO +60. Sämst var PEPE −109 och SUI −101.

**Hur mycket av candle-resultatet försvinner med tickdata?**
- Inget positivt candle-resultat fanns att förlora. Med rättad motor är candle-resultaten negativa i båda perioderna.
- Tick ger ett mindre negativt resultat än candle: 7–19 % bättre än 1m och 35–52 % bättre än 5m. Två skäl:
  - Candle-motorn tillåter inga affärer fram och tillbaka inom samma stapel, medan tick tillåter dem efter 1 s latens.
  - 5m-staplar ger grövre stopp.
- Det tidigare positiva candle-resultatet (grid_lab14) kom från buggen, inte från wicks.

## Skillnad mellan BingX och Binance (`grid_venue.py`)
Underlaget är 15 coins under 6 dagar, 3 i varje period: 129 600 jämförda minuter.

- Stängningskursen per minut skiljer i median 1,8 bp (p95 10,2 bp).
- BingX har i snitt 0,7 bp lägre minutlägsta och 1,7 bp lägre minuthögsta än Binance.
- Prisspannet per minut på BingX är i median 0,97 av Binances.
- Binance gick minst 10 bp djupare än BingX under 1,7 % av minuterna.

**Effekt:** skillnaden är liten jämfört med stegen (0,2–1 %). Den skulle ge något färre fyllnader på BingX, men den kan inte vända ett negativt resultat till positivt. Den påverkar inte slutsatsen.

## Slutsats
- **Med exakt tick-fyllnad förlorar alla fyra buy-only-griddar pengar** i både den fallande och den blandade perioden.
- **De förlorar redan före avgifter i den fallande perioden.** I den blandade perioden är de ungefär noll före avgifter, och avgifterna gör dem negativa.
- **Griden dämpar fallet men tjänar inte pengar.** I den fallande perioden förlorade den mycket mindre än köp och håll (−651 mot −5 612), eftersom den håller ett litet lager (cirka 16 %). Den skyddar men tjänar inte.
- **Ingen daglig vinst:** vinstdagarna är 54–59 %, men snittdagen är negativ.
- **Klassificering: EJ VERIFIERAD.** Robusthetskontrollen kördes inte, enligt den förregistrerade regeln.
- **Inget byggs vidare:** ingen ändring i tradingboten, ingen BingX-live, ingen GODFATHER-integration. LIVE är fortfarande stoppad.

## Filer
- `crypto_trading/entry_research/grid_tick.py`: tickmotor och körning.
- `grid_tick_report.py`: sammanställning.
- `grid_venue.py`: jämförelse mellan BingX och Binance.
- Rådata: `docs/superpowers/reports/2026-10-02-grid-tick-primary.json`.
- Cachefiler per coin och period: `data/entry_research/grid_tick/` (git-ignorerad).
- Tester:
  - `tests/crypto_trading/test_grid_tick.py`: fyllnad strikt genom nivån, touch, latens, likvidation, stopp, trendfilter, funding och en regression för heltalslagret.
  - `tests/crypto_trading/test_grid_lab.py`: regression för heltalslagret.
