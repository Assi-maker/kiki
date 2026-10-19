# Polymarket rewards PAPER test v3 FINAL (pre-registered 2026-10-19), writes data/polymarket_paper/paper_v3.db
# and freezes the verdict in paper_v3_result.json, then stops.
# The v1 and v2 databases stay untouched next to it in the volume.
exec python -m crypto_trading.polymarket.paper
