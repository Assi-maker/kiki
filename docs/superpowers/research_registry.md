# Research registry: externa metoder som vi prövat eller vill pröva

Principen:
- Vi hämtar **metodik**, inte strategier.
- Inga nya beroenden i LIVE.
- Allt implementeras först i research eller shadow och jämförs mot vår baseline OOS.

| # | Metod | Källa | Status hos oss | Var |
|---|---|---|---|---|
| 1 | **Lookahead-analys**: en indikator får inte ändras när framtida candles ändras | Freqtrade `lookahead-analysis` (freqtrade.io docs) | **Implementerad som test**: features jämförs mot data där allt ≥ T är förvanskat | `tests/crypto_trading/entry_research/test_edge_lab.py` |
| 2 | **Recursive-analys**: indikatorvärdet får inte bero på hur mycket historik som laddats (startup candles) | Freqtrade `recursive-analysis` | **Implementerad som test**. Vi använder Cutler-RSI och SMA-baserade mått som saknar rekursivt minne, så resultatet blir exakt lika | samma fil |
| 3 | **Vektoriserad signalutvärdering**: varje signal utvärderas på varje bar, inte bara när screenern triggade | VectorBT (vectorbt.dev), signal-/portföljmodellen | **Implementerad i ren Python**: ett 5-minutersgrid över 900 symboldagar med 53 000 rader | `crypto_trading/entry_research/edge_lab.py` |
| 4 | **Walk-forward och rullande split** | VectorBT `Splitter` samt Freqtrade backtesting och hyperopt-praxis | Tidsdelning TRAIN/VALID/TEST samt stabilitet per dag i entry-forskningen | `edge_lab.split`, `patterns.py` |
| 5 | **Purging och embargo**: etiketter som korsar periodgränsen tas bort | López de Prado, *Advances in Financial Machine Learning* kap. 7 | **Implementerad**. En rad vars 6 h-fönster (+23 min) når in i nästa period tas bort. Låst med test | `edge_lab.split` |
| 6 | **Triple-barrier-labels**: TP, SL och tidsgräns | López de Prado kap. 3 | Används: 2/3 ATR15 och 6 h, med verkliga kostnader | `edge_lab.outcome` |
| 7 | **Meta-labeling**: nivå 2 avgör om en redan utvald signal ska tas | López de Prado kap. 3.6 | **Testad**: logistisk nivå 2 tränad på TRAIN. **Inte robust OOS** (sämre i TEST) | `edge_lab_run.meta_labeling` |
| 8 | **Multipeltestkorrektion** | Benjamini–Hochberg (1995) | Används i varje steg | `stats.bh` |
| 9 | **Klustrade standardfel och block-bootstrap**: trades i samma marknadsrörelse är inte oberoende | Standard i ekonometri, Politis och Romano | Används med 4 h-kluster | `stats.py` |
| 10 | **Deflated Sharpe och data-snooping-test** | Bailey och López de Prado (2014), White Reality Check, Hansen SPA | **Inte implementerat.** Kandidat om ett mönster någonsin blir EDGE, som extra spärr mot "bästa av N" | – |
| 11 | **Kombinatorisk purgad CV (CPCV)** | López de Prado kap. 12 | **Inte implementerat.** Kräver mer data än 16 dagar för att ge något utöver dagens split | – |
| 12 | **Lookahead via framtida aggregat**: normalisering eller kvantiler över hela datasetet | Freqtrade-dokumentationens varning om `.mean()` över hela dataframe | Skyddat: tertilgränser beräknas bara på TRAIN, och regimer är tvärsnitt vid samma T | `patterns.tertile_cuts`, `edge_lab.add_regimes` |

**Inte importerat, avsiktligt:**
- Freqtrade-strategier från community-repot.
- Hyperopt-optimering av tröskelvärden. Det är exakt den överanpassning vi vill undvika.
- Nya pip-beroenden, till exempel vectorbt eller pandas, i LIVE-miljön.
