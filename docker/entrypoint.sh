#!/bin/bash
set -e

# Persistent state (trades.db, bot.log) lives on the /data volume instead of
# baked into the image, so a rebuild/recreate doesn't lose trade history —
# same pattern as sniping/docker/entrypoint.sh.
mkdir -p /data
touch /data/trades.db /data/bot.log
ln -sf /data/trades.db /app/trades.db
ln -sf /data/bot.log /app/bot.log

exec python3 polymarket_arb_bot.py
