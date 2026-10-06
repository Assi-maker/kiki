# One-time migration: a staged seed (local history) replaces the volume copy once, marked by .seeded.
if [ -f /app/seed/trend.db ] && [ ! -f /app/data/.seeded ]; then
  cp /app/seed/trend.db /app/data/trend.db && touch /app/data/.seeded && echo "seeded trend.db from local history"
fi
# Hourly, like the old Task Scheduler task: every run after the first of a UTC day is a no-op.
while true; do
  python -m crypto_trading.trend run || echo "trend run exited $?"
  sleep 3600
done
