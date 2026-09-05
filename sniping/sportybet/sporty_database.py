"""SQLite persistence for SportyBet rollovers and pick history."""

import time
import aiosqlite
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "sporty.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sporty_rollovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL DEFAULT 0,
    total_days INTEGER NOT NULL,
    target_odds REAL NOT NULL,
    current_day INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    start_time REAL NOT NULL,
    next_run_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sporty_picks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL,
    rollover_day INTEGER,
    fixture_id TEXT NOT NULL,
    af_fixture_id TEXT,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    league TEXT DEFAULT '',
    sport TEXT DEFAULT '',
    selection TEXT NOT NULL,
    pick_side TEXT NOT NULL,
    pick_kind TEXT NOT NULL DEFAULT 'winner',
    odds REAL NOT NULL,
    confidence REAL NOT NULL,
    booking_code TEXT DEFAULT '',
    commence_time TEXT DEFAULT '',
    placed_at REAL NOT NULL,
    settled INTEGER NOT NULL DEFAULT 0,
    won INTEGER
);

CREATE TABLE IF NOT EXISTS sporty_acca_runs (
    telegram_user_id INTEGER NOT NULL,
    run_date TEXT NOT NULL,
    run_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_user_id, run_date)
);
"""


async def init_sporty_db():
    """Create tables if they don't exist, and add any columns a schema
    change has introduced since the db file was first created — CREATE
    TABLE IF NOT EXISTS alone doesn't retroactively alter an existing table.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()

        cursor = await db.execute("PRAGMA table_info(sporty_picks)")
        existing_cols = {row[1] for row in await cursor.fetchall()}

        migrations = {
            "pick_kind": "ALTER TABLE sporty_picks ADD COLUMN pick_kind TEXT NOT NULL DEFAULT 'winner'",
            "telegram_user_id": "ALTER TABLE sporty_picks ADD COLUMN telegram_user_id INTEGER NOT NULL DEFAULT 0",
        }
        for col, ddl in migrations.items():
            if col not in existing_cols:
                await db.execute(ddl)

        cursor = await db.execute("PRAGMA table_info(sporty_rollovers)")
        existing_rollover_cols = {row[1] for row in await cursor.fetchall()}
        if "telegram_user_id" not in existing_rollover_cols:
            await db.execute(
                "ALTER TABLE sporty_rollovers ADD COLUMN telegram_user_id INTEGER NOT NULL DEFAULT 0"
            )

        await db.commit()


# ── Rollovers ─────────────────────────────────────────────────────────────────

async def create_rollover(telegram_user_id, total_days: int, target_odds: float) -> int:
    """Insert a new active rollover row for this user. Returns row id."""
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO sporty_rollovers
               (telegram_user_id, total_days, target_odds, current_day, active, start_time, next_run_at)
               VALUES (?, ?, ?, 0, 1, ?, ?)""",
            (int(telegram_user_id), total_days, target_odds, now, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_active_rollover(telegram_user_id) -> dict | None:
    """Return this user's active rollover row, if any."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sporty_rollovers WHERE active=1 AND telegram_user_id=? ORDER BY id DESC LIMIT 1",
            (int(telegram_user_id),),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_all_active_rollovers() -> list[dict]:
    """Return every user's active rollover row — used to resume all of them
    on bot startup, regardless of which user owns each one."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM sporty_rollovers WHERE active=1")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_rollover_progress(rollover_id: int, current_day: int, next_run_at: float):
    """Advance a rollover's day counter and next-run time."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sporty_rollovers SET current_day=?, next_run_at=? WHERE id=?",
            (current_day, next_run_at, rollover_id),
        )
        await db.commit()


async def deactivate_rollover(rollover_id: int):
    """Mark a rollover inactive (completed or stopped)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sporty_rollovers SET active=0 WHERE id=?", (rollover_id,)
        )
        await db.commit()


# ── Picks ─────────────────────────────────────────────────────────────────────

async def add_pick(
    telegram_user_id,
    kind: str,
    fixture_id: str,
    home_team: str,
    away_team: str,
    selection: str,
    pick_side: str,
    odds: float,
    confidence: float,
    league: str = "",
    sport: str = "",
    af_fixture_id: str | None = None,
    booking_code: str = "",
    commence_time: str = "",
    rollover_day: int | None = None,
    pick_kind: str = "winner",
) -> int:
    """Record a booked pick/leg for this user. Returns row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO sporty_picks
               (telegram_user_id, kind, rollover_day, fixture_id, af_fixture_id, home_team,
                away_team, league, sport, selection, pick_side, pick_kind, odds, confidence,
                booking_code, commence_time, placed_at, settled, won)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)""",
            (int(telegram_user_id), kind, rollover_day, fixture_id, af_fixture_id, home_team,
             away_team, league, sport, selection, pick_side, pick_kind, odds, confidence,
             booking_code, commence_time, time.time()),
        )
        await db.commit()
        return cursor.lastrowid


async def get_unsettled_picks() -> list[dict]:
    """Return picks not yet settled, with a known commence_time."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sporty_picks WHERE settled=0 AND commence_time != ''"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def settle_pick(pick_id: int, won: bool):
    """Mark a pick settled with its outcome."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE sporty_picks SET settled=1, won=? WHERE id=?",
            (1 if won else 0, pick_id),
        )
        await db.commit()


async def get_history_stats(telegram_user_id) -> dict:
    """Return this user's overall + per-confidence-bucket win rate stats."""
    uid = int(telegram_user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            "SELECT COUNT(*) as total, SUM(won) as wins FROM sporty_picks WHERE settled=1 AND telegram_user_id=?",
            (uid,),
        )
        overall = dict(await cursor.fetchone())

        cursor = await db.execute(
            "SELECT COUNT(*) as total, SUM(won) as wins FROM sporty_picks WHERE settled=0 AND telegram_user_id=?",
            (uid,),
        )
        pending = dict(await cursor.fetchone())

        buckets = [(0.85, 0.90), (0.90, 0.95), (0.95, 1.01)]
        bucket_stats = []
        for lo, hi in buckets:
            cursor = await db.execute(
                """SELECT COUNT(*) as total, SUM(won) as wins FROM sporty_picks
                   WHERE settled=1 AND telegram_user_id=? AND confidence >= ? AND confidence < ?""",
                (uid, lo, hi),
            )
            row = dict(await cursor.fetchone())
            bucket_stats.append({"range": (lo, min(hi, 1.0)), **row})

        return {
            "overall_total": overall["total"] or 0,
            "overall_wins": overall["wins"] or 0,
            "pending": pending["total"] or 0,
            "buckets": bucket_stats,
        }


# ── Daily accumulator-regeneration limit ─────────────────────────────────────

async def get_todays_run_count(telegram_user_id) -> int:
    """How many accumulators this user has generated today (UTC date)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT run_count FROM sporty_acca_runs WHERE telegram_user_id=? AND run_date=date('now')",
            (int(telegram_user_id),),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def increment_todays_run_count(telegram_user_id) -> int:
    """Record one more accumulator generation for this user today. Returns the new count."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO sporty_acca_runs (telegram_user_id, run_date, run_count)
               VALUES (?, date('now'), 1)
               ON CONFLICT(telegram_user_id, run_date)
               DO UPDATE SET run_count = run_count + 1""",
            (int(telegram_user_id),),
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT run_count FROM sporty_acca_runs WHERE telegram_user_id=? AND run_date=date('now')",
            (int(telegram_user_id),),
        )
        row = await cursor.fetchone()
        return row[0] if row else 1


async def get_todays_used_event_ids(telegram_user_id) -> set:
    """SportyBet event ids already used in this user's accumulators booked
    today — so regenerating produces genuinely different games/selections
    instead of the same top-ranked picks again."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT DISTINCT fixture_id FROM sporty_picks
               WHERE telegram_user_id=? AND kind='acca_leg_sb'
               AND date(placed_at, 'unixepoch') = date('now')""",
            (int(telegram_user_id),),
        )
        rows = await cursor.fetchall()
        return {r[0] for r in rows}
