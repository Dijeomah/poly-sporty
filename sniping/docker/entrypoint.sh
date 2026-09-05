#!/bin/bash
set -e

# --- Virtual display -------------------------------------------------------
# The SportyBet login flow launches a *visible* Chromium (headless is
# blocked by Cloudflare/reCAPTCHA) — Xvfb gives it a real display to render
# into even though there's no physical screen on this machine.
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99

# --- VNC + noVNC ------------------------------------------------------------
# Refuse to start rather than ever exposing an unauthenticated VNC session
# onto a browser that may hold a live, logged-in SportyBet session.
: "${VNC_PASSWORD:?Set VNC_PASSWORD in .env before starting this container}"

mkdir -p ~/.vnc
x11vnc -storepasswd "$VNC_PASSWORD" ~/.vnc/passwd
x11vnc -display :99 -forever -shared -rfbauth ~/.vnc/passwd -rfbport 5900 -q &

# Bridges the raw VNC port to a plain browser tab: http://<host>:6080/vnc.html
websockify --web=/usr/share/novnc/ 6080 localhost:5900 &

# --- Persistent state --------------------------------------------------------
# Everything the app would otherwise write next to its own code lives under
# a single /data volume instead, so a container rebuild/recreate doesn't
# lose the SportyBet session, pick history, or Solana positions. Symlinking
# (rather than bind-mounting each file individually) sidesteps the Docker
# gotcha where mounting a not-yet-existing single *file* silently creates a
# directory there instead.
mkdir -p /data/sessions
rm -rf /app/sportybet/sessions
ln -sfn /data/sessions /app/sportybet/sessions

touch /data/sporty.db /data/sporty_cache.json /data/positions.db
ln -sf /data/sporty.db /app/sportybet/sporty.db
ln -sf /data/sporty_cache.json /app/sportybet/.sporty_cache.json
ln -sf /data/positions.db /app/positions.db

# exec so bot.py becomes PID 1's replacement and gets `docker stop`'s
# SIGTERM directly, instead of it going to this shell.
exec python3 bot.py
