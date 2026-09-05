#!/usr/bin/env python3
"""
Test 2: Claude agent analysis — uses Anthropic API, no Polymarket API or wallet needed.
Verifies the new system prompt, model, and EV/stake fields are working.
"""
import sys
sys.path.insert(0, '.')

from polymarket_arb_bot import PolymarketArbBot

FAKE_MARKETS = [
    {
        'conditionId': 'abc123def456789a',
        'question': 'Will the Fed cut rates at the March 2026 FOMC meeting?',
        'description': 'Resolves YES if the Federal Reserve cuts the federal funds rate at the March 18-19 2026 FOMC meeting.',
        '_parsed_outcome_prices': [0.72, 0.28],
        'volume24hr': 50000,
        'endDateIso': '2026-03-20T00:00:00Z',
    },
    {
        'conditionId': 'xyz987mno654321b',
        'question': 'Will Bitcoin close above $100k on March 15 2026?',
        'description': 'Resolves YES if BTC/USD closes above $100,000 on March 15 2026.',
        '_parsed_outcome_prices': [0.45, 0.55],
        'volume24hr': 120000,
        'endDateIso': '2026-03-16T00:00:00Z',
    },
    {
        'conditionId': 'def456ghi789012c',
        'question': 'Will SpaceX Starship complete an orbital flight before April 2026?',
        'description': 'Resolves YES if SpaceX Starship completes a full orbital flight before April 1 2026.',
        '_parsed_outcome_prices': [0.88, 0.12],
        'volume24hr': 30000,
        'endDateIso': '2026-04-01T00:00:00Z',
    },
]

if __name__ == "__main__":
    print("Initializing bot (requires valid .env credentials)...")
    bot = PolymarketArbBot()

    print(f"\n{'='*65}")
    print("🧠 CLAUDE AGENT ANALYSIS TEST")
    print(f"{'='*65}")
    print(f"Model: claude-sonnet-4-6 | Markets: {len(FAKE_MARKETS)}")
    print(f"{'='*65}\n")

    result = bot.analyze_with_claude(FAKE_MARKETS)

    if not result:
        print("❌ No results returned — check ANTHROPIC_API_KEY in .env")
        sys.exit(1)

    for mid, a in result.items():
        conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(a['confidence'], "⚪")
        print(f"Market ID: {mid}")
        print(f"  Recommendation : {a['side']}")
        print(f"  True prob (YES): {a['probability']:.0%}")
        print(f"  Edge           : {a['edge']:.1%}")
        print(f"  EV per dollar  : ${a['ev_per_dollar']:.4f}")
        print(f"  Conviction     : {conf_emoji} {a['confidence']}")
        print(f"  Kelly stake    : ${a['kelly_stake']:.2f}")
        print(f"  Reasoning      : {a['reasoning']}")
        print(f"  Risk           : {a.get('risk', 'N/A')}")
        print()

    print(f"✅ Claude test complete — {len(result)}/{len(FAKE_MARKETS)} markets analyzed")
