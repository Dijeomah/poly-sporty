"""Background P&L monitoring with auto take-profit / stop-loss."""

import asyncio
import logging
from telegram import Bot
from config import settings
from database import get_open_positions, close_position
from trader import get_token_price_sol, sell_token
from wallet import get_token_balance

logger = logging.getLogger("monitor")
_running = False


async def start_monitor(bot: Bot, chat_id: int):
    """Background loop — check positions for TP/SL triggers."""
    global _running
    _running = True
    logger.info("Position monitor started")

    while _running:
        try:
            await _check_positions(bot, chat_id)
        except Exception as e:
            logger.error(f"Monitor error: {e}")
        await asyncio.sleep(settings.monitor_interval_sec)


async def stop_monitor():
    global _running
    _running = False


async def _check_positions(bot: Bot, chat_id: int):
    positions = await get_open_positions()
    if not positions:
        return

    # Batch price query
    mints = [p["token_mint"] for p in positions]

    for pos in positions:
        mint = pos["token_mint"]
        try:
            price = await get_token_price_sol(mint)
            if price is None:
                continue

            current_value = price * pos["amount_tokens"]
            pnl_pct = ((current_value - pos["sol_spent"]) / pos["sol_spent"]) * 100

            # Take profit
            if pnl_pct >= settings.take_profit_pct:
                await _auto_sell(bot, chat_id, pos, price, current_value, pnl_pct, "TAKE PROFIT")

            # Stop loss
            elif pnl_pct <= -settings.stop_loss_pct:
                await _auto_sell(bot, chat_id, pos, price, current_value, pnl_pct, "STOP LOSS")

        except Exception as e:
            logger.error(f"Error checking {mint[:12]}: {e}")


async def _auto_sell(bot: Bot, chat_id: int, pos: dict, price: float,
                     current_value: float, pnl_pct: float, reason: str):
    """Execute auto-sell and notify."""
    mint = pos["token_mint"]
    token_balance = await get_token_balance(mint)
    if token_balance <= 0:
        # Position has no tokens — mark as closed
        await close_position(mint, "", 0)
        return

    await bot.send_message(
        chat_id,
        f"{reason} triggered!\n\n"
        f"Token: `{mint[:12]}...`\n"
        f"P&L: {pnl_pct:+.1f}%\n"
        f"Selling {token_balance:,.2f} tokens...",
        parse_mode="Markdown",
    )

    result = await sell_token(mint, token_balance)
    if result.success:
        sol_received = result.out_amount
        pnl_sol = sol_received - pos["sol_spent"]
        await close_position(mint, result.tx_hash, pnl_sol)
        await bot.send_message(
            chat_id,
            f"{reason} — SOLD\n\n"
            f"Received: {sol_received:.4f} SOL\n"
            f"P&L: {pnl_sol:+.4f} SOL ({pnl_pct:+.1f}%)\n"
            f"TX: `{result.tx_hash}`",
            parse_mode="Markdown",
        )
    else:
        await bot.send_message(
            chat_id,
            f"{reason} sell FAILED: {result.error}\n"
            f"Manual sell needed: /sell {mint}",
        )
