"""Per-customer conversation state shared by every agent."""

import asyncio
from dataclasses import dataclass, field

from app.models import CartItem
from app.store import Store, StoreError


@dataclass
class Session:
    customer_id: str
    # Shared text-only chat history (Claude message format). Every agent sees all of it.
    history: list[dict] = field(default_factory=list)
    cart: list[CartItem] = field(default_factory=list)
    next_line_id: int = 1
    promo_code: str | None = None
    turn: int = 0  # incremented on every customer message
    quoted_turn: int | None = None  # turn on which checkout showed a price summary
    active_order_id: str | None = None
    feedback_order_id: str | None = None  # delivered order still awaiting feedback
    greeted: bool = False
    last_agent: str | None = None  # agent that sent the latest reply (for sticky routing)
    # Customer messages that arrived while an agent was busy; answered together in one turn.
    pending: list[str] = field(default_factory=list)
    draining: bool = False  # True while Orchestrator.handle is working through `pending`
    # What the chat UI shows, replayed when a page (re)connects: message frames with their
    # agent labels (history only keeps roles), and the latest order status frame.
    transcript: list[dict] = field(default_factory=list)
    last_status: dict | None = None
    # Frames destined for the websocket: {"type": "message"|"status"|"typing"|"cart", ...}
    outbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ---------- chat history ----------

    def add_message(self, role: str, text: str) -> None:
        """Append text, merging consecutive same-role turns so roles always alternate."""
        if self.history and self.history[-1]["role"] == role:
            self.history[-1]["content"] += "\n\n" + text
        else:
            self.history.append({"role": role, "content": text})

    async def emit(self, frame: dict) -> None:
        await self.outbox.put(frame)

    async def say(self, agent: str, text: str) -> None:
        """Send an agent message to the customer and record it in the shared history."""
        self.add_message("assistant", text)
        frame = {"type": "message", "agent": agent, "text": text}
        self.transcript.append(frame)
        await self.emit(frame)

    # ---------- cart ----------

    def add_to_cart(self, store: Store, **kwargs) -> CartItem:
        item = store.build_item(self.next_line_id, **kwargs)
        self.cart.append(item)
        self.next_line_id += 1
        self._invalidate_quote()
        return item

    def update_cart_item(self, store: Store, line_id: int, **changes) -> CartItem:
        idx = self._index(line_id)
        current = self.cart[idx].model_dump()
        current.update({k: v for k, v in changes.items() if v is not None})
        current.pop("line_id")
        self.cart[idx] = store.build_item(line_id, **current)
        self._invalidate_quote()
        return self.cart[idx]

    def remove_from_cart(self, line_id: int) -> None:
        self.cart.pop(self._index(line_id))
        self._invalidate_quote()

    def _index(self, line_id: int) -> int:
        for i, item in enumerate(self.cart):
            if item.line_id == line_id:
                return i
        raise StoreError(f"No cart line {line_id}.")

    def _invalidate_quote(self) -> None:
        # Any cart change means the customer must see a fresh price before we place the order.
        self.quoted_turn = None

    def cart_view(self, store: Store) -> list[dict]:
        return [{"line_id": i.line_id, "description": store.describe_item(i),
                 "quantity": i.quantity, "unit_price": store.unit_price(i)} for i in self.cart]
