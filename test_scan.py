#!/usr/bin/env python3
"""
Test 3: Full scan pipeline — Gamma API + orderbooks + Claude analysis.
No orders are placed. Shows ranked trade candidates with EV and Kelly stake.
"""
import sys
sys.path.insert(0, '.')

from polymarket_arb_bot import PolymarketArbBot

N_TRADES = 3  # how many top candidates to show

if __name__ == "__main__":
    print("Initializing bot (requires valid .env credentials)...")
    bot = PolymarketArbBot()

    print(f"\n{'='*65}")
    print("🔍 SCAN-ONLY TEST  (no orders will be placed)")
    print(f"{'='*65}")
    print(f"Balance : ${bot.usdc_balance:.2f}")
    print(f"Exposure: ${bot.current_exposure:.2f} / $10.00")
    print(f"{'='*65}\n")

    trades = bot.find_best_trades(n_trades=N_TRADES)

    print(f"\n{'='*65}")
    print(f"RESULT: {len(trades)} trade candidates found")
    print(f"{'='*65}")

    if not trades:
        print("No candidates — check bot.log for scan details")
        sys.exit(0)

    for i, t in enumerate(trades, 1):
        ca = t.get('claude_analysis', {})
        conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(
            ca.get('confidence', ''), "⚪"
        )
        print(f"\n#{i}: Buy {t['side']} on:")
        print(f"  {t['market_name'][:70]}")
        print(f"  Score  : {t['score']:.0f}")
        print(f"  Price  : ${t['trade_price']:.4f}  |  Spread: {t['spread_pct']*100:.1f}%")
        print(f"  Stake  : ${t['stake']:.2f}  (Kelly-sized)")
        print(f"  Volume : ${t['volume']:,.0f}")
        if ca:
            print(f"  Claude : {conf_emoji} {ca.get('side','?')} | "
                  f"P(YES)={ca['probability']:.0%} | "
                  f"Edge={ca['edge']:.1%} | "
                  f"EV=${ca['ev_per_dollar']:.4f}/$1")
            print(f"  Risk   : {ca.get('risk', 'N/A')}")

    print(f"\n✅ Scan test complete")
