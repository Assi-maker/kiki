# Railway image for the two PAPER bots (P5 trend, Polymarket rewards test).
# Neither bot needs API keys or the AI stack, so only their own deps are installed.
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 TZ=UTC
WORKDIR /app
RUN pip install --no-cache-dir "pydantic>=2.7" "httpx>=0.27" "tenacity>=9.0" "pyyaml>=6.0" "python-dotenv>=1.0"
COPY crypto_trading /app/crypto_trading
# seed/ holds a database only for the one-time move from the local PC (see start.sh)
COPY seed /app/seed
COPY start.sh /app/start.sh
# /app/data is a Railway volume (trend.db / polymarket_paper/paper.db)
CMD ["sh", "/app/start.sh"]
