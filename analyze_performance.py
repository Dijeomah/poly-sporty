#!/usr/bin/env python3
"""
Performance Analysis Script
Generates detailed reports from trading history
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Dict, List

def connect_db() -> sqlite3.Connection:
    """Connect to trades database"""
    try:
        return sqlite3.connect('trades.db')
    except Exception as e:
        print(f"Error connecting to database: {e}")
        sys.exit(1)

def get_overall_stats(conn: sqlite3.Connection) -> Dict:
    """Get overall performance statistics"""
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            COUNT(*) as total_trades,
            SUM(CASE WHEN status = 'EXECUTED' THEN 1 ELSE 0 END) as successful,
            SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) as failed,
            SUM(CASE WHEN status = 'PARTIAL' THEN 1 ELSE 0 END) as partial,
            SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) as errors,
            SUM(CASE WHEN status = 'EXECUTED' THEN actual_profit ELSE 0 END) as total_profit,
            AVG(CASE WHEN status = 'EXECUTED' THEN actual_profit ELSE NULL END) as avg_profit,
            MIN(CASE WHEN status = 'EXECUTED' THEN actual_profit ELSE NULL END) as min_profit,
            MAX(CASE WHEN status = 'EXECUTED' THEN actual_profit ELSE NULL END) as max_profit,
            AVG(CASE WHEN status = 'EXECUTED' THEN execution_time ELSE NULL END) as avg_exec_time
        FROM trades
    ''')
    
    row = cursor.fetchone()
    
    if row and row[0] > 0:
        total = row[0]
        successful = row[1] or 0
        
        return {
            'total_trades': total,
            'successful_trades': successful,
            'failed_trades': row[2] or 0,
            'partial_trades': row[3] or 0,
            'error_trades': row[4] or 0,
            'win_rate': (successful / total * 100) if total > 0 else 0,
            'total_profit': row[5] or 0,
            'avg_profit': row[6] or 0,
            'min_profit': row[7] or 0,
            'max_profit': row[8] or 0,
            'avg_execution_time': row[9] or 0
        }
    
    return {}

def get_daily_stats(conn: sqlite3.Connection, days: int = 7) -> List[Dict]:
    """Get daily statistics for last N days"""
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            DATE(timestamp) as date,
            COUNT(*) as trades,
            SUM(CASE WHEN status = 'EXECUTED' THEN 1 ELSE 0 END) as successful,
            SUM(CASE WHEN status = 'EXECUTED' THEN actual_profit ELSE 0 END) as profit
        FROM trades
        WHERE DATE(timestamp) >= DATE('now', ?)
        GROUP BY DATE(timestamp)
        ORDER BY date DESC
    ''', (f'-{days} days',))
    
    results = []
    for row in cursor.fetchall():
        results.append({
            'date': row[0],
            'trades': row[1],
            'successful': row[2],
            'profit': row[3],
            'win_rate': (row[2] / row[1] * 100) if row[1] > 0 else 0
        })
    
    return results

def get_top_markets(conn: sqlite3.Connection, limit: int = 10) -> List[Dict]:
    """Get top performing markets"""
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            market_name,
            COUNT(*) as trades,
            SUM(CASE WHEN status = 'EXECUTED' THEN 1 ELSE 0 END) as successful,
            SUM(CASE WHEN status = 'EXECUTED' THEN actual_profit ELSE 0 END) as total_profit,
            AVG(CASE WHEN status = 'EXECUTED' THEN actual_profit ELSE NULL END) as avg_profit
        FROM trades
        WHERE status = 'EXECUTED'
        GROUP BY market_id
        HAVING successful > 0
        ORDER BY total_profit DESC
        LIMIT ?
    ''', (limit,))
    
    results = []
    for row in cursor.fetchall():
        results.append({
            'market_name': row[0],
            'trades': row[1],
            'successful': row[2],
            'total_profit': row[3],
            'avg_profit': row[4]
        })
    
    return results

def get_failure_analysis(conn: sqlite3.Connection) -> List[Dict]:
    """Analyze failure patterns"""
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT 
            error_message,
            COUNT(*) as count
        FROM trades
        WHERE status IN ('FAILED', 'ERROR', 'PARTIAL')
        AND error_message IS NOT NULL
        GROUP BY error_message
        ORDER BY count DESC
        LIMIT 10
    ''')
    
    results = []
    for row in cursor.fetchall():
        results.append({
            'error': row[0],
            'count': row[1]
        })
    
    return results

def print_report():
    """Generate and print performance report"""
    conn = connect_db()
    
    print("=" * 80)
    print("POLYMARKET ARBITRAGE BOT - PERFORMANCE REPORT")
    print("=" * 80)
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    # Overall Stats
    stats = get_overall_stats(conn)
    
    if stats:
        print("OVERALL STATISTICS")
        print("-" * 80)
        print(f"Total Trades:        {stats['total_trades']}")
        print(f"Successful:          {stats['successful_trades']} ({stats['win_rate']:.1f}%)")
        print(f"Failed:              {stats['failed_trades']}")
        print(f"Partial Fills:       {stats['partial_trades']}")
        print(f"Errors:              {stats['error_trades']}")
        print(f"\nTotal Profit:        ${stats['total_profit']:.4f}")
        print(f"Average Profit:      ${stats['avg_profit']:.4f}")
        print(f"Min Profit:          ${stats['min_profit']:.4f}")
        print(f"Max Profit:          ${stats['max_profit']:.4f}")
        print(f"Avg Execution Time:  {stats['avg_execution_time']:.2f}s")
        
        # Win Rate Assessment
        win_rate = stats['win_rate']
        if win_rate >= 95:
            rating = "EXCELLENT ⭐⭐⭐⭐⭐"
        elif win_rate >= 90:
            rating = "VERY GOOD ⭐⭐⭐⭐"
        elif win_rate >= 80:
            rating = "GOOD ⭐⭐⭐"
        elif win_rate >= 70:
            rating = "FAIR ⭐⭐"
        else:
            rating = "NEEDS IMPROVEMENT ⭐"
        
        print(f"\nWin Rate Rating:     {rating}")
        
        # Daily Stats
        print("\n" + "=" * 80)
        print("DAILY PERFORMANCE (Last 7 Days)")
        print("-" * 80)
        print(f"{'Date':<12} {'Trades':<8} {'Success':<10} {'Win Rate':<10} {'Profit':<12}")
        print("-" * 80)
        
        daily = get_daily_stats(conn, 7)
        if daily:
            for day in daily:
                print(f"{day['date']:<12} {day['trades']:<8} {day['successful']:<10} "
                      f"{day['win_rate']:>6.1f}%    ${day['profit']:>8.4f}")
        else:
            print("No trades in the last 7 days")
        
        # Top Markets
        print("\n" + "=" * 80)
        print("TOP PERFORMING MARKETS")
        print("-" * 80)
        print(f"{'#':<4} {'Market':<45} {'Trades':<8} {'Profit':<12}")
        print("-" * 80)
        
        top_markets = get_top_markets(conn, 10)
        if top_markets:
            for i, market in enumerate(top_markets, 1):
                market_name = market['market_name'][:42] + "..." if len(market['market_name']) > 45 else market['market_name']
                print(f"{i:<4} {market_name:<45} {market['trades']:<8} ${market['total_profit']:>8.4f}")
        else:
            print("No successful trades yet")
        
        # Failure Analysis
        print("\n" + "=" * 80)
        print("FAILURE ANALYSIS")
        print("-" * 80)
        
        failures = get_failure_analysis(conn)
        if failures:
            print(f"{'Error Type':<60} {'Count':<8}")
            print("-" * 80)
            for failure in failures:
                error = failure['error'][:57] + "..." if len(failure['error']) > 60 else failure['error']
                print(f"{error:<60} {failure['count']:<8}")
        else:
            print("No failures recorded")
        
        # Recommendations
        print("\n" + "=" * 80)
        print("RECOMMENDATIONS")
        print("-" * 80)
        
        if win_rate < 95:
            print("• Win rate below target (95%)")
            print("  → Consider increasing MIN_PROFIT_AFTER_FEES to 0.03 (3%)")
            print("  → Consider increasing MIN_LIQUIDITY_DEPTH to 5.0")
            print("  → Consider decreasing MAX_SLIPPAGE to 0.003 (0.3%)")
        
        if stats['partial_trades'] > 0:
            print(f"• {stats['partial_trades']} partial fill(s) occurred")
            print("  → Check network stability")
            print("  → Review ORDER_TIMEOUT setting")
        
        if stats['total_profit'] < 0.01 and stats['successful_trades'] > 10:
            print("• Low profit per trade")
            print("  → Increase MIN_ARBITRAGE_PROFIT threshold")
            print("  → Focus on higher volume markets")
        
        if not failures:
            print("• No failures detected - bot performing well! ✅")
        
    else:
        print("No trades recorded yet. Start the bot to begin trading.")
    
    print("\n" + "=" * 80)
    
    conn.close()

if __name__ == "__main__":
    print_report()
