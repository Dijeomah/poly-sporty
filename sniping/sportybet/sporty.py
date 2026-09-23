"""
SportyBet Prediction & Booking Module

Features:
- Rollover: Pick 1 high-confidence game/day at target odds for X days
- Accumulator: Compile multiple 85%+ probability games to reach target total odds

Data sources: The Odds API + API-Football
Booking: SportyBet Nigeria via Playwright automation
"""

import asyncio
import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Awaitable, Callable, Optional

import aiohttp

from config import (
    ODDS_API_KEY,
    API_FOOTBALL_KEY,
    SPORTYBET_PHONE,
    SPORTYBET_PASSWORD,
)
from . import sporty_database as spdb
from . import claude_analyst

logger = logging.getLogger("sporty")


# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class Game:
    """A match with prediction data."""
    fixture_id: str
    home_team: str
    away_team: str
    league: str
    sport: str
    sport_key: str
    commence_time: str
    odds_home: float = 0.0
    odds_draw: float = 0.0
    odds_away: float = 0.0
    prob_home: float = 0.0
    prob_draw: float = 0.0
    prob_away: float = 0.0
    api_football_prob: Optional[float] = None  # Best outcome probability from API-Football
    implied_prob: float = 0.0  # Best implied probability from odds
    confidence: float = 0.0   # Combined confidence score
    best_pick: str = ""       # "home", "draw", or "away"
    best_odds: float = 0.0    # Odds for the best pick
    af_fixture_id: Optional[str] = None  # Resolved API-Football fixture id (for predictions + settlement)


@dataclass
class BetSelection:
    """A single bet selection for a slip."""
    game: Game
    market: str       # e.g. "match_winner", "over_under"
    selection: str    # e.g. "Home", "Away", "Over 2.5"
    odds: float
    confidence: float


@dataclass
class BookingResult:
    """Result of a booking attempt."""
    success: bool
    booking_code: Optional[str] = None
    total_odds: float = 0.0
    num_selections: int = 0
    selections_summary: str = ""
    error: Optional[str] = None
    # What was actually booked per leg (may differ from the requested
    # match-winner pick if a safer cross-market outcome was substituted) —
    # lets callers log accurate pick history for settlement.
    leg_details: list = field(default_factory=list)
    # ISO8601 kickoff of the LATEST-starting leg in this ticket — lets a
    # rollover schedule its next pick right after this ticket's games
    # actually finish, instead of a blind 24h timer.
    latest_kickoff: Optional[str] = None


@dataclass
class RolloverPlan:
    """Tracks a multi-day rollover."""
    total_days: int
    target_odds: float
    current_day: int = 0
    history: list = field(default_factory=list)  # List of daily BookingResult dicts
    active: bool = False
    start_time: float = 0.0
    db_id: Optional[int] = None    # sporty_rollovers.id — lets restarts resume this plan
    next_run_at: float = 0.0       # epoch seconds of the next scheduled pick


# ── Prediction Engine ────────────────────────────────────────────────────────

class PredictionEngine:
    """Fetches odds and predictions, scores confidence.

    Uses a daily file cache to avoid burning API credits on repeated requests.
    Games are fetched once per day per sport and saved to .sporty_cache.json.
    """

    ODDS_API_BASE = "https://api.the-odds-api.com/v4"
    API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
    CACHE_FILE = os.path.join(os.path.dirname(__file__), ".sporty_cache.json")

    def __init__(self):
        self._mem_cache: dict = {}    # sport_key -> list[Game] (in-memory)
        self._pred_cache: dict = {}   # af_fixture_id -> (timestamp, data)
        self._pred_cache_ttl = 300    # 5 minutes for predictions
        self._daily_cache: Optional[dict] = None  # loaded from file
        self._fixtures_by_date_cache: dict = {}  # "YYYY-MM-DD" -> list[API-Football fixture dicts]

    def _pred_cache_valid(self, cache_entry) -> bool:
        if cache_entry is None:
            return False
        ts, _ = cache_entry
        return (time.time() - ts) < self._pred_cache_ttl

    def _load_daily_cache(self) -> dict:
        """Load the daily cache file. Returns empty dict if not today's cache."""
        if self._daily_cache is not None:
            return self._daily_cache

        if not os.path.exists(self.CACHE_FILE):
            self._daily_cache = {}
            return self._daily_cache

        try:
            with open(self.CACHE_FILE, "r") as f:
                data = json.load(f)

            # Check if cache is from today
            cache_date = data.get("date", "")
            today = date.today().isoformat()
            if cache_date != today:
                logger.info(f"Cache is from {cache_date}, today is {today} — cache expired")
                self._daily_cache = {}
                return self._daily_cache

            self._daily_cache = data
            sport_count = len(data.get("sports", {}))
            game_count = sum(len(v) for v in data.get("sports", {}).values())
            logger.info(f"Loaded daily cache: {game_count} games across {sport_count} sports")
            return self._daily_cache

        except Exception as e:
            logger.warning(f"Error loading cache: {e}")
            self._daily_cache = {}
            return self._daily_cache

    def _save_daily_cache(self):
        """Save the current cache to disk."""
        if not self._daily_cache:
            return
        try:
            with open(self.CACHE_FILE, "w") as f:
                json.dump(self._daily_cache, f)
        except Exception as e:
            logger.warning(f"Error saving cache: {e}")

    def _games_to_dicts(self, games: list[Game]) -> list[dict]:
        """Serialize Game objects to dicts for JSON storage."""
        return [asdict(g) for g in games]

    def _dicts_to_games(self, dicts: list[dict]) -> list[Game]:
        """Deserialize dicts back to Game objects."""
        games = []
        for d in dicts:
            games.append(Game(
                fixture_id=d["fixture_id"],
                home_team=d["home_team"],
                away_team=d["away_team"],
                league=d["league"],
                sport=d["sport"],
                sport_key=d["sport_key"],
                commence_time=d["commence_time"],
                odds_home=d.get("odds_home", 0),
                odds_draw=d.get("odds_draw", 0),
                odds_away=d.get("odds_away", 0),
                prob_home=d.get("prob_home", 0),
                prob_draw=d.get("prob_draw", 0),
                prob_away=d.get("prob_away", 0),
                api_football_prob=d.get("api_football_prob"),
                implied_prob=d.get("implied_prob", 0),
                confidence=d.get("confidence", 0),
                best_pick=d.get("best_pick", ""),
                best_odds=d.get("best_odds", 0),
                af_fixture_id=d.get("af_fixture_id"),
            ))
        return games

    async def get_available_sports(self) -> list:
        """List available sports from The Odds API (free, no credits)."""
        if not ODDS_API_KEY:
            logger.warning("ODDS_API_KEY not set")
            return []

        try:
            url = f"{self.ODDS_API_BASE}/sports"
            params = {"apiKey": ODDS_API_KEY}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.error(f"Odds API sports error: {resp.status}")
                        return []
                    data = await resp.json()
                    return [s for s in data if s.get("active", False)]
        except Exception as e:
            logger.error(f"Error fetching sports: {e}")
            return []

    async def get_odds(self, sport_key: str) -> list[Game]:
        """Fetch odds from The Odds API for a sport. Returns list of Game objects.
        Costs 1 API credit per call — uses daily file cache to avoid repeats.
        """
        if not ODDS_API_KEY:
            return []

        # 1. Check in-memory cache first
        if sport_key in self._mem_cache:
            logger.info(f"[cache hit] {sport_key}: {len(self._mem_cache[sport_key])} games (memory)")
            return self._mem_cache[sport_key]

        # 2. Check daily file cache
        cache = self._load_daily_cache()
        sports_cache = cache.get("sports", {})
        if sport_key in sports_cache:
            games = self._dicts_to_games(sports_cache[sport_key])
            self._mem_cache[sport_key] = games
            logger.info(f"[cache hit] {sport_key}: {len(games)} games (daily file)")
            return games

        # 3. No cache — fetch from API (costs 1 credit)
        try:
            url = f"{self.ODDS_API_BASE}/sports/{sport_key}/odds"
            params = {
                "apiKey": ODDS_API_KEY,
                "regions": "eu",
                "markets": "h2h",
                "oddsFormat": "decimal",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error(f"Odds API error {resp.status}: {body[:200]}")
                        return []

                    # Log remaining credits
                    remaining = resp.headers.get("x-requests-remaining", "?")
                    used = resp.headers.get("x-requests-used", "?")
                    logger.info(f"Odds API credits: {used} used, {remaining} remaining")

                    data = await resp.json()

            games = []
            for event in data:
                game = Game(
                    fixture_id=event.get("id", ""),
                    home_team=event.get("home_team", ""),
                    away_team=event.get("away_team", ""),
                    league=event.get("sport_title", ""),
                    sport=event.get("sport_key", "").split("_")[0],
                    sport_key=event.get("sport_key", ""),
                    commence_time=event.get("commence_time", ""),
                )

                # Average odds across all bookmakers
                h2h_odds = []
                for bookmaker in event.get("bookmakers", []):
                    for market in bookmaker.get("markets", []):
                        if market.get("key") == "h2h":
                            outcomes = {o["name"]: o["price"] for o in market.get("outcomes", [])}
                            h2h_odds.append(outcomes)

                if h2h_odds:
                    # Average across bookmakers
                    home_odds_list = [o.get(game.home_team, 0) for o in h2h_odds if game.home_team in o]
                    away_odds_list = [o.get(game.away_team, 0) for o in h2h_odds if game.away_team in o]
                    draw_odds_list = [o.get("Draw", 0) for o in h2h_odds if "Draw" in o]

                    game.odds_home = sum(home_odds_list) / len(home_odds_list) if home_odds_list else 0
                    game.odds_away = sum(away_odds_list) / len(away_odds_list) if away_odds_list else 0
                    game.odds_draw = sum(draw_odds_list) / len(draw_odds_list) if draw_odds_list else 0

                    # Implied probabilities (1/odds)
                    game.prob_home = (1 / game.odds_home) if game.odds_home > 0 else 0
                    game.prob_away = (1 / game.odds_away) if game.odds_away > 0 else 0
                    game.prob_draw = (1 / game.odds_draw) if game.odds_draw > 0 else 0

                    # Find best pick (highest implied prob)
                    picks = [
                        ("home", game.prob_home, game.odds_home),
                        ("away", game.prob_away, game.odds_away),
                    ]
                    if game.odds_draw > 0:
                        picks.append(("draw", game.prob_draw, game.odds_draw))

                    best = max(picks, key=lambda x: x[1])
                    game.best_pick = best[0]
                    game.implied_prob = best[1]
                    game.best_odds = best[2]

                    games.append(game)

            # Save to both in-memory and daily file cache
            self._mem_cache[sport_key] = games
            cache = self._load_daily_cache()
            if "date" not in cache:
                cache["date"] = date.today().isoformat()
            if "sports" not in cache:
                cache["sports"] = {}
            cache["sports"][sport_key] = self._games_to_dicts(games)
            self._daily_cache = cache
            self._save_daily_cache()
            logger.info(f"[API call] Fetched {len(games)} games for {sport_key} (saved to daily cache)")
            return games

        except Exception as e:
            logger.error(f"Error fetching odds for {sport_key}: {e}")
            return []

    async def _get_fixtures_by_date(self, date_str: str) -> list[dict]:
        """Fetch (and cache) all API-Football fixtures on a given date.

        Cached per date so multiple games kicking off the same day share
        one API call instead of one each.
        """
        if date_str in self._fixtures_by_date_cache:
            return self._fixtures_by_date_cache[date_str]
        if not API_FOOTBALL_KEY:
            return []

        try:
            url = f"{self.API_FOOTBALL_BASE}/fixtures"
            params = {"date": date_str}
            headers = {"x-apisports-key": API_FOOTBALL_KEY}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        return []
                    data = await resp.json()
            fixtures = data.get("response", [])
            self._fixtures_by_date_cache[date_str] = fixtures
            return fixtures
        except Exception as e:
            logger.error(f"Error fetching API-Football fixtures for {date_str}: {e}")
            return []

    @staticmethod
    def _within_window(commence_time: str, days_ahead: Optional[float]) -> bool:
        """True if a game's kickoff falls within [now, now + days_ahead].

        days_ahead=None means no filtering (default — matches old behavior).
        Lets "today" (days_ahead=1), "this weekend" (3), "this week" (7), or
        a long 2-week ticket (14) restrict which games are even considered,
        rather than scanning everything up for grabs regardless of date.
        """
        if days_ahead is None:
            return True
        try:
            game_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return False
        now = datetime.now(timezone.utc)
        return now <= game_dt <= now + timedelta(days=days_ahead)

    @staticmethod
    def _names_match(a: str, b: str) -> bool:
        a, b = a.lower().strip(), b.lower().strip()
        if not a or not b:
            return False
        if a in b or b in a:
            return True
        return SequenceMatcher(None, a, b).ratio() > 0.6

    async def resolve_af_fixture(self, home_team: str, away_team: str,
                                  commence_time: str) -> Optional[dict]:
        """Find the API-Football fixture dict matching these teams/kickoff date.

        The Odds API's `event.id` is NOT an API-Football fixture id — the two
        APIs have unrelated identifier spaces — so predictions/results must be
        resolved by team-name + date lookup instead of passing that id
        straight through.
        """
        if not commence_time or len(commence_time) < 10:
            return None
        date_str = commence_time[:10]  # ISO8601 "YYYY-MM-DD" prefix

        fixtures = await self._get_fixtures_by_date(date_str)
        for fx in fixtures:
            teams = fx.get("teams", {})
            f_home = teams.get("home", {}).get("name", "")
            f_away = teams.get("away", {}).get("name", "")
            if self._names_match(home_team, f_home) and self._names_match(away_team, f_away):
                return fx
        return None

    async def get_fixture_result(self, af_fixture_id: str) -> Optional[dict]:
        """Return the final result for a settled API-Football fixture.

        Returns {"status": "FT", "winner": "home"|"away"|"draw", "goals":
        {"home": int, "away": int}} once the match has finished (goals may
        be None if not reported), or None if not found / not finished yet.
        """
        if not API_FOOTBALL_KEY or not af_fixture_id:
            return None

        try:
            url = f"{self.API_FOOTBALL_BASE}/fixtures"
            params = {"id": af_fixture_id}
            headers = {"x-apisports-key": API_FOOTBALL_KEY}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            results = data.get("response", [])
            if not results:
                return None

            fx = results[0]
            status = fx.get("fixture", {}).get("status", {}).get("short", "")
            if status not in ("FT", "AET", "PEN"):
                return None  # not finished yet

            teams = fx.get("teams", {})
            home_winner = teams.get("home", {}).get("winner")
            away_winner = teams.get("away", {}).get("winner")
            if home_winner is True:
                winner = "home"
            elif away_winner is True:
                winner = "away"
            elif home_winner is False and away_winner is False:
                winner = "draw"
            else:
                winner = None

            goals = fx.get("goals", {})
            return {
                "status": status,
                "winner": winner,
                "goals": {"home": goals.get("home"), "away": goals.get("away")},
            }
        except Exception as e:
            logger.error(f"Error fetching fixture result for {af_fixture_id}: {e}")
            return None

    async def get_prediction(self, fixture_id: str) -> Optional[dict]:
        """Fetch prediction from API-Football for a fixture.
        Returns dict with win/draw/loss probabilities or None.
        """
        if not API_FOOTBALL_KEY:
            return None

        cached = self._pred_cache.get(fixture_id)
        if self._pred_cache_valid(cached):
            return cached[1]

        try:
            url = f"{self.API_FOOTBALL_BASE}/predictions"
            params = {"fixture": fixture_id}
            headers = {"x-apisports-key": API_FOOTBALL_KEY}

            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()

            results = data.get("response", [])
            if not results:
                return None

            pred = results[0].get("predictions", {})
            percent = pred.get("percent", {})

            result = {
                "home": float(percent.get("home", "0").rstrip("%")) / 100,
                "draw": float(percent.get("draw", "0").rstrip("%")) / 100,
                "away": float(percent.get("away", "0").rstrip("%")) / 100,
                "advice": pred.get("advice", ""),
            }

            self._pred_cache[fixture_id] = (time.time(), result)
            return result

        except Exception as e:
            logger.error(f"Error fetching prediction for fixture {fixture_id}: {e}")
            return None

    def _compute_confidence(self, game: Game) -> float:
        """Compute combined confidence score for a game's best pick.

        confidence = (api_football_prob * 0.6) + (implied_odds_prob * 0.4)
        + bonus if both sources agree
        """
        implied = game.implied_prob

        if game.api_football_prob is not None:
            combined = (game.api_football_prob * 0.6) + (implied * 0.4)

            # Bonus if both sources agree on the same pick
            if game.api_football_prob > 0.5 and implied > 0.5:
                combined += 0.05  # 5% agreement bonus

            return min(combined, 1.0)
        else:
            # Only odds data available — use implied probability directly
            return implied

    async def find_high_confidence_games(
        self,
        min_prob: float = 0.85,
        sport_keys: Optional[list[str]] = None,
        days_ahead: Optional[float] = None,
    ) -> list[Game]:
        """Find games with confidence >= min_prob across multiple sports.

        Args:
            min_prob: Minimum confidence threshold (default 0.85)
            sport_keys: List of sport keys to search. If None, uses defaults.
            days_ahead: Only consider games kicking off within this many days
                from now (e.g. 1 = today, 7 = this week). None = no limit.

        Returns:
            List of Game objects sorted by confidence descending
        """
        if sport_keys is None:
            # Dynamically fetch active sports, then pick the ones most likely
            # to have heavy favorites (major leagues + diverse sports).
            # Each sport costs 1 API credit — cap at ~25 to preserve credits.
            active_sports = await self.get_available_sports()
            if active_sports:
                # Prioritize: major soccer, basketball, tennis, ice hockey, others
                priority_prefixes = [
                    "soccer_epl", "soccer_spain_la_liga", "soccer_germany_bundesliga",
                    "soccer_italy_serie_a", "soccer_france_ligue_one",
                    "soccer_uefa_champs_league", "soccer_uefa_europa_league",
                    "soccer_uefa_europa_conference_league",
                    "basketball_nba", "basketball_euroleague", "basketball_ncaab",
                    "icehockey_nhl",
                    "soccer_portugal_primeira_liga", "soccer_netherlands_eredivisie",
                    "soccer_belgium_first_div", "soccer_turkey_super_league",
                    "soccer_brazil_campeonato", "soccer_argentina_primera_division",
                    "soccer_efl_champ", "soccer_fa_cup", "soccer_mexico_ligamx",
                    "soccer_australia_aleague", "soccer_usa_mls",
                    "soccer_conmebol_copa_libertadores",
                    "basketball_nbl",
                ]
                active_keys = {s["key"] for s in active_sports if s.get("active")}
                # Also pick any active tennis/cricket/rugby
                extra = [k for k in active_keys if k.startswith(("tennis_", "cricket_", "rugby"))]
                sport_keys = [k for k in priority_prefixes if k in active_keys]
                sport_keys += [k for k in extra if k not in sport_keys]
                # Cap to avoid burning credits
                sport_keys = sport_keys[:30]
                logger.info(f"Scanning {len(sport_keys)} sports (out of {len(active_keys)} active)")
            else:
                # Fallback to hardcoded popular leagues
                sport_keys = [
                    "soccer_epl", "soccer_spain_la_liga",
                    "soccer_germany_bundesliga", "soccer_italy_serie_a",
                    "soccer_france_ligue_one", "soccer_uefa_champs_league",
                    "soccer_uefa_europa_league", "basketball_nba",
                    "basketball_euroleague", "icehockey_nhl",
                ]

        all_games = []
        for sport_key in sport_keys:
            games = await self.get_odds(sport_key)
            all_games.extend(games)
            # Small delay to be kind to the API
            await asyncio.sleep(0.3)

        # Apply the date window before enrichment — saves API-Football calls
        # on games we'd filter out anyway.
        if days_ahead is not None:
            before = len(all_games)
            all_games = [g for g in all_games if self._within_window(g.commence_time, days_ahead)]
            logger.info(f"Date window <= {days_ahead}d: {len(all_games)}/{before} games remain")

        # Enrich with API-Football predictions if available.
        # The Odds API's fixture id is not an API-Football id, so resolve the
        # real fixture by team name + kickoff date first (cached per date).
        if API_FOOTBALL_KEY:
            for game in all_games:
                if not game.af_fixture_id:
                    af_fixture = await self.resolve_af_fixture(
                        game.home_team, game.away_team, game.commence_time
                    )
                    if af_fixture:
                        game.af_fixture_id = str(af_fixture.get("fixture", {}).get("id", "")) or None

                if game.af_fixture_id:
                    pred = await self.get_prediction(game.af_fixture_id)
                    if pred:
                        # Map best pick to API-Football probability
                        game.api_football_prob = pred.get(game.best_pick, 0)
                await asyncio.sleep(0.2)

        # Compute confidence scores
        for game in all_games:
            game.confidence = self._compute_confidence(game)

        # Filter by minimum probability
        confident = [g for g in all_games if g.confidence >= min_prob]
        confident.sort(key=lambda g: g.confidence, reverse=True)

        logger.info(f"Found {len(confident)} games with confidence >= {min_prob} "
                     f"(out of {len(all_games)} total)")
        return confident

    async def find_accumulator_legs(
        self,
        target_total_odds: float,
        min_prob: float = 0.85,
        max_legs: int = 30,
        sport_keys: Optional[list[str]] = None,
        days_ahead: Optional[float] = None,
    ) -> list[BetSelection]:
        """Build accumulator by greedily selecting high-confidence games
        until target total odds is reached.

        If not enough games at min_prob, progressively lowers the threshold
        (down to 0.60) to find more legs and reach the target.

        Args:
            target_total_odds: Target combined odds (e.g. 100)
            min_prob: Minimum confidence per leg
            max_legs: Maximum number of legs
            sport_keys: Sports to search
            days_ahead: Only consider games within this many days from now
                (e.g. 1 for a "today only" short ticket, 14 for a long one)

        Returns:
            List of BetSelection objects
        """
        # Try progressively lower thresholds to gather enough legs
        thresholds = [min_prob]
        for step in [0.80, 0.75, 0.70, 0.65, 0.60]:
            if step < min_prob:
                thresholds.append(step)

        all_candidate_games = []
        seen_ids = set()

        for threshold in thresholds:
            games = await self.find_high_confidence_games(
                min_prob=threshold, sport_keys=sport_keys, days_ahead=days_ahead
            )
            for g in games:
                if g.fixture_id not in seen_ids:
                    seen_ids.add(g.fixture_id)
                    all_candidate_games.append(g)

            # Check if we have enough games to potentially reach target
            test_odds = 1.0
            for g in sorted(all_candidate_games, key=lambda x: -x.confidence):
                if g.best_odds >= 1.05:
                    test_odds *= g.best_odds
            if test_odds >= target_total_odds:
                logger.info(f"Enough games at threshold {threshold:.0%} — "
                           f"potential odds: {test_odds:.2f}")
                break

        if not all_candidate_games:
            logger.warning("No games found for accumulator at any threshold")
            return []

        # Sort by confidence (highest first) — greedy selection
        all_candidate_games.sort(key=lambda g: -g.confidence)

        selections = []
        current_odds = 1.0

        for game in all_candidate_games:
            if len(selections) >= max_legs:
                break
            if current_odds >= target_total_odds:
                break

            # Skip if odds are too low (not worth adding)
            if game.best_odds < 1.05:
                continue

            selection_name = {
                "home": game.home_team,
                "away": game.away_team,
                "draw": "Draw",
            }.get(game.best_pick, game.best_pick)

            sel = BetSelection(
                game=game,
                market="match_winner",
                selection=selection_name,
                odds=round(game.best_odds, 2),
                confidence=game.confidence,
            )
            selections.append(sel)
            current_odds *= game.best_odds

        logger.info(f"Built accumulator: {len(selections)} legs, "
                     f"total odds: {current_odds:.2f} (target: {target_total_odds})")
        return selections

    async def daily_pick_candidates(
        self,
        target_odds: float = 1.50,
        min_prob: float = 0.85,
        sport_keys: Optional[list[str]] = None,
        days_ahead: Optional[float] = None,
    ) -> list[Game]:
        """High-confidence games ordered best-first for a single daily pick:
        closest to target odds, then highest confidence."""
        games = await self.find_high_confidence_games(
            min_prob=min_prob, sport_keys=sport_keys, days_ahead=days_ahead
        )

        if not games:
            return []

        # Filter for games near target odds (within +/- 0.3)
        candidates = [g for g in games if abs(g.best_odds - target_odds) <= 0.3]

        # If no games near target, take any high-confidence game
        if not candidates:
            candidates = games

        # Sort by closest to target odds, then by confidence
        candidates.sort(key=lambda g: (abs(g.best_odds - target_odds), -g.confidence))
        return candidates

    @staticmethod
    def selection_for(game: Game) -> BetSelection:
        selection_name = {
            "home": game.home_team,
            "away": game.away_team,
            "draw": "Draw",
        }.get(game.best_pick, game.best_pick)

        return BetSelection(
            game=game,
            market="match_winner",
            selection=selection_name,
            odds=round(game.best_odds, 2),
            confidence=game.confidence,
        )

    async def find_daily_pick(
        self,
        target_odds: float = 1.50,
        min_prob: float = 0.85,
        sport_keys: Optional[list[str]] = None,
        days_ahead: Optional[float] = None,
    ) -> Optional[BetSelection]:
        """Find single best game near target odds with highest confidence.

        Args:
            target_odds: Desired odds for the pick (e.g. 1.50)
            min_prob: Minimum confidence
            sport_keys: Sports to search
            days_ahead: Only consider games within this many days from now

        Returns:
            BetSelection or None
        """
        candidates = await self.daily_pick_candidates(
            target_odds=target_odds, min_prob=min_prob,
            sport_keys=sport_keys, days_ahead=days_ahead,
        )
        return self.selection_for(candidates[0]) if candidates else None


# ── SportyBet Booker (Direct API) ─────────────────────────────────────────────
#
# Discovered SportyBet internal API (via network interception):
#
# Base URL:          https://www.sportybet.com/api/ng
# Auth:              Cookie-based (accessToken from login session)
# Session file:      .sportybet_session.json (Playwright storage state)
#
# Key endpoints:
#   POST /orders/share                          → create booking code
#     Body: {"selections": [{"eventId","marketId","outcomeId","sportId"}]}
#     Returns: {"shareCode": "ABC123", "shareURL": "..."}
#
#   GET /factsCenter/configurableCustomEvents    → upcoming featured events
#     Params: sportId, tournamentId (optional)
#     Returns events with full markets + outcomes
#
#   GET /factsCenter/wapConfigurableMixHighlightEvents → highlight events
#   GET /factsCenter/wapConfigurableIndexLiveEvents    → live events
#   GET /factsCenter/orderedSportList                  → sport list
#   GET /factsCenter/wapPopularAndSportOption/v2       → tournaments per sport
#
#   POST /patron/accessToken  → login (encrypted body)
#   GET  /patron/account/info → verify logged in
#   GET  /pocket/v1/wallet/assetsInfo → wallet balance

class SportyBetBooker:
    """Books bets on SportyBet Nigeria via direct API calls.

    Uses session cookies from a Playwright login (sporty_discover.py)
    to authenticate against SportyBet's internal API.

    Key API:
      POST /api/ng/orders/share → creates booking code from selections
    """

    API_BASE = "https://www.sportybet.com/api/ng"
    SESSIONS_DIR = os.path.join(os.path.dirname(__file__), "sessions")

    # SportyBet sport IDs (sr:sport:X) — verified against the live
    # /factsCenter/orderedSportList response in sporty_api_map.json, not guessed.
    SPORT_IDS = {
        "soccer": "sr:sport:1",
        "basketball": "sr:sport:2",
        "tennis": "sr:sport:5",
        "icehockey": "sr:sport:4",
        "baseball": "sr:sport:3",
        "mma": "sr:sport:117",
        "cricket": "sr:sport:21",
        # SportyBet has one combined "Rugby" category (sr:sport:12) — no
        # separate league/union split — so both prefixes map to it.
        "rugbyleague": "sr:sport:12",
        "rugbyunion": "sr:sport:12",
        "rugby": "sr:sport:12",
    }

    def __init__(self, user_key: str):
        """user_key identifies whose SportyBet session this is — normally a
        Telegram user id (as a string) — so each user gets their own
        isolated session file under SESSIONS_DIR and never sees another
        user's cookies/booking session.
        """
        self.user_key = user_key
        os.makedirs(self.SESSIONS_DIR, exist_ok=True)
        self.SESSION_FILE = os.path.join(self.SESSIONS_DIR, f"{user_key}.json")

        self._cookies: dict = {}
        self._logged_in = False
        self._user_id = ""
        self._events_cache: dict = {}  # sport_id -> (timestamp, events)
        self._events_cache_ttl = 300   # 5 minutes
        self._load_session()

    def _load_session(self):
        """Load saved session cookies from file."""
        if not os.path.exists(self.SESSION_FILE):
            logger.info("No SportyBet session file found")
            return

        try:
            with open(self.SESSION_FILE, "r") as f:
                storage = json.load(f)

            for cookie in storage.get("cookies", []):
                if cookie.get("domain", "").endswith("sportybet.com"):
                    self._cookies[cookie["name"]] = cookie["value"]

            self._user_id = self._cookies.get("userId", "")
            if self._cookies.get("accessToken"):
                self._logged_in = True
                logger.info(f"SportyBet session loaded (userId: {self._user_id})")
            else:
                logger.warning("SportyBet session file exists but no accessToken")
        except Exception as e:
            logger.warning(f"Error loading SportyBet session: {e}")

    def _headers(self) -> dict:
        """Build request headers with session cookies."""
        cookie_str = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        return {
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://www.sportybet.com/ng/m/sports",
            "Cookie": cookie_str,
        }

    async def verify_session(self) -> bool:
        """Check if the saved session can still create booking codes.

        Uses the events endpoint (which is more permissive than account info)
        to verify that the session cookies are still accepted.
        """
        if not self._logged_in:
            return False

        try:
            # Test with events endpoint — it requires auth but is more lenient
            # than /patron/account/info (which requires full accessToken validity)
            url = f"{self.API_BASE}/factsCenter/wapConfigurableEventsByOrder"
            body = {
                "productId": 3,
                "sportId": "sr:sport:1",
                "order": 0,
                "pageNum": 1,
                "pageSize": 1,
                "userId": self._user_id,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=self._headers(), json=body,
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("bizCode") == 10000:
                            logger.info("SportyBet session is valid")
                            return True
            self._logged_in = False
            logger.warning("SportyBet session expired")
            return False
        except Exception as e:
            logger.warning(f"Session verification failed: {e}")
            return False

    async def get_all_events(self, sport_id: str = "sr:sport:1",
                             page_size: int = 100) -> list[dict]:
        """Get ALL upcoming events from SportyBet with full market data.

        Results are cached for 5 minutes to avoid repeated API calls.

        Args:
            sport_id: SportyBet sport ID (e.g. "sr:sport:1" for football)
            page_size: Number of events per page (max 100)

        Returns:
            List of event dicts with eventId, homeTeamName, awayTeamName, markets
        """
        # Check cache
        cached = self._events_cache.get(sport_id)
        if cached:
            ts, events = cached
            if (time.time() - ts) < self._events_cache_ttl:
                return events

        try:
            url = f"{self.API_BASE}/factsCenter/wapConfigurableEventsByOrder"
            body = {
                "productId": 3,
                "sportId": sport_id,
                "order": 0,
                "pageNum": 1,
                "pageSize": page_size,
                "userId": self._user_id,
                "withTwoUpMarket": True,
                "withOneUpMarket": True,
            }

            all_events = []
            async with aiohttp.ClientSession() as session:
                # Fetch first page
                async with session.post(url, headers=self._headers(), json=body,
                                        timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        logger.error(f"SportyBet events API error: {resp.status}")
                        return []
                    data = await resp.json()
                    d = data.get("data", {})

                    # Events are nested inside tournaments
                    tournaments = d.get("tournaments", [])
                    for t in tournaments:
                        events = t.get("events", [])
                        all_events.extend(events)

                    more = d.get("moreEvents", False)

                # Fetch additional pages if needed
                page = 2
                while more and page <= 5:  # Cap at 5 pages (500 events)
                    body["pageNum"] = page
                    async with session.post(url, headers=self._headers(), json=body,
                                            timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        if resp.status != 200:
                            break
                        data = await resp.json()
                        d = data.get("data", {})
                        tournaments = d.get("tournaments", [])
                        for t in tournaments:
                            all_events.extend(t.get("events", []))
                        more = d.get("moreEvents", False)
                    page += 1
                    await asyncio.sleep(0.3)

            logger.info(f"Fetched {len(all_events)} events from SportyBet ({sport_id})")
            self._events_cache[sport_id] = (time.time(), all_events)
            return all_events

        except Exception as e:
            logger.error(f"Error fetching SportyBet events: {e}")
            return []

    async def find_event(self, home_team: str, away_team: str,
                          sport: str = "soccer", commence_time: str = "") -> Optional[dict]:
        """Find a match on SportyBet by team names.

        Fetches all upcoming events and picks the best-scoring match by
        team name + kickoff-time proximity (see `_best_matching_event`).

        Returns:
            Event dict with eventId, markets, etc. or None
        """
        sport_id = self.SPORT_IDS.get(sport)
        if sport_id is None:
            logger.warning(f"No SportyBet sport-id mapping for '{sport}' — skipping "
                            f"(was silently defaulting to soccer's id before, which never matched)")
            return None

        # Fetch all events for this sport
        all_events = await self.get_all_events(sport_id)

        event = self._best_matching_event(home_team, away_team, commence_time, all_events)
        if event is None:
            logger.warning(f"Match not found on SportyBet: {home_team} vs {away_team}")
        return event

    def _fuzzy_match(self, search: str, candidate: str) -> bool:
        """Fuzzy match team names.

        Handles common mismatches like:
        - "South Carolina Upstate Spartans" vs "SC Upstate Spartans"
        - "Wright St Raiders" vs "Wright State Raiders"
        - "Mansfield Town" vs "Mansfield"
        - "AVS Futebol SAD" vs "AVS"
        """
        s = search.lower().strip()
        c = candidate.lower().strip()

        # Direct substring match
        if s in c or c in s:
            return True

        # Handle name reversal for tennis: "FirstName LastName" vs "LastName, FirstName"
        # Also strip "(Srl)", "(SRL)" suffixes from SportyBet
        c_clean = c.replace("(srl)", "").replace("(srl)", "").strip()
        if "," in c_clean:
            parts = c_clean.split(",", 1)
            reversed_name = f"{parts[1].strip()} {parts[0].strip()}"
            if s in reversed_name or reversed_name in s:
                return True
            ratio = SequenceMatcher(None, s, reversed_name).ratio()
            if ratio > 0.65:
                return True

        # Strip common suffixes for comparison
        suffixes = [" fc", " sc", " cf", " afc", " sfc", " bc",
                    " united", " city", " town", " rovers", " wanderers",
                    " athletic", " sporting", " albion",
                    " eagles", " hawks", " bears", " bulldogs", " panthers",
                    " tigers", " lions", " warriors", " spartans", " raiders",
                    " jaguars", " wolves", " hornets", " cougars", " bobcats"]
        s_stripped = s
        c_stripped = c
        for suffix in suffixes:
            if s_stripped.endswith(suffix):
                s_stripped = s_stripped[:-len(suffix)].strip()
            if c_stripped.endswith(suffix):
                c_stripped = c_stripped[:-len(suffix)].strip()

        if s_stripped and c_stripped and (s_stripped in c_stripped or c_stripped in s_stripped):
            return True

        # Word overlap — if most significant words match, consider it a match
        s_words = set(s.split()) - {"fc", "sc", "cf", "afc", "bc", "the", "de", "vs", "of"}
        c_words = set(c.split()) - {"fc", "sc", "cf", "afc", "bc", "the", "de", "vs", "of"}
        if s_words and c_words:
            overlap = s_words & c_words
            smaller = min(len(s_words), len(c_words))
            if smaller > 0 and len(overlap) / smaller >= 0.5:
                return True

        # Common abbreviations
        abbreviations = {
            "manchester united": ["man utd", "man united", "manchester utd"],
            "manchester city": ["man city"],
            "tottenham hotspur": ["tottenham", "spurs"],
            "wolverhampton wanderers": ["wolves", "wolverhampton"],
            "west ham united": ["west ham"],
            "newcastle united": ["newcastle"],
            "nottingham forest": ["nott'm forest", "nottm forest", "nott forest"],
            "paris saint germain": ["psg", "paris sg", "paris saint-germain"],
            "bayern munich": ["bayern munchen", "fc bayern", "bayern münchen"],
            "borussia dortmund": ["dortmund", "bvb", "b. dortmund"],
            "atletico madrid": ["atl. madrid", "atletico", "atl madrid"],
            "fc barcelona": ["barcelona", "barca"],
            "inter milan": ["inter", "internazionale", "fc internazionale"],
            "south carolina": ["sc"],
            "north carolina": ["nc"],
        }

        for full_name, abbrevs in abbreviations.items():
            variants = [full_name] + abbrevs
            s_match = any(v in s or s in v for v in variants)
            c_match = any(v in c or c in v for v in variants)
            if s_match and c_match:
                return True

        # Abbreviation expansion: "St" -> "State", "Utd" -> "United"
        expansions = {"st": "state", "utd": "united", "intl": "international",
                      "univ": "university"}
        s_expanded = s
        c_expanded = c
        for abbr, full in expansions.items():
            s_expanded = s_expanded.replace(f" {abbr} ", f" {full} ").replace(f" {abbr}", f" {full}")
            c_expanded = c_expanded.replace(f" {abbr} ", f" {full} ").replace(f" {abbr}", f" {full}")

        if s_expanded in c_expanded or c_expanded in s_expanded:
            return True

        ratio = SequenceMatcher(None, s, c).ratio()
        if ratio > 0.65:
            return True

        # Also check expanded names ratio
        ratio2 = SequenceMatcher(None, s_expanded, c_expanded).ratio()
        return ratio2 > 0.65

    # Minimum combined score to accept a candidate event as a real match.
    _MATCH_SCORE_THRESHOLD = 0.6
    # If the top two candidates' scores are closer than this, treat it as
    # ambiguous and refuse to pick one — safer to drop the leg.
    _MATCH_AMBIGUITY_MARGIN = 0.05

    def _score_event_match(self, home_team: str, away_team: str,
                            commence_time: str, event: dict) -> float:
        """Score how likely `event` is the real match for the given teams/kickoff.

        Combines fuzzy team-name matching with kickoff-time proximity so that
        two similarly-named teams (or the same team's other fixture that day)
        don't get silently booked as the wrong match.

        Score components:
          - 0.5 if home+away names fuzzy-match, else 0
          - up to 0.5 based on how close the kickoff times are (full credit
            within 30 min, scaling down to 0 credit at 6+ hours apart)
          - if commence_time is missing/unparseable on either side, time
            scoring is skipped and the name-match component is doubled, so a
            plain name match without a time signal still resolves to ~1.0
            (matches old behavior) rather than being unfairly penalized.
        """
        home = event.get("homeTeamName", "")
        away = event.get("awayTeamName", "")
        names_match = self._fuzzy_match(home_team, home) and self._fuzzy_match(away_team, away)
        if not names_match:
            return 0.0

        event_start_ms = event.get("estimateStartTime")
        if not commence_time or not event_start_ms:
            return 1.0  # no time signal available — fall back to name-only match

        try:
            game_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
            event_dt = datetime.fromtimestamp(event_start_ms / 1000, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return 1.0

        diff_hours = abs((game_dt - event_dt).total_seconds()) / 3600.0
        if diff_hours <= 0.5:
            time_score = 0.5
        elif diff_hours >= 6.0:
            time_score = 0.0
        else:
            time_score = 0.5 * (1 - (diff_hours - 0.5) / 5.5)

        return 0.5 + time_score

    def _best_matching_event(self, home_team: str, away_team: str,
                              commence_time: str, events: list[dict]) -> Optional[dict]:
        """Pick the best-scoring SportyBet event for a team pair, or None.

        Scores every candidate instead of stopping at the first fuzzy hit,
        and refuses to pick one at all if the match is weak or ambiguous
        (two candidates scoring nearly the same) — better to skip a leg than
        book the wrong fixture.
        """
        scored = []
        for event in events:
            score = self._score_event_match(home_team, away_team, commence_time, event)
            if score > 0:
                scored.append((score, event))

        if not scored:
            return None

        scored.sort(key=lambda x: -x[0])
        best_score, best_event = scored[0]

        if best_score < self._MATCH_SCORE_THRESHOLD:
            logger.warning(
                f"Best SportyBet match for {home_team} vs {away_team} scored "
                f"only {best_score:.2f} — below threshold, skipping"
            )
            return None

        if len(scored) > 1:
            second_score = scored[1][0]
            if (best_score - second_score) < self._MATCH_AMBIGUITY_MARGIN:
                logger.warning(
                    f"Ambiguous SportyBet match for {home_team} vs {away_team}: "
                    f"top two candidates scored {best_score:.2f} and {second_score:.2f} "
                    f"— skipping to avoid booking the wrong fixture"
                )
                return None

        return best_event

    # Known primary moneyline market names across sports — checked first so a
    # secondary/derivative market (e.g. soccer's "1X2 - 1UP") with the same
    # outcome labels doesn't get picked over the real full-match market.
    _WINNER_MARKET_NAMES = {"1x2", "winner", "winner (incl. overtime)", "match winner", "12"}

    def _find_winner_market(self, event: dict, pick: str) -> Optional[tuple[str, str]]:
        """Resolve (market_id, outcome_id) for a home/draw/away pick on this event.

        SportyBet's moneyline market id/name AND outcome ids both vary by
        sport — soccer uses market id 1 "1X2" with outcome ids 1/2/3 for
        Home/Draw/Away, while basketball/tennis use a different market
        ("Winner", ids 219/186) with only 2 outcomes, ids 4/5 for Home/Away.
        Hardcoding soccer's ids caused "invalid event data, no market there"
        for every non-soccer leg. Matching by the outcome's own `desc` label
        instead works uniformly across sports.
        """
        desc_wanted = pick.strip().lower()
        if desc_wanted not in ("home", "draw", "away"):
            return None

        markets = event.get("markets", [])

        # Pass 1: known primary market names
        for m in markets:
            name = (m.get("name") or m.get("desc") or "").strip().lower()
            if name in self._WINNER_MARKET_NAMES:
                for o in m.get("outcomes", []):
                    if (o.get("desc") or "").strip().lower() == desc_wanted:
                        return str(m.get("id")), str(o.get("id"))

        # Pass 2: fall back to any market with a matching outcome label
        for m in markets:
            for o in m.get("outcomes", []):
                if (o.get("desc") or "").strip().lower() == desc_wanted:
                    return str(m.get("id")), str(o.get("id"))

        return None

    # Odds window for the "safest" cross-market outcome — reverted to the
    # user's standing 1.2-1.67 preference (~60-83% assurance) after a brief
    # narrowing to 1.01-1.25 for one 80-99%-confidence request.
    _SAFE_PICK_MIN_ODDS = 1.2
    _SAFE_PICK_MAX_ODDS = 1.67

    # Only markets whose booking payload shape is verified against real
    # SportyBet data (see _classify_pick and SportyBot._grade_pick for the
    # settlement side). Asian Handicap, Correct Score, minute-interval, and
    # combo markets are NOT in here because they never appear anywhere in
    # this API's event-listing response at all (verified by scanning 500
    # live events across the richest available matches — only 16 distinct
    # market names ever show up) — they'd need a different, undiscovered
    # SportyBet endpoint, not just an allowlist change. 1st-half variants,
    # "1UP"/"Never Down" novelty markets stay excluded since their payload
    # shape hasn't been separately verified.
    _SAFE_MARKET_NAMES = {
        "1x2", "winner", "winner (incl. overtime)", "double chance", "gg/ng",
        "over/under", "handicap", "corners - over/under", "home o/u", "away o/u",
        "1x2 - 1up", "1x2 - 2up", "1x2 - never down", "double chance - 1up",
    }

    # Preference for "Over" outcomes over "Under" when they're close: applied
    # as a penalty to Under's odds for comparison purposes only (the stored/
    # booked odds are always the real ones) — Under still wins when it's
    # genuinely the safer pick by more than this margin, which is the "rare
    # cases" where Under gets picked anyway.
    _UNDER_BIAS = 1.08

    def find_safest_selection(self, event: dict) -> Optional[dict]:
        """Scan this event's verified-safe markets and return the safest outcome.

        Rather than only ever betting the match-winner (1X2/Winner) market,
        this also considers Double Chance, GG/NG (BTTS), and Over/Under, and
        returns whichever outcome has the lowest real odds in [MIN, MAX] —
        the pick SportyBet's own live odds say is most likely to win. Over
        outcomes get a slight preference over Under ones on close calls
        (see _UNDER_BIAS).

        Returns a dict with market_id, market_name, outcome_id, outcome_desc,
        odds, specifier (None if not applicable) — or None if nothing on the
        event qualifies.
        """
        best = None
        best_effective_odds = None
        for m in event.get("markets", []):
            market_name = m.get("name") or m.get("desc") or ""
            if market_name.strip().lower() not in self._SAFE_MARKET_NAMES:
                continue
            for o in m.get("outcomes", []):
                try:
                    odds = float(o.get("odds", 0))
                except (TypeError, ValueError):
                    continue
                if odds < self._SAFE_PICK_MIN_ODDS or odds > self._SAFE_PICK_MAX_ODDS:
                    continue

                desc = (o.get("desc") or "").strip().lower()
                effective_odds = odds * self._UNDER_BIAS if desc.startswith("under") else odds

                if best is None or effective_odds < best_effective_odds:
                    best_effective_odds = effective_odds
                    best = {
                        "market_id": str(m.get("id")),
                        "market_name": market_name,
                        "outcome_id": str(o.get("id")),
                        "outcome_desc": o.get("desc", ""),
                        "odds": odds,
                        "specifier": m.get("specifier"),
                    }
        return best

    @staticmethod
    def _classify_pick(market_name: str, outcome_desc: str) -> str:
        """Turn a (market_name, outcome_desc) pair into a compact code the
        settlement logic (SportyBot.settle_picks) knows how to grade against
        a real match result — e.g. "over_1.5", "btts_yes", "dc_1x".

        Falls back to "unknown" for anything outside the verified-safe
        market set — such picks still get booked but are left unsettled
        rather than risk grading them wrong.
        """
        name = (market_name or "").strip().lower()
        desc = (outcome_desc or "").strip().lower()

        if name in ("1x2", "winner", "winner (incl. overtime)"):
            if desc in ("home", "draw", "away"):
                return desc

        elif name == "double chance":
            mapping = {
                "home or draw": "dc_1x",
                "home or away": "dc_12",
                "draw or away": "dc_x2",
            }
            if desc in mapping:
                return mapping[desc]

        elif name == "gg/ng":
            if desc in ("yes", "no"):
                return f"btts_{desc}"

        elif name == "over/under":
            m = re.match(r"(over|under)\s+([\d.]+)", desc)
            if m:
                return f"{m.group(1)}_{m.group(2)}"

        elif name == "handicap":
            m = re.match(r"(home|draw|away)\s*\((-?\d+):(-?\d+)\)", desc)
            if m:
                side, h, a = m.groups()
                return f"hcp_{h}:{a}_{side}"

        elif name == "corners - over/under":
            m = re.match(r"(over|under)\s+([\d.]+)", desc)
            if m:
                return f"corners_{m.group(1)}_{m.group(2)}"

        elif name == "home o/u":
            m = re.match(r"(over|under)\s+([\d.]+)", desc)
            if m:
                return f"home_ou_{m.group(1)}_{m.group(2)}"

        elif name == "away o/u":
            m = re.match(r"(over|under)\s+([\d.]+)", desc)
            if m:
                return f"away_ou_{m.group(1)}_{m.group(2)}"

        elif name == "1x2 - 1up":
            if desc in ("home", "draw", "away"):
                return f"1up_{desc}"

        elif name == "1x2 - 2up":
            if desc in ("home", "draw", "away"):
                return f"2up_{desc}"

        elif name == "1x2 - never down":
            if desc in ("home", "draw", "away"):
                return f"neverdown_{desc}"

        elif name == "double chance - 1up":
            mapping = {"home or draw": "dc1up_1x", "home or away": "dc1up_12", "draw or away": "dc1up_x2"}
            if desc in mapping:
                return mapping[desc]

        return "unknown"

    async def create_booking_code(self, selections: list[dict]) -> Optional[str]:
        """Create a booking code via SportyBet API.

        Args:
            selections: List of dicts with keys:
                - eventId: e.g. "sr:match:68417318"
                - marketId: e.g. "1" (1X2)
                - outcomeId: e.g. "1" (Home), "2" (Draw), "3" (Away)
                - sportId: e.g. "sr:sport:1"

        Returns:
            Booking code string (e.g. "XYJEHH") or None
        """
        if not self._logged_in:
            logger.error("Cannot create booking code: not logged in")
            return None

        try:
            url = f"{self.API_BASE}/orders/share"
            body = {"selections": selections}

            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=self._headers(), json=body,
                                        timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    data = await resp.json()

                    if data.get("bizCode") == 10000:
                        result = data.get("data", {})
                        code = result.get("shareCode", "")
                        share_url = result.get("shareURL", "")
                        logger.info(f"Booking code created: {code} ({share_url})")
                        return code
                    else:
                        msg = data.get("message", "Unknown error")
                        logger.error(f"Booking code creation failed: {msg}")
                        return None

        except Exception as e:
            logger.error(f"Error creating booking code: {e}")
            return None

    async def book_selections(self, selections: list[BetSelection]) -> BookingResult:
        """Create a booking code for the given selections via direct API.

        Maps team names from The Odds API to SportyBet event IDs,
        then calls the /orders/share endpoint to generate a code.

        Args:
            selections: List of BetSelection objects

        Returns:
            BookingResult with booking code or error
        """
        if not selections:
            return BookingResult(success=False, error="No selections provided")

        # Build summary
        total_odds = 1.0
        summary_lines = []
        for sel in selections:
            total_odds *= sel.odds
            summary_lines.append(
                f"{sel.game.home_team} vs {sel.game.away_team}: "
                f"{sel.selection} @ {sel.odds:.2f}"
            )
        summary = "\n".join(summary_lines)
        all_total_odds = total_odds  # Odds for all requested legs

        if not self._logged_in:
            return BookingResult(
                success=False,
                total_odds=round(total_odds, 2),
                num_selections=len(selections),
                selections_summary=summary,
                error=(
                    "Not logged in to SportyBet. "
                    "Run: python3 sporty_discover.py (one-time login to save session)"
                ),
            )

        # Verify session is still valid
        if not await self.verify_session():
            self._logged_in = False
            return BookingResult(
                success=False,
                total_odds=round(total_odds, 2),
                num_selections=len(selections),
                selections_summary=summary,
                error="SportyBet session expired. Run sporty_discover.py to re-login.",
            )

        # Pre-fetch events for all sports needed (avoids repeated API calls)
        needed_sports = set()
        for sel in selections:
            sport = sel.game.sport or "soccer"
            sport_id = self.SPORT_IDS.get(sport)
            if sport_id is None:
                logger.warning(f"No SportyBet sport-id mapping for '{sport}' — "
                                f"{sel.game.home_team} vs {sel.game.away_team} can't be matched")
                continue
            needed_sports.add(sport_id)

        all_sporty_events = []
        for sport_id in needed_sports:
            events = await self.get_all_events(sport_id)
            all_sporty_events.extend(events)
            await asyncio.sleep(0.3)

        logger.info(f"Pre-fetched {len(all_sporty_events)} SportyBet events "
                     f"across {len(needed_sports)} sports")

        # Map each selection to a SportyBet event
        api_selections = []
        booked_lines = []  # what was actually booked, for the real summary
        leg_details = []   # structured per-leg info for pick-history logging
        matched = 0
        booked_odds = 1.0

        for sel in selections:
            home_team = sel.game.home_team
            away_team = sel.game.away_team

            # Search in pre-fetched events — best-scoring match, not first hit
            event = self._best_matching_event(
                home_team, away_team, sel.game.commence_time, all_sporty_events
            )

            if not event:
                logger.warning(
                    f"Could not find on SportyBet: "
                    f"{home_team} vs {away_team}"
                )
                continue

            # Prefer the safest outcome across the event's verified-safe
            # markets (Over/Under, GG/NG, Double Chance), not just
            # match-winner — falling back to the match-winner pick only if
            # nothing else on the event qualifies as safe.
            safe = self.find_safest_selection(event)
            if safe:
                market_id, outcome_id = safe["market_id"], safe["outcome_id"]
                specifier = safe.get("specifier")
                leg_odds = safe["odds"]
                leg_desc = f"{safe['market_name']}: {safe['outcome_desc']}"
                pick_kind = self._classify_pick(safe["market_name"], safe["outcome_desc"])
            else:
                pick = sel.game.best_pick if hasattr(sel.game, "best_pick") else "home"
                resolved = self._find_winner_market(event, pick)
                if resolved is None:
                    logger.warning(
                        f"No bookable outcome for {home_team} vs {away_team} "
                        f"(pick={pick}) — skipping leg"
                    )
                    continue
                market_id, outcome_id = resolved
                specifier = None
                leg_odds = sel.odds
                leg_desc = sel.selection
                pick_kind = pick

            sport_id = event.get("sport", {}).get("id", "sr:sport:1")

            api_sel = {
                "eventId": event["eventId"],
                "marketId": market_id,
                "outcomeId": outcome_id,
                "sportId": sport_id,
            }
            if specifier:
                api_sel["specifier"] = specifier
            api_selections.append(api_sel)

            booked_lines.append(f"{home_team} vs {away_team}: {leg_desc} @ {leg_odds:.2f}")
            leg_details.append({
                "game": sel.game,
                "selection": leg_desc,
                "pick_kind": pick_kind,
                "odds": leg_odds,
                "confidence": sel.confidence,
            })
            matched += 1
            booked_odds *= leg_odds

        if not api_selections:
            return BookingResult(
                success=False,
                total_odds=round(total_odds, 2),
                num_selections=len(selections),
                selections_summary=summary,
                error="None of the selected matches were found on SportyBet",
            )

        # Create booking code
        code = await self.create_booking_code(api_selections)

        # Rebuild the summary from what was actually booked (may differ from
        # the original match-winner picks now that safer markets are used).
        summary = "\n".join(booked_lines)
        unmatched = len(selections) - matched
        if unmatched > 0:
            summary += (f"\n\n({matched}/{len(selections)} games found on SportyBet"
                        f" — {unmatched} not available)")

        if code:
            return BookingResult(
                success=True,
                booking_code=code,
                total_odds=round(booked_odds, 2),
                num_selections=matched,
                selections_summary=summary,
                leg_details=leg_details,
            )
        else:
            return BookingResult(
                success=False,
                total_odds=round(booked_odds, 2),
                num_selections=matched,
                selections_summary=summary,
                error="Matched events but failed to generate booking code",
                leg_details=leg_details,
            )

    async def _fill_otp(self, page, code: str):
        """Best-effort 2FA/OTP entry.

        SportyBet's exact verification-code markup hasn't been captured
        against a live 2FA screen, so this handles both common OTP UI
        patterns generically: several single-digit boxes, or one text input
        for the whole code — then tries Enter and a Verify/Confirm/Submit
        button. Only ever runs after a verification-code screen was actually
        detected (see login_interactive), so a wrong guess here just fails
        to submit rather than clicking something on an unrelated page.
        """
        code = "".join(ch for ch in code if ch.isdigit())
        if not code:
            return

        boxes = await page.query_selector_all('input[maxlength="1"]')
        visible_boxes = []
        for b in boxes:
            try:
                if await b.is_visible():
                    visible_boxes.append(b)
            except Exception:
                continue

        if len(visible_boxes) >= len(code):
            for i, digit in enumerate(code):
                try:
                    await visible_boxes[i].fill(digit)
                except Exception:
                    pass
        else:
            candidates = await page.query_selector_all(
                'input[type="text"], input[type="tel"], input[type="number"], input[inputmode="numeric"]'
            )
            for inp in candidates:
                try:
                    if not await inp.is_visible():
                        continue
                    maxlen = await inp.get_attribute("maxlength")
                    if maxlen is None or int(maxlen) >= len(code):
                        await inp.fill(code)
                        break
                except Exception:
                    continue

        await asyncio.sleep(1)
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass
        try:
            await page.evaluate("""() => {
                const candidates = Array.from(document.querySelectorAll('button'));
                const btn = candidates.find(b => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    return t === 'verify' || t === 'confirm' || t === 'submit' || t === 'continue';
                });
                if (btn) btn.click();
            }""")
        except Exception:
            pass

    async def login_interactive(self, phone: Optional[str] = None, password: Optional[str] = None,
                                 otp_callback: Optional[Callable[[], Awaitable[str]]] = None):
        """Open a visible browser for manual login (saves session cookies).

        This is the only way to authenticate — SportyBet uses Cloudflare
        + reCAPTCHA which blocks headless automation, so a human still needs
        to be at this machine's screen to solve it when it appears.

        Args:
            phone, password: SportyBet credentials to pre-fill. Defaults to
                the operator's own (config.SPORTYBET_PHONE/PASSWORD) only
                when not supplied — every other user must pass their own.
            otp_callback: async, no-args callable returning the 2FA/OTP code
                as a string once the user supplies it (e.g. via Telegram).
                Called at most once, only if a verification-code screen is
                actually detected after submitting phone/password.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright not installed. Run: pip install playwright && playwright install chromium"
            )

        phone = phone if phone is not None else SPORTYBET_PHONE
        password = password if password is not None else SPORTYBET_PASSWORD

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 430, "height": 932},
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
        )
        page = await context.new_page()

        logger.info("Opening SportyBet for manual login...")
        try:
            await page.goto("https://www.sportybet.com/ng/m", timeout=60000)
        except Exception:
            pass
        await asyncio.sleep(5)

        # Pre-fill credentials
        try:
            for selector in ['div.m-btn-login', '[data-op="nav-login"]']:
                btn = await page.query_selector(selector)
                if btn:
                    await btn.click()
                    await asyncio.sleep(3)
                    break

            # Wait for login form
            for _ in range(10):
                await asyncio.sleep(2)
                inner = await page.evaluate("""() => {
                    const el = document.querySelector('#popupLogin');
                    if (!el) return 'missing';
                    const html = el.innerHTML.trim();
                    return (html === '' || html === '<!---->') ? 'empty' : 'ready';
                }""")
                if inner == "ready":
                    break

            if phone:
                p = phone[1:] if phone.startswith("0") else phone
                pi = await page.query_selector('[data-op="login-phone"] input[type="tel"]')
                if pi:
                    await pi.fill(p)

            if password:
                pwi = await page.query_selector('[data-op="login-pswd"] input[type="password"]')
                if pwi:
                    await pwi.fill(password)

            await asyncio.sleep(1)
            await page.evaluate("""() => {
                // Dismiss overlays that block clicks
                document.querySelectorAll('.layout.mask, .es-dialog-mask').forEach(el => {
                    el.style.display = 'none';
                    el.style.pointerEvents = 'none';
                });
                const btn = document.querySelector('button[data-op="login-btn"]');
                if (btn) {
                    btn.disabled = false;
                    btn.classList.remove('is-disabled');
                    btn.click();
                }
            }""")
        except Exception as e:
            logger.warning(f"Could not pre-fill login: {e}")

        # Wait for login (poll up to 5 minutes), watching for a 2FA/OTP
        # prompt along the way — SportyBet may ask for a verification code
        # after phone/password are accepted.
        logged_in = False
        otp_attempted = False
        for _ in range(60):
            await asyncio.sleep(5)
            try:
                is_logged = await page.evaluate("""() => {
                    const loginBtn = document.querySelector('div.m-btn-login');
                    return !loginBtn || loginBtn.offsetParent === null;
                }""")
                if is_logged:
                    logged_in = True
                    break

                if not otp_attempted and otp_callback is not None:
                    needs_otp = await page.evaluate("""() => {
                        const text = document.body.innerText.toLowerCase();
                        return text.includes('verification code') || text.includes('enter otp') ||
                               text.includes('one-time password') || text.includes('enter the code') ||
                               text.includes('otp sent') || text.includes('enter code');
                    }""")
                    if needs_otp:
                        otp_attempted = True
                        logger.info("2FA/OTP prompt detected during login")
                        try:
                            code = await otp_callback()
                            if code:
                                await self._fill_otp(page, code)
                        except Exception as e:
                            logger.error(f"OTP entry failed: {e}")
            except Exception:
                continue

        if logged_in:
            storage = await context.storage_state()
            with open(self.SESSION_FILE, "w") as f:
                json.dump(storage, f)
            logger.info("Login successful! Session saved.")
            # Reload cookies
            self._load_session()

        await browser.close()
        await pw.stop()
        return logged_in

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in


# ── SportyBot Orchestrator ───────────────────────────────────────────────────

class SportyBot:
    """Main orchestrator for SportyBet predictions and booking."""

    def __init__(self, user_key: str, engine: Optional["PredictionEngine"] = None):
        """user_key identifies whose bot this is — a Telegram user id (as a
        string) — giving each user their own SportyBet session, rollover,
        and pick history. `engine` is shared across users by
        SportyBotManager (odds/prediction data isn't user-specific, so
        there's no reason to duplicate the daily Odds-API cache per user).
        """
        self.user_key = user_key
        self.engine = engine if engine is not None else PredictionEngine()
        self.booker = SportyBetBooker(user_key)
        self.rollover: Optional[RolloverPlan] = None
        self._rollover_task: Optional[asyncio.Task] = None
        self._notify_callback = None
        self._was_session_valid = self.booker.is_logged_in

    def set_notify_callback(self, callback):
        """Set async callback for Telegram notifications."""
        self._notify_callback = callback

    async def _notify(self, message: str):
        if self._notify_callback:
            try:
                await self._notify_callback(message)
            except Exception as e:
                logger.error(f"Notification failed: {e}")

    async def get_status(self) -> str:
        """Return formatted status string."""
        lines = ["SportyBet Bot Status\n"]

        # API status
        odds_status = "configured" if ODDS_API_KEY else "NOT SET"
        football_status = "configured" if API_FOOTBALL_KEY else "NOT SET"
        lines.append(f"Odds API: {odds_status}")
        lines.append(f"API-Football: {football_status}")

        # SportyBet session
        session_status = "active" if self.booker.is_logged_in else "not logged in"
        lines.append(f"SportyBet session: {session_status}")

        # Rollover status
        if self.rollover and self.rollover.active:
            r = self.rollover
            lines.append(f"\nRollover: Day {r.current_day}/{r.total_days}")
            lines.append(f"Target odds/day: {r.target_odds}")
            wins = sum(1 for h in r.history if h.get("success"))
            lines.append(f"Results: {wins}/{len(r.history)} successful")
        else:
            lines.append(f"\nRollover: inactive")

        return "\n".join(lines)

    async def _sportybet_candidates(self, days_ahead: Optional[float] = None) -> list[dict]:
        """Build a candidate pool DIRECTLY from SportyBet's own live events,
        ranked by each event's safest-market implied win probability.

        Every candidate here is already guaranteed to exist and be bookable
        on SportyBet — unlike sourcing from The Odds API first and only
        discovering afterward that a chunk of picks don't exist there (which
        is exactly the waste seen in generate_accumulator: often a third to
        half of Odds-API-sourced legs don't match anything on SportyBet).

        Returns a list of dicts: {event, sport_id, home_team, away_team,
        league, commence_time, safe}, sorted safest (lowest odds) first.
        """
        now_ms = time.time() * 1000
        seen_sport_ids = set()
        candidates = []
        sport_name_by_id = {v: k for k, v in self.booker.SPORT_IDS.items()}

        for sport_id in self.booker.SPORT_IDS.values():
            if sport_id in seen_sport_ids:  # rugbyleague/rugbyunion/rugby all share sr:sport:12
                continue
            seen_sport_ids.add(sport_id)

            events = await self.booker.get_all_events(sport_id)
            for event in events:
                # Skip simulated/virtual fixtures (SportyBet's "Simulated
                # Reality League" — confirmed present across soccer, tennis,
                # and cricket feeds) — not real games, so not relevant here.
                category_name = event.get("sport", {}).get("category", {}).get("name", "")
                if "simulated" in category_name.lower():
                    continue

                start_ms = event.get("estimateStartTime")
                if days_ahead is not None:
                    if not start_ms:
                        continue
                    if not (now_ms <= start_ms <= now_ms + days_ahead * 86400000):
                        continue

                safe = self.booker.find_safest_selection(event)
                if not safe:
                    continue

                commence_time = ""
                if start_ms:
                    commence_time = datetime.fromtimestamp(
                        start_ms / 1000, tz=timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ")

                tournament = event.get("sport", {}).get("category", {}).get("tournament", {})
                candidates.append({
                    "event": event,
                    "sport_id": sport_id,
                    "home_team": event.get("homeTeamName", ""),
                    "away_team": event.get("awayTeamName", ""),
                    "league": tournament.get("name", ""),
                    "sport": sport_name_by_id.get(sport_id, ""),
                    "commence_time": commence_time,
                    "safe": safe,
                })

        candidates.sort(key=lambda c: c["safe"]["odds"])
        logger.info(f"Built {len(candidates)} bookable candidates directly from SportyBet's own events")
        return candidates

    async def _apply_claude_analysis(self, candidates: list[dict], settings) -> list[dict]:
        """Run the candidates past the SportyBet Analyst agent (Claude Code
        CLI + sportybet-agent.md), which web-researches each match and scores
        the pick the bot wants to back on it.

        Rank + veto: returns only the candidates Claude backed at or above
        settings.sporty_claude_min_confidence, most confident first, each
        with a `claude` dict ({confidence, verdict, note}) attached. The
        odds/markets still come only from SportyBet's real live data —
        Claude just decides which verified candidates are worth using.

        Falls back to the original odds-based order untouched if Claude is
        disabled, the CLI is missing, or every batch fails — same graceful
        skip as the rest of this bot when Claude analysis isn't available.
        """
        if not getattr(settings, "sporty_claude_enabled", True) or not candidates:
            return candidates
        if not claude_analyst.is_available():
            logger.info("claude CLI not on PATH — using odds-only ranking")
            return candidates

        research = getattr(settings, "sporty_claude_research", True)
        min_conf = getattr(settings, "sporty_claude_min_confidence", 0.75)
        pool = candidates[:getattr(settings, "sporty_claude_max_games", 30)]

        await self._notify(
            f"Claude is analysing {len(pool)} games"
            f"{' (researching form, injuries, H2H)' if research else ''}... "
            f"this can take a few minutes."
        )

        try:
            analysis = await claude_analyst.analyze_all(
                pool, self.booker._SAFE_MARKET_NAMES, research=research
            )
        except Exception as e:
            logger.error(f"Claude analysis failed: {e}")
            analysis = None

        if analysis is None:
            await self._notify("Claude analysis unavailable right now — falling back to odds-only ranking.")
            return candidates

        verdicts, flags = analysis
        approved, vetoed, skipped = [], [], 0
        for c, v in zip(pool, verdicts):
            if v is None:
                skipped += 1
                continue
            c = {**c, "claude": v}
            if v["verdict"] == "avoid" or v["confidence"] < min_conf:
                vetoed.append(c)
            else:
                approved.append(c)
        approved.sort(key=lambda c: -c["claude"]["confidence"])

        lines = [
            f"Claude analysed {len(pool) - skipped} games: "
            f"{len(approved)} approved, {len(vetoed)} vetoed"
            + (f", {skipped} not analysed" if skipped else "")
            + f" (min confidence {min_conf:.0%})."
        ]
        if vetoed:
            lines.append("\nVetoed:")
            for c in sorted(vetoed, key=lambda c: c["claude"]["confidence"])[:8]:
                lines.append(
                    f"- {c['home_team']} vs {c['away_team']} "
                    f"({c['claude']['confidence']:.0%}): {c['claude']['note'][:120]}"
                )
        flag_lines = [
            f"- {pool[f['match_index']]['home_team']} vs {pool[f['match_index']]['away_team']}: {f.get('issue', '')}"
            for f in flags
        ]
        if flag_lines:
            lines.append("\nPricing inconsistencies flagged:")
            lines.extend(flag_lines[:5])
        await self._notify("\n".join(lines))

        return approved

    async def _claude_daily_pick(self, games: list, settings, max_games: int = 8):
        """Pick the daily game with Claude: research the top `max_games`
        odds-ranked candidates, veto weak ones, return the most confident
        survivor as (BetSelection, verdict). Falls back to the top
        odds-ranked game with verdict None when Claude is disabled or
        unavailable; returns (None, None) if Claude vetoed them all.
        """
        fallback = (self.engine.selection_for(games[0]), None)
        if not getattr(settings, "sporty_claude_enabled", True) or not claude_analyst.is_available():
            return fallback

        pool = games[:max_games]
        items = []
        for g in pool:
            sel = self.engine.selection_for(g)
            odds_lines = [f"1X2: Home={g.odds_home:.2f}, Draw={g.odds_draw:.2f}, Away={g.odds_away:.2f}"]
            if g.api_football_prob:
                odds_lines.append(f"API-Football model probability for the pick: {g.api_football_prob:.0%}")
            items.append({
                "home_team": g.home_team,
                "away_team": g.away_team,
                "league": g.league,
                "sport": g.sport,
                "commence_time": g.commence_time,
                "pick": {"market": "Match winner", "outcome": sel.selection, "odds": sel.odds},
                "odds_lines": odds_lines,
            })

        research = getattr(settings, "sporty_claude_research", True)
        min_conf = getattr(settings, "sporty_claude_min_confidence", 0.75)
        await self._notify(
            f"Claude is analysing the top {len(pool)} candidates"
            f"{' (researching form, injuries, H2H)' if research else ''}..."
        )
        try:
            analysis = await claude_analyst.analyze_all(
                items, self.booker._SAFE_MARKET_NAMES, research=research,
            )
        except Exception as e:
            logger.error(f"Claude daily-pick analysis failed: {e}")
            analysis = None
        if analysis is None:
            await self._notify("Claude analysis unavailable right now — using the odds-based pick.")
            return fallback

        verdicts, _ = analysis
        approved = [
            (g, v) for g, v in zip(pool, verdicts)
            if v and v["verdict"] != "avoid" and v["confidence"] >= min_conf
        ]
        if not approved:
            reasons = [
                f"- {g.home_team} vs {g.away_team} ({v['confidence']:.0%}): {v['note'][:120]}"
                for g, v in zip(pool, verdicts) if v
            ]
            await self._notify("Claude vetoed every daily-pick candidate:\n" + "\n".join(reasons[:8]))
            return None, None

        game, verdict = max(approved, key=lambda gv: gv[1]["confidence"])
        return self.engine.selection_for(game), verdict

    async def generate_accumulator_from_sportybet(self, target_odds: float, settings,
                                                   days_ahead: Optional[float] = None,
                                                   enforce_daily_limit: bool = True) -> BookingResult:
        """Build an accumulator sourced DIRECTLY from SportyBet's own event list.

        Unlike generate_accumulator() (which sources from The Odds API and
        then discovers afterward that some legs aren't on SportyBet), every
        candidate here is already confirmed bookable — no wasted legs, no
        fuzzy team-name matching needed, no risk of the wrong-fixture problem
        _best_matching_event exists to guard against.

        Also used as the per-day pick for an active rollover (see
        _rollover_loop) — with enforce_daily_limit=False there, since the
        5/day cap is meant to bound manual "give me a different ticket"
        regeneration, not a rollover's own single scheduled daily pick.

        Args:
            target_odds: Target total odds for the accumulator
            settings: BotSettings instance
            days_ahead: Only consider games within this many days from now
            enforce_daily_limit: apply the 5/day manual-generation cap

        Returns:
            BookingResult
        """
        # Up to 5 accumulator generations per user per day — regenerating
        # after that would just be asked to try again tomorrow.
        MAX_DAILY_RUNS = 5
        runs_today = await spdb.get_todays_run_count(self.user_key)
        if enforce_daily_limit and runs_today >= MAX_DAILY_RUNS:
            return BookingResult(
                success=False,
                error=f"Daily limit reached ({MAX_DAILY_RUNS} accumulator generations/day). Try again tomorrow.",
            )

        window_note = f" (within {days_ahead:.0f}d)" if days_ahead else ""
        await self._notify(
            f"Curating accumulator for {target_odds:.0f}x directly from SportyBet's "
            f"own events{window_note}... (generation {runs_today + 1}/{MAX_DAILY_RUNS} today)"
        )

        try:
            candidates = await self._sportybet_candidates(days_ahead=days_ahead)
            if not candidates:
                return BookingResult(success=False, error="No SportyBet events found matching criteria")

            # Exclude games already used in a ticket generated earlier today,
            # so a regenerate gives genuinely different games/selections.
            used_event_ids = await spdb.get_todays_used_event_ids(self.user_key)
            if used_event_ids:
                fresh = [c for c in candidates if c["event"]["eventId"] not in used_event_ids]
                if fresh:
                    candidates = fresh
                else:
                    logger.info(
                        "All candidates already used today — falling back to the full "
                        "pool since no unused games remain"
                    )

            candidates = await self._apply_claude_analysis(candidates, settings)
            if not candidates:
                return BookingResult(
                    success=False,
                    error="Claude vetoed every candidate game — nothing safe enough to book right now.",
                )

            max_legs = settings.sporty_max_legs
            api_selections = []
            summary_lines = []
            leg_details = []
            current_odds = 1.0

            for c in candidates:
                if len(api_selections) >= max_legs:
                    break
                if current_odds >= target_odds:
                    break

                safe = c["safe"]
                pick_kind = self.booker._classify_pick(safe["market_name"], safe["outcome_desc"])
                leg_desc = f"{safe['market_name']}: {safe['outcome_desc']}"

                api_sel = {
                    "eventId": c["event"]["eventId"],
                    "marketId": safe["market_id"],
                    "outcomeId": safe["outcome_id"],
                    "sportId": c["sport_id"],
                }
                if safe.get("specifier"):
                    api_sel["specifier"] = safe["specifier"]
                api_selections.append(api_sel)

                current_odds *= safe["odds"]
                claude_note = f" | Claude {c['claude']['confidence']:.0%}" if c.get("claude") else ""
                summary_lines.append(
                    f"{len(api_selections)}. {c['home_team']} vs {c['away_team']}\n"
                    f"   {leg_desc} @ {safe['odds']:.2f}{claude_note}"
                )
                leg_details.append({
                    "event_id": c["event"]["eventId"],
                    "home_team": c["home_team"],
                    "away_team": c["away_team"],
                    "league": c["league"],
                    "sport_id": c["sport_id"],
                    "commence_time": c["commence_time"],
                    "selection": leg_desc,
                    "pick_kind": pick_kind,
                    "odds": safe["odds"],
                    "confidence": c["claude"]["confidence"] if c.get("claude") else 1.0 / safe["odds"],
                })

            if not api_selections:
                return BookingResult(success=False, error="No SportyBet events found matching criteria")

            summary = "\n".join(summary_lines)
            summary += f"\n\nTotal Odds: {current_odds:.2f}"
            summary += f"\nTarget: {target_odds:.2f}"
            summary += f"\nLegs: {len(api_selections)}"
            if current_odds < target_odds:
                summary += (
                    f"\n\nNote: Could not reach target of {target_odds:.0f}x. "
                    f"Only {current_odds:.2f}x possible with available games."
                )

            await self._notify(f"Accumulator ready:\n{summary}\n\nBooking on SportyBet...")

            if not self.booker.is_logged_in:
                return BookingResult(
                    success=False, total_odds=round(current_odds, 2),
                    num_selections=len(api_selections), selections_summary=summary,
                    error="Not logged in to SportyBet. Run: python3 sporty_discover.py",
                )
            if not await self.booker.verify_session():
                self.booker._logged_in = False
                return BookingResult(
                    success=False, total_odds=round(current_odds, 2),
                    num_selections=len(api_selections), selections_summary=summary,
                    error="SportyBet session expired. Run sporty_discover.py to re-login.",
                )

            code = await self.booker.create_booking_code(api_selections)

            if code:
                # sport_id -> a display name for pick-history logging
                sport_name_by_id = {v: k for k, v in self.booker.SPORT_IDS.items()}
                for leg in leg_details:
                    try:
                        af_id = None
                        if leg["sport_id"] == "sr:sport:1":  # API-Football is soccer-only
                            fx = await self.engine.resolve_af_fixture(
                                leg["home_team"], leg["away_team"], leg["commence_time"]
                            )
                            af_id = str(fx.get("fixture", {}).get("id", "")) if fx else None
                        await spdb.add_pick(
                            telegram_user_id=self.user_key,
                            kind="acca_leg_sb",
                            fixture_id=leg["event_id"],
                            af_fixture_id=af_id,
                            home_team=leg["home_team"],
                            away_team=leg["away_team"],
                            league=leg["league"],
                            sport=sport_name_by_id.get(leg["sport_id"], ""),
                            selection=leg["selection"],
                            pick_side=leg["pick_kind"],
                            pick_kind=leg["pick_kind"],
                            odds=leg["odds"],
                            confidence=leg["confidence"],
                            booking_code=code,
                            commence_time=leg["commence_time"],
                        )
                    except Exception as e:
                        logger.error(f"Failed to log SportyBet-sourced leg history: {e}")

                new_run_count = await spdb.increment_todays_run_count(self.user_key)
                await self._notify(
                    f"Accumulator BOOKED! ({new_run_count}/{MAX_DAILY_RUNS} today)\n\n"
                    f"{summary}\n\nBooking Code: {code}"
                )
                kickoffs = [leg["commence_time"] for leg in leg_details if leg.get("commence_time")]
                return BookingResult(
                    success=True, booking_code=code, total_odds=round(current_odds, 2),
                    num_selections=len(api_selections), selections_summary=summary,
                    latest_kickoff=max(kickoffs) if kickoffs else None,
                )
            else:
                await self._notify(
                    f"Accumulator Built (booking failed):\n\n{summary}\n\n"
                    f"Error: booking code creation failed"
                )
                return BookingResult(
                    success=False, total_odds=round(current_odds, 2),
                    num_selections=len(api_selections), selections_summary=summary,
                    error="Matched events but failed to generate booking code",
                )

        except Exception as e:
            logger.error(f"SportyBet-sourced accumulator error: {e}")
            return BookingResult(success=False, error=str(e))

    async def find_daily_pick(self, target_odds: float, settings,
                               rollover_day: Optional[int] = None,
                               days_ahead: Optional[float] = None) -> BookingResult:
        """Find the best daily pick and book it on SportyBet.

        Args:
            target_odds: Desired odds for the pick
            settings: BotSettings instance
            rollover_day: If this pick is part of an active rollover, which day
            days_ahead: Only consider games within this many days from now
                (e.g. 1 for "today only"). None = no limit.

        Returns:
            BookingResult
        """
        window_note = f" (within {days_ahead:.0f}d)" if days_ahead else ""
        await self._notify(f"Searching for daily pick at ~{target_odds} odds{window_note}...")

        try:
            games = await self.engine.daily_pick_candidates(
                target_odds=target_odds,
                min_prob=settings.sporty_min_probability,
                days_ahead=days_ahead,
            )

            if not games:
                return BookingResult(
                    success=False,
                    error="No games found matching criteria"
                )

            selection, claude_verdict = await self._claude_daily_pick(games, settings)
            if not selection:
                return BookingResult(
                    success=False,
                    error="Claude vetoed every candidate game — nothing safe enough to book right now.",
                )

            game = selection.game
            if claude_verdict:
                selection.confidence = claude_verdict["confidence"]
            claude_line = f"\nClaude: {claude_verdict['note']}" if claude_verdict else ""
            summary = (
                f"{game.home_team} vs {game.away_team}\n"
                f"League: {game.league}\n"
                f"Pick: {selection.selection} @ {selection.odds:.2f}\n"
                f"Confidence: {selection.confidence:.0%}{claude_line}"
            )

            await self._notify(f"Found pick:\n{summary}\n\nBooking on SportyBet...")

            # Book via direct API (handles not-logged-in gracefully). This may
            # substitute a safer cross-market pick (Over/Under, GG/NG, Double
            # Chance) for the original match-winner one — result.selections_summary
            # and result.leg_details reflect what was ACTUALLY booked.
            result = await self.booker.book_selections([selection])
            if result.leg_details:
                booked = result.leg_details[0]
                display_summary = (
                    f"{game.home_team} vs {game.away_team}\n"
                    f"League: {game.league}\n"
                    f"Pick: {booked['selection']} @ {booked['odds']:.2f}\n"
                    f"Confidence: {selection.confidence:.0%}{claude_line}"
                )
            else:
                booked = None
                display_summary = summary
                if not result.selections_summary:
                    result.selections_summary = summary

            if result.success:
                try:
                    await spdb.add_pick(
                        telegram_user_id=self.user_key,
                        kind="rollover" if rollover_day else "daily",
                        rollover_day=rollover_day,
                        fixture_id=game.fixture_id,
                        af_fixture_id=game.af_fixture_id,
                        home_team=game.home_team,
                        away_team=game.away_team,
                        league=game.league,
                        sport=game.sport,
                        selection=booked["selection"] if booked else selection.selection,
                        pick_side=game.best_pick,
                        pick_kind=booked["pick_kind"] if booked else game.best_pick,
                        odds=booked["odds"] if booked else selection.odds,
                        confidence=selection.confidence,
                        booking_code=result.booking_code or "",
                        commence_time=game.commence_time,
                    )
                except Exception as e:
                    logger.error(f"Failed to log pick history: {e}")

                await self._notify(
                    f"Daily Pick BOOKED!\n\n"
                    f"{display_summary}\n\n"
                    f"Booking Code: {result.booking_code}\n"
                    f"Use this code on SportyBet to place the bet."
                )
            else:
                await self._notify(
                    f"Daily Pick Found (booking failed):\n\n"
                    f"{summary}\n\n"
                    f"Error: {result.error}\n"
                    f"You can manually search this match on SportyBet."
                )
            return result

        except Exception as e:
            logger.error(f"Daily pick error: {e}")
            return BookingResult(success=False, error=str(e))

    async def generate_accumulator(self, target_odds: float, settings,
                                    days_ahead: Optional[float] = None) -> BookingResult:
        """Build accumulator ticket and book on SportyBet.

        Pre-filters games against SportyBet availability so every leg
        in the accumulator is guaranteed to be bookable.

        Args:
            target_odds: Target total odds for the accumulator
            settings: BotSettings instance
            days_ahead: Only consider games within this many days from now —
                e.g. 1 for a short "today only" ticket, 14 for a long ticket
                spanning two weeks of fixtures. None = no limit.

        Returns:
            BookingResult
        """
        window_note = f"\nWindow: within {days_ahead:.0f} day(s)" if days_ahead else ""
        await self._notify(
            f"Building accumulator for {target_odds:.0f}x total odds...\n"
            f"Min confidence per leg: {settings.sporty_min_probability:.0%}\n"
            f"Max legs: {settings.sporty_max_legs}{window_note}"
        )

        try:
            # Step 1: Get all high-confidence games
            selections = await self.engine.find_accumulator_legs(
                target_total_odds=target_odds * 2,  # Overshoot to have buffer for filtering
                min_prob=settings.sporty_min_probability,
                max_legs=settings.sporty_max_legs + 10,  # Extra legs for filtering
                days_ahead=days_ahead,
            )

            if not selections:
                return BookingResult(
                    success=False,
                    error="No high-confidence games found for accumulator"
                )

            # Step 2: Pre-fetch SportyBet events and filter to bookable only
            if self.booker.is_logged_in:
                await self._notify("Checking game availability on SportyBet...")

                # Gather all sport IDs needed
                needed_sports = set()
                for sel in selections:
                    sport = sel.game.sport or "soccer"
                    sport_id = self.booker.SPORT_IDS.get(sport)
                    if sport_id is None:
                        logger.warning(f"No SportyBet sport-id mapping for '{sport}' — "
                                        f"{sel.game.home_team} vs {sel.game.away_team} can't be matched")
                        continue
                    needed_sports.add(sport_id)

                # Pre-fetch all events
                all_sporty_events = []
                for sport_id in needed_sports:
                    events = await self.booker.get_all_events(sport_id)
                    all_sporty_events.extend(events)

                logger.info(f"Pre-fetched {len(all_sporty_events)} SportyBet events "
                           f"for filtering")

                # Filter: only keep selections with a confident, unambiguous
                # match on SportyBet (best-scoring event, not first fuzzy hit)
                bookable = []
                for sel in selections:
                    event = self.booker._best_matching_event(
                        sel.game.home_team, sel.game.away_team,
                        sel.game.commence_time, all_sporty_events,
                    )
                    if event is not None:
                        bookable.append(sel)

                skipped = len(selections) - len(bookable)
                if skipped:
                    logger.info(f"Filtered {skipped} games not on SportyBet, "
                               f"{len(bookable)} remaining")

                # Rebuild accumulator from bookable games only
                selections = []
                current_odds = 1.0
                for sel in bookable:
                    if len(selections) >= settings.sporty_max_legs:
                        break
                    if current_odds >= target_odds:
                        break
                    if sel.odds < 1.05:
                        continue
                    selections.append(sel)
                    current_odds *= sel.odds

            if not selections:
                return BookingResult(
                    success=False,
                    error="No bookable games found on SportyBet"
                )

            # Step 3: Build summary
            total_odds = 1.0
            summary_lines = []
            for i, sel in enumerate(selections, 1):
                total_odds *= sel.odds
                summary_lines.append(
                    f"{i}. {sel.game.home_team} vs {sel.game.away_team}\n"
                    f"   {sel.selection} @ {sel.odds:.2f} "
                    f"({sel.confidence:.0%})"
                )

            summary = "\n".join(summary_lines)
            summary += f"\n\nTotal Odds: {total_odds:.2f}"
            summary += f"\nTarget: {target_odds:.2f}"
            summary += f"\nLegs: {len(selections)}"
            if total_odds < target_odds:
                summary += (
                    f"\n\nNote: Could not reach target of {target_odds:.0f}x. "
                    f"Only {total_odds:.2f}x possible with available games."
                )

            await self._notify(f"Accumulator ready:\n{summary}\n\nBooking on SportyBet...")

            # Step 4: Book — all games are pre-verified on SportyBet. This may
            # substitute a safer cross-market pick (Over/Under, GG/NG, Double
            # Chance) for the original match-winner one per leg, so the real
            # summary/total odds come from result.selections_summary /
            # result.total_odds, not the pre-booking estimate above.
            result = await self.booker.book_selections(selections)
            if not result.selections_summary:
                result.selections_summary = summary

            if result.success:
                for leg in result.leg_details:
                    game = leg["game"]
                    try:
                        await spdb.add_pick(
                            telegram_user_id=self.user_key,
                            kind="acca_leg",
                            fixture_id=game.fixture_id,
                            af_fixture_id=game.af_fixture_id,
                            home_team=game.home_team,
                            away_team=game.away_team,
                            league=game.league,
                            sport=game.sport,
                            selection=leg["selection"],
                            pick_side=game.best_pick,
                            pick_kind=leg["pick_kind"],
                            odds=leg["odds"],
                            confidence=leg["confidence"],
                            booking_code=result.booking_code or "",
                            commence_time=game.commence_time,
                        )
                    except Exception as e:
                        logger.error(f"Failed to log accumulator leg history: {e}")

                await self._notify(
                    f"Accumulator BOOKED!\n\n"
                    f"{result.selections_summary}\n\n"
                    f"Total Odds: {result.total_odds:.2f}\n"
                    f"Booking Code: {result.booking_code}"
                )
            else:
                await self._notify(
                    f"Accumulator Built (booking failed):\n\n"
                    f"{summary}\n\n"
                    f"Error: {result.error}\n"
                    f"You can manually place these selections on SportyBet."
                )
            return result

        except Exception as e:
            logger.error(f"Accumulator error: {e}")
            return BookingResult(success=False, error=str(e))

    async def start_rollover(self, days: int, daily_odds: float, settings) -> bool:
        """Start a multi-day rollover plan.

        Args:
            days: Number of days
            daily_odds: Target odds per day
            settings: BotSettings instance

        Returns:
            True if started successfully
        """
        if self.rollover and self.rollover.active:
            await self._notify("A rollover is already active. Stop it first with /sporty_stop")
            return False

        now = time.time()
        db_id = await spdb.create_rollover(self.user_key, days, daily_odds)

        self.rollover = RolloverPlan(
            total_days=days,
            target_odds=daily_odds,
            current_day=0,
            history=[],
            active=True,
            start_time=now,
            db_id=db_id,
            next_run_at=now,
        )

        await self._notify(
            f"Rollover STARTED!\n\n"
            f"Plan: {days} days at {daily_odds} odds/day\n"
            f"Target final odds: {daily_odds ** days:.2f}x\n\n"
            f"I'll pick 1 game each day and send you the booking code."
        )

        self._rollover_task = asyncio.create_task(self._rollover_loop(settings))
        return True

    async def resume_rollover(self, settings, row: Optional[dict] = None):
        """Reload an active rollover from disk after a restart and resume its loop.

        Rollover progress lives in `sporty_rollovers` (see start_rollover), so
        a crash/restart mid-plan picks up where it left off instead of
        silently losing the plan (the old in-memory-only design).

        `row` is normally supplied by SportyBotManager (which fetches every
        user's active rollover in one query on startup and dispatches each to
        the right user's SportyBot); falls back to looking up this specific
        user's own row when called directly.
        """
        if row is None:
            row = await spdb.get_active_rollover(self.user_key)
        if not row:
            return

        self.rollover = RolloverPlan(
            total_days=row["total_days"],
            target_odds=row["target_odds"],
            current_day=row["current_day"],
            history=[],
            active=True,
            start_time=row["start_time"],
            db_id=row["id"],
            next_run_at=row["next_run_at"],
        )
        logger.info(f"Resumed SportyBet rollover: day {row['current_day']}/{row['total_days']}")
        await self._notify(
            f"Resumed rollover after restart — Day {row['current_day']}/{row['total_days']}."
        )
        self._rollover_task = asyncio.create_task(self._rollover_loop(settings))

    async def _rollover_loop(self, settings):
        """Async loop that picks one game per day for the rollover.

        Sleeps only until the persisted `next_run_at` rather than a blind
        24h, so resuming after a restart doesn't re-wait a full day.
        """
        try:
            while self.rollover and self.rollover.active:
                sleep_for = max(0.0, self.rollover.next_run_at - time.time())
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                if not self.rollover or not self.rollover.active:
                    break

                self.rollover.current_day += 1
                day = self.rollover.current_day

                if day > self.rollover.total_days:
                    # Rollover complete
                    wins = sum(1 for h in self.rollover.history if h.get("success"))
                    await self._notify(
                        f"Rollover COMPLETE!\n\n"
                        f"Days: {self.rollover.total_days}\n"
                        f"Successful picks: {wins}/{len(self.rollover.history)}\n"
                        f"Target odds/day: {self.rollover.target_odds}"
                    )
                    self.rollover.active = False
                    if self.rollover.db_id is not None:
                        await spdb.deactivate_rollover(self.rollover.db_id)
                    break

                await self._notify(f"Rollover Day {day}/{self.rollover.total_days}\nSearching for today's pick...")

                # Sourced directly from SportyBet's own events (like /sporty_acca),
                # not the single-game Odds-API picker — a single real match can't
                # reach an arbitrary target odds, so this builds a same-day
                # mini-accumulator when the requested odds need multiple legs.
                # days_ahead=1 keeps every leg to games actually happening
                # today, rather than pulling in games days out just because
                # they were needed to hit the target odds.
                result = await self.generate_accumulator_from_sportybet(
                    self.rollover.target_odds, settings, days_ahead=1, enforce_daily_limit=False
                )

                self.rollover.history.append({
                    "day": day,
                    "success": result.success,
                    "booking_code": result.booking_code,
                    "summary": result.selections_summary,
                    "error": result.error,
                })

                # Schedule the next pick for right after TODAY's games finish
                # — not a blind 24h timer — using the latest leg's kickoff
                # plus a generous duration/stoppage/settlement buffer. Falls
                # back to +24h if nothing booked (e.g. no games found today)
                # or the kickoff couldn't be parsed.
                next_run_at = time.time() + 24 * 60 * 60
                if result.success and result.latest_kickoff:
                    try:
                        last_kickoff = datetime.fromisoformat(
                            result.latest_kickoff.replace("Z", "+00:00")
                        )
                        next_run_at = max(
                            (last_kickoff + timedelta(hours=3)).timestamp(),
                            time.time() + 60,
                        )
                    except (ValueError, TypeError):
                        pass
                self.rollover.next_run_at = next_run_at
                if self.rollover.db_id is not None:
                    await spdb.update_rollover_progress(
                        self.rollover.db_id, day, self.rollover.next_run_at
                    )

        except asyncio.CancelledError:
            logger.info("Rollover loop cancelled")
            if self.rollover:
                self.rollover.active = False
        except Exception as e:
            logger.error(f"Rollover loop error: {e}")
            if self.rollover:
                self.rollover.active = False
                if self.rollover.db_id is not None:
                    await spdb.deactivate_rollover(self.rollover.db_id)
            await self._notify(f"Rollover stopped due to error: {e}")

    async def login_interactive(self, phone: Optional[str] = None, password: Optional[str] = None,
                                 otp_callback: Optional[Callable[[], Awaitable[str]]] = None) -> bool:
        """Open a visible browser for manual SportyBet login.

        otp_callback: async, no-args callable that asks the user (e.g. via
        Telegram) for their 2FA/OTP code and returns it — invoked only if a
        verification-code screen actually appears during login.

        Returns True if login was successful and session saved.
        """
        await self._notify(
            "Opening SportyBet in a visible browser...\n"
            "Please complete the login (handle CAPTCHA if needed).\n"
            "You have 5 minutes."
        )
        success = await self.booker.login_interactive(phone, password, otp_callback)
        if success:
            await self._notify("SportyBet login successful! Session saved for future use.")
        else:
            await self._notify("SportyBet login failed or timed out.")
        return success

    def stop(self):
        """Stop active rollover and clean up."""
        if self.rollover:
            self.rollover.active = False
            if self.rollover.db_id is not None:
                asyncio.create_task(spdb.deactivate_rollover(self.rollover.db_id))
        if self._rollover_task and not self._rollover_task.done():
            self._rollover_task.cancel()

    async def check_session_health(self):
        """Check SportyBet session validity; notify only on a fresh expiry.

        Without this, session expiry is only discovered reactively — buried
        in a BookingResult.error the next time a pick tries to book — which
        can mean days of a silently-broken rollover before anyone notices.
        """
        if not self.booker.is_logged_in:
            return
        valid = await self.booker.verify_session()
        if not valid and self._was_session_valid:
            await self._notify(
                "SportyBet session has expired.\n"
                "Run /sporty_login to log back in — auto-booking is paused until then."
            )
        self._was_session_valid = valid

    @staticmethod
    def _grade_pick(pick_kind: str, pick_side: str, result: dict) -> Optional[bool]:
        """Decide win/loss for a settled fixture given the pick's classification.

        pick_kind is the compact code from SportyBetBooker._classify_pick
        (e.g. "home", "over_1.5", "btts_yes", "dc_1x"). Unrecognized codes
        fall back to the plain home/draw/away pick_side comparison. Returns
        None when it can't be graded (not finished, missing goals data, or a
        push on a whole-number Over/Under line) — leaves the pick unsettled
        for a later retry rather than guessing.
        """
        winner = result.get("winner")
        goals = result.get("goals") or {}
        home_goals, away_goals = goals.get("home"), goals.get("away")

        if pick_kind in ("home", "draw", "away"):
            return (winner == pick_kind) if winner else None

        if pick_kind in ("btts_yes", "btts_no"):
            if home_goals is None or away_goals is None:
                return None
            both_scored = home_goals > 0 and away_goals > 0
            return both_scored == (pick_kind == "btts_yes")

        if pick_kind.startswith("over_") or pick_kind.startswith("under_"):
            if home_goals is None or away_goals is None:
                return None
            try:
                line = float(pick_kind.split("_", 1)[1])
            except ValueError:
                return None
            total = home_goals + away_goals
            if total == line:
                return None  # push on a whole-number line — leave ungraded
            return (total > line) if pick_kind.startswith("over_") else (total < line)

        if pick_kind.startswith("dc_"):
            pairs = {"dc_1x": ("home", "draw"), "dc_12": ("home", "away"), "dc_x2": ("draw", "away")}
            allowed = pairs.get(pick_kind)
            return (winner in allowed) if (winner and allowed) else None

        if pick_kind.startswith("hcp_"):
            # European handicap: add the line to the actual score, then
            # grade home/draw/away on the adjusted result.
            if home_goals is None or away_goals is None:
                return None
            m = re.match(r"hcp_(-?\d+):(-?\d+)_(home|draw|away)", pick_kind)
            if not m:
                return None
            h_adj, a_adj, side = int(m.group(1)), int(m.group(2)), m.group(3)
            adj_home, adj_away = home_goals + h_adj, away_goals + a_adj
            if adj_home == adj_away:
                adjusted_result = "draw"
            else:
                adjusted_result = "home" if adj_home > adj_away else "away"
            return adjusted_result == side

        if pick_kind.startswith("home_ou_") or pick_kind.startswith("away_ou_"):
            goals_for_side = home_goals if pick_kind.startswith("home_ou_") else away_goals
            if goals_for_side is None:
                return None
            m = re.match(r"(?:home|away)_ou_(over|under)_([\d.]+)", pick_kind)
            if not m:
                return None
            direction, line = m.group(1), float(m.group(2))
            if goals_for_side == line:
                return None  # push on a whole-number line
            return (goals_for_side > line) if direction == "over" else (goals_for_side < line)

        if pick_kind.startswith("corners_"):
            # Corner counts aren't in the fixture result we fetch (goals
            # only) — bookable, but left unsettled until a corners-stats
            # data source is wired in.
            return None

        if pick_kind.startswith(("1up_", "2up_", "neverdown_", "dc1up_")):
            # These settle on the goal-by-goal timeline (did a team ever
            # lead by N goals / never trail), not the final score alone —
            # bookable, but left unsettled until fixture goal-events data
            # (minute-by-minute scoring) is wired in.
            return None

        # "unknown" or a legacy row without a real pick_kind — fall back to
        # the plain home/draw/away comparison.
        return (winner == pick_side) if winner else None

    async def settle_picks(self):
        """Check unsettled picks against real results and record win/loss.

        This is the feedback loop needed before the confidence formula's
        blend weights (see PredictionEngine._compute_confidence) could ever
        be responsibly retuned — right now nobody knows if it's calibrated.
        """
        try:
            picks = await spdb.get_unsettled_picks()
        except Exception as e:
            logger.error(f"Error loading unsettled picks: {e}")
            return

        for pick in picks:
            try:
                commence = datetime.fromisoformat(pick["commence_time"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if commence > datetime.now(timezone.utc):
                continue  # hasn't kicked off yet

            af_id = pick.get("af_fixture_id")
            if not af_id:
                fx = await self.engine.resolve_af_fixture(
                    pick["home_team"], pick["away_team"], pick["commence_time"]
                )
                af_id = str(fx.get("fixture", {}).get("id", "")) if fx else None
                if not af_id:
                    continue  # can't resolve yet — retry next cycle

            result = await self.engine.get_fixture_result(af_id)
            if not result:
                continue  # not finished yet

            won = self._grade_pick(pick.get("pick_kind") or "winner", pick["pick_side"], result)
            if won is None:
                continue  # can't grade yet (or ever) — leave unsettled

            try:
                await spdb.settle_pick(pick["id"], won)
                logger.info(
                    f"Settled pick {pick['id']}: {pick['home_team']} vs "
                    f"{pick['away_team']} — {'WON' if won else 'LOST'}"
                )
            except Exception as e:
                logger.error(f"Error settling pick {pick['id']}: {e}")



# ── Multi-user manager ───────────────────────────────────────────────────────

class SportyBotManager:
    """Lazily creates and caches one SportyBot per Telegram user, so each
    user gets their own isolated SportyBet session, rollover, and pick
    history — while sharing one PredictionEngine (and its Odds-API daily
    cache) across everyone, since odds/prediction data isn't user-specific.
    """

    def __init__(self):
        self._shared_engine = PredictionEngine()
        self._bots: dict[str, SportyBot] = {}

    def get(self, user_key) -> SportyBot:
        user_key = str(user_key)
        if user_key not in self._bots:
            self._bots[user_key] = SportyBot(user_key, engine=self._shared_engine)
        return self._bots[user_key]

    def all_bots(self) -> list[SportyBot]:
        return list(self._bots.values())

    async def run_periodic_maintenance(self):
        """Background loop: per-user session health checks + one shared
        pick-settlement pass, every 6h. Settlement is user-agnostic (it
        grades every unsettled pick regardless of who placed it), so it
        only needs to run once per cycle via any single bot's shared engine.
        """
        while True:
            try:
                for bot in self.all_bots():
                    await bot.check_session_health()
                if self._bots:
                    await next(iter(self._bots.values())).settle_picks()
            except Exception as e:
                logger.error(f"Periodic maintenance error: {e}")
            await asyncio.sleep(6 * 60 * 60)


sporty_bot_manager = SportyBotManager()
