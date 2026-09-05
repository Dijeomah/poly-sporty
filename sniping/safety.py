"""Token safety checks — RugCheck, DexScreener, mint authority, holder concentration."""

import asyncio
import httpx
from dataclasses import dataclass
from config import RUGCHECK_API_URL, DEXSCREENER_API_URL, HELIUS_RPC_URL, settings
from wallet import rpc_request


@dataclass
class SafetyReport:
    passed: bool
    score: int               # 0–100, lower = riskier
    risks: list[str]
    liquidity_usd: float
    mint_authority_revoked: bool
    top_holder_pct: float
    is_pump_fun: bool        # True if still on bonding curve
    details: str             # Human-readable summary


async def check_rugcheck(mint: str) -> tuple[int, list[str]]:
    """RugCheck API — returns (score, [risk_descriptions])."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{RUGCHECK_API_URL}/{mint}/report/summary")
            if resp.status_code == 404:
                return 0, ["Token not found on RugCheck"]
            resp.raise_for_status()
            data = resp.json()
            score = data.get("score", 0)
            risks = []
            for risk in data.get("risks", []):
                name = risk.get("name", "unknown")
                level = risk.get("level", "")
                description = risk.get("description", "")
                if level in ("danger", "warn"):
                    risks.append(f"{name}: {description}")
            return score, risks
    except Exception as e:
        return 0, [f"RugCheck error: {e}"]


async def check_dexscreener(mint: str) -> float:
    """DexScreener — returns liquidity in USD (best pool)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{DEXSCREENER_API_URL}/{mint}")
            resp.raise_for_status()
            pairs = resp.json()
            if not pairs:
                return 0.0
            # pairs is a list; find the one with most liquidity
            best_liq = 0.0
            for pair in pairs:
                liq = pair.get("liquidity", {})
                usd = liq.get("usd", 0) if isinstance(liq, dict) else 0
                if usd and usd > best_liq:
                    best_liq = usd
            return best_liq
    except Exception as e:
        print(f"DexScreener error: {e}")
        return 0.0


async def check_mint_authority(mint: str) -> bool:
    """Check if mint authority is revoked (None). Returns True if revoked (safe)."""
    try:
        result = await rpc_request(
            "getAccountInfo",
            [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        if not result or not result.get("value"):
            return False
        parsed = result["value"]["data"]["parsed"]["info"]
        mint_auth = parsed.get("mintAuthority")
        return mint_auth is None
    except Exception as e:
        print(f"Mint authority check error: {e}")
        return False


async def check_top_holders(mint: str) -> tuple[float, bool]:
    """Return (top_holder_pct, is_pump_fun).

    Pump.fun tokens on the bonding curve are not standard SPL mints,
    so getTokenLargestAccounts will fail with "not a Token mint".
    We detect this and flag it as a pump.fun bonding-curve token.
    """
    try:
        result = await rpc_request(
            "getTokenLargestAccounts",
            [mint, {"commitment": "confirmed"}],
        )
        accounts = result.get("value", [])
        if not accounts:
            return 100.0, False

        # Get total supply
        supply_result = await rpc_request(
            "getTokenSupply",
            [mint, {"commitment": "confirmed"}],
        )
        total_supply = float(supply_result["value"]["uiAmount"] or 1)
        if total_supply == 0:
            return 100.0, False

        top_amount = float(accounts[0].get("uiAmount", 0) or 0)
        return (top_amount / total_supply) * 100, False
    except Exception as e:
        err_str = str(e)
        if "not a Token mint" in err_str:
            # Brand-new pump.fun token still on bonding curve
            return 0.0, True
        print(f"Holder check error: {e}")
        return 100.0, False


async def run_safety_checks(mint: str) -> SafetyReport:
    """Run all four safety checks concurrently. Returns a SafetyReport."""
    rugcheck_task = check_rugcheck(mint)
    dex_task = check_dexscreener(mint)
    mint_task = check_mint_authority(mint)
    holder_task = check_top_holders(mint)

    (score, risks), liquidity, mint_revoked, (top_holder, is_pump_fun) = await asyncio.gather(
        rugcheck_task, dex_task, mint_task, holder_task
    )

    # Build decision
    fail_reasons = []

    if is_pump_fun:
        # Token is still on pump.fun bonding curve — skip liquidity & holder checks
        # since it hasn't migrated to a DEX yet. RugCheck alone decides.
        pass
    else:
        if liquidity < settings.min_liquidity_usd:
            fail_reasons.append(
                f"Liquidity ${liquidity:,.0f} < min ${settings.min_liquidity_usd:,.0f}"
            )
        if top_holder > 20:
            fail_reasons.append(f"Top holder owns {top_holder:.1f}% (>20%)")

    # RugCheck danger-level risks
    danger_risks = [r for r in risks if "danger" in r.lower() or "critical" in r.lower()]
    if danger_risks:
        fail_reasons.append(f"RugCheck dangers: {'; '.join(danger_risks[:3])}")

    passed = len(fail_reasons) == 0

    # Build summary
    lines = []
    if is_pump_fun:
        lines.append("pump.fun bonding curve (pre-migration)")
    lines += [
        f"RugCheck score: {score}/100",
        f"Liquidity: ${liquidity:,.0f}" + (" (N/A — bonding curve)" if is_pump_fun else ""),
        f"Mint authority revoked: {'Yes' if mint_revoked else 'NO'}",
        f"Top holder: {top_holder:.1f}%" + (" (N/A — bonding curve)" if is_pump_fun else ""),
    ]
    if risks:
        lines.append(f"Risks: {'; '.join(risks[:5])}")
    if fail_reasons:
        lines.append(f"FAILED: {'; '.join(fail_reasons)}")
    else:
        lines.append("All checks PASSED")

    return SafetyReport(
        passed=passed,
        score=score,
        risks=risks,
        liquidity_usd=liquidity,
        mint_authority_revoked=mint_revoked,
        top_holder_pct=top_holder,
        is_pump_fun=is_pump_fun,
        details="\n".join(lines),
    )
