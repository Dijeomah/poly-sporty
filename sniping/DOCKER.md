# Running sniping/ in Docker

## Setup

1. Make sure `sniping/.env` exists with your usual variables
   (`TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_ID`, `PRIVATE_KEY`, `ODDS_API_KEY`,
   `API_FOOTBALL_KEY`, `SPORTYBET_PHONE`/`SPORTYBET_PASSWORD`, etc.).
2. Add one more line to that same `.env`:
   ```
   VNC_PASSWORD=<pick something — not your SportyBet password>
   ```
   The container refuses to start without it (see `docker/entrypoint.sh`) —
   VNC here means remote control of a browser that may hold a live, logged
   -in SportyBet session, so it's never allowed to run unauthenticated.

## Build & run

```bash
cd sniping
docker compose up --build -d
docker compose logs -f       # same log lines you'd see running bot.py directly
```

## Logging in to SportyBet (CAPTCHA/2FA)

SportyBet blocks headless login (Cloudflare + reCAPTCHA), so this needs a
human to see the browser. The container runs a virtual display (Xvfb) and
exposes it over VNC:

1. Open `http://<host>:6080/vnc.html` in a browser, enter `VNC_PASSWORD`.
   (Or point a native VNC client at `<host>:5900` instead.)
2. From Telegram, send `/sporty_login <phone> <password>` (or, as the bot
   owner, `/sporty_login` with no args to use the `.env` credentials).
3. The Chromium window Playwright opens appears in that noVNC tab — solve
   the CAPTCHA/2FA there same as you would on a real screen. If SportyBet
   asks for a 2FA code, the bot will also message you on Telegram asking
   for it (see `sportybet/README.md`) — either path works, since both are
   driving the same browser session.

Session cookies, the SportyBet daily odds cache, pick history, and Solana
positions all live on the `sniping-data` volume (`/data` in the container —
see `docker/entrypoint.sh` for the symlinks), so they survive
`docker compose restart` / image rebuilds.

## Known limitation: Claude analysis

`sportybet/claude_analyst.py` shells out to the `claude` CLI (Claude Code)
to have the SportyBet Analyst agent rank picks — that CLI is tied to a
*host machine's* own authenticated Claude Code installation and isn't
inside this container. The code already checks for it
(`claude_analyst.is_available()`) and silently falls back to pure odds
-based ranking when it's missing, so nothing breaks — accumulators just
won't get the Claude-analysis pass while running containerized, unless you
separately install and authenticate `claude` inside the image yourself.

## Security note

Don't expose ports `5900`/`6080` to the open internet without a reverse
proxy or VPN in front of them. Anyone who can reach that VNC session can
drive the browser — including a logged-in SportyBet session and, if the
Solana sniper side is ever actively trading, whatever else is visible on
that display.
