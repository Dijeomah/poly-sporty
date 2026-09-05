#!/usr/bin/env python3
"""
Test 1: Kelly stake calculator — no API calls, no wallet needed.
"""
import sys
sys.path.insert(0, '.')


class FakeBot:
    """Minimal stub so we can test _calculate_kelly_stake in isolation."""
    usdc_balance = 0.0

    from polymarket_arb_bot import PolymarketArbBot
    _calculate_kelly_stake = PolymarketArbBot._calculate_kelly_stake


def run(bankroll, cases):
    b = FakeBot()
    b.usdc_balance = bankroll
    print(f"\nBankroll: ${bankroll}")
    print(f"  {'Conviction':<10} {'Edge':>6} {'Price':>7} {'Stake':>8}")
    print(f"  {'-'*38}")
    for conf, edge, price in cases:
        stake = b._calculate_kelly_stake(edge, price, conf)
        print(f"  {conf:<10} {edge:>6.0%} {price:>7.2f} ${stake:>7.2f}")


if __name__ == "__main__":
    cases = [
        ("high",   0.16, 0.72),
        ("high",   0.10, 0.85),
        ("medium", 0.08, 0.78),
        ("medium", 0.05, 0.65),
        ("low",    0.03, 0.70),
        ("low",    0.02, 0.90),
    ]

    for bankroll in [10, 25, 60, 150, 300]:
        run(bankroll, cases)

    print("\n✅ Kelly test complete")
