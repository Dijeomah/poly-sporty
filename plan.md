# Bot Update Plan

## Changes to `polymarket_arb_bot.py`

### 1. Track open positions to prevent duplicates
- Use the `active_positions` dict (currently unused) to track market_id → position details
- On startup, load open positions from the `trades` database (status='EXECUTED')
- In `find_best_trades()`, skip any market_id already in `active_positions`
- In `execute_value_trade()`, add the market to `active_positions` on success

### 2. Convert `run()` to continuous loop
Replace the one-shot `run()` with a loop that:
- **Phase 1 — Trade**: If under exposure limit, scan for best trades and execute (skip duplicates)
- **Phase 2 — Monitor**: Run performance analysis every 5 minutes
- Loop indefinitely with a configurable scan interval (e.g., 60 seconds between scans)
- Graceful shutdown on Ctrl+C

### 3. Add position monitoring
- Add `monitor_positions()` method that:
  - Checks current orderbook prices for all active positions
  - Logs unrealized P&L for each position
  - Prints a portfolio summary
- Called every 5 minutes during the monitor phase

### Flow:
```
Bot starts
  → Load existing positions from DB
  → Loop:
      1. If exposure < max: scan & trade (skip duplicate markets)
      2. Monitor positions + show P&L
      3. Print performance summary every 5 minutes
      4. Sleep 60 seconds
      5. Repeat
```

### Files changed:
- `polymarket_arb_bot.py` — all changes in this file only
