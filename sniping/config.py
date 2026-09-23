"""Constants, API endpoints, and bot settings."""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

# ── Solana Constants ──────────────────────────────────────────────────────────
SOL_MINT = "So11111111111111111111111111111111111111112"
SOL_DECIMALS = 9
LAMPORTS_PER_SOL = 1_000_000_000
PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# ── RPC ───────────────────────────────────────────────────────────────────────
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_WSS_URL = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# ── Jupiter V6 (requires API key from portal.jup.ag) ─────────────────────────
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"
JUPITER_PRICE_URL = "https://api.jup.ag/price/v2"

# ── External APIs ─────────────────────────────────────────────────────────────
RUGCHECK_API_URL = "https://api.rugcheck.xyz/v1/tokens"
DEXSCREENER_API_URL = "https://api.dexscreener.com/tokens/v1/solana"

# ── PumpPortal WebSocket ──────────────────────────────────────────────────────
PUMPPORTAL_WSS_URL = "wss://pumpportal.fun/api/data"

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "positions.db")


@dataclass
class BotSettings:
    """Mutable trading settings — changed via /settings."""
    buy_amount_sol: float = 0.01        # SOL per snipe
    slippage_bps: int = 1500            # 15% default slippage
    take_profit_pct: float = 100.0      # +100% (2x)
    stop_loss_pct: float = 20.0         # -20%
    min_liquidity_usd: float = 1000.0   # Minimum liquidity to pass safety
    priority_fee_lamports: int = 5_000_000  # 0.005 SOL — needed to land on pump.fun
    auto_listen: bool = False           # pump.fun auto-buy toggle
    monitor_interval_sec: int = 10      # P&L check frequency

    # ── BTC 5-Min Arb Settings ───────────────────────────────────────────────
    btc_bet_amount: float = 2.0         # USDC per trade
    btc_min_edge: float = 0.02          # Minimum momentum threshold (%)
    btc_max_price: float = 0.55         # Max price to pay for a side
    btc_enabled: bool = True            # Toggle BTC arb on/off

    # ── SportyBet Settings ───────────────────────────────────────────────────
    sporty_min_probability: float = 0.85   # 85% minimum win probability
    sporty_default_odds: float = 1.50      # Default rollover odds/day
    sporty_rollover_days: int = 10         # Default rollover period
    sporty_acca_target: float = 100.0      # Default accumulator target odds
    sporty_max_legs: int = 40              # Max accumulator legs
    sporty_enabled: bool = True            # Toggle SportyBet on/off

    # ── Claude analysis of generated picks (sportybet/claude_analyst.py) ─────
    sporty_claude_enabled: bool = True         # Run picks past Claude before booking
    sporty_claude_research: bool = True        # Let Claude web-search form/injuries/H2H
    sporty_claude_min_confidence: float = 0.75 # Veto picks Claude rates below this
    sporty_claude_max_games: int = 30          # Candidates analysed per accumulator


# Singleton settings instance
settings = BotSettings()

# ── SportyBet Credentials (from env) ─────────────────────────────────────
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
API_FOOTBALL_KEY = os.getenv("API_FOOTBALL_KEY", "")
SPORTYBET_PHONE = os.getenv("SPORTYBET_PHONE", "")
SPORTYBET_PASSWORD = os.getenv("SPORTYBET_PASSWORD", "")
