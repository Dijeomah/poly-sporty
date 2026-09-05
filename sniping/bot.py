"""Main entry point — Telegram bot handlers + orchestration."""

import asyncio
import logging
from functools import wraps
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID, settings
from database import init_db, add_position, close_position, get_open_positions, get_position_by_mint
from wallet import load_or_create_keypair, get_pubkey, get_sol_balance, get_token_balance, get_token_accounts
from safety import run_safety_checks
from trader import buy_token, sell_token, get_token_price_sol
from monitor import start_monitor, stop_monitor
from listener import start_listener, stop_listener
from btc_arb import btc_arb
from sportybet.sporty import sporty_bot_manager
from sportybet import sporty_database as spdb

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("snipe-bot")

# Telegram user id -> Future awaiting their next plain-text reply, used to
# collect a SportyBet 2FA/OTP code mid-login (see _make_otp_callback and
# handle_plain_text below).
_pending_otp: dict[int, asyncio.Future] = {}


def _make_otp_callback(bot, chat_id: int, user_id: int):
    """Build an otp_callback for SportyBot.login_interactive: asks this user
    for their 2FA/OTP code over Telegram and waits (up to 3 min) for their
    plain-text reply, resolved via _pending_otp / handle_plain_text."""
    async def otp_callback() -> str:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        _pending_otp[user_id] = fut
        await bot.send_message(
            chat_id,
            "SportyBet is asking for a verification code (2FA/OTP).\n"
            "Reply with the code you received to continue logging in.",
        )
        try:
            return await asyncio.wait_for(fut, timeout=180)
        except asyncio.TimeoutError:
            await bot.send_message(chat_id, "Timed out waiting for the verification code.")
            return ""
        finally:
            _pending_otp.pop(user_id, None)
    return otp_callback


# ── Auth helpers ─────────────────────────────────────────────────────────────
def owner_only(func):
    """Restrict command to ALLOWED_USER_ID."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != ALLOWED_USER_ID:
            logger.warning(f"Unauthorized: user {user_id} != allowed {ALLOWED_USER_ID}")
            await update.message.reply_text(f"Unauthorized. Your ID: {user_id}")
            return
        return await func(update, context)
    return wrapper


def _check_callback_auth(query) -> bool:
    return query.from_user.id == ALLOWED_USER_ID


# ── Menu builders ────────────────────────────────────────────────────────────
def _main_menu_keyboard():
    """Build the main menu inline keyboard."""
    listener_icon = "ON" if settings.auto_listen else "OFF"
    btc_icon = "ON" if btc_arb.running else "OFF"
    sporty_icon = "ON" if settings.sporty_enabled else "OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Wallet", callback_data="nav_wallet"),
            InlineKeyboardButton("Positions", callback_data="nav_positions"),
        ],
        [
            InlineKeyboardButton("Trading", callback_data="nav_trading"),
            InlineKeyboardButton("Settings", callback_data="nav_settings"),
        ],
        [
            InlineKeyboardButton(f"Listener [{listener_icon}]", callback_data="nav_listener"),
            InlineKeyboardButton(f"BTC Arb [{btc_icon}]", callback_data="nav_btc"),
        ],
        [
            InlineKeyboardButton(f"SportyBet [{sporty_icon}]", callback_data="nav_sporty"),
        ],
        [
            InlineKeyboardButton("Refresh", callback_data="nav_main"),
        ],
    ])


async def _main_menu_text():
    """Build the main menu message text."""
    pubkey = str(get_pubkey())
    balance = await get_sol_balance()
    positions = await get_open_positions()
    listener_status = "ON" if settings.auto_listen else "OFF"
    btc_status = "ON" if btc_arb.running else "OFF"
    sporty_status = "ON" if settings.sporty_enabled else "OFF"
    rollover_info = ""
    owner_sbot = sporty_bot_manager.get(ALLOWED_USER_ID)
    if owner_sbot.rollover and owner_sbot.rollover.active:
        r = owner_sbot.rollover
        rollover_info = f" (Day {r.current_day}/{r.total_days})"

    return (
        f"Snipe Bot - Main Menu\n"
        f"{'=' * 28}\n\n"
        f"Wallet: `{pubkey}`\n"
        f"Balance: {balance:.4f} SOL\n"
        f"Open positions: {len(positions)}\n\n"
        f"Listener: {listener_status}\n"
        f"BTC Arb: {btc_status}\n"
        f"SportyBet: {sporty_status}{rollover_info}\n\n"
        f"Select a section below:"
    )


def _back_button(label="Back to Menu"):
    """Single-row back button."""
    return [InlineKeyboardButton(label, callback_data="nav_main")]


# ── /start ────────────────────────────────────────────────────────────────────
@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await _main_menu_text()
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=_main_menu_keyboard(),
    )


# ── /wallet ───────────────────────────────────────────────────────────────────
@owner_only
async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pubkey = str(get_pubkey())
    balance = await get_sol_balance()
    tokens = await get_token_accounts()
    lines = [
        f"Wallet\n{'=' * 28}\n",
        f"Address: `{pubkey}`",
        f"SOL: {balance:.4f}",
        "",
        "Token Balances:",
    ]
    if tokens:
        for t in tokens[:10]:
            lines.append(f"  `{t['mint'][:8]}...` — {t['amount']:,.2f}")
    else:
        lines.append("  (none)")
    keyboard = InlineKeyboardMarkup([_back_button()])
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


# ── /snipe <mint> ─────────────────────────────────────────────────────────────
@owner_only
async def cmd_snipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /snipe <token_mint_address>")
        return

    mint = context.args[0].strip()
    sol_amount = settings.buy_amount_sol

    # Check balance — need buy amount + ~0.005 SOL for fees/priority/rent
    balance = await get_sol_balance()
    if balance < sol_amount + 0.005:
        await update.message.reply_text(
            f"Insufficient SOL. Have {balance:.4f}, need {sol_amount + 0.005:.4f}"
        )
        return

    msg = await update.message.reply_text(f"Running safety checks on `{mint[:12]}...`", parse_mode="Markdown")

    # Safety checks
    report = await run_safety_checks(mint)
    await msg.edit_text(f"Safety Report:\n```\n{report.details}\n```", parse_mode="Markdown")

    if not report.passed:
        await update.message.reply_text(
            "Safety checks FAILED. Send /snipe_force <mint> to buy anyway."
        )
        return

    await _execute_buy(update, mint, sol_amount, is_pump_fun=report.is_pump_fun)


@owner_only
async def cmd_snipe_force(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buy a token bypassing safety checks."""
    if not context.args:
        await update.message.reply_text("Usage: /snipe_force <token_mint_address>")
        return
    mint = context.args[0].strip()
    await _execute_buy(update, mint, settings.buy_amount_sol)


async def _execute_buy(update: Update, mint: str, sol_amount: float, is_pump_fun: bool = False):
    """Shared buy logic."""
    route_label = "PumpPortal (bonding curve)" if is_pump_fun else "Jupiter"
    msg = await update.message.reply_text(
        f"Buying with {sol_amount} SOL via {route_label}...\n"
        f"Slippage: {settings.slippage_bps/100:.1f}%"
    )

    result = await buy_token(mint, sol_amount, is_pump_fun=is_pump_fun)
    if not result.success:
        await msg.edit_text(f"Buy FAILED: {result.error}")
        return

    # Calculate entry price (SOL per token)
    entry_price = sol_amount / result.out_amount if result.out_amount else 0

    await add_position(
        token_mint=mint,
        token_symbol="",
        entry_price=entry_price,
        amount_tokens=result.out_amount,
        sol_spent=sol_amount,
        buy_tx=result.tx_hash,
    )

    received_line = f"Received: {result.out_amount:,.2f} tokens\n" if result.out_amount else ""
    entry_line = f"Entry price: {entry_price:.10f} SOL/token\n" if entry_price else ""

    await msg.edit_text(
        f"BUY SUCCESS via {result.route}\n\n"
        f"Token: `{mint[:12]}...`\n"
        f"Spent: {sol_amount} SOL\n"
        f"{received_line}"
        f"{entry_line}"
        f"TX: `{result.tx_hash}`\n\n"
        f"[View on Solscan](https://solscan.io/tx/{result.tx_hash})",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ── /sell <mint> ──────────────────────────────────────────────────────────────
@owner_only
async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /sell <token_mint_address>")
        return

    mint = context.args[0].strip()
    position = await get_position_by_mint(mint)

    # Get actual on-chain token balance
    token_balance = await get_token_balance(mint)
    if token_balance <= 0:
        await update.message.reply_text("No token balance found for this mint.")
        return

    msg = await update.message.reply_text(f"Selling {token_balance:,.2f} tokens...")

    result = await sell_token(mint, token_balance)
    if not result.success:
        await msg.edit_text(f"Sell FAILED: {result.error}")
        return

    sol_received = result.out_amount
    pnl = sol_received - (position["sol_spent"] if position else 0)

    if position:
        await close_position(mint, result.tx_hash, pnl)

    pnl_pct = (pnl / position["sol_spent"] * 100) if position else 0
    emoji = "+" if pnl >= 0 else ""

    await msg.edit_text(
        f"SELL SUCCESS\n\n"
        f"Token: `{mint[:12]}...`\n"
        f"Sold: {token_balance:,.2f} tokens\n"
        f"Received: {sol_received:.4f} SOL\n"
        f"P&L: {emoji}{pnl:.4f} SOL ({emoji}{pnl_pct:.1f}%)\n"
        f"TX: `{result.tx_hash}`\n\n"
        f"[View on Solscan](https://solscan.io/tx/{result.tx_hash})",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ── /positions ────────────────────────────────────────────────────────────────
@owner_only
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    positions = await get_open_positions()
    keyboard = InlineKeyboardMarkup([_back_button()])
    if not positions:
        await update.message.reply_text(
            f"Positions\n{'=' * 28}\n\nNo open positions.",
            reply_markup=keyboard,
        )
        return

    lines = [f"Positions\n{'=' * 28}\n"]
    for p in positions:
        mint = p["token_mint"]
        current_price = await get_token_price_sol(mint)
        if current_price:
            current_val = current_price * p["amount_tokens"]
            pnl = current_val - p["sol_spent"]
            pnl_pct = (pnl / p["sol_spent"]) * 100
            sign = "+" if pnl >= 0 else ""
            lines.append(
                f"`{mint[:12]}...`\n"
                f"  Tokens: {p['amount_tokens']:,.2f}\n"
                f"  Spent: {p['sol_spent']:.4f} SOL\n"
                f"  Value: {current_val:.4f} SOL ({sign}{pnl_pct:.1f}%)\n"
            )
        else:
            lines.append(
                f"`{mint[:12]}...`\n"
                f"  Tokens: {p['amount_tokens']:,.2f}\n"
                f"  Spent: {p['sol_spent']:.4f} SOL\n"
                f"  Value: price unavailable\n"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


# ── /settings ─────────────────────────────────────────────────────────────────
@owner_only
async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    if not args:
        keyboard = InlineKeyboardMarkup([_back_button()])
        await update.message.reply_text(
            f"Settings\n{'=' * 28}\n\n"
            f"Solana Sniper:\n"
            f"  buy\\_amount: {settings.buy_amount_sol} SOL\n"
            f"  slippage: {settings.slippage_bps/100:.1f}%\n"
            f"  take\\_profit: {settings.take_profit_pct}%\n"
            f"  stop\\_loss: {settings.stop_loss_pct}%\n"
            f"  min\\_liquidity: ${settings.min_liquidity_usd:,.0f}\n"
            f"  priority\\_fee: {settings.priority_fee_lamports} lamports\n\n"
            f"BTC Arb:\n"
            f"  btc\\_bet: ${settings.btc_bet_amount}\n"
            f"  btc\\_edge: {settings.btc_min_edge}%\n"
            f"  btc\\_max\\_price: {settings.btc_max_price}\n"
            f"  btc\\_enabled: {settings.btc_enabled}\n\n"
            f"SportyBet:\n"
            f"  sporty\\_prob: {settings.sporty_min_probability}\n"
            f"  sporty\\_odds: {settings.sporty_default_odds}\n"
            f"  sporty\\_days: {settings.sporty_rollover_days}\n"
            f"  sporty\\_acca: {settings.sporty_acca_target}\n"
            f"  sporty\\_legs: {settings.sporty_max_legs}\n"
            f"  sporty\\_enabled: {settings.sporty_enabled}\n\n"
            f"To change: /settings <key> <value>\n"
            f"Example: /settings buy\\_amount 0.05",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return

    if len(args) < 2:
        await update.message.reply_text("Usage: /settings <key> <value>")
        return

    key, val = args[0], args[1]
    mapping = {
        "buy_amount": ("buy_amount_sol", float),
        "slippage": ("slippage_bps", lambda v: int(float(v) * 100)),
        "take_profit": ("take_profit_pct", float),
        "stop_loss": ("stop_loss_pct", float),
        "min_liquidity": ("min_liquidity_usd", float),
        "priority_fee": ("priority_fee_lamports", int),
        "btc_bet": ("btc_bet_amount", float),
        "btc_edge": ("btc_min_edge", float),
        "btc_max_price": ("btc_max_price", float),
        "btc_enabled": ("btc_enabled", lambda v: v.lower() in ("true", "1", "yes")),
        "sporty_prob": ("sporty_min_probability", float),
        "sporty_odds": ("sporty_default_odds", float),
        "sporty_days": ("sporty_rollover_days", int),
        "sporty_acca": ("sporty_acca_target", float),
        "sporty_legs": ("sporty_max_legs", int),
        "sporty_enabled": ("sporty_enabled", lambda v: v.lower() in ("true", "1", "yes")),
    }
    if key not in mapping:
        await update.message.reply_text(f"Unknown setting: {key}\nValid: {', '.join(mapping)}")
        return

    attr, converter = mapping[key]
    try:
        setattr(settings, attr, converter(val))
        await update.message.reply_text(f"Updated {key} = {getattr(settings, attr)}")
    except ValueError:
        await update.message.reply_text(f"Invalid value: {val}")


# ── /listen ───────────────────────────────────────────────────────────────────
@owner_only
async def cmd_listen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if settings.auto_listen:
        await update.message.reply_text("Listener already running. Use the Stop button or /stop_listen.")
        return
    settings.auto_listen = True
    asyncio.create_task(start_listener(new_token_callback(update, context)))
    keyboard = [[InlineKeyboardButton("Stop Listener", callback_data="menu_listen_stop")]]
    await update.message.reply_text(
        "Auto-snipe listener ENABLED. Monitoring pump.fun for new tokens.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


@owner_only
async def cmd_stop_listen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not settings.auto_listen:
        await update.message.reply_text("Listener is not running.")
        return
    settings.auto_listen = False
    await stop_listener()
    await update.message.reply_text("Auto-snipe listener STOPPED.")


def new_token_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Factory for the listener callback that sends Telegram notifications."""
    chat_id = update.effective_chat.id

    async def callback(mint: str, name: str, symbol: str):
        bot = context.bot
        report = await run_safety_checks(mint)
        text = (
            f"New Token Detected!\n\n"
            f"Name: {name}\n"
            f"Symbol: {symbol}\n"
            f"Mint: `{mint}`\n\n"
            f"Safety:\n```\n{report.details}\n```\n"
        )
        if report.passed:
            # Check balance before buying — need buy amount + fees (~0.005 SOL buffer)
            balance = await get_sol_balance()
            fee_buffer = 0.005
            if balance < settings.buy_amount_sol + fee_buffer:
                await bot.send_message(
                    chat_id,
                    f"Passed safety but SKIPPING — insufficient SOL.\n"
                    f"Have: {balance:.4f} SOL, need: {settings.buy_amount_sol + fee_buffer:.4f} SOL",
                )
                return

            route_label = "PumpPortal" if report.is_pump_fun else "Jupiter"
            text += f"\nAuto-buying with {settings.buy_amount_sol} SOL via {route_label}..."
            await bot.send_message(chat_id, text, parse_mode="Markdown")
            result = await buy_token(mint, settings.buy_amount_sol, is_pump_fun=report.is_pump_fun)
            if result.success:
                entry_price = settings.buy_amount_sol / result.out_amount if result.out_amount else 0
                await add_position(mint, symbol, entry_price, result.out_amount,
                                   settings.buy_amount_sol, result.tx_hash)
                await bot.send_message(
                    chat_id,
                    f"Auto-buy SUCCESS: {result.out_amount:,.2f} {symbol}\n"
                    f"TX: `{result.tx_hash}`",
                    parse_mode="Markdown",
                )
            else:
                await bot.send_message(chat_id, f"Auto-buy FAILED: {result.error}")
        else:
            text += "\nFailed safety — skipping."
            await bot.send_message(chat_id, text, parse_mode="Markdown")

    return callback


# ── BTC 5-Min Arb Commands ───────────────────────────────────────────────────
@owner_only
async def cmd_btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current BTC 5-min market status."""
    msg = await update.message.reply_text("Fetching BTC arb status...")
    status = await btc_arb.get_status()
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Start", callback_data="act_btc_start"),
            InlineKeyboardButton("Stop", callback_data="act_btc_stop"),
            InlineKeyboardButton("Refresh", callback_data="nav_btc"),
        ],
        _back_button(),
    ])
    await msg.edit_text(status, reply_markup=keyboard)


@owner_only
async def cmd_btc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the BTC 5-min arb loop."""
    if btc_arb.running:
        await update.message.reply_text("BTC arb loop is already running. /btc_stop to stop.")
        return

    chat_id = update.effective_chat.id
    bot = context.bot

    async def notify(message: str):
        await bot.send_message(chat_id, message)

    btc_arb.set_notify_callback(notify)
    btc_arb.start(settings)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Stop BTC Arb", callback_data="act_btc_stop")],
        _back_button(),
    ])
    await update.message.reply_text(
        f"BTC 5-min arb loop STARTED\n\n"
        f"Bet: ${settings.btc_bet_amount}\n"
        f"Edge: {settings.btc_min_edge}%\n"
        f"Max price: {settings.btc_max_price}\n"
        f"Scanning every 30 seconds.",
        reply_markup=keyboard,
    )


@owner_only
async def cmd_btc_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop the BTC 5-min arb loop."""
    if not btc_arb.running:
        await update.message.reply_text("BTC arb loop is not running.")
        return
    btc_arb.stop()
    await update.message.reply_text(
        "BTC 5-min arb loop STOPPED.",
        reply_markup=InlineKeyboardMarkup([_back_button()]),
    )


# ── SportyBet Commands ───────────────────────────────────────────────────────
# Open to any Telegram user — each gets their own isolated SportyBet session,
# rollover, and pick history via sporty_bot_manager. Unlike the Solana
# sniper/BTC-arb commands above, these are intentionally NOT @owner_only.
async def cmd_sporty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show SportyBet status."""
    sbot = sporty_bot_manager.get(update.effective_user.id)
    msg = await update.message.reply_text("Fetching SportyBet status...")
    status = await sbot.get_status()
    login_label = "Logged In" if sbot.booker.is_logged_in else "Login to SportyBet"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Daily Pick", callback_data="act_sporty_pick"),
            InlineKeyboardButton("Accumulator", callback_data="act_sporty_acca"),
        ],
        [
            InlineKeyboardButton("Start Rollover", callback_data="act_sporty_rollover_start"),
            InlineKeyboardButton("Stop Rollover", callback_data="act_sporty_rollover_stop"),
        ],
        [
            InlineKeyboardButton(login_label, callback_data="act_sporty_login"),
            InlineKeyboardButton("History", callback_data="act_sporty_history"),
        ],
        _back_button(),
    ])
    await msg.edit_text(status, reply_markup=keyboard)


async def cmd_sporty_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Find a single game pick and book it. Usage: /sporty_pick [odds] [days]"""
    sbot = sporty_bot_manager.get(update.effective_user.id)
    target_odds = settings.sporty_default_odds
    days_ahead = None
    if context.args:
        try:
            target_odds = float(context.args[0])
            if len(context.args) > 1:
                days_ahead = float(context.args[1])
        except ValueError:
            await update.message.reply_text(
                "Usage: /sporty_pick [odds] [days]\n"
                "Example: /sporty_pick 1.5 1  (games today only)"
            )
            return

    chat_id = update.effective_chat.id
    bot = context.bot

    async def notify(message: str):
        await bot.send_message(chat_id, message)

    sbot.set_notify_callback(notify)
    msg = await update.message.reply_text(f"Searching for daily pick at ~{target_odds} odds...")
    result = await sbot.find_daily_pick(target_odds, settings, days_ahead=days_ahead)

    if result.success:
        await msg.edit_text(
            f"Daily Pick BOOKED!\n\n"
            f"{result.selections_summary}\n\n"
            f"Booking Code: {result.booking_code}\n"
            f"Use code on sportybet.com.ng",
            reply_markup=InlineKeyboardMarkup([_back_button()]),
        )
    else:
        text = f"Daily Pick Result:\n\n"
        if result.selections_summary:
            text += f"{result.selections_summary}\n\n"
        if result.error:
            text += f"Note: {result.error}"
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([_back_button()]))


async def cmd_sporty_acca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Build accumulator ticket. Usage: /sporty_acca [target_odds] [days]

    days controls how far out games can kick off — e.g. 1 for a short
    "today only" ticket, 14 for a long ticket spanning two weeks. Capped at
    5 generations per user per day; each regenerate excludes games already
    used in a ticket booked earlier today.
    """
    sbot = sporty_bot_manager.get(update.effective_user.id)
    target = settings.sporty_acca_target
    days_ahead = None
    if context.args:
        try:
            target = float(context.args[0])
            if len(context.args) > 1:
                days_ahead = float(context.args[1])
        except ValueError:
            await update.message.reply_text(
                "Usage: /sporty_acca [target_odds] [days]\n"
                "Example: /sporty_acca 100 14  (long ticket, games within 2 weeks)"
            )
            return

    chat_id = update.effective_chat.id
    bot = context.bot

    async def notify(message: str):
        await bot.send_message(chat_id, message)

    sbot.set_notify_callback(notify)
    msg = await update.message.reply_text(f"Building accumulator for {target:.0f}x odds...")
    result = await sbot.generate_accumulator_from_sportybet(target, settings, days_ahead=days_ahead)

    if result.success:
        await msg.edit_text(
            f"Accumulator BOOKED!\n\n"
            f"{result.selections_summary}\n\n"
            f"Booking Code: {result.booking_code}",
            reply_markup=InlineKeyboardMarkup([_back_button()]),
        )
    else:
        text = f"Accumulator Result:\n\n"
        if result.selections_summary:
            text += f"{result.selections_summary}\n\n"
        if result.error:
            text += f"Note: {result.error}"
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([_back_button()]))


async def cmd_sporty_rollover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start daily rollover. Usage: /sporty_rollover [odds] [days]"""
    sbot = sporty_bot_manager.get(update.effective_user.id)
    days = settings.sporty_rollover_days
    odds = settings.sporty_default_odds

    if context.args:
        try:
            odds = float(context.args[0])
            if len(context.args) > 1:
                days = int(context.args[1])
        except ValueError:
            await update.message.reply_text("Usage: /sporty_rollover [odds] [days]\nExample: /sporty_rollover 1.5 5")
            return

    chat_id = update.effective_chat.id
    bot = context.bot

    async def notify(message: str):
        await bot.send_message(chat_id, message)

    sbot.set_notify_callback(notify)
    started = await sbot.start_rollover(days, odds, settings)

    if started:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Stop Rollover", callback_data="act_sporty_rollover_stop")],
            _back_button(),
        ])
        await update.message.reply_text(
            f"Rollover STARTED\n\n"
            f"Plan: {days} days at {odds} odds/day\n"
            f"Target final odds: {odds ** days:.2f}x\n\n"
            f"You'll get a booking code each day.",
            reply_markup=keyboard,
        )
    else:
        await update.message.reply_text(
            "Could not start rollover. A rollover may already be active.\n"
            "Use /sporty_stop to stop it first.",
            reply_markup=InlineKeyboardMarkup([_back_button()]),
        )


async def cmd_sporty_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop active rollover."""
    sbot = sporty_bot_manager.get(update.effective_user.id)
    if not sbot.rollover or not sbot.rollover.active:
        await update.message.reply_text("No active rollover to stop.")
        return

    sbot.stop()
    await update.message.reply_text(
        "Rollover STOPPED.",
        reply_markup=InlineKeyboardMarkup([_back_button()]),
    )


async def cmd_sporty_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log in to SportyBet interactively. Usage: /sporty_login [phone] [password]

    Opens a VISIBLE browser on the machine running the bot — a human still
    needs to be at that screen to solve SportyBet's CAPTCHA when it appears.
    Only the resulting session cookies are ever stored; your password is
    never written to disk or logged, and this bot best-effort deletes your
    credential message right after reading it (Telegram may not always
    permit that in a private chat — don't rely on it; treat this the same
    as sending your password to whoever runs this bot).

    With no args: the bot owner falls back to the SPORTYBET_PHONE/PASSWORD
    configured in .env. Anyone else must pass their own phone + password.
    """
    sbot = sporty_bot_manager.get(update.effective_user.id)
    phone = password = None

    if context.args:
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /sporty_login <phone> <password>")
            return
        phone, password = context.args[0], context.args[1]
        try:
            await context.bot.delete_message(update.effective_chat.id, update.message.message_id)
        except Exception:
            logger.warning("Could not delete credential message (Telegram may restrict this in private chats)")
    elif update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text(
            "Usage: /sporty_login <phone> <password>\n"
            "Your SportyBet phone + password — this bot logs in on its host "
            "machine's browser (a human there still solves any CAPTCHA)."
        )
        return

    await update.message.reply_text(
        "Opening SportyBet in a visible browser...\n"
        "Someone needs to be at the machine running the bot to solve any CAPTCHA.\n"
        "You have 5 minutes."
    )

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    otp_callback = _make_otp_callback(context.bot, chat_id, user_id)
    success = await sbot.login_interactive(phone, password, otp_callback)
    if success:
        await update.message.reply_text(
            "SportyBet login successful! Session saved.\n"
            "Future /sporty_pick and /sporty_acca will auto-book.",
            reply_markup=InlineKeyboardMarkup([_back_button()]),
        )
    else:
        await update.message.reply_text(
            "SportyBet login failed or timed out.\n"
            "Try again with /sporty_login",
            reply_markup=InlineKeyboardMarkup([_back_button()]),
        )


async def handle_plain_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch-all for non-command text messages.

    Currently only used to collect a SportyBet 2FA/OTP code when
    cmd_sporty_login is mid-flow and waiting on this user (see
    _pending_otp) — otherwise does nothing, so it never interferes with
    normal command handling.
    """
    user_id = update.effective_user.id
    fut = _pending_otp.get(user_id)
    if fut and not fut.done():
        code = (update.message.text or "").strip()
        fut.set_result(code)
        chat_id = update.effective_chat.id
        try:
            await context.bot.delete_message(chat_id, update.message.message_id)
        except Exception:
            pass
        await context.bot.send_message(chat_id, "Got it — continuing login...")


async def _sporty_history_text(telegram_user_id) -> str:
    """Build the /sporty_history status text from this user's settled pick stats."""
    stats = await spdb.get_history_stats(telegram_user_id)
    total = stats["overall_total"]
    wins = stats["overall_wins"]
    lines = ["SportyBet Track Record\n" + "=" * 28 + "\n"]

    if total == 0:
        lines.append("No settled picks yet.")
    else:
        win_rate = (wins / total) * 100
        lines.append(f"Overall: {wins}/{total} correct ({win_rate:.1f}%)")
        lines.append("")
        lines.append("By confidence bucket:")
        for b in stats["buckets"]:
            lo, hi = b["range"]
            b_total = b["total"] or 0
            b_wins = b["wins"] or 0
            if b_total == 0:
                lines.append(f"  {lo:.0%}-{hi:.0%}: no settled picks yet")
            else:
                b_rate = (b_wins / b_total) * 100
                lines.append(f"  {lo:.0%}-{hi:.0%}: {b_wins}/{b_total} ({b_rate:.1f}%)")

    lines.append(f"\nPending (not yet settled): {stats['pending']}")
    return "\n".join(lines)


async def cmd_sporty_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show win/loss track record for past SportyBet picks."""
    text = await _sporty_history_text(update.effective_user.id)
    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup([_back_button()])
    )


# ── Inline button callbacks (navigable menu system) ─────────────────────────
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    # SportyBet buttons are open to any user (each gets their own isolated
    # session via sporty_bot_manager); every other button stays owner-only.
    is_sporty_button = data.startswith(("nav_sporty", "act_sporty"))
    if not is_sporty_button and not _check_callback_auth(query):
        await query.answer("Unauthorized.")
        return

    await query.answer()
    sbot = sporty_bot_manager.get(query.from_user.id) if is_sporty_button else None

    # ── Navigation: Main Menu ────────────────────────────────────────────
    if data == "nav_main":
        text = await _main_menu_text()
        await query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=_main_menu_keyboard(),
        )

    # ── Navigation: Wallet ───────────────────────────────────────────────
    elif data == "nav_wallet":
        pubkey = str(get_pubkey())
        balance = await get_sol_balance()
        tokens = await get_token_accounts()
        lines = [
            "Wallet\n" + "=" * 28 + "\n",
            f"Address: `{pubkey}`",
            f"SOL: {balance:.4f}\n",
            "Token Balances:",
        ]
        if tokens:
            for t in tokens[:10]:
                lines.append(f"  `{t['mint'][:8]}...` — {t['amount']:,.2f}")
        else:
            lines.append("  (none)")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Refresh", callback_data="nav_wallet")],
            _back_button(),
        ])
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)

    # ── Navigation: Positions ────────────────────────────────────────────
    elif data == "nav_positions":
        positions = await get_open_positions()
        if not positions:
            keyboard = InlineKeyboardMarkup([_back_button()])
            await query.edit_message_text(
                "Positions\n" + "=" * 28 + "\n\nNo open positions.",
                reply_markup=keyboard,
            )
            return

        lines = ["Positions\n" + "=" * 28 + "\n"]
        for p in positions:
            mint = p["token_mint"]
            current_price = await get_token_price_sol(mint)
            if current_price:
                current_val = current_price * p["amount_tokens"]
                pnl = current_val - p["sol_spent"]
                pnl_pct = (pnl / p["sol_spent"]) * 100
                sign = "+" if pnl >= 0 else ""
                lines.append(
                    f"`{mint[:12]}...`\n"
                    f"  Tokens: {p['amount_tokens']:,.2f}\n"
                    f"  Spent: {p['sol_spent']:.4f} SOL\n"
                    f"  Value: {current_val:.4f} SOL ({sign}{pnl_pct:.1f}%)\n"
                )
            else:
                lines.append(
                    f"`{mint[:12]}...`\n"
                    f"  Tokens: {p['amount_tokens']:,.2f}\n"
                    f"  Spent: {p['sol_spent']:.4f} SOL\n"
                    f"  Value: price unavailable\n"
                )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Refresh", callback_data="nav_positions")],
            _back_button(),
        ])
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)

    # ── Navigation: Trading ──────────────────────────────────────────────
    elif data == "nav_trading":
        text = (
            "Trading\n" + "=" * 28 + "\n\n"
            "Use these commands to trade:\n\n"
            "/snipe <mint> — Buy a token\n"
            "/snipe\\_force <mint> — Buy (skip safety)\n"
            "/sell <mint> — Sell a token\n"
        )
        keyboard = InlineKeyboardMarkup([_back_button()])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    # ── Navigation: Settings ─────────────────────────────────────────────
    elif data == "nav_settings":
        text = (
            "Settings\n" + "=" * 28 + "\n\n"
            "Solana Sniper:\n"
            f"  buy\\_amount: {settings.buy_amount_sol} SOL\n"
            f"  slippage: {settings.slippage_bps/100:.1f}%\n"
            f"  take\\_profit: {settings.take_profit_pct}%\n"
            f"  stop\\_loss: {settings.stop_loss_pct}%\n"
            f"  min\\_liquidity: ${settings.min_liquidity_usd:,.0f}\n"
            f"  priority\\_fee: {settings.priority_fee_lamports} lamports\n\n"
            "BTC Arb:\n"
            f"  btc\\_bet: ${settings.btc_bet_amount}\n"
            f"  btc\\_edge: {settings.btc_min_edge}%\n"
            f"  btc\\_max\\_price: {settings.btc_max_price}\n"
            f"  btc\\_enabled: {settings.btc_enabled}\n\n"
            "SportyBet:\n"
            f"  sporty\\_prob: {settings.sporty_min_probability}\n"
            f"  sporty\\_odds: {settings.sporty_default_odds}\n"
            f"  sporty\\_days: {settings.sporty_rollover_days}\n"
            f"  sporty\\_acca: {settings.sporty_acca_target}\n"
            f"  sporty\\_legs: {settings.sporty_max_legs}\n"
            f"  sporty\\_enabled: {settings.sporty_enabled}\n\n"
            "To change: /settings <key> <value>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Refresh", callback_data="nav_settings")],
            _back_button(),
        ])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    # ── Navigation: Listener ─────────────────────────────────────────────
    elif data == "nav_listener":
        status = "RUNNING" if settings.auto_listen else "STOPPED"
        text = (
            "Auto-Snipe Listener\n" + "=" * 28 + "\n\n"
            f"Status: {status}\n\n"
            "Monitors pump.fun for new token launches\n"
            "and auto-buys tokens that pass safety checks."
        )
        if settings.auto_listen:
            buttons = [
                InlineKeyboardButton("Stop Listener", callback_data="act_listen_stop"),
            ]
        else:
            buttons = [
                InlineKeyboardButton("Start Listener", callback_data="act_listen_start"),
            ]
        keyboard = InlineKeyboardMarkup([buttons, _back_button()])
        await query.edit_message_text(text, reply_markup=keyboard)

    # ── Navigation: BTC Arb ──────────────────────────────────────────────
    elif data == "nav_btc":
        status_text = await btc_arb.get_status()
        if btc_arb.running:
            buttons = [
                InlineKeyboardButton("Stop", callback_data="act_btc_stop"),
                InlineKeyboardButton("Refresh", callback_data="nav_btc"),
            ]
        else:
            buttons = [
                InlineKeyboardButton("Start", callback_data="act_btc_start"),
                InlineKeyboardButton("Refresh", callback_data="nav_btc"),
            ]
        keyboard = InlineKeyboardMarkup([buttons, _back_button()])
        await query.edit_message_text(status_text, reply_markup=keyboard)

    # ── Actions: Listener ────────────────────────────────────────────────
    elif data == "act_listen_start":
        if settings.auto_listen:
            await query.edit_message_text(
                "Listener is already running.",
                reply_markup=InlineKeyboardMarkup([_back_button()]),
            )
            return
        settings.auto_listen = True
        asyncio.create_task(start_listener(new_token_callback(update, context)))
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Stop Listener", callback_data="act_listen_stop")],
            _back_button(),
        ])
        await query.edit_message_text(
            "Auto-snipe listener STARTED\n\nMonitoring pump.fun for new tokens.",
            reply_markup=keyboard,
        )

    elif data == "act_listen_stop":
        if not settings.auto_listen:
            await query.edit_message_text(
                "Listener is already stopped.",
                reply_markup=InlineKeyboardMarkup([_back_button()]),
            )
            return
        settings.auto_listen = False
        await stop_listener()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Start Listener", callback_data="act_listen_start")],
            _back_button(),
        ])
        await query.edit_message_text("Auto-snipe listener STOPPED.", reply_markup=keyboard)

    # ── Actions: BTC Arb ─────────────────────────────────────────────────
    elif data == "act_btc_start":
        if btc_arb.running:
            await query.edit_message_text(
                "BTC arb loop is already running.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Stop", callback_data="act_btc_stop")],
                    _back_button(),
                ]),
            )
            return

        chat_id = query.message.chat_id
        bot = context.bot

        async def _btc_notify(message: str):
            await bot.send_message(chat_id, message)

        btc_arb.set_notify_callback(_btc_notify)
        btc_arb.start(settings)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Stop", callback_data="act_btc_stop"),
                InlineKeyboardButton("Status", callback_data="nav_btc"),
            ],
            _back_button(),
        ])
        await query.edit_message_text(
            f"BTC 5-min arb loop STARTED\n\n"
            f"Bet: ${settings.btc_bet_amount}\n"
            f"Edge: {settings.btc_min_edge}%\n"
            f"Max price: {settings.btc_max_price}\n"
            f"Scanning every 30 seconds.",
            reply_markup=keyboard,
        )

    elif data == "act_btc_stop":
        if not btc_arb.running:
            await query.edit_message_text(
                "BTC arb loop is already stopped.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Start", callback_data="act_btc_start")],
                    _back_button(),
                ]),
            )
            return
        btc_arb.stop()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Start", callback_data="act_btc_start")],
            _back_button(),
        ])
        await query.edit_message_text("BTC 5-min arb loop STOPPED.", reply_markup=keyboard)

    # ── Navigation: SportyBet ─────────────────────────────────────────────
    elif data == "nav_sporty":
        status_text = await sbot.get_status()
        login_label = "Logged In" if sbot.booker.is_logged_in else "Login to SportyBet"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Daily Pick", callback_data="act_sporty_pick"),
                InlineKeyboardButton("Accumulator", callback_data="act_sporty_acca"),
            ],
            [
                InlineKeyboardButton("Start Rollover", callback_data="act_sporty_rollover_start"),
                InlineKeyboardButton("Stop Rollover", callback_data="act_sporty_rollover_stop"),
            ],
            [
                InlineKeyboardButton(login_label, callback_data="act_sporty_login"),
                InlineKeyboardButton("History", callback_data="act_sporty_history"),
            ],
            [InlineKeyboardButton("Refresh", callback_data="nav_sporty")],
            _back_button(),
        ])
        await query.edit_message_text(status_text, reply_markup=keyboard)

    # ── Actions: SportyBet ────────────────────────────────────────────────
    elif data == "act_sporty_pick":
        chat_id = query.message.chat_id
        bot = context.bot

        async def _sporty_pick_notify(message: str):
            await bot.send_message(chat_id, message)

        sbot.set_notify_callback(_sporty_pick_notify)
        await query.edit_message_text(
            f"Searching for daily pick at ~{settings.sporty_default_odds} odds...",
        )
        result = await sbot.find_daily_pick(settings.sporty_default_odds, settings)

        if result.success:
            text = (
                f"Daily Pick BOOKED!\n\n"
                f"{result.selections_summary}\n\n"
                f"Booking Code: {result.booking_code}"
            )
        else:
            text = f"Daily Pick Result:\n\n"
            if result.selections_summary:
                text += f"{result.selections_summary}\n\n"
            if result.error:
                text += f"Note: {result.error}"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to SportyBet", callback_data="nav_sporty")],
            _back_button(),
        ])
        await query.edit_message_text(text, reply_markup=keyboard)

    elif data == "act_sporty_acca":
        chat_id = query.message.chat_id
        bot = context.bot

        async def _sporty_acca_notify(message: str):
            await bot.send_message(chat_id, message)

        sbot.set_notify_callback(_sporty_acca_notify)
        await query.edit_message_text(
            f"Building accumulator for {settings.sporty_acca_target:.0f}x odds..."
        )
        result = await sbot.generate_accumulator_from_sportybet(settings.sporty_acca_target, settings)

        if result.success:
            text = (
                f"Accumulator BOOKED!\n\n"
                f"{result.selections_summary}\n\n"
                f"Booking Code: {result.booking_code}"
            )
        else:
            text = f"Accumulator Result:\n\n"
            if result.selections_summary:
                text += f"{result.selections_summary}\n\n"
            if result.error:
                text += f"Note: {result.error}"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to SportyBet", callback_data="nav_sporty")],
            _back_button(),
        ])
        await query.edit_message_text(text, reply_markup=keyboard)

    elif data == "act_sporty_rollover_start":
        if sbot.rollover and sbot.rollover.active:
            await query.edit_message_text(
                "A rollover is already active. Stop it first.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Stop Rollover", callback_data="act_sporty_rollover_stop")],
                    _back_button(),
                ]),
            )
            return

        chat_id = query.message.chat_id
        bot = context.bot

        async def _sporty_rollover_notify(message: str):
            await bot.send_message(chat_id, message)

        sbot.set_notify_callback(_sporty_rollover_notify)
        days = settings.sporty_rollover_days
        odds = settings.sporty_default_odds
        await sbot.start_rollover(days, odds, settings)

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Stop Rollover", callback_data="act_sporty_rollover_stop")],
            [InlineKeyboardButton("Status", callback_data="nav_sporty")],
            _back_button(),
        ])
        await query.edit_message_text(
            f"Rollover STARTED\n\n"
            f"Plan: {days} days at {odds} odds/day\n"
            f"Target: {odds ** days:.2f}x\n\n"
            f"You'll get daily booking codes.",
            reply_markup=keyboard,
        )

    elif data == "act_sporty_rollover_stop":
        if not sbot.rollover or not sbot.rollover.active:
            await query.edit_message_text(
                "No active rollover to stop.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to SportyBet", callback_data="nav_sporty")],
                    _back_button(),
                ]),
            )
            return

        sbot.stop()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to SportyBet", callback_data="nav_sporty")],
            _back_button(),
        ])
        await query.edit_message_text("Rollover STOPPED.", reply_markup=keyboard)

    elif data == "act_sporty_login":
        chat_id = query.message.chat_id
        bot = context.bot
        if query.from_user.id != ALLOWED_USER_ID:
            # Buttons can't collect text input — non-owners must use the
            # text command so they can supply their own credentials.
            await query.edit_message_text(
                "Use /sporty_login <phone> <password> with your own SportyBet "
                "login (this button only works for the bot owner's .env credentials)."
            )
            return
        await query.edit_message_text(
            "Opening SportyBet in a visible browser...\n"
            "Complete login on the machine running the bot (5 min timeout)."
        )

        otp_callback = _make_otp_callback(bot, chat_id, query.from_user.id)
        success = await sbot.login_interactive(otp_callback=otp_callback)
        if success:
            await bot.send_message(
                chat_id,
                "SportyBet login successful! Session saved.\n"
                "Future picks/accas will auto-book.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Back to SportyBet", callback_data="nav_sporty")],
                    _back_button(),
                ]),
            )
        else:
            await bot.send_message(
                chat_id,
                "SportyBet login failed or timed out.\nTry again from the SportyBet menu.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Retry Login", callback_data="act_sporty_login")],
                    _back_button(),
                ]),
            )

    elif data == "act_sporty_history":
        text = await _sporty_history_text(query.from_user.id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Back to SportyBet", callback_data="nav_sporty")],
            _back_button(),
        ])
        await query.edit_message_text(text, reply_markup=keyboard)


# ── Application setup ─────────────────────────────────────────────────────────
def main():
    load_or_create_keypair()

    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: Set TELEGRAM_BOT_TOKEN in .env")
        return

    if ALLOWED_USER_ID == 0:
        print("WARNING: ALLOWED_USER_ID not set — bot will reject all commands!")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("snipe", cmd_snipe))
    app.add_handler(CommandHandler("snipe_force", cmd_snipe_force))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("listen", cmd_listen))
    app.add_handler(CommandHandler("stop_listen", cmd_stop_listen))
    app.add_handler(CommandHandler("btc", cmd_btc))
    app.add_handler(CommandHandler("btc_start", cmd_btc_start))
    app.add_handler(CommandHandler("btc_stop", cmd_btc_stop))
    app.add_handler(CommandHandler("sporty", cmd_sporty))
    app.add_handler(CommandHandler("sporty_pick", cmd_sporty_pick))
    app.add_handler(CommandHandler("sporty_acca", cmd_sporty_acca))
    app.add_handler(CommandHandler("sporty_rollover", cmd_sporty_rollover))
    app.add_handler(CommandHandler("sporty_stop", cmd_sporty_stop))
    app.add_handler(CommandHandler("sporty_login", cmd_sporty_login))
    app.add_handler(CommandHandler("sporty_history", cmd_sporty_history))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_plain_text))

    # Start background monitor + register command menu after bot starts
    async def post_init(application: Application):
        await init_db()
        await spdb.init_sporty_db()
        asyncio.create_task(start_monitor(application.bot, ALLOWED_USER_ID))

        # Resume every user's active rollover (not just the owner's) after a
        # restart, each notifying its own chat — without this they'd have
        # nowhere to send messages until that user runs a live command again.
        def _make_sporty_notify(user_id):
            async def _notify(message: str):
                await application.bot.send_message(user_id, message)
            return _notify

        for row in await spdb.get_all_active_rollovers():
            uid = row["telegram_user_id"]
            resumed_sbot = sporty_bot_manager.get(uid)
            resumed_sbot.set_notify_callback(_make_sporty_notify(uid))
            await resumed_sbot.resume_rollover(settings, row)

        asyncio.create_task(sporty_bot_manager.run_periodic_maintenance())

        # Register Telegram command menu (shows in "/" autocomplete)
        await application.bot.set_my_commands([
            BotCommand("start", "Main menu"),
            BotCommand("wallet", "Wallet info & balances"),
            BotCommand("positions", "View open positions"),
            BotCommand("snipe", "Buy token — /snipe <mint>"),
            BotCommand("sell", "Sell token — /sell <mint>"),
            BotCommand("settings", "View or change settings"),
            BotCommand("listen", "Start auto-snipe listener"),
            BotCommand("stop_listen", "Stop auto-snipe listener"),
            BotCommand("btc", "BTC 5-min arb status"),
            BotCommand("btc_start", "Start BTC arb loop"),
            BotCommand("btc_stop", "Stop BTC arb loop"),
            BotCommand("sporty", "SportyBet status"),
            BotCommand("sporty_pick", "Daily pick — /sporty_pick [odds] [days]"),
            BotCommand("sporty_acca", "Accumulator — /sporty_acca [target] [days]"),
            BotCommand("sporty_rollover", "Start rollover — /sporty_rollover [odds] [days]"),
            BotCommand("sporty_stop", "Stop active rollover"),
            BotCommand("sporty_login", "Login to SportyBet (visible browser)"),
            BotCommand("sporty_history", "SportyBet pick track record"),
        ])

        logger.info("Bot started. Monitor running. Commands registered.")

    app.post_init = post_init

    print("Bot starting... Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
