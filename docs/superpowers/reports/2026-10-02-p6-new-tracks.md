# P6: nytt spår – samla in funding i stället för att förutsäga priset (2026-10-02)

## Varför
Alla tidigare studier försökte förutsäga nästa prisrörelse: intradag (P1–P4, 7 studier), trend (P5) och grid (inklusive tickvalidering). Ingen fann en fördel som gav pengar dagligen. P6 testar strategier som inte behöver någon prisprognos. De samlar in betalningar som är inbyggda i marknaden, nämligen funding.

## C1: cash-and-carry på altcoins (förregistrerad @7c46f32)
**Strategi:** long spot och short perp på samma coin, alltså delta-neutralt. Positionen tar emot positiv funding.
- 249 coins med både spot och perp, de 50 mest likvida, 2020 till 09/2026.
- Inträde när genomsnittlig funding de senaste K dagarna (årstakt) överstiger T. Utträde under T/2.
- Högst 5 positioner. Taker-avgifter på båda benen plus slippage (0,50 % tur och retur). Perp-benet har 3x hävstång.

**Resultat:** 0 av 9 varianter godkändes. Alla är negativa i varje period, med −10 till −22 % per år och största nedgång −53 till −82 %.

Huvudvarianten (K3, T20), uppdelad på källor i andel av kapitalet:

| Källa | Andel av kapitalet |
|---|---|
| Funding | +74 % |
| Basis | −5,5 % |
| Kostnader | −21 % |
| 33 likvidationer | dominerade och vände resultatet till förlust |

- **Orsak:** coins med hög funding är just de som pumpar. Med 3x hävstång likvideras den korta perp-positionen vid en uppgång på 30 % på en dag.
- **Datafel som också hittades:** vissa spot- och perp-par matchar inte (SC, RAY, FTT, STRAX, DGB och andra, omnamnade eller avlistade tokens).

**Efterhandsanalys** (`p6_carry_followup.py`, märkt POST HOC): 1x hävstång och ett filter som utesluter par där spot och perp skiljer mer än 2 %.
- Bästa varianten (K7, T20) gav +3,5 % per år med Sharpe 0,5 och största nedgång −19 %.
- Per år: 2020 −7 %, 2021 +23 %, 2022 0 %, 2023 +3 %, 2024 +7 %, 2025 +1 %, 2026 0 %.
- **Tolkning:** vinsten kom nästan bara under tjuråret 2021. Sedan 2025 är funding för låg för att något ska passera tröskeln. Resultatet är nära noll och dessutom valt i efterhand, så det duger inte som underlag för en bot.

## C2: funding-snipe (perp, 14 månader 5m)
**Strategi:** gå in 5 minuter före en betalning på den sida som tar emot funding, om förra betalningen översteg θ, och gå ut 5 minuter efter.

| θ | Affärer | Snitt per affär | Vinnande affärer | Godkänd |
|---|---|---|---|---|
| 0,1 % | 6 370 | −0,28 till −0,33 % | 37–38 % | nej |
| 0,2 % | 3 267 | −0,29 till −0,43 % | 39–40 % | nej |
| 0,5 % | 1 267 | −0,34 till −0,62 % | 36–44 % | nej |

**Tolkning:** priset rör sig emot den som tar emot funding runt betalningen, så marknaden prisar redan in den.

## C3: skillnad i funding mellan BingX och Binance (beskrivande)
**Underlag:** BingX API ger bara de senaste betalningarna, så det enda tillgängliga är 25 dagar för 60 coins (grid.db).

**Övre tak med facit i hand:** rätt sida väljs för hela fönstret, och 4 ben kostar 0,4 %.
- Mediancoinen ger −0,14 % per 25 dagar, och bara 16 av 60 coins är positiva.
- Medelvärdet +0,26 % drivs av ett enda extremfall (LSK, cirka −55 % funding på båda börserna).
- Strategin kräver dessutom kapital på två börser. Inte gångbar.

## Slutsats
- **Funding är verklig inkomst** men går inte att samla in lönsamt i liten skala efter avgifter och pumprisk. Den mesta avkastningen fanns i 2021.
- **Inget av de tre nya spåren klarar målen** effektivitet, lönsamhet och daglig träffsäkerhet.
- **Inget byggs.** LIVE är fortfarande stoppad, och tradingboten är oförändrad.

## Filer
- `entry_research/p6_data.py` (spot dagsdata)
- `p6_carry.py` (C1 och C2, förregistrerad)
- `p6_carry_followup.py` (post hoc)
- `p6_crossvenue.py` (C3)
- Rådata: `2026-10-02-p6-carry.json` och `2026-10-02-p6-carry-followup.json`
