"""
BTC 5-Minute Arbitrage Module for Polymarket

Strategy: Polymarket offers rolling 5-minute binary markets ("Will BTC be up or down?")
settled by Chainlink oracle. We fetch real-time BTC price from Binance, compare against
Polymarket odds, and buy the underpriced side when there's an edge.

Market slugs are deterministic: btc-updown-5m-{unix_time // 300 * 300}
"""

import asyncio
import json
import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, PartialCreateOrderOptions

from dotenv import dotenv_values

logger = logging.getLogger("btc-arb")

# Read parent .env as a separate dict — sniping/.env has Solana keys under the
# same names (PRIVATE_KEY etc.), so we must NOT use load_dotenv which would
# either skip (override=False) or clobber (override=True) the Solana values.
_parent_env = Path(__file__).resolve().parent.parent / ".env"
_poly_env = dotenv_values(_parent_env)


# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class BTCMarket:
    """Represents a current 5-min BTC up/down market."""
    condition_id: str
    question: str
    slug: str
    up_token_id: str
    down_token_id: str
    end_timestamp: int  # When this 5-min window closes
    accepting_orders: bool


@dataclass
class BTCEdge:
    """A detected trading edge."""
    side: str           # "Up" or "Down"
    token_id: str
    price: float        # Best ask price
    momentum: float     # BTC price change %
    btc_price: float    # Current BTC price
    time_remaining: int # Seconds until market closes
    market: BTCMarket


@dataclass
class TradeResult:
    success: bool
    order_id: Optional[str] = None
    side: Optional[str] = None
    price: Optional[float] = None
    size: Optional[float] = None
    cost: Optional[float] = None
    error: Optional[str] = None


# ── Price History Buffer ─────────────────────────────────────────────────────

class PriceHistory:
    """Ring buffer of recent BTC prices (last 12 ticks = 6 min at 30s intervals)."""

    def __init__(self, maxlen: int = 12):
        self._prices: deque = deque(maxlen=maxlen)

    def add(self, price: float):
        self._prices.append((time.time(), price))

    @property
    def current(self) -> Optional[float]:
        return self._prices[-1][1] if self._prices else None

    def change_pct(self, seconds: int) -> Optional[float]:
        """Return % change over the last N seconds, or None if insufficient data."""
        if len(self._prices) < 2:
            return None
        now = time.time()
        cutoff = now - seconds
        # Find the oldest price within the lookback window
        for ts, price in self._prices:
            if ts >= cutoff:
                current = self._prices[-1][1]
                if price == 0:
                    return None
                return (current - price) / price * 100
        return None

    def __len__(self):
        return len(self._prices)


# ── CLOB Client Singleton ───────────────────────────────────────────────────

_clob_client: Optional[ClobClient] = None


def get_clob_client() -> ClobClient:
    """Initialize and return the Polymarket CLOB client (singleton).

    Reads credentials from the parent .env (Polymarket hex key), NOT from
    os.environ which contains the Solana base58 key from sniping/.env.
    """
    global _clob_client
    if _clob_client is not None:
        return _clob_client

    private_key = _poly_env.get("PRIVATE_KEY", "")
    if not private_key:
        raise ValueError("PRIVATE_KEY not found in parent .env")
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    api_key = _poly_env.get("API_KEY", "")
    api_secret = _poly_env.get("API_SECRET", "")
    api_passphrase = _poly_env.get("API_PASSPHRASE", "")

    if api_key and api_secret and api_passphrase:
        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        _clob_client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=137,
            creds=creds,
        )
        logger.info("CLOB client initialized with authentication")
    else:
        _clob_client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=137,
        )
        logger.warning("CLOB client initialized WITHOUT authentication — orders will fail")

    return _clob_client


# ── Core Functions ───────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"


async def get_current_btc_market() -> Optional[BTCMarket]:
    """Fetch the current active 5-min BTC up/down market from Gamma API."""
    now = int(time.time())
    # Current 5-min window: round down to nearest 300s
    window_start = (now // 300) * 300
    slug = f"btc-updown-5m-{window_start}"

    try:
        async with aiohttp.ClientSession() as session:
            # Try current window first, then next window if current is about to close
            for attempt_slug in [slug, f"btc-updown-5m-{window_start + 300}"]:
                url = f"{GAMMA_API}/markets?slug={attempt_slug}"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    if not data:
                        continue

                    market = data[0] if isinstance(data, list) else data

                    if not market.get("acceptingOrders", False):
                        continue

                    clob_token_ids = json.loads(market.get("clobTokenIds", "[]"))
                    if len(clob_token_ids) < 2:
                        continue

                    # Parse end date for time remaining calculation
                    end_date = market.get("endDate", "")
                    try:
                        from datetime import datetime, timezone
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        end_ts = int(end_dt.timestamp())
                    except (ValueError, AttributeError):
                        # Fallback: window_start + 300
                        ts_from_slug = int(attempt_slug.split("-")[-1])
                        end_ts = ts_from_slug + 300

                    return BTCMarket(
                        condition_id=market.get("conditionId", ""),
                        question=market.get("question", ""),
                        slug=attempt_slug,
                        up_token_id=clob_token_ids[0],    # Outcomes[0] = Up
                        down_token_id=clob_token_ids[1],   # Outcomes[1] = Down
                        end_timestamp=end_ts,
                        accepting_orders=True,
                    )

        logger.warning(f"No active BTC 5-min market found (tried slug: {slug})")
        return None

    except Exception as e:
        logger.error(f"Error fetching BTC market: {e}")
        return None


async def get_btc_price() -> Optional[float]:
    """Fetch real-time BTC price. Binance primary, CoinGecko fallback."""
    async with aiohttp.ClientSession() as session:
        # Primary: Binance
        try:
            url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data["price"])
        except Exception as e:
            logger.warning(f"Binance price fetch failed: {e}")

        # Fallback: CoinGecko
        try:
            url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data["bitcoin"]["usd"])
        except Exception as e:
            logger.warning(f"CoinGecko price fetch failed: {e}")

    logger.error("All BTC price sources failed")
    return None


async def get_orderbook_prices(market: BTCMarket) -> dict:
    """Fetch best ask prices for Up and Down tokens from the CLOB orderbook.

    Returns dict with keys 'up_ask', 'down_ask' (floats), or None if unavailable.
    IMPORTANT: Asks must be sorted ascending before taking [0] as best ask.
    """
    client = get_clob_client()
    result = {"up_ask": None, "down_ask": None}

    try:
        # Up token orderbook
        up_book = client.get_order_book(market.up_token_id)
        if up_book and up_book.asks:
            # CRITICAL: Sort asks ascending by price — API returns unsorted
            sorted_asks = sorted(up_book.asks, key=lambda x: float(x.price))
            result["up_ask"] = float(sorted_asks[0].price)

        # Down token orderbook
        down_book = client.get_order_book(market.down_token_id)
        if down_book and down_book.asks:
            sorted_asks = sorted(down_book.asks, key=lambda x: float(x.price))
            result["down_ask"] = float(sorted_asks[0].price)

    except Exception as e:
        logger.error(f"Error fetching orderbook: {e}")

    return result


def get_btc_momentum(price_history: PriceHistory) -> dict:
    """Analyze BTC price momentum from recent price history.

    Returns dict with:
        - momentum_30s: % change over last 30 seconds
        - momentum_60s: % change over last 60 seconds
        - direction: "up", "down", or "neutral"
    """
    m30 = price_history.change_pct(30)
    m60 = price_history.change_pct(60)

    if m30 is None:
        return {"momentum_30s": 0, "momentum_60s": 0, "direction": "neutral"}

    if m30 > 0.02:
        direction = "up"
    elif m30 < -0.02:
        direction = "down"
    else:
        direction = "neutral"

    return {
        "momentum_30s": m30 or 0,
        "momentum_60s": m60 or 0,
        "direction": direction,
    }


def find_btc_edge(
    market: BTCMarket,
    prices: dict,
    momentum: dict,
    btc_price: float,
    max_price: float = 0.55,
    min_edge: float = 0.02,
) -> Optional[BTCEdge]:
    """Compare BTC momentum vs market odds to find mispricing.

    Args:
        market: Current BTCMarket
        prices: Orderbook prices (up_ask, down_ask)
        momentum: Momentum data from get_btc_momentum()
        btc_price: Current BTC price
        max_price: Max price to pay for a side (default 0.55)
        min_edge: Min momentum threshold in % (default 0.02%)

    Returns:
        BTCEdge if edge found, None otherwise
    """
    now = int(time.time())
    time_remaining = market.end_timestamp - now

    # Don't trade in the last 60 seconds — too risky
    if time_remaining < 60:
        logger.debug(f"Skipping — only {time_remaining}s remaining in window")
        return None

    m30 = momentum["momentum_30s"]
    direction = momentum["direction"]

    # Buy UP when BTC is going up but "Up" token is still cheap
    if direction == "up" and abs(m30) >= min_edge:
        up_ask = prices.get("up_ask")
        if up_ask is not None and up_ask <= max_price:
            return BTCEdge(
                side="Up",
                token_id=market.up_token_id,
                price=up_ask,
                momentum=m30,
                btc_price=btc_price,
                time_remaining=time_remaining,
                market=market,
            )

    # Buy DOWN when BTC is going down but "Down" token is still cheap
    if direction == "down" and abs(m30) >= min_edge:
        down_ask = prices.get("down_ask")
        if down_ask is not None and down_ask <= max_price:
            return BTCEdge(
                side="Down",
                token_id=market.down_token_id,
                price=down_ask,
                momentum=m30,
                btc_price=btc_price,
                time_remaining=time_remaining,
                market=market,
            )

    return None


def execute_btc_trade(edge: BTCEdge, bet_amount: float = 2.0) -> TradeResult:
    """Place a BUY order on the identified edge via py_clob_client.

    Args:
        edge: The BTCEdge to trade on
        bet_amount: USDC amount to bet (default $2)

    Returns:
        TradeResult with order details
    """
    client = get_clob_client()

    try:
        trade_price = round(edge.price, 2)
        # Calculate size: shares = bet_amount / price
        raw_size = bet_amount / trade_price
        # Round up to ensure order value >= $1 minimum
        size = math.ceil(raw_size * 100) / 100
        while size * trade_price < 1.0:
            size += 0.01
        size = round(size, 2)

        logger.info(
            f"Placing BUY {edge.side} order: "
            f"price=${trade_price:.2f}, size={size}, cost=~${size * trade_price:.2f}"
        )

        order_args = OrderArgs(
            token_id=edge.token_id,
            price=trade_price,
            size=size,
            side="BUY",
        )

        # 5-min BTC markets are standard binary (negRisk=false), tick_size 0.01
        options = PartialCreateOrderOptions(
            tick_size="0.01",
            neg_risk=False,
        )

        result = client.create_and_post_order(order_args, options)

        order_id = None
        if isinstance(result, dict):
            order_id = result.get("orderID")
            success = result.get("success", False)
            if not success:
                error_msg = result.get("errorMsg", "Unknown error")
                return TradeResult(success=False, error=error_msg)

        return TradeResult(
            success=True,
            order_id=order_id or str(result),
            side=edge.side,
            price=trade_price,
            size=size,
            cost=round(size * trade_price, 2),
        )

    except Exception as e:
        logger.error(f"Trade execution failed: {e}")
        return TradeResult(success=False, error=str(e))


# ── Main Loop ────────────────────────────────────────────────────────────────

class BTCArbitrage:
    """Manages the BTC 5-min arbitrage loop and state."""

    def __init__(self):
        self.running = False
        self.price_history = PriceHistory(maxlen=12)
        self.last_trade_window: Optional[str] = None  # Slug of last traded window
        self.trades_today: list = []
        self._task: Optional[asyncio.Task] = None
        self._notify_callback = None  # async callable(message: str)

    def set_notify_callback(self, callback):
        """Set async callback for Telegram notifications: callback(message: str)."""
        self._notify_callback = callback

    async def _notify(self, message: str):
        if self._notify_callback:
            try:
                await self._notify_callback(message)
            except Exception as e:
                logger.error(f"Notification failed: {e}")

    async def get_status(self) -> str:
        """Return a formatted status string for /btc command."""
        btc_price = await get_btc_price()
        market = await get_current_btc_market()

        lines = ["BTC 5-Min Arb Status\n"]

        if btc_price:
            lines.append(f"BTC Price: ${btc_price:,.2f}")
        else:
            lines.append("BTC Price: unavailable")

        # Momentum
        if len(self.price_history) >= 2:
            momentum = get_btc_momentum(self.price_history)
            m30 = momentum["momentum_30s"]
            m60 = momentum["momentum_60s"]
            direction = momentum["direction"]
            arrow = {"up": "^", "down": "v", "neutral": "-"}[direction]
            lines.append(f"Momentum: {arrow} {m30:+.4f}% (30s) / {m60:+.4f}% (60s)")
        else:
            lines.append("Momentum: collecting data...")

        # Market info
        if market:
            now = int(time.time())
            remaining = market.end_timestamp - now
            lines.append(f"\nMarket: {market.slug}")
            lines.append(f"Time remaining: {remaining}s")

            prices = await get_orderbook_prices(market)
            if prices["up_ask"] is not None:
                lines.append(f"Up ask:   ${prices['up_ask']:.2f}")
            else:
                lines.append("Up ask:   no asks")
            if prices["down_ask"] is not None:
                lines.append(f"Down ask: ${prices['down_ask']:.2f}")
            else:
                lines.append("Down ask: no asks")
        else:
            lines.append("\nNo active 5-min BTC market found")

        lines.append(f"\nLoop: {'RUNNING' if self.running else 'STOPPED'}")
        lines.append(f"Trades today: {len(self.trades_today)}")

        return "\n".join(lines)

    async def run_loop(self, settings):
        """Main arbitrage loop. Scans every ~30 seconds.

        Args:
            settings: BotSettings instance with btc_* fields
        """
        self.running = True
        logger.info("BTC arb loop started")
        await self._notify("BTC 5-min arb loop STARTED. Scanning every 30s.")

        try:
            while self.running:
                try:
                    await self._tick(settings)
                except Exception as e:
                    logger.error(f"BTC arb tick error: {e}")

                await asyncio.sleep(30)
        finally:
            self.running = False
            logger.info("BTC arb loop stopped")

    async def _tick(self, settings):
        """Single scan iteration."""
        if not settings.btc_enabled:
            return

        # 1. Fetch BTC price and update history
        btc_price = await get_btc_price()
        if btc_price is None:
            return
        self.price_history.add(btc_price)

        # Need at least 2 data points for momentum
        if len(self.price_history) < 2:
            logger.debug("Collecting price data...")
            return

        # 2. Fetch current market
        market = await get_current_btc_market()
        if market is None:
            return

        # Don't trade the same window twice
        if market.slug == self.last_trade_window:
            return

        # 3. Get orderbook prices
        prices = await get_orderbook_prices(market)

        # 4. Analyze momentum
        momentum = get_btc_momentum(self.price_history)

        # 5. Find edge
        edge = find_btc_edge(
            market=market,
            prices=prices,
            momentum=momentum,
            btc_price=btc_price,
            max_price=settings.btc_max_price,
            min_edge=settings.btc_min_edge,
        )

        if edge is None:
            return

        # 6. Execute trade
        logger.info(
            f"Edge found! {edge.side} @ ${edge.price:.2f} "
            f"(momentum: {edge.momentum:+.4f}%, remaining: {edge.time_remaining}s)"
        )

        result = execute_btc_trade(edge, bet_amount=settings.btc_bet_amount)

        if result.success:
            self.last_trade_window = market.slug
            self.trades_today.append({
                "time": time.time(),
                "side": result.side,
                "price": result.price,
                "size": result.size,
                "cost": result.cost,
                "slug": market.slug,
                "btc_price": btc_price,
                "momentum": edge.momentum,
            })

            msg = (
                f"BTC Trade Executed!\n\n"
                f"Side: {result.side}\n"
                f"Price: ${result.price:.2f}\n"
                f"Size: {result.size} shares\n"
                f"Cost: ${result.cost:.2f}\n"
                f"BTC: ${btc_price:,.2f}\n"
                f"Momentum: {edge.momentum:+.4f}%\n"
                f"Window: {market.slug}\n"
                f"Remaining: {edge.time_remaining}s\n"
                f"Order ID: {result.order_id}"
            )
            await self._notify(msg)
            logger.info(f"Trade success: {result.side} @ ${result.price}")
        else:
            logger.warning(f"Trade failed: {result.error}")
            await self._notify(f"BTC trade FAILED: {result.error}")

    def start(self, settings) -> asyncio.Task:
        """Start the arb loop as an asyncio task."""
        if self._task and not self._task.done():
            raise RuntimeError("BTC arb loop already running")
        self._task = asyncio.create_task(self.run_loop(settings))
        return self._task

    def stop(self):
        """Stop the arb loop."""
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()


# Module-level singleton
btc_arb = BTCArbitrage()
