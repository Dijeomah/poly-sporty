"""Solana wallet management — keypair generation, balance queries."""

import os
import base58
import httpx
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from config import HELIUS_RPC_URL, LAMPORTS_PER_SOL

_keypair: Keypair | None = None


def _env_path() -> str:
    return os.path.join(os.path.dirname(__file__), ".env")


def load_or_create_keypair() -> Keypair:
    """Load keypair from PRIVATE_KEY in .env, or generate a new one."""
    global _keypair
    if _keypair is not None:
        return _keypair

    pk_str = os.getenv("PRIVATE_KEY", "")
    if pk_str:
        secret = base58.b58decode(pk_str)
        _keypair = Keypair.from_bytes(secret)
        print(f"Wallet loaded: {_keypair.pubkey()}")
    else:
        _keypair = Keypair()
        encoded = base58.b58encode(bytes(_keypair)).decode()
        # Append to .env
        env_file = _env_path()
        with open(env_file, "a") as f:
            f.write(f"\nPRIVATE_KEY={encoded}\n")
        print(f"NEW wallet generated: {_keypair.pubkey()}")
        print(f"Private key saved to .env — fund this wallet with SOL!")

    return _keypair


def get_pubkey() -> Pubkey:
    kp = load_or_create_keypair()
    return kp.pubkey()


async def rpc_request(method: str, params: list) -> dict:
    """Make a JSON-RPC request to Helius."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            HELIUS_RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise Exception(f"RPC error: {data['error']}")
        return data["result"]


async def get_sol_balance() -> float:
    """Get SOL balance in SOL (not lamports)."""
    pubkey = str(get_pubkey())
    result = await rpc_request("getBalance", [pubkey, {"commitment": "confirmed"}])
    return result["value"] / LAMPORTS_PER_SOL


async def get_token_balance(mint: str) -> float:
    """Get SPL token balance for a specific mint. Returns 0 if no account."""
    pubkey = str(get_pubkey())
    result = await rpc_request(
        "getTokenAccountsByOwner",
        [
            pubkey,
            {"mint": mint},
            {"encoding": "jsonParsed", "commitment": "confirmed"},
        ],
    )
    accounts = result.get("value", [])
    if not accounts:
        return 0.0
    info = accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]
    return float(info["uiAmount"] or 0)


async def get_token_accounts() -> list[dict]:
    """Get all token accounts with non-zero balances."""
    pubkey = str(get_pubkey())
    result = await rpc_request(
        "getTokenAccountsByOwner",
        [
            pubkey,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed", "commitment": "confirmed"},
        ],
    )
    tokens = []
    for acc in result.get("value", []):
        info = acc["account"]["data"]["parsed"]["info"]
        amount = float(info["tokenAmount"]["uiAmount"] or 0)
        if amount > 0:
            tokens.append({
                "mint": info["mint"],
                "amount": amount,
                "decimals": info["tokenAmount"]["decimals"],
            })
    return tokens
