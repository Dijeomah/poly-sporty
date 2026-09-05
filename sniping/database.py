"""SQLite position tracking with async access."""

import aiosqlite
from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_mint TEXT NOT NULL,
    token_symbol TEXT DEFAULT '',
    entry_price REAL NOT NULL,
    amount_tokens REAL NOT NULL,
    sol_spent REAL NOT NULL,
    buy_tx TEXT NOT NULL,
    sell_tx TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    pnl_sol REAL DEFAULT 0.0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP
);
"""


async def init_db():
    """Create tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def add_position(
    token_mint: str,
    token_symbol: str,
    entry_price: float,
    amount_tokens: float,
    sol_spent: float,
    buy_tx: str,
) -> int:
    """Record a new open position. Returns row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO positions
               (token_mint, token_symbol, entry_price, amount_tokens, sol_spent, buy_tx, status)
               VALUES (?, ?, ?, ?, ?, ?, 'open')""",
            (token_mint, token_symbol, entry_price, amount_tokens, sol_spent, buy_tx),
        )
        await db.commit()
        return cursor.lastrowid


async def close_position(token_mint: str, sell_tx: str, pnl_sol: float):
    """Mark a position as closed with P&L."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE positions
               SET status='closed', sell_tx=?, pnl_sol=?, closed_at=CURRENT_TIMESTAMP
               WHERE token_mint=? AND status='open'""",
            (sell_tx, pnl_sol, token_mint),
        )
        await db.commit()


async def get_open_positions() -> list[dict]:
    """Return all open positions as dicts."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM positions WHERE status='open' ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_position_by_mint(token_mint: str) -> dict | None:
    """Get an open position for a specific token."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM positions WHERE token_mint=? AND status='open' LIMIT 1",
            (token_mint,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_all_positions() -> list[dict]:
    """Return all positions (open and closed)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM positions ORDER BY created_at DESC LIMIT 50"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
