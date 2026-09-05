FROM python:3.11-slim

# No browser automation here (unlike sniping/) — plain Python + network
# calls to Polymarket's CLOB API, Polygon RPC, and the Anthropic API.

WORKDIR /app

COPY requirements.txt .
# Installed as separate layers (not one `-r requirements.txt` run) so a
# retry on this slow/stalling connection doesn't re-download packages that
# already succeeded — each RUN below is its own cache layer. Lightest first,
# pandas (the largest download) last. --timeout/--retries tolerate stalls
# pip's default 15s read-timeout was killing mid-download.
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir --timeout 180 --retries 10 python-dotenv>=1.0.0 requests>=2.31.0
RUN pip install --no-cache-dir --timeout 180 --retries 10 web3>=6.11.0
RUN pip install --no-cache-dir --timeout 180 --retries 10 py-clob-client>=0.34.5
RUN pip install --no-cache-dir --timeout 180 --retries 10 pandas>=2.1.0

COPY polymarket_arb_bot.py .
COPY docker/entrypoint.sh docker/entrypoint.sh
RUN chmod +x docker/entrypoint.sh

ENTRYPOINT ["docker/entrypoint.sh"]
