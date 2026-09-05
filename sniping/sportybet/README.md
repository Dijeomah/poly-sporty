# SportyBet Module

A Telegram-driven SportyBet betting-tip assistant, built as a sub-project
inside the larger `sniping/` bot. It pulls high-confidence picks directly
from SportyBet's own live odds, curates single picks or accumulators, has
Claude review them for extra analytical edge, and generates a SportyBet
booking code you tap to confirm and place the actual stake yourself.

This module does **not** run standalone — it's imported by `../bot.py`,
the shared Telegram entrypoint for this whole repo (Solana sniper + BTC arb
+ SportyBet all live in one bot process). To run everything:

```bash
cd ..              # sniping/
python3 bot.py
```

## Layout

| File | Purpose |
|---|---|
| `sporty.py` | Prediction engine, SportyBet API client, per-user orchestrator (`SportyBot`), `SportyBotManager` |
| `sporty_database.py` | SQLite persistence — rollovers, pick history, daily generation limits (`sporty.db`, gitignored) |
| `claude_analyst.py` | Shells out to the `claude` CLI to rank curated picks by analytical confidence |
| `sportybet-agent.md` | System prompt for the Claude analysis pass — the "SportyBet Analyst" persona |
| `sporty_discover.py` | One-off manual tool: opens a browser, logs in with `.env` credentials, and dumps SportyBet's API shapes to `sporty_api_map.json`. Reference/debugging only — not part of the live login flow below. |
| `sessions/` | One session-cookie file per Telegram user (`<user_id>.json`), gitignored |

## Multi-user model

Every `cmd_sporty_*` Telegram command is intentionally **not** owner-only —
unlike the Solana sniper / BTC-arb commands in `bot.py`, anyone who messages
the bot can use SportyBet features. `SportyBotManager` (in `sporty.py`)
lazily creates one isolated `SportyBot` per Telegram user id the first time
they interact — separate SportyBet session, separate active rollover,
separate pick history, separate daily generation count. Odds/prediction
data (`PredictionEngine`) is shared across everyone since it isn't
user-specific.

### Logging in

```
/sporty_login <phone> <password>
```

This opens a **visible** browser on the machine running the bot and
attempts the login with the credentials you sent. SportyBet's login is
protected by Cloudflare + reCAPTCHA, so **a human needs to be at that
machine's screen** to solve the CAPTCHA when it appears — this cannot be
done headlessly or purely through Telegram text.

If SportyBet also asks for a 2FA/verification code, the bot will message
you asking for it — just reply with the code in the chat and it types it
into the page automatically. This detection is best-effort (SportyBet's
exact verification-code markup hasn't been captured against a live 2FA
screen, so it uses generic heuristics — common OTP UI patterns and text
matching like "verification code"/"enter otp"). You have 3 minutes to
reply once asked.

**Security note, read before using this on a shared bot:** your SportyBet
phone and password are sent as a plain Telegram message to whoever runs
this bot. The bot deletes that message immediately after reading it
(best-effort — Telegram doesn't always allow a bot to delete a user's own
message in a private chat, so don't rely on it) and never writes the raw
password to disk or logs — only the resulting session cookies are
persisted, in `sessions/<your_telegram_id>.json`. Still, treat sending
`/sporty_login` the same as handing your password to the bot's operator.
If you're not comfortable with that, don't use this flow — run your own
copy of the bot instead, or use a SportyBet account you don't mind the
operator having access to.

The bot owner (set via `ALLOWED_USER_ID` in `.env`) can instead run
`/sporty_login` with no arguments to use the `SPORTYBET_PHONE`/
`SPORTYBET_PASSWORD` already configured in `.env` — unchanged from before
this module supported multiple users.

## What it actually does

- `/sporty_pick [odds] [days]` — one single-game pick near the target odds
- `/sporty_acca [target] [days]` — an accumulator sourced **directly from
  SportyBet's own live event list** (not The Odds API), so every leg is
  already confirmed to exist and be bookable. Picks the safest verified
  market per game — 1X2/Winner, Over/Under (all lines, plus Home/Away/Corners
  variants), Handicap, GG/NG, Double Chance, and the early-settlement
  markets 1UP/2UP/Never Down/Double Chance-1UP — the only market shapes
  whose booking payload has been verified against real SportyBet responses.
  Excludes SportyBet's "Simulated Reality League" (virtual/simulated
  fixtures). Capped at **5 generations per user per day**; each regenerate
  excludes games already used in a ticket booked earlier that day, so you
  get genuinely different games/selections.
- `/sporty_rollover [odds] [days]` — same SportyBet-sourced accumulator
  logic as `/sporty_acca`, run once per day for N days (a same-day
  mini-accumulator when the target odds need multiple legs — a single real
  match usually can't reach an arbitrary target on its own). Each day's
  ticket is restricted to games kicking off within that day (`days_ahead=1`)
  so it never mixes in games from later days just to hit the target odds.
  The next day's ticket is scheduled for right after the current day's last
  game actually finishes (latest leg's kickoff + a 3h buffer for match
  length/stoppage/settlement), not a blind 24h timer. Persisted so a
  bot restart resumes it instead of losing the plan
- `/sporty_history` — win/loss track record once picks have kicked off and
  been settled against real results
- Claude analysis (via `claude_analyst.py` + `sportybet-agent.md`) reviews
  the top curated candidates each time an accumulator is built and
  re-ranks them by analytical confidence — this shells out to the `claude`
  CLI in non-interactive mode, so it runs through your existing Claude Code
  session/subscription rather than needing a separate `ANTHROPIC_API_KEY`.
  If the `claude` CLI isn't on `PATH`, or its output can't be parsed, this
  step is silently skipped and the pure odds-based ranking is used instead.

## What it deliberately does not do

- **No real-money auto-staking.** SportyBet's API only exposes a
  booking-code endpoint (`/orders/share`) — there is no discovered
  endpoint to place an actual wager. You still tap to confirm the stake
  yourself on SportyBet.
- **No untested markets.** `SportyBetBooker._SAFE_MARKET_NAMES` in
  `sporty.py` lists every market whose booking payload shape (including the
  `specifier` field some markets require) has been verified against real
  SportyBet responses — currently 1X2/Winner, Over/Under (all lines +
  Home/Away/Corners variants), Handicap, GG/NG, Double Chance, and
  1UP/2UP/Never Down/Double Chance-1UP. **Asian Handicap, Correct Score,
  minute-interval markets, and combo markets (e.g. "1X2 & GG/NG") don't
  appear at all** in the event-listing API this module reads from — verified
  by scanning 500 live events across the richest available matches, only 16
  distinct market names ever showed up. Getting those would need discovering
  a different SportyBet endpoint (the full match-detail page, not the bulk
  listing), not just a code change. Betting real money into a guessed
  payload shape is exactly the kind of mistake this module is built to avoid.
- **Corners and 1UP/2UP/Never Down/Double Chance-1UP are bookable but not
  auto-settled.** Corners needs a corner-count stats source we don't fetch;
  the early-settlement markets need the goal-by-goal timeline (did a team
  ever lead by N goals), not the final score. Both are left `pending`
  forever in `/sporty_history` rather than risk grading them wrong.
