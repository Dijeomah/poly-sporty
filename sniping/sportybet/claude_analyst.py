"""SportyBet pick analysis via the Claude Code CLI.

Shells out to the `claude` CLI in non-interactive print mode instead of
calling the Anthropic API directly — this runs through the user's existing
Claude Code session/subscription rather than requiring separate
ANTHROPIC_API_KEY credits, which the user does not have. On the server
(Docker) the CLI authenticates via CLAUDE_CODE_OAUTH_TOKEN from
`claude setup-token` — see DOCKER.md.

With research enabled, Claude gets the WebSearch/WebFetch tools (and
nothing else) to look up form, injuries, head-to-head and lineups for each
match before scoring it.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from typing import Optional

logger = logging.getLogger("sporty.claude_analyst")

AGENT_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "sportybet-agent.md")
# Web research across a 5-match batch takes several minutes; odds-only
# analysis is well under one.
RESEARCH_TIMEOUT_SEC = 900
ODDS_ONLY_TIMEOUT_SEC = 240
RESEARCH_TOOLS = "WebSearch,WebFetch"
# Optional override, e.g. CLAUDE_ANALYST_MODEL=opus — default is the CLI's own.
MODEL = os.getenv("CLAUDE_ANALYST_MODEL", "")


def is_available() -> bool:
    """True if the `claude` CLI is on PATH — callers skip analysis otherwise."""
    return shutil.which("claude") is not None


def _load_system_prompt() -> str:
    with open(AGENT_PROMPT_FILE, "r") as f:
        return f.read()


def _format_match_for_prompt(index: int, candidate: dict, safe_market_names: set) -> str:
    """Render one candidate as text: the pick the bot wants to back, plus
    every safe/bookable market's odds for cross-market checks.

    Accepts either a SportyBet-sourced candidate (`event` + `safe`) or a
    lighter one with a prebuilt `pick` dict and `odds_lines` list (the
    daily-pick path, sourced from The Odds API).
    """
    lines = [
        f"### Match {index}: {candidate['home_team']} vs {candidate['away_team']}",
        f"Sport: {candidate.get('sport', '')}",
        f"League: {candidate.get('league', '')}",
        f"Kickoff (UTC): {candidate.get('commence_time', '')}",
    ]

    safe = candidate.get("safe")
    pick = candidate.get("pick")
    if safe:
        lines.append(f"Pick to evaluate: {safe['market_name']}: {safe['outcome_desc']} @ {safe['odds']:.2f}")
    elif pick:
        lines.append(f"Pick to evaluate: {pick['market']}: {pick['outcome']} @ {pick['odds']:.2f}")

    lines.append("Markets:")
    event = candidate.get("event")
    if event:
        for m in event.get("markets", []):
            name = (m.get("name") or m.get("desc") or "")
            if name.strip().lower() not in safe_market_names:
                continue
            outcomes = m.get("outcomes", [])
            if not outcomes:
                continue
            spec = f" [{m['specifier']}]" if m.get("specifier") else ""
            outcome_str = ", ".join(f"{o.get('desc', '')}={o.get('odds', '')}" for o in outcomes)
            lines.append(f"  - {name}{spec}: {outcome_str}")
    for line in candidate.get("odds_lines", []):
        lines.append(f"  - {line}")
    return "\n".join(lines)


def _build_user_prompt(candidates: list[dict], safe_market_names: set, research: bool) -> str:
    sections = [
        _format_match_for_prompt(i, c, safe_market_names)
        for i, c in enumerate(candidates)
    ]
    mode = (
        "Web research is ENABLED for this run: use WebSearch/WebFetch to check "
        "each match's recent form, injuries/suspensions, head-to-head, likely "
        "lineups and motivation before scoring it."
        if research else
        "Web research is DISABLED for this run: analyze from the odds only."
    )
    return (
        f"{mode}\n\nAnalyze these live SportyBet matches and return your "
        "verdicts per the required JSON output format.\n\n" + "\n\n".join(sections)
    )


def _extract_json_block(text: str) -> Optional[dict]:
    """Pull the mandatory ```json ... ``` block out of Claude's response —
    the last one, in case the summary quotes an example earlier."""
    matches = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except json.JSONDecodeError:
        return None


async def analyze_candidates(candidates: list[dict], safe_market_names: set,
                             research: bool = True) -> Optional[dict]:
    """Ask the SportyBet Analyst agent (via the Claude Code CLI) to score
    one batch of curated candidates.

    The returned `match_index` values refer to positions in this exact list.

    Returns {"ranked_picks": [...], "flags": [...]}, or None if the CLI
    isn't available, times out, or its output can't be parsed — callers
    must fall back to pure odds-based ranking in that case.
    """
    if not is_available():
        logger.info("claude CLI not found on PATH — skipping analysis")
        return None
    if not candidates:
        return None

    system_prompt = _load_system_prompt()
    user_prompt = _build_user_prompt(candidates, safe_market_names, research)
    timeout = RESEARCH_TIMEOUT_SEC if research else ODDS_ONLY_TIMEOUT_SEC

    # Only the web tools, never file/shell tools — and run from an empty
    # temp dir, so nothing a fetched web page says can get Claude near the
    # bot's .env / session cookies.
    args = [
        "claude", "-p", user_prompt,
        "--system-prompt", system_prompt,
        "--output-format", "json",
        "--tools", RESEARCH_TOOLS if research else "",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    if research:
        args += ["--allowedTools", RESEARCH_TOOLS]
    if MODEL:
        args += ["--model", MODEL]

    proc = None
    try:
        with tempfile.TemporaryDirectory(prefix="sporty-claude-") as workdir:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"Claude analysis timed out after {timeout}s")
        if proc:
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
            f"claude CLI exited {proc.returncode}: "
            f"{(stderr or stdout).decode(errors='replace')[:300]}"
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
        f"Claude analyzed {len(candidates)} candidates "
        f"(research={'on' if research else 'off'}), "
        f"returned {len(parsed.get('ranked_picks', []))} verdicts"
    )
    return parsed


async def analyze_all(candidates: list[dict], safe_market_names: set,
                      research: bool = True, batch_size: int = 5,
                      concurrency: int = 4) -> Optional[tuple[list[Optional[dict]], list[dict]]]:
    """Score every candidate, split into batches run `concurrency` at a time.

    Returns (verdicts, flags): `verdicts[i]` is Claude's verdict dict for
    `candidates[i]` ({confidence, verdict, note}), or None if that
    candidate's batch failed or Claude skipped it. `flags` carry
    `match_index` remapped to positions in `candidates`. Returns None only
    if every batch failed.
    """
    batches = [
        (start, candidates[start:start + batch_size])
        for start in range(0, len(candidates), batch_size)
    ]
    sem = asyncio.Semaphore(concurrency)

    async def run(batch):
        async with sem:
            return await analyze_candidates(batch, safe_market_names, research=research)

    results = await asyncio.gather(*(run(b) for _, b in batches), return_exceptions=True)

    verdicts: list[Optional[dict]] = [None] * len(candidates)
    flags: list[dict] = []
    any_ok = False
    for (start, batch), result in zip(batches, results):
        if isinstance(result, Exception):
            logger.error(f"Claude batch at {start} raised: {result}")
            continue
        if not result:
            continue
        any_ok = True
        for p in result.get("ranked_picks", []):
            i = p.get("match_index")
            if not isinstance(i, int) or not 0 <= i < len(batch):
                continue
            try:
                confidence = float(p.get("confidence", 0))
            except (TypeError, ValueError):
                continue
            verdicts[start + i] = {
                "confidence": confidence,
                "verdict": str(p.get("verdict", "back")).lower(),
                "note": p.get("note", ""),
            }
        for f in result.get("flags", []):
            i = f.get("match_index")
            if isinstance(i, int) and 0 <= i < len(batch):
                flags.append({**f, "match_index": start + i})

    if not any_ok:
        return None
    return verdicts, flags
