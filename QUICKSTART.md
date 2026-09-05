# Quick Start Guide

## Setup (5 minutes)

1. **Install:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure:**
   ```bash
   cp .env.example .env
   nano .env  # Add your Polygon private key
   ```

3. **Fund Wallet:**
   - Send $20 USDC (Polygon) to your wallet
   - Keep ~$0.50 MATIC for gas

4. **Run:**
   ```bash
   python polymarket_arb_bot.py
   ```

## First Trade Checklist

✅ USDC balance shows in logs  
✅ Bot scanning markets  
✅ Confidence scores appearing  
✅ "Trade executed" message  
✅ Win rate tracking  

## Expected Results

**First Hour:**
- 100-300 market scans
- 0-5 opportunities found
- 0-2 trades executed

**First Day:**
- 5-20 trades (market dependent)
- 90-97% win rate target
- $0.10-0.50 profit (with $1 bets)

## Key Settings (95% Win Rate Target)

```python
MIN_PROFIT_AFTER_FEES = 0.025  # 2.5%
MIN_LIQUIDITY_DEPTH = 2.0      # $2
MAX_SLIPPAGE = 0.005           # 0.5%
Confidence threshold = 70%      # Auto-reject low confidence
```

## Monitor Performance

**Real-time:**
```bash
tail -f bot.log
```

**Summary:**
```bash
python analyze_performance.py
```

**Database:**
```bash
sqlite3 trades.db "SELECT * FROM trades ORDER BY timestamp DESC LIMIT 5;"
```

## Common Issues

**No opportunities?**
- Normal! Arbitrage is rare
- Bot runs 24/7 waiting for opportunities
- Consider lowering thresholds (reduces win rate)

**Trades failing?**
- Check USDC balance
- Check MATIC for gas
- Review bot.log for errors

**Low win rate (<90%)?**
- Increase MIN_PROFIT_AFTER_FEES to 0.03
- Increase MIN_LIQUIDITY_DEPTH to 5.0
- Reduce MAX_SLIPPAGE to 0.003

## Support

1. Check `README.md` for full documentation
2. Review `bot.log` for detailed errors
3. Run `python analyze_performance.py` for insights
4. Verify balance on Polygon network

## Safety Reminders

- Start small ($1 bets)
- Monitor first 10 trades closely
- Bot auto-limits exposure to $5
- Keep manual control of your wallet
- Review trades on polymarket.com
