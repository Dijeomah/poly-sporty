FROM python:3.11-slim

# Xvfb: virtual display Playwright's headed Chromium renders into (SportyBet
# blocks headless automation via Cloudflare + reCAPTCHA — see
# sportybet/sporty.py SportyBetBooker.login_interactive). x11vnc exposes
# that display over VNC; novnc/websockify bridge it to a plain browser tab
# so CAPTCHA/2FA can be solved remotely without a native VNC client.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify \
        wget ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# Installed as separate layers (not one `-r requirements.txt` run) so a
# retry on this slow/stalling connection doesn't re-download packages that
# already succeeded — each RUN below is its own cache layer. Lightest first,
# the Rust-wheel solana/solders and the Chromium download last.
# --timeout/--retries tolerate stalls pip's default 15s read-timeout was
# killing mid-download.
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir --timeout 180 --retries 10 \
        python-dotenv==1.0.1 base58==2.1.1 aiosqlite==0.20.0 "websockets>=9.0,<12.0"
RUN pip install --no-cache-dir --timeout 180 --retries 10 httpx==0.27.2 "aiohttp>=3.9.0"
RUN pip install --no-cache-dir --timeout 180 --retries 10 python-telegram-bot==21.7
RUN pip install --no-cache-dir --timeout 180 --retries 10 "py-clob-client>=0.34.5"
RUN pip install --no-cache-dir --timeout 180 --retries 10 solana==0.34.3 solders==0.21.0
RUN pip install --no-cache-dir --timeout 180 --retries 10 "playwright>=1.40.0"
RUN playwright install --with-deps chromium

# Claude Code CLI — sportybet/claude_analyst.py shells out to `claude -p` to
# research and veto picks. Authenticates via CLAUDE_CODE_OAUTH_TOKEN in .env
# (from `claude setup-token`, uses your Claude subscription) — see DOCKER.md.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force

COPY . .
RUN chmod +x docker/entrypoint.sh

ENV DISPLAY=:99
# 5900: raw VNC (native client). 6080: noVNC web UI — browse to
# http://<host>:6080/vnc.html.
EXPOSE 5900 6080

ENTRYPOINT ["docker/entrypoint.sh"]
