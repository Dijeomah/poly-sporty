"""WebSocket listener for new token launches — PumpPortal + Helius."""

import asyncio
import json
import logging
from typing import Callable, Awaitable

import websockets

from config import PUMPPORTAL_WSS_URL, HELIUS_WSS_URL

logger = logging.getLogger("listener")

_ws_connection = None
_running = False

# Callback type: async def callback(mint: str, name: str, symbol: str)
TokenCallback = Callable[[str, str, str], Awaitable[None]]


async def start_listener(callback: TokenCallback):
    """Connect to PumpPortal WebSocket and listen for new token creates."""
    global _ws_connection, _running
    _running = True

    while _running:
        try:
            logger.info("Connecting to PumpPortal WebSocket...")
            async with websockets.connect(PUMPPORTAL_WSS_URL, ping_interval=20) as ws:
                _ws_connection = ws

                # Subscribe to new token events
                subscribe_msg = {
                    "method": "subscribeNewToken",
                }
                await ws.send(json.dumps(subscribe_msg))
                logger.info("Subscribed to new token events")

                async for raw_msg in ws:
                    if not _running:
                        break
                    try:
                        data = json.loads(raw_msg)
                        await _handle_message(data, callback)
                    except json.JSONDecodeError:
                        continue
                    except Exception as e:
                        logger.error(f"Handler error: {e}")

        except websockets.ConnectionClosed:
            logger.warning("WebSocket disconnected, reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"WebSocket error: {e}")
            if _running:
                await asyncio.sleep(5)

    logger.info("Listener stopped")


async def stop_listener():
    """Stop the WebSocket listener."""
    global _running, _ws_connection
    _running = False
    if _ws_connection:
        await _ws_connection.close()
        _ws_connection = None


async def _handle_message(data: dict, callback: TokenCallback):
    """Parse PumpPortal message and invoke callback on new token creation."""
    # PumpPortal sends different event types
    if isinstance(data, dict) and data.get("txType") == "create":
        mint = data.get("mint", "")
        name = data.get("name", "Unknown")
        symbol = data.get("symbol", "???")

        if mint:
            logger.info(f"New token: {name} ({symbol}) — {mint}")
            await callback(mint, name, symbol)


async def start_helius_listener(callback: TokenCallback):
    """Alternative: listen for pump.fun program logs via Helius logsSubscribe."""
    global _running
    _running = True
    pump_program = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

    while _running:
        try:
            logger.info("Connecting to Helius WebSocket...")
            async with websockets.connect(HELIUS_WSS_URL, ping_interval=20) as ws:
                subscribe = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "logsSubscribe",
                    "params": [
                        {"mentions": [pump_program]},
                        {"commitment": "confirmed"},
                    ],
                }
                await ws.send(json.dumps(subscribe))
                logger.info("Subscribed to pump.fun program logs")

                async for raw_msg in ws:
                    if not _running:
                        break
                    try:
                        data = json.loads(raw_msg)
                        result = data.get("params", {}).get("result", {})
                        value = result.get("value", {})
                        logs = value.get("logs", [])

                        # Check if this is a "Create" instruction
                        is_create = any("Create" in log for log in logs)
                        if is_create:
                            # Extract mint from account keys
                            tx = value.get("transaction", {})
                            if isinstance(tx, dict):
                                keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
                                if len(keys) > 1:
                                    mint = keys[1]  # Typically the 2nd key is the new mint
                                    logger.info(f"Helius: new pump.fun token {mint}")
                                    await callback(mint, "pump.fun token", "???")
                    except Exception as e:
                        logger.error(f"Helius handler error: {e}")

        except websockets.ConnectionClosed:
            logger.warning("Helius WebSocket disconnected, reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Helius WebSocket error: {e}")
            if _running:
                await asyncio.sleep(5)
