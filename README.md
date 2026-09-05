# Polymarket Advanced Arbitrage Trading Bot

**Target: 95% Profitable Trades**

## Features

### Core Arbitrage Strategy
- Finds markets where YES + NO < $1.00
- Buys both sides for guaranteed profit when market resolves
- Accounts for platform fees (~2%) and slippage

### Advanced Safety Features

**Multi-Layer Risk Management:**
1. **Liquidity Validation** - Requires minimum $2 liquidity depth
2. **Slippage Protection** - Max 0.5% slippage tolerance
3. **Spread Analysis** - Rejects markets with >15% spread
4. **Volume Filtering** - Only trades markets with >$100 24h volume
5. **Market Blacklisting** - Temporarily blacklists problematic markets
6. **Confidence Scoring** - Rates each opportunity 0-100%
7. **Balance Monitoring** - Continuous USDC balance checks
8. **Order Fill Verification** - Waits for confirmed fills before proceeding

**Edge Case Handling:**
- Partial fill protection (prevents holding one-sided position)
- Retry logic with exponential backoff (3 attempts)
- Order timeout monitoring (30 seconds)
- Network error recovery
- API rate limit handling
- Market cache to reduce API calls

**Profit Optimization:**
- Higher profit threshold (3.5% gross, 2.5% net after fees)
- Confidence-based execution (only trades >70% confidence)
- Fee-adjusted profit calculations
- Real-time slippage estimation

## Setup Instructions

### 1. Install Dependencies (Termux/Linux)
```bash
pkg install python git  # Termux
# OR
sudo apt install python3 python3-pip git  # Linux

pip install -r requirements.txt
```

### 2. Configure Wallet
```bash
# Copy the example env file
cp .env.example .env

# Edit .env and add your private key
nano .env
```

**Get Your Private Key:**
1. Open MetaMask
2. Click account → Account details → Show private key
3. Enter password and copy the key (with or without 0x prefix)
4. Paste into `.env` file

**Fund Your Wallet:**
- Send USDC (Polygon network) to your wallet address
- Minimum recommended: $20 to start
- Ensure you have ~$0.50 MATIC for gas fees

### 3. Run the Bot
```bash
python polymarket_arb_bot.py
```

## How It Works

### Arbitrage Example
```
Market: "Will X happen?"
YES price: $0.46
NO price: $0.48
─────────────────────
Total cost: $0.94
Platform fees (2%): $0.02
Total with fees: $0.96
Guaranteed payout: $1.00
Net profit: $0.04 (4.17% return)
```

### Confidence Scoring
Each opportunity gets a confidence score (0-100%):
- **Base score:** 50
- **Profit boost:** +30 max (higher profit = higher score)
- **Slippage penalty:** Lower slippage adds +10 per side
- **Volume boost:** High volume markets get +5 to +10

Only opportunities with >70% confidence are executed.

### Risk Parameters
```
Max bet per trade: $1
Max total exposure: $5
Stop loss: 0.25%
Min gross profit: 3.5%
Min net profit (after fees): 2.5%
Min liquidity per side: $2
Max slippage: 0.5%
Max spread: 15%
Min market volume: $100
```

## Database Schema

### trades
Logs all trade attempts with detailed metadata:
- Timestamp, market info, prices
- Expected vs actual profit
- Execution time
- Status (EXECUTED, FAILED, PARTIAL, ERROR)
- Error messages

### market_analysis
Tracks market quality metrics:
- Volume, liquidity, spread
- Arbitrage opportunity frequency
- Last checked timestamp

### performance_metrics
Historical performance tracking:
- Total/successful/failed trades
- Win rate, total profit
- Average profit per trade

## Monitoring

### Real-Time Output
```
📊 Scanning 450 markets...
🎯 Found 3 arbitrage opportunities!
1. Will BTC hit $100k? | Profit: 4.2% | Confidence: 85.3%
2. US GDP growth >3%? | Profit: 3.8% | Confidence: 78.1%

💰 ARBITRAGE OPPORTUNITY (Confidence: 85.3%)
Market: Will BTC hit $100k?
YES price: $0.4750 (effective: $0.4773)
NO price: $0.4850 (effective: $0.4874)
Total cost with fees: $0.9647
Expected profit: 3.66% ($0.0353)
✅ TRADE EXECUTED SUCCESSFULLY
```

### Performance Summary (every 5 minutes)
```
📊 PERFORMANCE SUMMARY
Total trades: 12
Successful: 11
Failed: 1
Win rate: 91.7%
Total profit: $0.3845
Avg profit/trade: $0.0349
```

### Database Queries

**View recent trades:**
```bash
sqlite3 trades.db "SELECT timestamp, market_name, expected_profit, actual_profit, status FROM trades ORDER BY timestamp DESC LIMIT 10;"
```

**Calculate win rate:**
```bash
sqlite3 trades.db "SELECT 
  COUNT(*) as total,
  SUM(CASE WHEN status='EXECUTED' THEN 1 ELSE 0 END) as wins,
  ROUND(100.0 * SUM(CASE WHEN status='EXECUTED' THEN 1 ELSE 0 END) / COUNT(*), 2) as win_rate
FROM trades;"
```

**Top performing markets:**
```bash
sqlite3 trades.db "SELECT market_name, COUNT(*) as trades, AVG(actual_profit) as avg_profit FROM trades WHERE status='EXECUTED' GROUP BY market_id ORDER BY avg_profit DESC LIMIT 5;"
```

## Safety Features Explained

### 1. Market Blacklisting
Markets that cause failures are temporarily blacklisted (5 minutes):
- Partial fills
- Repeated order failures
- High failure rate (3+ failures)

### 2. Order Fill Verification
Bot waits up to 30 seconds for each order to fill:
- Prevents partial fills
- Retries failed orders (up to 3 attempts)
- Cancels YES order if NO order fails

### 3. Slippage Protection
Calculates expected slippage before trading:
- Walks through orderbook depth
- Rejects if slippage >0.5%
- Adjusts prices for realistic profit

### 4. Balance Monitoring
Checks USDC balance every 60 seconds:
- Prevents insufficient fund errors
- Updates exposure tracking
- Logs balance in performance summaries

### 5. Comprehensive Logging
Everything logged to `bot.log` file:
- All trades (successful and failed)
- Market scans
- Errors with stack traces
- Performance metrics

## Troubleshooting

### "PRIVATE_KEY not found"
```bash
# Ensure .env file exists
ls -la .env

# Check format
cat .env
# Should show: PRIVATE_KEY=0x...
```

### "Insufficient balance"
```bash
# Check Polygon USDC balance in MetaMask
# Ensure you're on Polygon network, not Ethereum

# Bridge USDC to Polygon at:
# https://wallet.polygon.technology/
```

### "No opportunities found"
This is normal - pure arbitrage is rare:
- Bot scans continuously
- Most markets are efficiently priced
- Opportunities appear sporadically (minutes to hours between)
- Consider lowering MIN_ARBITRAGE_PROFIT (not recommended for 95% target)

### Connection errors
```bash
# Test Polymarket API
curl https://clob.polymarket.com/markets

# Check internet
ping 8.8.8.8

# Restart bot
python polymarket_arb_bot.py
```

### Partial fills warning
```
⚠️  WARNING: Only YES position filled - HEDGE MANUALLY!
```
**Action:** Manually sell the YES position or buy the NO side
- This market is auto-blacklisted for 5 minutes
- Check `trades.db` for details
- View positions at https://polymarket.com/

### High failure rate
If win rate drops below 90%:
1. Check `bot.log` for error patterns
2. Increase `MIN_PROFIT_AFTER_FEES` (reduces opportunities but increases quality)
3. Increase `MIN_LIQUIDITY_DEPTH`
4. Decrease `MAX_SLIPPAGE`

## Optimization Tips

### For Higher Win Rate (>95%)
```python
MIN_ARBITRAGE_PROFIT = 0.04  # 4% gross
MIN_PROFIT_AFTER_FEES = 0.03  # 3% net
MIN_LIQUIDITY_DEPTH = 5.0  # $5
MAX_SLIPPAGE = 0.003  # 0.3%
```
Trade-off: Fewer opportunities

### For More Opportunities (90-95% win rate)
```python
MIN_ARBITRAGE_PROFIT = 0.03  # 3% gross
MIN_PROFIT_AFTER_FEES = 0.02  # 2% net
MIN_LIQUIDITY_DEPTH = 2.0  # $2
MAX_SLIPPAGE = 0.005  # 0.5%
```

## Stopping the Bot

**Graceful shutdown:**
```bash
# Press Ctrl+C once
# Bot will finish current operation and print final summary
```

**Force stop:**
```bash
# Press Ctrl+C twice or:
pkill -9 python
```

## Advanced Usage

### Run in Background (Termux)
```bash
# Install tmux
pkg install tmux

# Start session
tmux new -s polybot

# Run bot
python polymarket_arb_bot.py

# Detach: Ctrl+B then D
# Reattach: tmux attach -t polybot
```

### Monitor Performance
```bash
# Watch logs in real-time
tail -f bot.log

# Monitor trades
watch -n 5 'sqlite3 trades.db "SELECT COUNT(*) as total, SUM(CASE WHEN status=\"EXECUTED\" THEN 1 ELSE 0 END) as wins FROM trades"'
```

### Export Trade History
```bash
# CSV export
sqlite3 -header -csv trades.db "SELECT * FROM trades;" > trades_export.csv

# Performance report
sqlite3 trades.db "SELECT DATE(timestamp) as date, COUNT(*) as trades, SUM(actual_profit) as profit FROM trades WHERE status='EXECUTED' GROUP BY DATE(timestamp);"
```

## Target: 95% Win Rate

**How to achieve:**
1. ✅ Higher profit thresholds (3.5% gross, 2.5% net)
2. ✅ Comprehensive edge case handling
3. ✅ Multi-retry logic with order verification
4. ✅ Liquidity and slippage validation
5. ✅ Market blacklisting for problematic markets
6. ✅ Confidence-based execution (>70% only)
7. ✅ Fee-adjusted calculations
8. ✅ Balance monitoring and exposure limits

**Expected results:**
- Win rate: 90-97% (target 95%)
- Trades per day: 5-20 (depends on market conditions)
- Avg profit per trade: 2.5-4%
- Time to $5 exposure: 5-10 successful trades

## Support

Issues? Check:
1. `bot.log` for detailed errors
2. `trades.db` for trade history
3. Balance and gas on Polygon
4. Polymarket API status

## Disclaimer

This bot is for educational purposes. Trading involves risk. Past performance doesn't guarantee future results. Always start with small amounts.
