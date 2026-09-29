import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol

from app.session import Session
from app.store import Store, StoreError

if TYPE_CHECKING:
    from app.models import Order


class Services(Protocol):
    """Hooks the orchestrator exposes to agent tools."""

    def start_tracking(self, session: Session, order: "Order") -> None: ...
    def stop_tracking(self, order_id: str) -> None: ...


@dataclass
class AgentContext:
    store: Store
    session: Session
    services: Services


@dataclass
class AgentSpec:
    name: str  # routing key, e.g. "intake"
    label: str  # shown in the chat UI, e.g. "Order Intake"
    system: str
    effort: str  # output_config.effort
    make_tools: Callable[[AgentContext], list]
    # Optional data appended to the system prompt each turn (menu, cart, ...), so the agent
    # doesn't spend a tool round-trip fetching it. Tools stay available for fresh reads.
    context: Callable[[AgentContext], str] | None = None


SHARED_RULES = """
You are one of five cooperating agents in a chat for "Scoops & Co", an ice cream shop.
The agents are: Order Intake (builds the cart), Flavor Recommender (suggestions),
Checkout & Pricing (prices and places orders), Order Tracker (status, ETA, cancellations)
and Follow-up (feedback after delivery). A router picks which agent answers each message,
so you only see the conversation so far. Do your own job; if the customer asks for
something another agent owns, answer briefly and tell them what to say next
(e.g. "say 'checkout' when you're ready").
Keep replies short, warm, and chat-friendly (1-4 sentences, simple lists are fine).
Never invent menu items, prices, order ids or statuses; use your tools.
"""


def ok(data: Any) -> str:
    """Serialize a tool result for Claude."""
    return json.dumps(data, default=str)


def guarded(fn: Callable[[], Any]) -> str:
    """Run a tool body, turning business-rule violations into a readable error result."""
    try:
        return ok(fn())
    except StoreError as e:
        return ok({"error": str(e)})
