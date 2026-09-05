"""SportyBet pick analysis via the Claude Code CLI.

Shells out to the `claude` CLI in non-interactive print mode instead of
calling the Anthropic API directly — this runs through the user's existing
Claude Code session/subscription rather than requiring separate
ANTHROPIC_API_KEY credits, which the user does not have.
"""

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Optional

logger = logging.getLogger("sporty.claude_analyst")

AGENT_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "sportybet-agent.md")
CLAUDE_TIMEOUT_SEC = 240


def is_available() -> bool:
    """True if the `claude` CLI is on PATH — callers skip analysis otherwise."""
    return shutil.which("claude") is not None


def _load_system_prompt() -> str:
    with open(AGENT_PROMPT_FILE, "r") as f:
        return f.read()


def _format_match_for_prompt(index: int, candidate: dict, safe_market_names: set) -> str:
    """Render one candidate's odds across the safe/bookable markets as text."""
    event = candidate["event"]
    lines = [
        f"### Match {index}: {candidate['home_team']} vs {candidate['away_team']}",
        f"League: {candidate.get('league', '')}",
        f"Kickoff (UTC): {candidate.get('commence_time', '')}",
        "Markets:",
    ]
    for m in event.get("markets", []):
        name = (m.get("name") or m.get("desc") or "")
        if name.strip().lower() not in safe_market_names:
            continue
        outcomes = m.get("outcomes", [])
        if not outcomes:
            continue
        outcome_str = ", ".join(f"{o.get('desc', '')}={o.get('odds', '')}" for o in outcomes)
        lines.append(f"  - {name}: {outcome_str}")
    return "\n".join(lines)


def _build_user_prompt(candidates: list[dict], safe_market_names: set) -> str:
    sections = [
        _format_match_for_prompt(i, c, safe_market_names)
        for i, c in enumerate(candidates)
    ]
    return (
        "Analyze these live SportyBet matches and return your ranking per "
        "the required JSON output format.\n\n" + "\n\n".join(sections)
    )


def _extract_json_block(text: str) -> Optional[dict]:
    """Pull the mandatory ```json ... ``` block out of Claude's response."""
    match = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


async def analyze_candidates(candidates: list[dict], safe_market_names: set) -> Optional[dict]:
    """Ask the SportyBet Analyst agent (via the Claude Code CLI) to rank a
    batch of curated candidates by confidence.

    `candidates` should already be truncated to the batch being analyzed —
    the returned ranking's `match_index` values refer to positions in this
    exact list.

    Returns {"ranked_picks": [...], "flags": [...]}, or None if the CLI
    isn't available, times out, or its output can't be parsed — callers
    must fall back to pure odds-based ranking in that case, the same way
    the rest of this bot gracefully skips Claude analysis when unavailable.
    """
    if not is_available():
        logger.info("claude CLI not found on PATH — skipping analysis")
        return None
    if not candidates:
        return None

    system_prompt = _load_system_prompt()
    user_prompt = _build_user_prompt(candidates, safe_market_names)

    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", user_prompt,
            "--system-prompt", system_prompt,
            "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        logger.warning(f"Claude analysis timed out after {CLAUDE_TIMEOUT_SEC}s")
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None
    except Exception as e:
        logger.error(f"Error invoking claude CLI: {e}")
        return None

    if proc.returncode != 0:
        logger.warning(
            f"claude CLI exited {proc.returncode}: {stderr.decode(errors='replace')[:300]}"
        )
        return None

    try:
        outer = json.loads(stdout.decode())
    except json.JSONDecodeError:
        logger.warning("claude CLI did not return valid JSON")
        return None

    if outer.get("is_error"):
        logger.warning(f"Claude analysis reported an error: {outer.get('result')}")
        return None

    result_text = outer.get("result", "")
    parsed = _extract_json_block(result_text)
    if parsed is None:
        logger.warning("Could not find/parse the mandatory JSON block in Claude's analysis")
        return None

    logger.info(
        f"Claude analyzed {len(candidates)} candidates, "
        f"ranked {len(parsed.get('ranked_picks', []))}"
    )
    return parsed
