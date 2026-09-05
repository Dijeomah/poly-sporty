"""Swap execution — Jupiter V6 for DEX tokens, PumpPortal for bonding curve tokens."""

import asyncio
import base64
import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from config import (
    JUPITER_API_KEY,
    JUPITER_QUOTE_URL,
    JUPITER_SWAP_URL,
    DEXSCREENER_API_URL,
    HELIUS_RPC_URL,
    SOL_MINT,
    LAMPORTS_PER_SOL,
    settings,
)
from wallet import load_or_create_keypair, rpc_request


def _jup_headers() -> dict:
    """Jupiter API headers — includes API key if configured."""
    headers = {}
    if JUPITER_API_KEY:
        headers["x-api-key"] = JUPITER_API_KEY
    return headers

# ── PumpPortal endpoint ──────────────────────────────────────────────────────
PUMPPORTAL_TRADE_URL = "https://pumpportal.fun/api/trade-local"


class SwapResult:
    def __init__(self, success: bool, tx_hash: str = "", error: str = "",
                 in_amount: float = 0, out_amount: float = 0, route: str = ""):
        self.success = success
        self.tx_hash = tx_hash
        self.error = error
        self.in_amount = in_amount
        self.out_amount = out_amount
        self.route = route  # "jupiter" or "pumpportal"

    def __repr__(self):
        if self.success:
            return f"SwapResult(OK via {self.route} tx={self.tx_hash[:16]}... in={self.in_amount} out={self.out_amount})"
        return f"SwapResult(FAIL: {self.error})"


# ─── Shared helpers ───────────────────────────────────────────────────────────

def sign_transaction(tx_bytes: bytes, keypair: Keypair) -> bytes:
    """Deserialize, sign, and re-serialize the transaction."""
    tx = VersionedTransaction.from_bytes(tx_bytes)
    signed = VersionedTransaction(tx.message, [keypair])
    return bytes(signed)


async def send_transaction(signed_bytes: bytes) -> str:
    """Send signed transaction via Helius RPC. Returns tx signature."""
    encoded = base64.b64encode(signed_bytes).decode()
    result = await rpc_request(
        "sendTransaction",
        [
            encoded,
            {
                "encoding": "base64",
                "skipPreflight": True,
                "maxRetries": 3,
            },
        ],
    )
    return result  # tx signature string


async def confirm_transaction(tx_sig: str, timeout_sec: int = 45) -> bool:
    """Poll getSignatureStatuses until confirmed or timeout."""
    for _ in range(timeout_sec // 2):
        await asyncio.sleep(2)
        try:
            result = await rpc_request("getSignatureStatuses", [[tx_sig]])
            statuses = result.get("value", [])
            if statuses and statuses[0]:
                status = statuses[0]
                if status.get("err"):
                    print(f"TX failed on-chain: {status['err']}")
                    return False
                conf = status.get("confirmationStatus", "")
                if conf in ("confirmed", "finalized"):
                    return True
        except Exception as e:
            print(f"Confirm poll error: {e}")
    return False


# ─── PumpPortal (bonding curve) ──────────────────────────────────────────────

MAX_RETRIES = 3
CONFIRM_FAST_SEC = 15  # Quick confirm check per attempt before retrying


async def _pumpportal_build_and_send(
    keypair: Keypair, action: str, mint: str,
    amount: float, denominated_in_sol: bool,
) -> tuple[str, str]:
    """Build TX via PumpPortal, sign, send. Returns (tx_sig, error)."""
    payload = {
        "publicKey": str(keypair.pubkey()),
        "action": action,
        "mint": mint,
        "amount": amount,
        "denominatedInSol": "true" if denominated_in_sol else "false",
        "slippage": settings.slippage_bps / 100,  # PumpPortal uses %
        "priorityFee": settings.priority_fee_lamports / LAMPORTS_PER_SOL,
        "pool": "auto",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(PUMPPORTAL_TRADE_URL, data=payload)
        if resp.status_code != 200:
            return "", f"PumpPortal error ({resp.status_code}): {resp.text}"
        tx_bytes = resp.content

    signed = sign_transaction(tx_bytes, keypair)
    tx_sig = await send_transaction(signed)
    return tx_sig, ""


async def _confirm_fast(tx_sig: str, timeout_sec: int = CONFIRM_FAST_SEC) -> bool | None:
    """Quick confirmation check. Returns True/False/None (None = still pending)."""
    for _ in range(timeout_sec // 2):
        await asyncio.sleep(2)
        try:
            result = await rpc_request("getSignatureStatuses", [[tx_sig]])
            statuses = result.get("value", [])
            if statuses and statuses[0]:
                status = statuses[0]
                if status.get("err"):
                    print(f"TX failed on-chain: {status['err']}")
                    return False
                conf = status.get("confirmationStatus", "")
                if conf in ("confirmed", "finalized"):
                    return True
        except Exception as e:
            print(f"Confirm poll error: {e}")
    return None  # Still pending / blockhash may have expired


async def _pumpportal_trade(
    action: str,       # "buy" or "sell"
    mint: str,
    amount: float,
    denominated_in_sol: bool,
) -> SwapResult:
    """Execute a trade via PumpPortal with retry logic.

    Each attempt requests a fresh TX (fresh blockhash) from PumpPortal,
    signs, sends, and does a quick confirm check. Retries up to MAX_RETRIES
    times if the TX doesn't land.
    """
    keypair = load_or_create_keypair()
    last_tx_sig = ""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            print(f"PumpPortal {action} attempt {attempt}/{MAX_RETRIES}")

            tx_sig, err = await _pumpportal_build_and_send(
                keypair, action, mint, amount, denominated_in_sol
            )
            if err:
                print(f"  Build/send error: {err}")
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(1)
                    continue
                return SwapResult(False, "", err)

            last_tx_sig = tx_sig
            print(f"  TX sent: {tx_sig}")

            # Quick confirm — 15s per attempt
            result = await _confirm_fast(tx_sig)

            if result is True:
                return SwapResult(True, tx_sig, "", amount, 0, route="pumpportal")
            elif result is False:
                # On-chain error — don't retry, it'll fail again
                return SwapResult(False, tx_sig, "Transaction failed on-chain", route="pumpportal")
            else:
                # None = still pending / expired, retry with fresh TX
                print(f"  Not confirmed in {CONFIRM_FAST_SEC}s, retrying with fresh TX...")

        except Exception as e:
            print(f"  Attempt {attempt} error: {e}")
            if attempt == MAX_RETRIES:
                return SwapResult(False, last_tx_sig, str(e))
            await asyncio.sleep(1)

    return SwapResult(False, last_tx_sig, f"Not confirmed after {MAX_RETRIES} attempts", route="pumpportal")


# ─── Jupiter (DEX) ───────────────────────────────────────────────────────────

async def _jupiter_get_quote(
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int | None = None,
) -> dict:
    """Get a Jupiter quote."""
    if not JUPITER_API_KEY:
        raise Exception("Jupiter API key not set — add JUPITER_API_KEY to .env (get free key at portal.jup.ag)")
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "slippageBps": slippage_bps or settings.slippage_bps,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(JUPITER_QUOTE_URL, params=params, headers=_jup_headers())
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise Exception(data["error"])
        return data


async def _jupiter_build_swap_tx(quote: dict, keypair: Keypair) -> bytes:
    """Request Jupiter to build the swap transaction."""
    body = {
        "quoteResponse": quote,
        "userPublicKey": str(keypair.pubkey()),
        "wrapAndUnwrapSol": True,
        "prioritizationFeeLamports": settings.priority_fee_lamports,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(JUPITER_SWAP_URL, json=body, headers=_jup_headers())
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise Exception(f"Jupiter swap error: {data['error']}")
        return base64.b64decode(data["swapTransaction"])


async def _jupiter_buy(token_mint: str, sol_amount: float) -> SwapResult:
    """Buy a token with SOL via Jupiter."""
    keypair = load_or_create_keypair()
    amount_lamports = int(sol_amount * LAMPORTS_PER_SOL)

    quote = await _jupiter_get_quote(SOL_MINT, token_mint, amount_lamports)
    out_amount_raw = int(quote.get("outAmount", 0))
    tx_bytes = await _jupiter_build_swap_tx(quote, keypair)
    signed = sign_transaction(tx_bytes, keypair)
    tx_sig = await send_transaction(signed)
    print(f"Jupiter buy TX sent: {tx_sig}")

    confirmed = await confirm_transaction(tx_sig)
    if not confirmed:
        return SwapResult(False, tx_sig, "Transaction not confirmed within timeout", route="jupiter")

    out_decimals = int(quote.get("outputDecimals", 6))
    out_amount = out_amount_raw / (10 ** out_decimals)
    return SwapResult(True, tx_sig, "", sol_amount, out_amount, route="jupiter")


async def _jupiter_sell(token_mint: str, token_amount: float, token_decimals: int = 6) -> SwapResult:
    """Sell a token for SOL via Jupiter."""
    keypair = load_or_create_keypair()
    amount_raw = int(token_amount * (10 ** token_decimals))

    quote = await _jupiter_get_quote(token_mint, SOL_MINT, amount_raw)
    out_lamports = int(quote.get("outAmount", 0))
    tx_bytes = await _jupiter_build_swap_tx(quote, keypair)
    signed = sign_transaction(tx_bytes, keypair)
    tx_sig = await send_transaction(signed)
    print(f"Jupiter sell TX sent: {tx_sig}")

    confirmed = await confirm_transaction(tx_sig)
    if not confirmed:
        return SwapResult(False, tx_sig, "Transaction not confirmed within timeout", route="jupiter")

    sol_received = out_lamports / LAMPORTS_PER_SOL
    return SwapResult(True, tx_sig, "", token_amount, sol_received, route="jupiter")


# ─── Public API: auto-routes Jupiter → PumpPortal ────────────────────────────

async def buy_token(token_mint: str, sol_amount: float, is_pump_fun: bool = False) -> SwapResult:
    """Buy a token. Tries Jupiter first; falls back to PumpPortal for bonding curve tokens."""
    if is_pump_fun:
        # Skip Jupiter — go straight to PumpPortal
        print(f"Pump.fun token detected — using PumpPortal for buy")
        return await _pumpportal_trade("buy", token_mint, sol_amount, denominated_in_sol=True)

    # Try Jupiter first
    try:
        return await _jupiter_buy(token_mint, sol_amount)
    except Exception as jup_err:
        jup_error = str(jup_err)
        print(f"Jupiter buy failed: {jup_error}")

        # If Jupiter has no route, fall back to PumpPortal
        if any(kw in jup_error.lower() for kw in ("no route", "no quote", "could not find")):
            print("Falling back to PumpPortal...")
            return await _pumpportal_trade("buy", token_mint, sol_amount, denominated_in_sol=True)

        return SwapResult(False, "", f"Jupiter: {jup_error}")


async def sell_token(token_mint: str, token_amount: float, token_decimals: int = 6, is_pump_fun: bool = False) -> SwapResult:
    """Sell a token. Tries Jupiter first; falls back to PumpPortal for bonding curve tokens."""
    if is_pump_fun:
        print(f"Pump.fun token detected — using PumpPortal for sell")
        return await _pumpportal_trade("sell", token_mint, token_amount, denominated_in_sol=False)

    # Try Jupiter first
    try:
        return await _jupiter_sell(token_mint, token_amount, token_decimals)
    except Exception as jup_err:
        jup_error = str(jup_err)
        print(f"Jupiter sell failed: {jup_error}")

        if any(kw in jup_error.lower() for kw in ("no route", "no quote", "could not find")):
            print("Falling back to PumpPortal...")
            return await _pumpportal_trade("sell", token_mint, token_amount, denominated_in_sol=False)

        return SwapResult(False, "", f"Jupiter: {jup_error}")


async def get_token_price_sol(token_mint: str) -> float | None:
    """Get token price in SOL via DexScreener (no API key needed)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{DEXSCREENER_API_URL}/{token_mint}")
            resp.raise_for_status()
            pairs = resp.json()
            if not pairs:
                return None
            # Find SOL-paired pool, or use priceNative from any pool
            for pair in pairs:
                price_native = pair.get("priceNative")
                if price_native:
                    return float(price_native)
    except Exception as e:
        print(f"Price fetch error: {e}")
    return None
