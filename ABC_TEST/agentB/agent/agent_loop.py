"""
Anthropic tool-use loop with safe handling of max_tokens and empty tool batches.

When the model hits max_tokens mid-turn it may return no tool_use blocks; appending
an empty user message causes a 400 from the API and skips the dashboard upload.
This module centralises those edge cases so every variant exits cleanly.
"""
import json
import logging
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = int(__import__("os").environ.get("AGENT_MAX_TOKENS", "8192"))
DEFAULT_MAX_ITERATIONS = int(__import__("os").environ.get("AGENT_MAX_ITERATIONS", "15"))


def run_tool_loop(
    client,
    *,
    model: str,
    system,
    tools,
    messages: list,
    execute_tool_call: Callable,
    max_tokens: int | None = None,
    max_iterations: int | None = None,
) -> tuple[int, str]:
    """
    Run the agent until end_turn, max_tokens, or the iteration safety limit.

    Returns (iteration_count, concatenated summary text from assistant blocks).
    """
    max_tokens = max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS
    max_iterations = max_iterations if max_iterations is not None else DEFAULT_MAX_ITERATIONS

    iterations = 0
    summary_parts: list[str] = []

    while True:
        if iterations >= max_iterations:
            logger.warning(
                "Agent loop stopped at iteration safety limit (%d)", max_iterations,
            )
            break

        response = client.messages.create(
            model=model,
            system=system,
            tools=tools,
            messages=messages,
            max_tokens=max_tokens,
        )
        iterations += 1
        stop = response.stop_reason
        logger.info("Agent iteration %d | stop_reason=%s", iterations, stop)

        for block in response.content:
            if getattr(block, "type", None) == "text" and getattr(block, "text", ""):
                summary_parts.append(block.text)
                logger.info("Agent summary:\n%s", block.text)

        if stop == "end_turn":
            break

        if stop == "max_tokens":
            logger.warning(
                "Model hit max_tokens (%d) on iteration %d — ending run",
                max_tokens, iterations,
            )
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = execute_tool_call(block.name, block.input)
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     json.dumps(result, default=str),
            })

        if stop == "tool_use" and not tool_results:
            logger.warning(
                "stop_reason=tool_use but no tool_use blocks — ending run",
            )
            break

        if stop != "tool_use":
            logger.warning("Unexpected stop_reason=%s — ending run", stop)
            break

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return iterations, "\n".join(summary_parts).strip()
