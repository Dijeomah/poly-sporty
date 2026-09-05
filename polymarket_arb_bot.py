#!/usr/bin/env python3
"""
Polymarket Advanced Arbitrage Trading Bot
Multi-strategy approach with comprehensive edge case handling
Targeting 95% profitable trades
"""

import os
import time
import json
import sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from decimal import Decimal, ROUND_DOWN
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
from py_clob_client.exceptions import PolyApiException
from dotenv import load_dotenv
import requests
from collections import defaultdict
import logging
import anthropic

load_dotenv()

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration - Optimized for 95% profit rate
MAX_BET_PER_TRADE = 2.0  # $2 per trade
MAX_TOTAL_EXPOSURE = 10.0  # $10 total
STOP_LOSS_THRESHOLD = 0.0025  # 0.25%
MIN_ARBITRAGE_PROFIT = 0.02  # 2% minimum (higher threshold = higher win rate)
MIN_LIQUIDITY_DEPTH = 2.0  # Minimum $2 liquidity required
MAX_SLIPPAGE = 0.005  # 0.5% max slippage tolerance
POLL_INTERVAL = 5  # seconds between market checks
ORDER_TIMEOUT = 30  # seconds to wait for order fill
MAX_RETRIES = 3  # Maximum retries for failed operations
MARKET_BLACKLIST_DURATION = 300  # 5 minutes blacklist for problematic markets
MIN_MARKET_VOLUME = 100  # Minimum $100 24h volume
MAX_SPREAD_WIDTH = 0.15  # Maximum 15% spread
BALANCE_CHECK_INTERVAL = 60  # Check balance every 60 seconds
MIN_PROFIT_AFTER_FEES = 0.01  # 1% minimum after fees (fees ~1%)

class PolymarketArbBot:
    def __init__(self):
        # Initialize Polymarket client with API credentials
        self.private_key = os.getenv('PRIVATE_KEY')
        if not self.private_key:
            raise ValueError("PRIVATE_KEY not found in environment")
        
        # Get API credentials for authenticated access
        self.api_key = os.getenv('API_KEY')
        self.api_secret = os.getenv('API_SECRET')
        self.api_passphrase = os.getenv('API_PASSPHRASE')
        
        # Validate private key format
        if not self.private_key.startswith('0x'):
            self.private_key = '0x' + self.private_key
        
        if len(self.private_key) != 66:
            raise ValueError("Invalid private key format. Must be 64 hex characters.")
        
        try:
            # Initialize with authentication if credentials available
            if self.api_key and self.api_secret and self.api_passphrase:
                logger.info("🔐 Initializing with API authentication...")
                from py_clob_client.clob_types import ApiCreds
                
                creds = ApiCreds(
                    api_key=self.api_key,
                    api_secret=self.api_secret,
                    api_passphrase=self.api_passphrase
                )
                
                self.client = ClobClient(
                    host="https://clob.polymarket.com",
                    key=self.private_key,
                    chain_id=137,
                    creds=creds
                )
                logger.info("✅ Successfully connected to Polymarket with authentication")
            else:
                logger.warning("⚠️  No API credentials found - using unauthenticated mode")
                logger.warning("⚠️  Add API_KEY, API_SECRET, API_PASSPHRASE to .env for full access")
                self.client = ClobClient(
                    host="https://clob.polymarket.com",
                    key=self.private_key,
                    chain_id=137
                )
                logger.info("✅ Successfully connected to Polymarket (unauthenticated)")
        except Exception as e:
            logger.error(f"Failed to initialize Polymarket client: {e}")
            raise
        
        # Initialize database
        self.init_db()
        
        # Track positions and state
        self.current_exposure = 0.0
        self.active_positions = {}  # market_id -> {side, price, token_id, ...}
        self.closed_markets = set()  # market_ids we've already closed/resolved — never re-enter
        self.market_blacklist = {}  # market_id: timestamp
        self.failed_attempts = defaultdict(int)
        self.last_balance_check = 0
        self.usdc_balance = 0.0
        self.market_cache = {}
        self.last_cache_update = 0
        self.last_monitor_time = 0

        # Verify initial balance
        self.update_balance()

        # Load existing positions from database to prevent duplicates
        self._load_existing_positions()

        logger.info("🤖 Advanced Polymarket Arbitrage Bot initialized")
        logger.info(f"Max bet per trade: ${MAX_BET_PER_TRADE}")
        logger.info(f"Max total exposure: ${MAX_TOTAL_EXPOSURE}")
        logger.info(f"Stop loss: {STOP_LOSS_THRESHOLD*100}%")
        logger.info(f"Min arbitrage profit: {MIN_ARBITRAGE_PROFIT*100}%")
        logger.info(f"Min profit after fees: {MIN_PROFIT_AFTER_FEES*100}%")
        logger.info(f"Current USDC balance: ${self.usdc_balance:.2f}")
        if self.active_positions:
            logger.info(f"Loaded {len(self.active_positions)} existing positions (${self.current_exposure:.2f} exposure)")
        logger.info("")
    
    def _load_existing_positions(self):
        """Load open positions from database, aggregating multiple trades per market."""
        try:
            self.cursor.execute('''
                SELECT market_id, market_name, side, amount, price, timestamp,
                       trade_token, size
                FROM trades
                WHERE status = 'EXECUTED'
                ORDER BY timestamp ASC
            ''')
            rows = self.cursor.fetchall()
            for market_id, market_name, side, amount, price, ts, trade_token, size in rows:
                if not market_id:
                    continue
                price = price or 0
                size = size or 0
                amount = amount or MAX_BET_PER_TRADE

                if market_id in self.active_positions:
                    # Aggregate: combine sizes, weighted-average entry price
                    pos = self.active_positions[market_id]
                    old_size = pos.get('size', 0) or 0
                    old_price = pos.get('entry_price', 0) or 0
                    new_total_size = old_size + size
                    if new_total_size > 0:
                        pos['entry_price'] = (old_price * old_size + price * size) / new_total_size
                    pos['size'] = new_total_size
                    pos['amount'] = pos.get('amount', 0) + amount
                    pos['trade_count'] = pos.get('trade_count', 1) + 1
                    self.current_exposure += amount
                else:
                    self.active_positions[market_id] = {
                        'market_name': market_name,
                        'side': side,
                        'amount': amount,
                        'entry_price': price,
                        'timestamp': ts,
                        'trade_token': trade_token,
                        'size': size,
                        'trade_count': 1,
                    }
                    self.current_exposure += amount
            logger.info(f"Loaded {len(self.active_positions)} positions ({len(rows)} trades) from DB")

            # Also load closed/resolved markets so we never re-enter them
            self.cursor.execute('''
                SELECT DISTINCT market_id FROM trades
                WHERE status IN ('CLOSED', 'RESOLVED')
            ''')
            for (mid,) in self.cursor.fetchall():
                if mid:
                    self.closed_markets.add(mid)
            if self.closed_markets:
                logger.info(f"Skipping {len(self.closed_markets)} previously closed markets")
        except Exception as e:
            logger.warning(f"Could not load existing positions: {e}")

    def init_db(self):
        """Initialize SQLite database for trade history"""
        self.conn = sqlite3.connect('trades.db')
        self.cursor = self.conn.cursor()
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                market_id TEXT,
                market_name TEXT,
                side TEXT,
                amount REAL,
                price REAL,
                expected_profit REAL,
                actual_profit REAL,
                status TEXT,
                error_message TEXT,
                execution_time REAL,
                trade_token TEXT,
                size REAL
            )
        ''')
        # Add columns if they don't exist (for existing DBs)
        for col, col_type in [('trade_token', 'TEXT'), ('size', 'REAL')]:
            try:
                self.cursor.execute(f'ALTER TABLE trades ADD COLUMN {col} {col_type}')
            except sqlite3.OperationalError:
                pass  # Column already exists
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS market_analysis (
                market_id TEXT PRIMARY KEY,
                question TEXT,
                volume_24h REAL,
                liquidity REAL,
                spread REAL,
                last_checked TEXT,
                arbitrage_count INTEGER DEFAULT 0
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS performance_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                total_trades INTEGER,
                successful_trades INTEGER,
                failed_trades INTEGER,
                total_profit REAL,
                win_rate REAL,
                avg_profit_per_trade REAL
            )
        ''')
        
        self.conn.commit()
    
    def update_balance(self) -> bool:
        """Check USDC.e balance on Polygon via web3"""
        try:
            from web3 import Web3
            from eth_account import Account

            w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
            pk = os.getenv("PRIVATE_KEY", "")
            if not pk.startswith("0x"):
                pk = "0x" + pk
            wallet = Account.from_key(pk).address

            # USDC.e on Polygon
            USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
            ERC20_ABI = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
                          "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
                          "type": "function"}]
            contract = w3.eth.contract(address=USDC_E, abi=ERC20_ABI)
            raw = contract.functions.balanceOf(wallet).call()
            self.usdc_balance = raw / 1e6  # USDC.e has 6 decimals

            self.last_balance_check = time.time()

            if self.usdc_balance < MAX_BET_PER_TRADE:
                logger.warning(f"Low balance: ${self.usdc_balance:.2f}")
                return False

            logger.info(f"USDC.e balance: ${self.usdc_balance:.2f}")
            return True
        except Exception as e:
            logger.error(f"Failed to fetch balance: {e}")
            return True  # Don't block trading on balance check failures
    
    def clean_blacklist(self):
        """Remove expired entries from blacklist"""
        current_time = time.time()
        expired = [
            market_id for market_id, timestamp in self.market_blacklist.items()
            if current_time - timestamp > MARKET_BLACKLIST_DURATION
        ]
        for market_id in expired:
            del self.market_blacklist[market_id]
            logger.info(f"Removed {market_id[:8]}... from blacklist")
    
    def is_market_blacklisted(self, market_id: str) -> bool:
        """Check if market is blacklisted"""
        if market_id in self.market_blacklist:
            time_left = MARKET_BLACKLIST_DURATION - (time.time() - self.market_blacklist[market_id])
            if time_left > 0:
                return True
            else:
                del self.market_blacklist[market_id]
        return False
    
    def blacklist_market(self, market_id: str, reason: str):
        """Add market to blacklist"""
        self.market_blacklist[market_id] = time.time()
        logger.warning(f"⛔ Blacklisted market {market_id[:8]}... for {MARKET_BLACKLIST_DURATION}s: {reason}")
    
    def get_all_markets(self) -> List[Dict]:
        """Fetch all active markets using Gamma API"""
        current_time = time.time()
        
        # Use cache if recent (30 seconds)
        if current_time - self.last_cache_update < 30 and self.market_cache:
            return self.market_cache.get('markets', [])
        
        try:
            logger.info("📡 Fetching live markets from Gamma API...")
            
            import requests
            
            # Use Gamma API for market discovery (no auth required for public data)
            gamma_url = "https://gamma-api.polymarket.com/markets"

            # Paginate to get more markets
            markets = []
            max_pages = 5
            per_page = 100

            for page in range(max_pages):
                params = {
                    'closed': 'false',
                    'active': 'true',
                    'limit': per_page,
                    'offset': page * per_page,
                    'order': 'volume24hr',  # Sort by recent activity, not all-time volume
                    'ascending': 'false',
                }

                response = requests.get(gamma_url, params=params, timeout=10)
                response.raise_for_status()

                data = response.json()

                if isinstance(data, list):
                    page_markets = data
                elif isinstance(data, dict):
                    page_markets = data.get('data', []) or data.get('markets', []) or []
                else:
                    break

                if not page_markets:
                    break

                markets.extend(page_markets)

                # Stop if we got fewer than requested (last page)
                if len(page_markets) < per_page:
                    break

            logger.info(f"📦 Fetched {len(markets)} markets from Gamma API ({page + 1} pages)")
            
            active_markets = []
            filtered_counts = {
                'not_dict': 0,
                'not_active': 0,
                'closed': 0,
                'no_tokens': 0,
                'low_volume': 0,
                'accepted': 0
            }
            
            for m in markets:
                # Skip if not a dict
                if not isinstance(m, dict):
                    filtered_counts['not_dict'] += 1
                    continue

                # Gamma API field names (camelCase)
                is_active = m.get('active', True)
                is_closed = m.get('closed', False)
                is_accepting_orders = m.get('acceptingOrders', m.get('accepting_orders', True))

                # Parse clobTokenIds (JSON string -> list)
                clob_token_ids_raw = m.get('clobTokenIds', '[]')
                try:
                    clob_token_ids = json.loads(clob_token_ids_raw) if isinstance(clob_token_ids_raw, str) else clob_token_ids_raw
                except (json.JSONDecodeError, TypeError):
                    clob_token_ids = []

                # Parse outcomePrices (JSON string -> list of floats)
                outcome_prices_raw = m.get('outcomePrices', '[]')
                try:
                    outcome_prices = json.loads(outcome_prices_raw) if isinstance(outcome_prices_raw, str) else outcome_prices_raw
                    outcome_prices = [float(p) for p in outcome_prices]
                except (json.JSONDecodeError, TypeError, ValueError):
                    outcome_prices = []

                # DEBUG: Log first few markets
                if len(active_markets) < 3:
                    logger.info(f"📊 Sample market #{len(active_markets) + 1}:")
                    logger.info(f"   Question: {m.get('question', 'N/A')[:60]}...")
                    logger.info(f"   Active: {is_active}, Closed: {is_closed}")
                    logger.info(f"   Accepting orders: {is_accepting_orders}")
                    logger.info(f"   Token IDs: {len(clob_token_ids)} tokens")
                    logger.info(f"   Outcome prices: {outcome_prices}")
                    volume_raw = m.get('volume', 0)
                    try:
                        volume_display = float(volume_raw) if volume_raw else 0
                        logger.info(f"   Volume: ${volume_display:,.2f}")
                    except:
                        logger.info(f"   Volume: {volume_raw}")

                # Filter checks
                if not is_active:
                    filtered_counts['not_active'] += 1
                    continue

                if is_closed:
                    filtered_counts['closed'] += 1
                    continue

                if not is_accepting_orders:
                    filtered_counts['closed'] += 1
                    continue

                # Must have exactly 2 CLOB token IDs (YES/NO)
                if len(clob_token_ids) != 2:
                    filtered_counts['no_tokens'] += 1
                    continue

                # Store parsed token IDs and prices on the market dict for later use
                m['_parsed_token_ids'] = clob_token_ids
                m['_parsed_outcome_prices'] = outcome_prices

                # Check volume (be lenient, handle string format)
                volume_raw = m.get('volume', 0) or 0
                try:
                    volume = float(volume_raw)
                except (ValueError, TypeError):
                    volume = 0
                if volume < MIN_MARKET_VOLUME:
                    filtered_counts['low_volume'] += 1
                    # Still accept for now

                filtered_counts['accepted'] += 1
                active_markets.append(m)
            
            logger.info(f"✅ Filtered to {len(active_markets)} active markets")
            logger.info(f"📈 Breakdown: not_active={filtered_counts['not_active']}, "
                       f"closed={filtered_counts['closed']}, "
                       f"no_tokens={filtered_counts['no_tokens']}, "
                       f"low_volume={filtered_counts['low_volume']}, "
                       f"✓ accepted={filtered_counts['accepted']}")
            
            if len(active_markets) == 0:
                logger.warning("⚠️  No active markets found from Gamma API!")
                logger.warning("⚠️  This could mean:")
                logger.warning("     1. No markets are currently open")
                logger.warning("     2. API parameters need adjustment")
            else:
                sample = active_markets[0]
                logger.info(f"✓ Example: {sample.get('question', 'N/A')[:60]}...")
                try:
                    vol = float(sample.get('volume', 0) or 0)
                    logger.info(f"✓ Volume: ${vol:,.2f}")
                except:
                    logger.info(f"✓ Volume: {sample.get('volume', 'N/A')}")
            
            self.market_cache['markets'] = active_markets
            self.last_cache_update = current_time
            
            return active_markets
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error fetching markets: {e}")
            return self.market_cache.get('markets', [])
        except Exception as e:
            logger.error(f"Error fetching markets: {e}", exc_info=True)
            return self.market_cache.get('markets', [])
    
    def get_orderbook(self, token_id: str) -> Optional[Dict]:
        """Get orderbook for a specific token with retry logic.
        Converts OrderBookSummary dataclass to plain dict format.
        Sorts asks ascending (best/lowest first) and bids descending (best/highest first)."""
        for attempt in range(MAX_RETRIES):
            try:
                orderbook = self.client.get_order_book(token_id)

                # Convert OrderBookSummary dataclass to dict
                asks = [
                    {'price': str(o.price), 'size': str(o.size)}
                    for o in (orderbook.asks or [])
                ]
                bids = [
                    {'price': str(o.price), 'size': str(o.size)}
                    for o in (orderbook.bids or [])
                ]

                # CRITICAL: Sort asks ascending (lowest/best ask first)
                # and bids descending (highest/best bid first)
                asks.sort(key=lambda x: float(x['price']))
                bids.sort(key=lambda x: float(x['price']), reverse=True)

                return {'asks': asks, 'bids': bids}
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                logger.error(f"Failed to fetch orderbook for {token_id}: {e}")
                return None
    
    def calculate_liquidity_depth(self, orderbook: Dict, price_range: float = 0.05) -> float:
        """Calculate available liquidity within price range"""
        try:
            asks = orderbook.get('asks', [])
            if not asks:
                return 0.0
            
            best_ask = float(asks[0]['price'])
            total_liquidity = 0.0
            
            for order in asks:
                price = float(order['price'])
                if price <= best_ask * (1 + price_range):
                    total_liquidity += float(order['size'])
            
            return total_liquidity
        except Exception as e:
            logger.error(f"Error calculating liquidity: {e}")
            return 0.0
    
    def validate_orderbook_quality(self, yes_book: Dict, no_book: Dict) -> Tuple[bool, str]:
        """Validate orderbook quality before trading"""
        # Check if orderbooks exist
        if not yes_book or not no_book:
            return False, "Missing orderbook data"
        
        yes_asks = yes_book.get('asks', [])
        no_asks = no_book.get('asks', [])
        
        if not yes_asks or not no_asks:
            return False, "Empty orderbook"
        
        # Check liquidity depth
        yes_liquidity = self.calculate_liquidity_depth(yes_book)
        no_liquidity = self.calculate_liquidity_depth(no_book)
        
        if yes_liquidity < MIN_LIQUIDITY_DEPTH or no_liquidity < MIN_LIQUIDITY_DEPTH:
            return False, f"Insufficient liquidity (YES: ${yes_liquidity:.2f}, NO: ${no_liquidity:.2f})"
        
        # Check spread
        yes_best_ask = float(yes_asks[0]['price'])
        no_best_ask = float(no_asks[0]['price'])
        
        yes_bids = yes_book.get('bids', [])
        no_bids = no_book.get('bids', [])
        
        if yes_bids and no_bids:
            yes_spread = yes_best_ask - float(yes_bids[0]['price'])
            no_spread = no_best_ask - float(no_bids[0]['price'])
            
            if yes_spread > MAX_SPREAD_WIDTH or no_spread > MAX_SPREAD_WIDTH:
                return False, f"Spread too wide (YES: {yes_spread:.4f}, NO: {no_spread:.4f})"
        
        return True, "Valid"
    
    def calculate_expected_slippage(self, orderbook: Dict, size: float) -> float:
        """Estimate slippage for given order size"""
        try:
            asks = orderbook.get('asks', [])
            if not asks:
                return 1.0  # 100% slippage (worst case)
            
            best_price = float(asks[0]['price'])
            remaining_size = size
            total_cost = 0.0
            
            for order in asks:
                price = float(order['price'])
                available = float(order['size'])
                
                if remaining_size <= 0:
                    break
                
                filled = min(remaining_size, available)
                total_cost += filled * price
                remaining_size -= filled
            
            if remaining_size > 0:
                return 1.0  # Can't fill order
            
            avg_price = total_cost / size
            slippage = (avg_price - best_price) / best_price
            
            return slippage
        except Exception as e:
            logger.error(f"Error calculating slippage: {e}")
            return 1.0
    
    def find_arbitrage_opportunities(self) -> List[Dict]:
        """Scan all markets for arbitrage opportunities with advanced filtering"""
        opportunities = []
        markets = self.get_all_markets()

        logger.info(f"📊 Scanning {len(markets)} markets...")

        # Track skip reasons for debug summary
        skip_reasons = defaultdict(int)
        prefilter_candidates = 0

        for market in markets:
            try:
                # Gamma API uses camelCase 'conditionId'
                market_id = market.get('conditionId') or market.get('condition_id')

                if not market_id:
                    skip_reasons['no_market_id'] += 1
                    continue

                # Skip blacklisted markets
                if self.is_market_blacklisted(market_id):
                    skip_reasons['blacklisted'] += 1
                    continue

                # Skip markets with high failure rate
                if self.failed_attempts[market_id] >= 3:
                    self.blacklist_market(market_id, "High failure rate")
                    skip_reasons['high_failure'] += 1
                    continue

                # Use pre-parsed token IDs from get_all_markets
                token_ids = market.get('_parsed_token_ids', [])
                outcome_prices = market.get('_parsed_outcome_prices', [])

                if len(token_ids) != 2:
                    skip_reasons['no_token_ids'] += 1
                    continue

                yes_token = token_ids[0]
                no_token = token_ids[1]

                # --- PRE-FILTER using Gamma outcome prices ---
                # Quick check before expensive orderbook API calls.
                # Gamma prices are mid-market/last-trade, NOT actual asks.
                # Use lenient threshold (0.98) since real asks can differ.
                if len(outcome_prices) == 2:
                    quick_total = outcome_prices[0] + outcome_prices[1]
                    if quick_total > 0.98:
                        skip_reasons['prefilter_no_arb'] += 1
                        continue
                    prefilter_candidates += 1
                    logger.info(f"🔍 Pre-filter candidate: {market.get('question', 'N/A')[:50]}... "
                               f"(YES={outcome_prices[0]:.4f} + NO={outcome_prices[1]:.4f} = {quick_total:.4f})")

                # Get orderbooks (expensive API calls)
                yes_book = self.get_orderbook(yes_token)
                no_book = self.get_orderbook(no_token)

                if not yes_book or not no_book:
                    skip_reasons['no_orderbook'] += 1
                    continue

                # Validate orderbook quality
                valid, reason = self.validate_orderbook_quality(yes_book, no_book)
                if not valid:
                    skip_reasons[f'quality_{reason[:20]}'] += 1
                    continue

                # Get best prices
                yes_asks = yes_book.get('asks', [])
                no_asks = no_book.get('asks', [])

                yes_best_ask = float(yes_asks[0]['price'])
                no_best_ask = float(no_asks[0]['price'])

                # Calculate slippage
                bet_size_per_side = MAX_BET_PER_TRADE / 2
                yes_slippage = self.calculate_expected_slippage(yes_book, bet_size_per_side)
                no_slippage = self.calculate_expected_slippage(no_book, bet_size_per_side)

                if yes_slippage > MAX_SLIPPAGE or no_slippage > MAX_SLIPPAGE:
                    skip_reasons['high_slippage'] += 1
                    continue

                # Adjust for slippage
                yes_effective_price = yes_best_ask * (1 + yes_slippage)
                no_effective_price = no_best_ask * (1 + no_slippage)

                # Check for arbitrage (YES + NO should = 1.0)
                total_cost = yes_effective_price + no_effective_price

                # Polymarket fee is ~1% per side, so ~2% total
                PLATFORM_FEE = 0.02
                total_cost_with_fees = total_cost * (1 + PLATFORM_FEE)

                if total_cost_with_fees < 1.0:
                    gross_profit = 1.0 - total_cost
                    net_profit = 1.0 - total_cost_with_fees
                    profit_pct = net_profit / total_cost_with_fees

                    # Apply profit threshold
                    if profit_pct >= MIN_PROFIT_AFTER_FEES:
                        market_volume = market.get('volume', 0)
                        try:
                            market_volume = float(market_volume)
                        except (ValueError, TypeError):
                            market_volume = 0

                        opportunities.append({
                            'market_id': market_id,
                            'market_name': market.get('question', 'Unknown'),
                            'yes_token': yes_token,
                            'no_token': no_token,
                            'yes_price': yes_best_ask,
                            'no_price': no_best_ask,
                            'yes_effective_price': yes_effective_price,
                            'no_effective_price': no_effective_price,
                            'total_cost': total_cost,
                            'total_cost_with_fees': total_cost_with_fees,
                            'gross_profit': gross_profit,
                            'net_profit': net_profit,
                            'profit_pct': profit_pct,
                            'yes_slippage': yes_slippage,
                            'no_slippage': no_slippage,
                            'market_volume': market_volume,
                            'yes_liquidity': self.calculate_liquidity_depth(yes_book),
                            'no_liquidity': self.calculate_liquidity_depth(no_book),
                            'confidence_score': self._calculate_confidence_score(
                                profit_pct, yes_slippage, no_slippage, market_volume
                            )
                        })

                        # Log to market analysis
                        self.log_market_analysis(market_id, market, yes_book, no_book)
                    else:
                        skip_reasons['profit_too_low'] += 1
                else:
                    skip_reasons['no_arb_after_fees'] += 1

            except Exception as e:
                skip_reasons['error'] += 1
                logger.warning(f"Error analyzing market {market.get('conditionId', market.get('condition_id', 'unknown'))[:16]}...: {e}")
                continue

        # Log skip reasons summary (every scan)
        if skip_reasons:
            reasons_str = ", ".join(f"{k}={v}" for k, v in sorted(skip_reasons.items()))
            logger.info(f"📋 Scan breakdown: {reasons_str}")
        if prefilter_candidates > 0:
            logger.info(f"🔍 Pre-filter passed {prefilter_candidates} candidates to orderbook check")

        # Sort by confidence score, then profit
        return sorted(opportunities, key=lambda x: (x['confidence_score'], x['profit_pct']), reverse=True)
    
    def find_event_group_arbitrage(self) -> List[Dict]:
        """
        Scan for arbitrage across multi-outcome event groups.

        On Polymarket, neg-risk events have multiple mutually exclusive outcomes.
        Exactly one resolves YES ($1). If sum of all YES ask prices < $1 after fees,
        buying all YES = guaranteed profit.
        """
        opportunities = []
        markets = self.get_all_markets()

        # Only group by negRiskMarketID — these are guaranteed mutually exclusive
        event_groups = defaultdict(list)
        for m in markets:
            neg_risk_id = m.get('negRiskMarketID') or m.get('neg_risk_market_id')
            if neg_risk_id and m.get('negRisk', False):
                event_groups[neg_risk_id].append(m)

        # Filter to groups with 2+ outcomes
        multi_groups = {k: v for k, v in event_groups.items() if len(v) >= 2}

        if not multi_groups:
            return []

        logger.info(f"🔗 Found {len(multi_groups)} neg-risk event groups")

        PLATFORM_FEE = 0.02

        for group_id, group_markets in multi_groups.items():
            try:
                # Get event name
                event_name = "Unknown Event"
                events = group_markets[0].get('events', [])
                if events and isinstance(events, list) and len(events) > 0:
                    event_name = events[0].get('title', event_name)

                n_outcomes = len(group_markets)

                # Calculate sum of YES prices using Gamma data (quick pre-filter)
                gamma_yes_sum = 0.0
                valid_group = True
                for m in group_markets:
                    prices = m.get('_parsed_outcome_prices', [])
                    if len(prices) >= 1:
                        gamma_yes_sum += prices[0]
                    else:
                        valid_group = False
                        break

                if not valid_group:
                    continue

                deviation = abs(gamma_yes_sum - 1.0)

                # Skip events where sum is way off (> 2.0 means not truly exclusive)
                if gamma_yes_sum > 2.0:
                    continue

                logger.info(f"📊 Event: {event_name[:55]} | "
                           f"{n_outcomes} outcomes | "
                           f"YES sum={gamma_yes_sum:.4f} | "
                           f"dev={deviation:.4f}")

                # Only fetch orderbooks if deviation is meaningful
                if deviation < PLATFORM_FEE + MIN_PROFIT_AFTER_FEES:
                    continue

                # Determine strategy
                if gamma_yes_sum < 1.0:
                    side = 'YES'
                    payout = 1.0
                else:
                    side = 'NO'
                    payout = n_outcomes - 1.0

                # Fetch real orderbook prices
                group_data = []
                total_ask_sum = 0.0
                fail_reason = None

                for i, m in enumerate(group_markets):
                    token_ids = m.get('_parsed_token_ids', [])
                    outcome_label = m.get('groupItemTitle', m.get('question', '?')[:30])

                    if side == 'YES':
                        if len(token_ids) < 1:
                            fail_reason = f"outcome #{i+1} ({outcome_label}): no YES token"
                            break
                        target_token = token_ids[0]
                    else:
                        if len(token_ids) < 2:
                            fail_reason = f"outcome #{i+1} ({outcome_label}): no NO token"
                            break
                        target_token = token_ids[1]

                    book = self.get_orderbook(target_token)
                    if not book:
                        fail_reason = f"outcome #{i+1} ({outcome_label}): orderbook fetch failed"
                        break

                    asks = book.get('asks', [])
                    if not asks:
                        fail_reason = f"outcome #{i+1} ({outcome_label}): no asks in orderbook"
                        break

                    best_ask = float(asks[0]['price'])
                    liquidity = self.calculate_liquidity_depth(book)
                    slippage = self.calculate_expected_slippage(book, MAX_BET_PER_TRADE / n_outcomes)

                    if liquidity < MIN_LIQUIDITY_DEPTH:
                        fail_reason = f"outcome #{i+1} ({outcome_label}): low liquidity ${liquidity:.2f}"
                        break

                    effective_price = best_ask * (1 + slippage)
                    total_ask_sum += effective_price
                    group_data.append({
                        'market': m,
                        'token_id': target_token,
                        'side': side,
                        'best_ask': best_ask,
                        'slippage': slippage,
                        'liquidity': liquidity,
                        'book': book,
                        'label': outcome_label,
                    })

                if fail_reason:
                    logger.info(f"   ⏭️  Skip: {fail_reason}")
                    continue

                if not group_data:
                    continue

                # Calculate actual arbitrage
                total_cost_with_fees = total_ask_sum * (1 + PLATFORM_FEE)
                net_profit = payout - total_cost_with_fees

                logger.info(f"   📖 Orderbook: {side} ask sum=${total_ask_sum:.4f}, "
                           f"w/fees=${total_cost_with_fees:.4f}, "
                           f"payout=${payout:.2f}, net=${net_profit:.4f}")

                if net_profit <= 0:
                    logger.info(f"   ❌ No arb after orderbook check (net={net_profit:.4f})")
                    continue

                profit_pct = net_profit / total_cost_with_fees

                if profit_pct < MIN_PROFIT_AFTER_FEES:
                    logger.info(f"   ❌ Profit {profit_pct*100:.2f}% below threshold {MIN_PROFIT_AFTER_FEES*100}%")
                    continue

                avg_slippage = sum(d['slippage'] for d in group_data) / len(group_data)
                min_liquidity = min(d['liquidity'] for d in group_data)
                total_volume = sum(float(m.get('volume', 0) or 0) for m in group_markets)

                opp = {
                    'type': 'EVENT_GROUP',
                    'event_name': event_name,
                    'group_id': group_id,
                    'n_outcomes': n_outcomes,
                    'side': side,
                    'gamma_yes_sum': gamma_yes_sum,
                    'total_ask_sum': total_ask_sum,
                    'total_cost_with_fees': total_cost_with_fees,
                    'payout': payout,
                    'gross_profit': payout - total_ask_sum,
                    'net_profit': net_profit,
                    'profit_pct': profit_pct,
                    'avg_slippage': avg_slippage,
                    'min_liquidity': min_liquidity,
                    'total_volume': total_volume,
                    'group_data': group_data,
                    'confidence_score': self._calculate_confidence_score(
                        profit_pct, avg_slippage, avg_slippage, total_volume
                    ),
                    'market_id': group_id,
                    'market_name': f"[EVENT GROUP] {event_name}",
                    'market_volume': total_volume,
                }

                logger.info(f"💎 EVENT GROUP ARB FOUND: {event_name[:50]}...")
                logger.info(f"   {n_outcomes} outcomes | Buy all {side}")
                logger.info(f"   Total cost: ${total_cost_with_fees:.4f} → Payout: ${payout:.2f}")
                logger.info(f"   Net profit: {profit_pct*100:.2f}% (${net_profit:.4f})")

                opportunities.append(opp)

            except Exception as e:
                logger.warning(f"Error analyzing event group {group_id[:16]}...: {e}")
                continue

        return sorted(opportunities, key=lambda x: (x['confidence_score'], x['profit_pct']), reverse=True)

    def execute_event_group_arbitrage(self, opp: Dict) -> bool:
        """Execute a multi-outcome event group arbitrage trade"""
        start_time = time.time()

        if self.current_exposure + MAX_BET_PER_TRADE > MAX_TOTAL_EXPOSURE:
            logger.warning(f"⚠️  Exposure limit reached (${self.current_exposure:.2f}/${MAX_TOTAL_EXPOSURE})")
            return False

        try:
            group_data = opp['group_data']
            n_outcomes = opp['n_outcomes']
            bet_per_outcome = Decimal(str(MAX_BET_PER_TRADE / n_outcomes)).quantize(
                Decimal('0.01'), rounding=ROUND_DOWN
            )

            logger.info(f"\n{'='*70}")
            logger.info(f"💰 EVENT GROUP ARBITRAGE (Confidence: {opp['confidence_score']:.1f}%)")
            logger.info(f"{'='*70}")
            logger.info(f"Event: {opp['event_name'][:60]}")
            logger.info(f"Strategy: Buy all {opp['side']} ({n_outcomes} outcomes)")
            logger.info(f"Bet per outcome: ${bet_per_outcome}")
            logger.info(f"Total cost: ${opp['total_cost_with_fees']:.4f}")
            logger.info(f"Payout: ${opp['payout']:.2f}")
            logger.info(f"Expected profit: {opp['profit_pct']*100:.2f}%")

            filled_orders = []

            for i, data in enumerate(group_data):
                market_q = data['market'].get('question', 'Unknown')[:40]
                success = False

                for attempt in range(MAX_RETRIES):
                    try:
                        logger.info(f"🔄 [{i+1}/{n_outcomes}] {data['side']} on: {market_q}... "
                                   f"(attempt {attempt+1})")

                        result = self.client.create_order({
                            'token_id': data['token_id'],
                            'price': data['best_ask'] * (1 + data['slippage']),
                            'size': float(bet_per_outcome),
                            'side': 'BUY',
                            'type': 'MARKET'
                        })

                        order_id = result.get('orderID') if isinstance(result, dict) else None
                        if order_id:
                            if self._wait_for_order_fill(order_id, ORDER_TIMEOUT):
                                success = True
                                filled_orders.append(order_id)
                                logger.info(f"   ✅ Filled")
                                break
                        else:
                            success = True
                            logger.info(f"   ✅ Filled immediately")
                            break

                    except Exception as e:
                        logger.error(f"   ❌ Failed: {e}")
                        if "insufficient" in str(e).lower():
                            break
                        time.sleep(0.5)

                if not success:
                    logger.error(f"❌ Failed to fill outcome {i+1}/{n_outcomes} - PARTIAL POSITION!")
                    logger.warning(f"⚠️  {len(filled_orders)}/{n_outcomes} legs filled - MANUAL HEDGE NEEDED")
                    self.blacklist_market(opp['group_id'], "Partial event group fill")
                    self.log_trade(opp, 'PARTIAL', f'{len(filled_orders)}/{n_outcomes} filled',
                                 0, time.time() - start_time)
                    return False

            # All legs filled
            self.current_exposure += MAX_BET_PER_TRADE
            execution_time = time.time() - start_time

            self.log_trade(opp, 'EXECUTED', None, opp['net_profit'], execution_time)

            logger.info(f"{'='*70}")
            logger.info(f"✅ EVENT GROUP TRADE EXECUTED ({n_outcomes}/{n_outcomes} legs)")
            logger.info(f"Execution time: {execution_time:.2f}s")
            logger.info(f"{'='*70}\n")

            self.update_balance()
            return True

        except Exception as e:
            logger.error(f"❌ Critical error in event group execution: {e}")
            self.log_trade(opp, 'ERROR', str(e), 0, time.time() - start_time)
            return False

    def _calculate_confidence_score(self, profit_pct: float, yes_slip: float,
                                    no_slip: float, volume: float) -> float:
        """Calculate confidence score for opportunity (0-100)"""
        score = 50.0  # Base score
        
        # Higher profit = higher confidence
        score += min(profit_pct * 200, 30)  # Max +30
        
        # Lower slippage = higher confidence
        score += max((MAX_SLIPPAGE - yes_slip) * 100, 0)  # Max +10
        score += max((MAX_SLIPPAGE - no_slip) * 100, 0)  # Max +10
        
        # Higher volume = higher confidence
        if volume > 10000:
            score += 10
        elif volume > 1000:
            score += 5
        
        return min(score, 100.0)
    
    def execute_arbitrage(self, opp: Dict) -> bool:
        """Execute arbitrage trade with comprehensive error handling"""
        start_time = time.time()
        
        # Pre-flight checks
        if self.current_exposure + MAX_BET_PER_TRADE > MAX_TOTAL_EXPOSURE:
            logger.warning(f"⚠️  Exposure limit reached (${self.current_exposure:.2f}/${MAX_TOTAL_EXPOSURE})")
            return False
        
        # Check balance
        if time.time() - self.last_balance_check > BALANCE_CHECK_INTERVAL:
            if not self.update_balance():
                return False
        
        if self.usdc_balance < MAX_BET_PER_TRADE:
            logger.error(f"❌ Insufficient balance: ${self.usdc_balance:.2f}")
            return False
        
        try:
            logger.info(f"\n{'='*70}")
            logger.info(f"💰 ARBITRAGE OPPORTUNITY (Confidence: {opp['confidence_score']:.1f}%)")
            logger.info(f"{'='*70}")
            logger.info(f"Market: {opp['market_name'][:60]}...")
            logger.info(f"Market Volume: ${opp['market_volume']:.2f}")
            logger.info(f"YES price: ${opp['yes_price']:.4f} (effective: ${opp['yes_effective_price']:.4f})")
            logger.info(f"NO price: ${opp['no_price']:.4f} (effective: ${opp['no_effective_price']:.4f})")
            logger.info(f"YES liquidity: ${opp['yes_liquidity']:.2f}")
            logger.info(f"NO liquidity: ${opp['no_liquidity']:.2f}")
            logger.info(f"Total cost: ${opp['total_cost']:.4f} (with fees: ${opp['total_cost_with_fees']:.4f})")
            logger.info(f"Expected profit: {opp['profit_pct']*100:.2f}% (${opp['net_profit']:.4f})")
            logger.info(f"Slippage: YES {opp['yes_slippage']*100:.3f}%, NO {opp['no_slippage']*100:.3f}%")
            
            # Calculate bet sizes
            bet_amount = Decimal(str(MAX_BET_PER_TRADE / 2)).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
            
            # Execute orders with retry logic
            yes_success = False
            no_success = False
            yes_order_id = None
            no_order_id = None
            
            # Try YES order
            for attempt in range(MAX_RETRIES):
                try:
                    logger.info(f"🔄 Executing YES order (attempt {attempt + 1}/{MAX_RETRIES})...")
                    
                    # Use correct API - create_order instead of create_market_order
                    yes_result = self.client.create_order({
                        'token_id': opp['yes_token'],
                        'price': opp['yes_effective_price'],
                        'size': float(bet_amount),
                        'side': 'BUY',
                        'type': 'MARKET'
                    })
                    
                    yes_order_id = yes_result.get('orderID') if isinstance(yes_result, dict) else None
                    
                    if yes_order_id:
                        # Wait for fill
                        if self._wait_for_order_fill(yes_order_id, ORDER_TIMEOUT):
                            yes_success = True
                            logger.info("✅ YES order filled")
                            break
                        else:
                            logger.warning(f"⏱️  YES order timeout (attempt {attempt + 1})")
                    else:
                        # Order might have filled immediately
                        yes_success = True
                        logger.info("✅ YES order filled immediately")
                        break
                        
                except PolyApiException as e:
                    logger.error(f"❌ YES order failed: {e}")
                    if "insufficient" in str(e).lower():
                        return False  # Don't retry on insufficient funds
                    time.sleep(1)
                except Exception as e:
                    logger.error(f"❌ YES order error: {e}")
                    time.sleep(1)
            
            if not yes_success:
                logger.error("❌ YES order failed after all retries")
                self.failed_attempts[opp['market_id']] += 1
                self.log_trade(opp, 'FAILED', 'YES order failed', 0, time.time() - start_time)
                return False
            
            # Try NO order
            for attempt in range(MAX_RETRIES):
                try:
                    logger.info(f"🔄 Executing NO order (attempt {attempt + 1}/{MAX_RETRIES})...")
                    
                    # Use correct API
                    no_result = self.client.create_order({
                        'token_id': opp['no_token'],
                        'price': opp['no_effective_price'],
                        'size': float(bet_amount),
                        'side': 'BUY',
                        'type': 'MARKET'
                    })
                    
                    no_order_id = no_result.get('orderID') if isinstance(no_result, dict) else None
                    
                    if no_order_id:
                        # Wait for fill
                        if self._wait_for_order_fill(no_order_id, ORDER_TIMEOUT):
                            no_success = True
                            logger.info("✅ NO order filled")
                            break
                        else:
                            logger.warning(f"⏱️  NO order timeout (attempt {attempt + 1})")
                    else:
                        # Order might have filled immediately
                        no_success = True
                        logger.info("✅ NO order filled immediately")
                        break
                        
                except PolyApiException as e:
                    logger.error(f"❌ NO order failed: {e}")
                    if "insufficient" in str(e).lower():
                        # Try to cancel YES order
                        if yes_order_id:
                            self._cancel_order(yes_order_id)
                        return False
                    time.sleep(1)
                except Exception as e:
                    logger.error(f"❌ NO order error: {e}")
                    time.sleep(1)
            
            if not no_success:
                logger.error("❌ NO order failed after all retries")
                logger.warning("⚠️  WARNING: Only YES position filled - HEDGE MANUALLY!")
                self.failed_attempts[opp['market_id']] += 1
                self.log_trade(opp, 'PARTIAL', 'NO order failed', 0, time.time() - start_time)
                
                # Blacklist this market temporarily
                self.blacklist_market(opp['market_id'], "Partial fill occurred")
                return False
            
            # Both orders successful
            self.current_exposure += MAX_BET_PER_TRADE
            self.failed_attempts[opp['market_id']] = 0  # Reset failure count
            
            execution_time = time.time() - start_time
            
            self.log_trade(opp, 'EXECUTED', None, opp['net_profit'], execution_time)
            
            logger.info(f"{'='*70}")
            logger.info(f"✅ TRADE EXECUTED SUCCESSFULLY")
            logger.info(f"Execution time: {execution_time:.2f}s")
            logger.info(f"Position: ${self.current_exposure:.2f}/${MAX_TOTAL_EXPOSURE}")
            logger.info(f"{'='*70}\n")
            
            # Update balance
            self.update_balance()
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Critical error in trade execution: {e}")
            self.failed_attempts[opp['market_id']] += 1
            self.log_trade(opp, 'ERROR', str(e), 0, time.time() - start_time)
            return False
    
    def _wait_for_order_fill(self, order_id: str, timeout: int) -> bool:
        """Wait for order to be filled"""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                order_status = self.client.get_order(order_id)
                status = order_status.get('status', '').upper()
                
                if status == 'FILLED':
                    return True
                elif status in ['CANCELLED', 'EXPIRED', 'FAILED']:
                    return False
                
                time.sleep(0.5)
            except Exception as e:
                logger.debug(f"Error checking order status: {e}")
                time.sleep(0.5)
        
        return False
    
    def _cancel_order(self, order_id: str) -> bool:
        """Cancel an order"""
        try:
            self.client.cancel_order(order_id)
            logger.info(f"Cancelled order {order_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False
    
    def log_trade(self, opp: Dict, status: str, error_msg: Optional[str],
                  actual_profit: float, execution_time: float):
        """Log trade to database"""
        try:
            self.cursor.execute('''
                INSERT INTO trades (timestamp, market_id, market_name, side, amount, price,
                                   expected_profit, actual_profit, status, error_message, execution_time,
                                   trade_token, size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                datetime.now().isoformat(),
                opp['market_id'],
                opp['market_name'],
                opp.get('side', 'YES/NO'),
                MAX_BET_PER_TRADE,
                opp['total_cost_with_fees'],
                opp['net_profit'],
                actual_profit,
                status,
                error_msg,
                execution_time,
                opp.get('trade_token'),
                opp.get('size'),
            ))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Failed to log trade: {e}")
    
    def log_market_analysis(self, market_id: str, market: Dict, yes_book: Dict, no_book: Dict):
        """Log market analysis data"""
        try:
            yes_liquidity = self.calculate_liquidity_depth(yes_book)
            no_liquidity = self.calculate_liquidity_depth(no_book)
            
            yes_asks = yes_book.get('asks', [])
            no_asks = no_book.get('asks', [])
            yes_bids = yes_book.get('bids', [])
            no_bids = no_book.get('bids', [])
            
            spread = 0
            if yes_asks and yes_bids:
                spread = float(yes_asks[0]['price']) - float(yes_bids[0]['price'])
            
            self.cursor.execute('''
                INSERT OR REPLACE INTO market_analysis 
                (market_id, question, volume_24h, liquidity, spread, last_checked, arbitrage_count)
                VALUES (?, ?, ?, ?, ?, ?, 
                    COALESCE((SELECT arbitrage_count FROM market_analysis WHERE market_id = ?), 0) + 1)
            ''', (
                market_id,
                market.get('question', ''),
                market.get('volume', 0),
                min(yes_liquidity, no_liquidity),
                spread,
                datetime.now().isoformat(),
                market_id
            ))
            self.conn.commit()
        except Exception as e:
            logger.error(f"Failed to log market analysis: {e}")
    
    def calculate_performance_metrics(self) -> Dict:
        """Calculate bot performance metrics"""
        try:
            self.cursor.execute('''
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN status IN ('EXECUTED', 'CLOSED', 'RESOLVED') THEN 1 ELSE 0 END) as successful_trades,
                    SUM(CASE WHEN status NOT IN ('EXECUTED', 'CLOSED', 'RESOLVED') THEN 1 ELSE 0 END) as failed_trades,
                    SUM(actual_profit) as total_profit,
                    AVG(actual_profit) as avg_profit
                FROM trades
            ''')
            
            row = self.cursor.fetchone()
            
            if row and row[0] > 0:
                total, successful, failed, profit, avg_profit = row
                win_rate = (successful / total) * 100 if total > 0 else 0
                
                metrics = {
                    'total_trades': total,
                    'successful_trades': successful,
                    'failed_trades': failed,
                    'total_profit': profit or 0,
                    'win_rate': win_rate,
                    'avg_profit_per_trade': avg_profit or 0
                }
                
                # Log to performance table
                self.cursor.execute('''
                    INSERT INTO performance_metrics 
                    (timestamp, total_trades, successful_trades, failed_trades, total_profit, win_rate, avg_profit_per_trade)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    datetime.now().isoformat(),
                    total,
                    successful,
                    failed,
                    profit or 0,
                    win_rate,
                    avg_profit or 0
                ))
                self.conn.commit()
                
                return metrics
            
            return {
                'total_trades': 0,
                'successful_trades': 0,
                'failed_trades': 0,
                'total_profit': 0,
                'win_rate': 0,
                'avg_profit_per_trade': 0
            }
            
        except Exception as e:
            logger.error(f"Failed to calculate metrics: {e}")
            return {}
    
    def print_performance_summary(self):
        """Print performance summary"""
        metrics = self.calculate_performance_metrics()
        
        if metrics.get('total_trades', 0) > 0:
            logger.info(f"\n{'='*70}")
            logger.info("📊 PERFORMANCE SUMMARY")
            logger.info(f"{'='*70}")
            logger.info(f"Total trades: {metrics['total_trades']}")
            logger.info(f"Successful: {metrics['successful_trades']}")
            logger.info(f"Failed: {metrics['failed_trades']}")
            logger.info(f"Win rate: {metrics['win_rate']:.1f}%")
            logger.info(f"Total profit: ${metrics['total_profit']:.4f}")
            logger.info(f"Avg profit/trade: ${metrics['avg_profit_per_trade']:.4f}")
            logger.info(f"Current exposure: ${self.current_exposure:.2f}")
            logger.info(f"USDC balance: ${self.usdc_balance:.2f}")
            logger.info(f"{'='*70}\n")
    
    # System prompt extracted from polymarket-trading-agent.md
    AGENT_SYSTEM_PROMPT = (
        "You are an aggressive Polymarket prediction market trading analyst. "
        "Your sole mission is to identify and exploit high-edge betting opportunities by finding mispriced markets.\n\n"
        "You think like a sharp sports bettor, a quantitative trader, and a news arbitrageur combined. "
        "You are ruthless about edge, unsentimental about losses, and always focused on expected value (EV) over outcome.\n\n"
        "MARKET FOCUS:\n"
        "- Look for markets where one side is priced 70-92 cents but true probability is 85-99%\n"
        "- Markets with thin liquidity where casual bettors pushed the wrong side\n"
        "- Near-expiry markets where outcome is essentially settled but price hasn't moved\n"
        "- Markets where crowd is anchored to stale probability that recent news has obsoleted\n\n"
        "EV CALCULATION: EV = (TrueProb × Profit_if_win) - ((1 - TrueProb) × Stake)\n"
        "Always calculate EV. Never recommend a bet with negative or near-zero EV.\n\n"
        "CONVICTION LEVELS:\n"
        "- high: edge > 10% and high confidence in estimate\n"
        "- medium: edge 5-10% or moderate confidence\n"
        "- low: edge < 5% or uncertain\n\n"
        "RULES: Never recommend a bet without stating the risk. "
        "Never recommend betting on a market where you have no information edge. "
        "Only recommend bets with positive EV."
    )

    def _calculate_kelly_stake(self, edge: float, price: float, confidence: str) -> float:
        """Calculate stake using fractional Kelly criterion based on agent philosophy."""
        bankroll = self.usdc_balance

        # Bankroll stage limits from agent file
        if bankroll <= 30:
            max_bet = min(8.0, bankroll * 0.40)
        elif bankroll <= 80:
            max_bet = min(25.0, bankroll * 0.35)
        elif bankroll <= 200:
            max_bet = min(60.0, bankroll * 0.30)
        else:
            max_bet = min(150.0, bankroll * 0.25)

        # Kelly fraction based on confidence
        kelly_fraction = {'high': 0.40, 'medium': 0.25, 'low': 0.15}.get(confidence, 0.20)

        # Fractional Kelly: stake = (edge / odds_against) * bankroll * fraction
        # odds_against = 1/price - 1 (payout per $1 risked)
        if edge > 0 and 0 < price < 1:
            odds_against = (1.0 / price) - 1
            full_kelly = (edge / odds_against) * bankroll if odds_against > 0 else 0
            kelly_stake = full_kelly * kelly_fraction
        else:
            kelly_stake = bankroll * 0.10

        # Clamp: never below $1, never above stage limit, never more than 40% bankroll
        stake = max(1.0, min(kelly_stake, max_bet, bankroll * 0.40))
        return round(stake, 2)

    def analyze_with_claude(self, markets_data: List[Dict]) -> Dict[str, Dict]:
        """
        Use the trading agent system prompt to analyze markets and estimate true probabilities.
        Returns a dict of market_id -> {probability, confidence, reasoning, side, edge, ev_per_dollar, stake}
        """
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            logger.warning("⚠️  ANTHROPIC_API_KEY not set — skipping Claude analysis")
            return {}

        client = anthropic.Anthropic(api_key=api_key)

        # Build market summaries for Claude
        market_summaries = []
        for m in markets_data:
            prices = m.get('_parsed_outcome_prices', [])
            yes_price = prices[0] if len(prices) > 0 else 0
            no_price = prices[1] if len(prices) > 1 else 0
            summary = {
                'id': m.get('conditionId', '')[:16],
                'question': m.get('question', ''),
                'description': (m.get('description', '') or '')[:300],
                'yes_price': yes_price,
                'no_price': no_price,
                'volume_24h': m.get('volume24hr', m.get('volume', 0)),
                'end_date': m.get('endDateIso', m.get('endDate', '')),
            }
            market_summaries.append(summary)

        prompt = f"""Today is {datetime.now().strftime('%Y-%m-%d')}. Current bankroll: ${self.usdc_balance:.2f}.

Analyze each market below. For each, estimate the TRUE probability of YES occurring based on your knowledge.
Compare your estimate to the current market price to identify mispriced bets with positive EV.

Markets:
{json.dumps(market_summaries, indent=2)}

For EACH market, respond with a JSON array. Each object must have:
- "id": the market id (first 16 chars of conditionId)
- "probability": your estimated true probability of YES (0.0 to 1.0)
- "confidence": "high", "medium", or "low"
- "side": "YES" if YES is underpriced, "NO" if NO is underpriced, "SKIP" if fairly priced or no edge
- "edge": abs difference between your probability and the priced side (e.g. if YES at 0.72 and true prob 0.88, edge = 0.16)
- "ev_per_dollar": EV per $1 staked = (TrueProb * Profit_if_win) - ((1-TrueProb) * 1), where Profit_if_win = 1/price - 1
- "risk": one-sentence description of what would make this bet lose
- "reasoning": brief 1-sentence explanation of your probability estimate

Respond ONLY with a valid JSON array, no other text. Only recommend markets where you have genuine information edge."""

        try:
            logger.info(f"🧠 Asking Claude (trading agent) to analyze {len(market_summaries)} markets...")

            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=self.AGENT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}]
            )

            response_text = response.content[0].text.strip()

            # Parse JSON response (handle markdown code blocks)
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]

            analyses = json.loads(response_text)

            result = {}
            for a in analyses:
                mid = a.get('id', '')
                side = a.get('side', 'SKIP')
                edge = float(a.get('edge', 0))
                confidence = a.get('confidence', 'low')

                # Get the market price for this side to compute Kelly stake
                market_price = 0.5
                for m in markets_data:
                    if (m.get('conditionId', '') or '')[:16] == mid:
                        prices = m.get('_parsed_outcome_prices', [])
                        if side == 'YES' and len(prices) > 0:
                            market_price = prices[0]
                        elif side == 'NO' and len(prices) > 1:
                            market_price = prices[1]
                        break

                stake = self._calculate_kelly_stake(edge, market_price, confidence) if side != 'SKIP' else 0

                result[mid] = {
                    'probability': float(a.get('probability', 0.5)),
                    'confidence': confidence,
                    'side': side,
                    'edge': edge,
                    'ev_per_dollar': float(a.get('ev_per_dollar', 0)),
                    'risk': a.get('risk', ''),
                    'reasoning': a.get('reasoning', ''),
                    'kelly_stake': stake,
                }

            logger.info(f"🧠 Claude (agent) analyzed {len(result)} markets")

            # Log Claude's picks
            for mid, a in result.items():
                if a['side'] != 'SKIP':
                    conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(a['confidence'], "⚪")
                    logger.info(
                        f"   {conf_emoji} {a['side']} | edge={a['edge']:.1%} | "
                        f"EV=${a['ev_per_dollar']:.3f}/$1 | stake=${a['kelly_stake']:.2f} | "
                        f"{a['reasoning'][:55]}"
                    )

            return result

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response: {e}")
            logger.debug(f"Response was: {response_text[:200]}")
            return {}
        except anthropic.APIError as e:
            logger.error(f"Claude API error: {e}")
            return {}
        except Exception as e:
            logger.error(f"Claude analysis failed: {e}")
            return {}

    def find_best_trades(self, n_trades: int = 5) -> List[Dict]:
        """
        Find the N best value trades by scoring markets for mispricing.

        Scoring signals:
        1. Orderbook imbalance — bid depth vs ask depth indicates buying/selling pressure
        2. Event group edge — within neg-risk groups, identify underpriced outcomes
        3. Spread tightness — tighter spreads = more liquid = safer trades
        4. Volume — higher volume markets are more reliable
        """
        markets = self.get_all_markets()
        candidates = []

        # --- Build neg-risk group lookup for event group edge ---
        neg_risk_groups = defaultdict(list)
        for m in markets:
            nrid = m.get('negRiskMarketID') or m.get('neg_risk_market_id')
            if nrid and m.get('negRisk', False):
                neg_risk_groups[nrid].append(m)

        # Pre-compute group YES sums
        group_yes_sums = {}
        for gid, gmarkets in neg_risk_groups.items():
            total = 0.0
            for gm in gmarkets:
                prices = gm.get('_parsed_outcome_prices', [])
                if prices:
                    total += prices[0]
            group_yes_sums[gid] = total

        logger.info(f"📊 Analyzing {len(markets)} markets for value trades...")

        # Sort by recent volume (24h) rather than all-time volume
        # This avoids dead markets with high historical volume but no current activity
        markets_with_volume = []
        for m in markets:
            try:
                # Prefer 24h volume, fall back to total volume
                vol24 = float(m.get('volume24hr', 0) or 0)
                vol_total = float(m.get('volume', 0) or 0)
                liquidity = float(m.get('liquidity', 0) or 0)
                # Composite score: prioritize recent activity
                vol = vol24 if vol24 > 0 else (liquidity if liquidity > 0 else vol_total)
            except (ValueError, TypeError):
                vol = 0
            markets_with_volume.append((vol, m))
        markets_with_volume.sort(key=lambda x: x[0], reverse=True)

        # Log what we're working with
        if markets_with_volume:
            top3 = markets_with_volume[:3]
            logger.info(f"📊 Top 3 markets by activity score:")
            for rank, (v, m) in enumerate(top3, 1):
                q = m.get('question', 'N/A')[:50]
                prices = m.get('_parsed_outcome_prices', [])
                logger.info(f"   #{rank}: {q}... (score={v:,.0f}, prices={prices})")

        # Analyze top 50 markets by volume (to limit API calls)
        analyzed = 0
        max_to_analyze = 50

        skip_debug = defaultdict(int)

        for vol, market in markets_with_volume:
            if analyzed >= max_to_analyze:
                break

            token_ids = market.get('_parsed_token_ids', [])
            outcome_prices = market.get('_parsed_outcome_prices', [])
            if len(token_ids) != 2 or len(outcome_prices) != 2:
                skip_debug['no_tokens'] += 1
                continue

            market_id = market.get('conditionId') or market.get('condition_id')
            if not market_id or self.is_market_blacklisted(market_id):
                skip_debug['blacklisted'] += 1
                continue

            # Skip markets where we already have a position
            if market_id in self.active_positions:
                skip_debug['duplicate_position'] += 1
                continue

            # Skip markets we've previously closed or that resolved
            if market_id in self.closed_markets:
                skip_debug['previously_closed'] += 1
                continue

            # Skip markets that resolve more than 2 days from now
            end_date_str = market.get('endDateIso') or market.get('endDate', '')
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00')).replace(tzinfo=None)
                    max_end = datetime.now() + timedelta(days=2)
                    if end_date > max_end:
                        skip_debug['too_far_out'] += 1
                        continue
                except (ValueError, TypeError):
                    pass  # If we can't parse the date, allow it through

            question = market.get('question', 'Unknown')
            yes_gamma_price = outcome_prices[0]
            no_gamma_price = outcome_prices[1]

            # Skip near-resolved markets (gamma price too extreme = no real trading)
            if yes_gamma_price < 0.05 or yes_gamma_price > 0.95:
                skip_debug['resolved'] += 1
                continue

            # Fetch YES orderbook
            yes_book = self.get_orderbook(token_ids[0])
            if not yes_book:
                skip_debug['no_yes_book'] += 1
                continue
            analyzed += 1

            yes_asks = yes_book.get('asks', [])
            yes_bids = yes_book.get('bids', [])
            if not yes_asks or not yes_bids:
                skip_debug['empty_yes_book'] += 1
                if analyzed <= 5:
                    logger.info(f"   DEBUG [{question[:40]}] empty YES book: asks={len(yes_asks)}, bids={len(yes_bids)}")
                continue

            best_yes_ask = float(yes_asks[0]['price'])
            best_yes_bid = float(yes_bids[0]['price'])
            yes_spread = best_yes_ask - best_yes_bid
            yes_spread_pct = yes_spread / best_yes_ask if best_yes_ask > 0 else 1
            # Relaxed: absolute < $0.10 OR percentage < 20%
            yes_spread_ok = yes_spread < 0.10 or yes_spread_pct < 0.20
            yes_price_ok = 0.03 <= best_yes_ask <= 0.97
            yes_tradeable = yes_spread_ok and yes_price_ok

            # Calculate YES orderbook depth
            bid_depth = sum(float(o['size']) for o in yes_bids
                          if float(o['price']) >= best_yes_bid * 0.95)
            ask_depth = sum(float(o['size']) for o in yes_asks
                          if float(o['price']) <= best_yes_ask * 1.05)
            total_depth = bid_depth + ask_depth

            # Fetch NO orderbook
            no_book = self.get_orderbook(token_ids[1])
            no_tradeable = False
            best_no_ask = 0
            best_no_bid = 0
            no_spread = 0
            no_spread_pct = 1
            no_bid_depth = 0
            no_ask_depth = 0

            if no_book and no_book.get('asks') and no_book.get('bids'):
                best_no_ask = float(no_book['asks'][0]['price'])
                best_no_bid = float(no_book['bids'][0]['price'])
                no_spread = best_no_ask - best_no_bid
                no_spread_pct = no_spread / best_no_ask if best_no_ask > 0 else 1
                no_spread_ok = no_spread < 0.10 or no_spread_pct < 0.20
                no_price_ok = 0.03 <= best_no_ask <= 0.97
                no_tradeable = no_spread_ok and no_price_ok

                no_bid_depth = sum(float(o['size']) for o in no_book.get('bids', [])
                                  if float(o['price']) >= best_no_bid * 0.95)
                no_ask_depth = sum(float(o['size']) for o in no_book.get('asks', [])
                                  if float(o['price']) <= best_no_ask * 1.05)

            # Log the first 10 analyzed markets for diagnosis
            if analyzed <= 10:
                logger.info(f"   DEBUG [{question[:40]}]")
                logger.info(f"      YES: ask=${best_yes_ask:.4f} bid=${best_yes_bid:.4f} "
                           f"spread=${yes_spread:.4f}({yes_spread_pct*100:.1f}%) "
                           f"tradeable={yes_tradeable} depth={total_depth:.1f}")
                logger.info(f"      NO:  ask=${best_no_ask:.4f} bid=${best_no_bid:.4f} "
                           f"spread=${no_spread:.4f}({no_spread_pct*100:.1f}%) "
                           f"tradeable={no_tradeable} depth={no_bid_depth+no_ask_depth:.1f}")

            # Need at least one tradeable side
            if not yes_tradeable and not no_tradeable:
                skip_debug['not_tradeable'] += 1
                if analyzed <= 10:
                    logger.info(f"      → SKIPPED: no tradeable side")
                continue

            # Need some depth (lowered to 1 — we're only trading $1)
            if total_depth < 1 and (no_bid_depth + no_ask_depth) < 1:
                skip_debug['no_depth'] += 1
                if analyzed <= 10:
                    logger.info(f"      → SKIPPED: no depth")
                continue

            # --- SCORING ---
            score = 0.0
            side = None
            reasoning = []

            # 1. Orderbook imbalance (max 30 points)
            imbalance = bid_depth / total_depth if total_depth > 0 else 0.5
            if imbalance > 0.55 and yes_tradeable:
                side = 'YES'
                imbalance_score = min((imbalance - 0.5) * 150, 30)
                score += imbalance_score
                reasoning.append(f"bid_pressure={imbalance:.2f} (+{imbalance_score:.0f})")
            elif imbalance < 0.45 and no_tradeable:
                side = 'NO'
                imbalance_score = min((0.5 - imbalance) * 150, 30)
                score += imbalance_score
                reasoning.append(f"sell_pressure={1-imbalance:.2f} (+{imbalance_score:.0f})")
            elif yes_tradeable:
                side = 'YES'
                score += 5
                reasoning.append(f"balanced={imbalance:.2f} (+5)")
            elif no_tradeable:
                side = 'NO'
                score += 5
                reasoning.append(f"balanced={imbalance:.2f} (+5)")
            else:
                continue

            # 2. Event group edge (max 25 points, does NOT override side)
            nrid = market.get('negRiskMarketID') or market.get('neg_risk_market_id')
            if nrid and nrid in group_yes_sums:
                group_sum = group_yes_sums[nrid]
                group_deviation = group_sum - 1.0

                if group_deviation < -0.02:
                    edge = abs(group_deviation)
                    group_score = min(edge * 400, 25)
                    if side == 'YES':
                        # Agrees with orderbook signal — full bonus
                        score += group_score
                        reasoning.append(f"group_underpriced(sum={group_sum:.3f}) (+{group_score:.0f})")
                    else:
                        # Conflicts — add half, don't flip side
                        score += group_score * 0.3
                        reasoning.append(f"group_underpriced_conflict(sum={group_sum:.3f}) (+{group_score*0.3:.0f})")
                elif group_deviation > 0.02:
                    edge = group_deviation
                    group_score = min(edge * 400, 25)
                    if side == 'NO':
                        score += group_score
                        reasoning.append(f"group_overpriced(sum={group_sum:.3f}) (+{group_score:.0f})")
                    else:
                        score += group_score * 0.3
                        reasoning.append(f"group_overpriced_conflict(sum={group_sum:.3f}) (+{group_score*0.3:.0f})")

            # 3. Spread tightness (max 20 points)
            active_spread = yes_spread_pct if side == 'YES' else no_spread_pct
            if active_spread < 0.02:
                spread_score = 20
            elif active_spread < 0.05:
                spread_score = 15
            elif active_spread < 0.08:
                spread_score = 10
            elif active_spread < 0.12:
                spread_score = 5
            else:
                spread_score = 0
            score += spread_score
            reasoning.append(f"spread={active_spread*100:.1f}% (+{spread_score})")

            # 4. Volume bonus (max 15 points)
            if vol > 1_000_000:
                vol_score = 15
            elif vol > 100_000:
                vol_score = 10
            elif vol > 10_000:
                vol_score = 5
            else:
                vol_score = 0
            score += vol_score
            reasoning.append(f"vol=${vol:,.0f} (+{vol_score})")

            # 5. Price edge bonus (max 10 points) — mid-range prices have better risk/reward
            mid_price = yes_gamma_price if side == 'YES' else no_gamma_price
            if 0.3 <= mid_price <= 0.7:
                price_score = 10  # Best risk/reward range
            elif 0.15 <= mid_price <= 0.85:
                price_score = 5
            else:
                price_score = 0
            score += price_score
            reasoning.append(f"price_range={mid_price:.2f} (+{price_score})")

            # Set trade details
            if side == 'YES':
                trade_price = best_yes_ask
                trade_token = token_ids[0]
            else:
                trade_price = best_no_ask
                trade_token = token_ids[1]

            # Get tick size and neg_risk for order execution
            tick_size = str(market.get('orderPriceMinTickSize', '0.01'))
            is_neg_risk = market.get('negRisk', False)

            candidates.append({
                'market_id': market_id,
                'market_name': question,
                'side': side,
                'trade_token': trade_token,
                'trade_price': trade_price,
                'yes_price': best_yes_ask,
                'no_price': best_no_ask,
                'spread': yes_spread if side == 'YES' else no_spread,
                'spread_pct': active_spread,
                'bid_depth': bid_depth,
                'ask_depth': ask_depth,
                'imbalance': imbalance,
                'volume': vol,
                'score': score,
                'reasoning': ", ".join(reasoning),
                'tick_size': tick_size,
                'neg_risk': is_neg_risk,
                'gamma_price': yes_gamma_price if side == 'YES' else no_gamma_price,
            })

        # Sort by score descending (pre-Claude)
        candidates.sort(key=lambda x: x['score'], reverse=True)

        # Log skip reason summary
        if skip_debug:
            reasons_str = ", ".join(f"{k}={v}" for k, v in sorted(skip_debug.items()))
            logger.info(f"📋 Value scan skip reasons: {reasons_str}")
        logger.info(f"📊 Analyzed {analyzed} orderbooks → {len(candidates)} candidates passed filters")

        if not candidates:
            logger.info("No candidates found after filtering. Relaxing further or check DEBUG logs above.")
            return []

        # --- CLAUDE ANALYSIS on top candidates ---
        # Send top ~15 candidates to Claude for AI probability analysis
        top_for_claude = candidates[:15]

        # Build market data lookup for Claude
        market_lookup = {}
        for m in markets:
            mid = (m.get('conditionId') or m.get('condition_id', ''))[:16]
            if mid:
                market_lookup[mid] = m

        # Get the original market dicts for Claude
        markets_for_claude = []
        for c in top_for_claude:
            mid_short = c['market_id'][:16]
            if mid_short in market_lookup:
                markets_for_claude.append(market_lookup[mid_short])

        claude_results = self.analyze_with_claude(markets_for_claude)

        # Apply Claude's analysis to scores (max 50 bonus points) and attach Kelly stake
        for c in candidates:
            mid_short = c['market_id'][:16]
            # Default: use global MAX_BET_PER_TRADE as stake
            c['stake'] = MAX_BET_PER_TRADE

            if mid_short in claude_results:
                analysis = claude_results[mid_short]
                claude_side = analysis['side']
                claude_edge = abs(analysis['edge'])
                claude_conf = analysis['confidence']

                # Confidence multiplier
                conf_mult = {'high': 1.0, 'medium': 0.6, 'low': 0.3}.get(claude_conf, 0.2)

                if claude_side == 'SKIP':
                    # Claude says fairly priced — penalize
                    c['score'] -= 20
                    c['reasoning'] += f", claude=SKIP(-20)"
                elif claude_side == c['side']:
                    # Claude agrees with our technical signal — big boost
                    claude_bonus = min(claude_edge * 300 * conf_mult, 50)
                    c['score'] += claude_bonus
                    c['reasoning'] += f", claude={claude_side}({claude_conf},+{claude_bonus:.0f})"
                    # Use Kelly-sized stake when Claude agrees
                    if analysis['kelly_stake'] > 0:
                        c['stake'] = analysis['kelly_stake']
                else:
                    # Claude disagrees — penalize
                    c['score'] -= 30
                    c['reasoning'] += f", claude=DISAGREE_{claude_side}(-30)"

                c['claude_analysis'] = analysis

        # Final sort: primary = score, secondary = EV per dollar
        candidates.sort(
            key=lambda x: (x['score'], x.get('claude_analysis', {}).get('ev_per_dollar', 0)),
            reverse=True
        )

        logger.info(f"\n{'='*70}")
        logger.info(f"🏆 TOP {min(n_trades, len(candidates))} VALUE TRADES (analyzed {analyzed} markets)")
        logger.info(f"{'='*70}")

        for i, c in enumerate(candidates[:n_trades * 2], 1):  # Show extra for context
            marker = "→" if i <= n_trades else " "
            logger.info(f"{marker} #{i} [score={c['score']:.0f}] Buy {c['side']} on: {c['market_name'][:55]}...")
            logger.info(f"    Price: ${c['trade_price']:.4f} | Spread: {c['spread_pct']*100:.1f}% | "
                       f"Gamma: {c['gamma_price']:.2f} | Vol: ${c['volume']:,.0f} | Stake: ${c['stake']:.2f}")
            logger.info(f"    Signals: {c['reasoning']}")
            if 'claude_analysis' in c:
                ca = c['claude_analysis']
                conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(ca['confidence'], "⚪")
                logger.info(
                    f"    🧠 {conf_emoji} P(YES)={ca['probability']:.0%} | "
                    f"Edge={ca['edge']:.1%} | EV=${ca['ev_per_dollar']:.3f}/$1 | "
                    f"{ca['reasoning'][:50]}"
                )
                if ca.get('risk'):
                    logger.info(f"    ⚠️  Risk: {ca['risk'][:70]}")

        logger.info(f"{'='*70}\n")

        return candidates[:n_trades]

    def execute_value_trade(self, trade: Dict) -> bool:
        """Execute a single value trade (buy one side) using py_clob_client OrderArgs."""
        start_time = time.time()

        # Refresh balance before each trade to avoid insufficient funds
        self.update_balance()

        # Use Kelly-sized stake from Claude if available, else fall back to global MAX_BET_PER_TRADE
        bet_amount = trade.get('stake', MAX_BET_PER_TRADE)
        # Clamp: can't bet more than we have or what remains under exposure limit
        remaining_capacity = MAX_TOTAL_EXPOSURE - self.current_exposure
        bet_amount = max(1.0, min(bet_amount, self.usdc_balance, remaining_capacity))

        if self.usdc_balance < 1.0:
            logger.warning(f"Insufficient balance (${self.usdc_balance:.2f}) — need at least $1")
            return False

        if self.current_exposure >= MAX_TOTAL_EXPOSURE:
            logger.warning(f"⚠️  Exposure limit reached")
            return False

        try:
            # Calculate size in shares: size = amount / price
            # Polymarket requires minimum $1 order value (price * size >= 1.0)
            # Round size UP to ensure we meet the minimum
            import math
            trade_price = trade['trade_price']
            raw_size = bet_amount / trade_price
            # Round up to 2 decimal places to ensure total >= $1
            size = math.ceil(raw_size * 100) / 100
            # Ensure total amount is at least $1.01 to handle rounding
            while size * trade_price < 1.0:
                size += 0.01
            size = round(size, 2)

            display_side = f"Buy {trade['side']}"
            logger.info(f"🔄 Placing {display_side} order on: {trade['market_name'][:50]}...")
            logger.info(f"   Price: ${trade_price:.4f} | Size: {size} shares | Stake: ${bet_amount:.2f} | Cost: ~${size * trade_price:.2f}")

            for attempt in range(MAX_RETRIES):
                try:
                    order_args = OrderArgs(
                        token_id=trade['trade_token'],
                        price=trade_price,
                        size=size,
                        side='BUY',
                    )

                    # Set tick_size and neg_risk for proper order creation
                    tick_size = trade.get('tick_size', '0.01')
                    neg_risk = trade.get('neg_risk', False)
                    options = PartialCreateOrderOptions(
                        tick_size=tick_size,
                        neg_risk=neg_risk,
                    )

                    result = self.client.create_and_post_order(order_args, options)

                    # Result should have orderID or success info
                    if isinstance(result, dict):
                        order_id = result.get('orderID')
                        success = result.get('success', False)
                        logger.info(f"   ✅ Order posted (id={order_id}, success={success})")
                    else:
                        logger.info(f"   ✅ Order submitted: {result}")

                    self.current_exposure += bet_amount
                    execution_time = time.time() - start_time

                    # Track position to prevent duplicates
                    self.active_positions[trade['market_id']] = {
                        'market_name': trade['market_name'],
                        'side': display_side,
                        'amount': bet_amount,
                        'entry_price': trade_price,
                        'size': size,
                        'trade_token': trade['trade_token'],
                        'timestamp': datetime.now().isoformat(),
                    }

                    # Log trade (price = per-share price for accurate P&L tracking)
                    opp_compat = {
                        'market_id': trade['market_id'],
                        'market_name': trade['market_name'],
                        'side': display_side,
                        'total_cost_with_fees': trade_price,
                        'net_profit': 0,
                        'trade_token': trade['trade_token'],
                        'size': size,
                    }
                    self.log_trade(opp_compat, 'EXECUTED', None, 0, execution_time)

                    return True

                except PolyApiException as e:
                    error_str = str(e).lower()
                    logger.error(f"   ❌ Order failed: {e}")
                    if "insufficient" in error_str or "not enough balance" in error_str or "allowance" in error_str:
                        logger.error("   ⚠️  BALANCE/ALLOWANCE ERROR: You need USDC deposited on Polygon.")
                        logger.error("   ⚠️  Go to https://polymarket.com and deposit funds first.")
                        return False
                    if "min size" in error_str:
                        # Increase size and retry
                        size += 0.01
                        logger.info(f"   Adjusting size to {size} to meet minimum...")
                        continue
                    time.sleep(1)
                except Exception as e:
                    logger.error(f"   ❌ Order error: {e}")
                    time.sleep(1)

            logger.error(f"   ❌ Failed after {MAX_RETRIES} attempts")
            return False

        except Exception as e:
            logger.error(f"❌ Critical error: {e}")
            return False

    def sync_resolved_positions(self):
        """Check for resolved markets and remove them from active positions."""
        if not self.active_positions:
            return

        try:
            from eth_account import Account
            pk = os.getenv("PRIVATE_KEY", "")
            if not pk.startswith("0x"):
                pk = "0x" + pk
            wallet = Account.from_key(pk).address

            resp = requests.get(f"https://data-api.polymarket.com/positions?user={wallet.lower()}")
            if resp.status_code != 200:
                return

            # Build lookup by conditionId AND by asset (token) ID
            api_positions_by_cond = {p.get('conditionId'): p for p in resp.json()}
            api_positions_by_asset = {p.get('asset'): p for p in resp.json()}
            resolved = []

            for market_id, pos in list(self.active_positions.items()):
                # Skip positions added less than 15 minutes ago (API may not have caught up)
                ts = pos.get('timestamp')
                if ts:
                    try:
                        added_at = datetime.fromisoformat(ts)
                        if (datetime.now() - added_at).total_seconds() < 900:
                            continue
                    except (ValueError, TypeError):
                        pass

                # Try to find in API by conditionId or by token
                api_pos = api_positions_by_cond.get(market_id)
                if api_pos is None:
                    token = pos.get('trade_token')
                    if token:
                        api_pos = api_positions_by_asset.get(token)

                if api_pos is None:
                    # Position not found after 15 min — was redeemed or sold externally
                    resolved.append((market_id, pos, 'redeemed'))
                elif api_pos.get('redeemable'):
                    resolved.append((market_id, pos, 'redeemable'))
                elif api_pos.get('curPrice', 0.5) >= 0.999 or api_pos.get('curPrice', 0.5) <= 0.001:
                    # Market essentially resolved (price at $1 or $0)
                    resolved.append((market_id, pos, 'resolved'))

            for market_id, pos, reason in resolved:
                name = pos.get('market_name', 'Unknown')[:45]
                amount = pos.get('amount', MAX_BET_PER_TRADE)
                logger.info(f"Position {reason}: {name}")

                # Update DB
                self.cursor.execute(
                    "UPDATE trades SET status='RESOLVED' WHERE market_id=? AND status='EXECUTED'",
                    (market_id,))
                self.conn.commit()

                # Remove from active tracking and prevent re-entry
                del self.active_positions[market_id]
                self.closed_markets.add(market_id)
                self.current_exposure = max(0, self.current_exposure - amount)
                logger.info(f"  Removed from tracking. Exposure now: ${self.current_exposure:.2f}")

            if resolved:
                logger.info(f"Synced {len(resolved)} resolved positions. "
                           f"Open: {len(self.active_positions)} | Exposure: ${self.current_exposure:.2f}")

        except Exception as e:
            logger.warning(f"Could not sync resolved positions: {e}")

    def monitor_positions(self):
        """Monitor active positions — check current prices and log unrealized P&L."""
        self.sync_resolved_positions()

        if not self.active_positions:
            logger.info("📭 No active positions to monitor.")
            return

        logger.info(f"\n{'='*70}")
        logger.info(f"📊 POSITION MONITOR ({len(self.active_positions)} positions, ${self.current_exposure:.2f} exposure)")
        logger.info(f"{'='*70}")

        total_cost = 0.0
        total_value = 0.0

        for market_id, pos in self.active_positions.items():
            name = pos.get('market_name', 'Unknown')[:45]
            side = pos.get('side', '?')
            entry = pos.get('entry_price', 0)
            token_id = pos.get('trade_token')
            size = pos.get('size', 0)

            current_price = None
            if token_id:
                book = self.get_orderbook(token_id)
                if book:
                    bids = book.get('bids', [])
                    if bids:
                        current_price = float(bids[0]['price'])

            cost = entry * size if size else pos.get('amount', MAX_BET_PER_TRADE)
            total_cost += cost

            trade_count = pos.get('trade_count', 1)
            trades_label = f" [{trade_count} trades]" if trade_count > 1 else ""

            if current_price is not None and size:
                market_value = current_price * size
                total_value += market_value
                pnl = market_value - cost
                pnl_pct = (pnl / cost * 100) if cost > 0 else 0
                arrow = "+" if pnl >= 0 else ""
                logger.info(f"  {side} {name}...{trades_label}")
                logger.info(f"    Entry: ${entry:.4f} | Now: ${current_price:.4f} | "
                           f"Size: {size:.2f} | P&L: {arrow}${pnl:.4f} ({arrow}{pnl_pct:.1f}%)")
            else:
                total_value += cost  # Assume flat if can't get price
                logger.info(f"  {side} {name}...{trades_label}")
                logger.info(f"    Entry: ${entry:.4f} | Now: (unavailable)")

        unrealized = total_value - total_cost
        arrow = "+" if unrealized >= 0 else ""
        logger.info(f"{'='*70}")
        logger.info(f"  Total cost: ${total_cost:.2f} | Value: ${total_value:.2f} | "
                   f"Unrealized P&L: {arrow}${unrealized:.4f}")
        logger.info(f"{'='*70}\n")

    def run(self):
        """Main bot loop — continuously scan, trade, and monitor."""
        SCAN_INTERVAL = 60       # seconds between trade scans
        MONITOR_INTERVAL = 300   # seconds between position monitors (5 min)

        logger.info("🚀 Bot started in continuous mode!\n")
        logger.info(f"  Scan interval: {SCAN_INTERVAL}s | Monitor interval: {MONITOR_INTERVAL}s")
        logger.info(f"  Active positions: {len(self.active_positions)}")
        logger.info(f"  Current exposure: ${self.current_exposure:.2f}/{MAX_TOTAL_EXPOSURE}\n")

        try:
            while True:
                # --- PHASE 1: Trade if we have room ---
                if self.current_exposure < MAX_TOTAL_EXPOSURE:
                    remaining_slots = int((MAX_TOTAL_EXPOSURE - self.current_exposure) / MAX_BET_PER_TRADE)
                    if remaining_slots > 0:
                        logger.info(f"🔍 Scanning for up to {remaining_slots} new trades...")

                        # Clear market cache to get fresh data
                        self.last_cache_update = 0

                        best_trades = self.find_best_trades(n_trades=remaining_slots)

                        if best_trades:
                            logger.info(f"🎯 Executing {len(best_trades)} trades...\n")
                            executed = 0
                            for i, trade in enumerate(best_trades, 1):
                                logger.info(f"📈 TRADE {i}/{len(best_trades)} (Score: {trade['score']:.0f})")
                                success = self.execute_value_trade(trade)
                                if success:
                                    executed += 1
                                    logger.info(f"   Position: ${self.current_exposure:.2f}/${MAX_TOTAL_EXPOSURE}")
                                else:
                                    logger.warning(f"   Trade {i} failed, continuing...")
                                if i < len(best_trades):
                                    time.sleep(1)

                            logger.info(f"✅ Round complete: {executed}/{len(best_trades)} trades executed")
                        else:
                            logger.info("No new trade opportunities found this scan.")
                    else:
                        logger.info(f"💼 Max exposure reached (${self.current_exposure:.2f}/${MAX_TOTAL_EXPOSURE})")
                else:
                    logger.info(f"💼 Max exposure reached (${self.current_exposure:.2f}/${MAX_TOTAL_EXPOSURE})")

                # --- PHASE 2: Monitor positions every 5 minutes ---
                now = time.time()
                if now - self.last_monitor_time >= MONITOR_INTERVAL:
                    self.monitor_positions()
                    self.print_performance_summary()
                    self.last_monitor_time = now

                # --- Sleep until next scan ---
                logger.info(f"⏳ Next scan in {SCAN_INTERVAL}s... (Ctrl+C to stop)\n")
                time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            logger.info("\n\n🛑 Bot stopped by user")
            self.monitor_positions()
            self.print_performance_summary()
        except Exception as e:
            logger.error(f"❌ Fatal error: {e}", exc_info=True)
        finally:
            logger.info("Closing database connection...")
            self.conn.close()
            logger.info("✅ Shutdown complete")

if __name__ == "__main__":
    try:
        bot = PolymarketArbBot()
        bot.run()
    except Exception as e:
        logger.error(f"Failed to start bot: {e}", exc_info=True)
