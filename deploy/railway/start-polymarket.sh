# One-time migration: a staged seed (local test so far) is copied in once, marked by .seeded.
if [ -f /app/seed/paper.db ] && [ ! -f /app/data/.seeded ]; then
  mkdir -p /app/data/polymarket_paper
  cp /app/seed/paper.db /app/data/polymarket_paper/paper.db && touch /app/data/.seeded && echo "seeded paper.db from local test"
fi
exec python -m crypto_trading.polymarket.paper
