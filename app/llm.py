"""The only module that talks to Claude: via the Anthropic SDK (LLM_BACKEND=api) or the
local Claude Code CLI (LLM_BACKEND=claude_code, see app/claude_code.py)."""

import logging

import anthropic

from app import claude_code, config
from app.models import Route

log = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None

# Server-side refusal fallback: if a safety classifier declines, the API retries on a
# suitable fallback model inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

REFUSAL_TEXT = "Sorry, I can't help with that one. Is there anything ice-cream related I can do?"


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


def _final_text(message) -> str:
    if message.stop_reason == "refusal":
        return REFUSAL_TEXT
    text = "\n".join(b.text for b in message.content if b.type == "text").strip()
    if message.stop_reason == "max_tokens":
        log.warning("agent reply truncated at max_tokens")
    return text or "(no reply)"


async def run_agent(system: str, tools: list, messages: list[dict], effort: str, *,
                    agent: str, customer_id: str) -> str:
    """Run one agent turn: Claude may call tools repeatedly; returns the final reply text."""
    if config.LLM_BACKEND == "claude_code":
        # Tools are reached through the app's /mcp endpoint instead of being passed in.
        return await claude_code.run_agent(system, messages, effort, agent=agent,
                                           customer_id=customer_id)
    runner = client().beta.messages.tool_runner(
        model=config.MODEL,
        max_tokens=config.MAX_TOKENS,
        system=system,
        tools=tools,
        messages=list(messages),  # the runner appends tool turns; keep shared history text-only
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    final = await runner.until_done()
    return _final_text(final)


ROUTER_SYSTEM = """You route messages in an ice cream shop chat to one of five agents:
- intake: ordering, adding/changing/removing items, "I want...", answering a clarifying question about an item.
- recommender: asking what to get, suggestions, "what's good", "surprise me" before choosing.
- checkout: checkout, pay, price/total, promo codes, confirming ("yes") a price summary.
- tracker: order status, ETA, "where is my order", cancelling a placed order, past orders.
- followup: feedback/ratings/complaints about a delivered order.
Use the session state and the recent conversation to resolve short replies like "yes" or "the first one".
"""


async def route(transcript: str, state: str) -> Route:
    """Pick the agent for the latest customer message."""
    if config.LLM_BACKEND == "claude_code":
        agent = await claude_code.route(ROUTER_SYSTEM, transcript, state)
        return Route(agent=agent, reason="claude code router")
    response = await client().messages.parse(
        model=config.MODEL,
        max_tokens=1024,
        system=ROUTER_SYSTEM,
        messages=[{"role": "user", "content": f"Session state:\n{state}\n\nConversation (latest last):\n{transcript}"}],
        output_format=Route,
        output_config={"effort": "low"},
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        return Route(agent="intake", reason="router fallback")
    return response.parsed_output
