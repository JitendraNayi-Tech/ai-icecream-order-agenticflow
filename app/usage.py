"""Token and cost ledger: records every Claude run (and every step handled by code) and
attributes it to an order, for the cost panel in the chat UI.

Cost is the API-equivalent price reported per call. On the claude_code backend it comes
from the CLI's own `modelUsage[*].costUSD`; a subscription isn't billed per token, so treat
it as "what this would cost on the API" (and a proxy for subscription usage consumed).
"""

import itertools
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime

# USD per million tokens, used only by the API backend (the CLI reports its own cost).
API_PRICES = {  # model: (input, output, cache_read, cache_write_5m)
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
}
CODE = "code"  # model name for steps answered by deterministic code (no Claude call)


@dataclass
class UsageRecord:
    customer_id: str
    agent: str  # "router" or an agent name
    run_id: int  # one Claude run (CLI process / API agent loop) can span several models
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    order_id: str | None = None  # set once the call can be tied to an order
    at: datetime = field(default_factory=datetime.now)


LEDGER: list[UsageRecord] = []
_run_ids = itertools.count(1)

# Who is making the current Claude call: (customer_id, agent, order_id in scope or None).
# Set by the orchestrator when a step starts, so the backends don't need to know about
# sessions and a step that ends an order (e.g. recording feedback) still counts toward it.
current_call: ContextVar[tuple[str, str, str | None] | None] = ContextVar("current_call",
                                                                          default=None)


def record_claude_code(data: dict) -> None:
    """Record one `claude -p --output-format json` result (one record per model it used)."""
    who = current_call.get()
    if not who:
        return
    run_id = next(_run_ids)
    for model, u in (data.get("modelUsage") or {}).items():
        LEDGER.append(UsageRecord(
            customer_id=who[0], agent=who[1], run_id=run_id, model=model, order_id=who[2],
            input_tokens=u.get("inputTokens", 0), output_tokens=u.get("outputTokens", 0),
            cache_read_tokens=u.get("cacheReadInputTokens", 0),
            cache_write_tokens=u.get("cacheCreationInputTokens", 0),
            cost_usd=u.get("costUSD", 0.0)))


def record_api(model: str, usages: list) -> None:
    """Record one API agent run: the `usage` blocks of every response in its tool loop."""
    who = current_call.get()
    if not who:
        return
    run_id = next(_run_ids)
    p_in, p_out, p_cr, p_cw = API_PRICES.get(model, API_PRICES["claude-opus-5-5"])
    for usage in usages:
        if usage is None:
            continue
        inp, out = usage.input_tokens or 0, usage.output_tokens or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        LEDGER.append(UsageRecord(
            customer_id=who[0], agent=who[1], run_id=run_id, model=model, order_id=who[2],
            input_tokens=inp,
            output_tokens=out, cache_read_tokens=cr, cache_write_tokens=cw,
            cost_usd=(inp * p_in + out * p_out + cr * p_cr + cw * p_cw) / 1_000_000))


def record_code(customer_id: str, agent: str, order_id: str | None = None) -> None:
    """Record a step answered by deterministic code (shown as "code", $0)."""
    LEDGER.append(UsageRecord(customer_id=customer_id, agent=agent, run_id=next(_run_ids),
                              model=CODE, order_id=order_id))


def tag_untagged(customer_id: str, order_id: str) -> None:
    """Attribute this customer's not-yet-attributed records to an order."""
    for r in LEDGER:
        if r.customer_id == customer_id and r.order_id is None:
            r.order_id = order_id


def cost_breakdown(records: list[UsageRecord]) -> dict:
    """Totals plus one row per agent (in order of first appearance)."""
    claude = [r for r in records if r.model != CODE]
    agents: dict[str, dict] = {}
    for r in records:
        row = agents.setdefault(r.agent, {"agent": r.agent, "runs": set(), "cost_usd": 0.0,
                                          "code_steps": 0})
        if r.model == CODE:
            row["code_steps"] += 1
        else:
            row["runs"].add(r.run_id)
            row["cost_usd"] += r.cost_usd
    by_agent = [{**row, "runs": len(row["runs"]), "cost_usd": round(row["cost_usd"], 6)}
                for row in agents.values()]
    return {
        "ai_cost_usd": round(sum(r.cost_usd for r in claude), 6),
        "claude_runs": len({r.run_id for r in claude}),
        "input_tokens": sum(r.input_tokens for r in claude),
        "output_tokens": sum(r.output_tokens for r in claude),
        "cache_read_tokens": sum(r.cache_read_tokens for r in claude),
        "cache_write_tokens": sum(r.cache_write_tokens for r in claude),
        "by_agent": by_agent,
    }


def records_for(customer_id: str) -> list[UsageRecord]:
    return [r for r in LEDGER if r.customer_id == customer_id]
