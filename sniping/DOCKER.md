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

## Claude analysis of picks

Every accumulator, rollover day and daily pick is run past Claude before
booking (`sportybet/claude_analyst.py` + `sportybet/sportybet-agent.md`).
Claude web-searches each shortlisted match (form, injuries, H2H, lineups),
scores the pick, and vetoes weak ones — only games it backs at or above
`sporty_claude_min_confidence` (default 75%) go on the ticket. The Telegram
chat gets a summary of what was approved/vetoed and why.

The image includes the `claude` CLI. To log it in with your Claude
subscription (no API credits needed):

1. On any machine where you're logged in to Claude Code (e.g. your Mac),
   run:
   ```bash
   claude setup-token
   ```
   and copy the long-lived token it prints.
2. Add it to `sniping/.env` on the server:
   ```
   CLAUDE_CODE_OAUTH_TOKEN=<token>
   ```
3. Rebuild: `docker compose up --build -d`
4. Check it works inside the container:
   ```bash
   docker compose exec sniping-bot claude -p "reply with OK"
   ```

If the CLI is missing, not logged in, or times out, the bot falls back to
the old odds-only ranking rather than failing. Tunables in `config.py`
(`BotSettings`): `sporty_claude_enabled`, `sporty_claude_research`,
`sporty_claude_min_confidence`, `sporty_claude_max_games`. Web research on
30 games takes roughly 10–15 minutes per accumulator and counts against your
Claude plan's usage limits — lower `sporty_claude_max_games` if you hit them.

## Security note

Don't expose ports `5900`/`6080` to the open internet without a reverse
proxy or VPN in front of them. Anyone who can reach that VNC session can
drive the browser — including a logged-in SportyBet session and, if the
Solana sniper side is ever actively trading, whatever else is visible on
that display.
