"""Agent 4 - Order Tracker.

Two parts:
- OrderTracker: a background task per order that advances the status on timers and calls
  back into the orchestrator, which pushes live updates to the chat.
- AGENT: the conversational side ("where's my order?", "cancel it").
"""

import asyncio
from datetime import datetime
from typing import Awaitable, Callable

from anthropic import beta_async_tool

from app import config
from app.agents.base import SHARED_RULES, AgentContext, AgentSpec, guarded
from app.models import STATUS_FLOW, Order, OrderStatus
from app.store import Store, StoreError

StatusCallback = Callable[[Order], Awaitable[None]]


def eta_minutes(order: Order, delays: dict[str, float] = config.STATUS_DELAYS) -> int:
    """Simulated minutes until delivery (1 simulated minute = 1 real second)."""
    if order.status not in STATUS_FLOW[:-1]:
        return 0
    idx = STATUS_FLOW.index(order.status)
    entered = order.status_history[-1][1] if order.status_history else datetime.now()
    elapsed = (datetime.now() - entered).total_seconds()
    remaining = max(delays[order.status.value] - elapsed, 0)
    remaining += sum(delays[s.value] for s in STATUS_FLOW[idx + 1:-1])
    return max(round(remaining), 1)


class OrderTracker:
    def __init__(self, store: Store, on_status: StatusCallback,
                 delays: dict[str, float] = config.STATUS_DELAYS):
        self.store = store
        self.on_status = on_status
        self.delays = delays
        self.tasks: dict[str, asyncio.Task] = {}

    def start(self, order: Order) -> None:
        self.tasks[order.id] = asyncio.create_task(self._run(order.id))

    def stop(self, order_id: str) -> None:
        task = self.tasks.pop(order_id, None)
        if task:
            task.cancel()

    async def _run(self, order_id: str) -> None:
        order = self.store.get_order(order_id)
        await self.on_status(order)  # announce PLACED
        for current, nxt in zip(STATUS_FLOW, STATUS_FLOW[1:]):
            await asyncio.sleep(self.delays[current.value])
            if order.status == OrderStatus.CANCELLED:
                return
            self.store.set_status(order_id, nxt)
            await self.on_status(order)
        self.tasks.pop(order_id, None)


STATUS_TEXT = {
    OrderStatus.PLACED: "Order {id} received! We'll start on it shortly.",
    OrderStatus.PREPARING: "Our scoopers are preparing order {id} now.",
    OrderStatus.READY: "Order {id} is packed and ready for pickup by the rider.",
    OrderStatus.OUT_FOR_DELIVERY: "Order {id} is out for delivery, about {eta} min away.",
    OrderStatus.DELIVERED: "Order {id} has been delivered. Enjoy!",
    OrderStatus.CANCELLED: "Order {id} has been cancelled.",
}


def status_line(order: Order) -> str:
    return STATUS_TEXT[order.status].format(id=order.id, eta=eta_minutes(order))


SYSTEM = SHARED_RULES + """
You are the ORDER TRACKER agent. Live status updates are already pushed to the chat
automatically; you answer questions about orders.
- Use get_order_status for "where is my order", "how long", etc. Give status + ETA.
- Orders can be cancelled only while PLACED (before preparation starts). Confirm the
  customer really wants to cancel before calling cancel_order, unless they were explicit.
- If the customer has no active order, say so and offer to start one.
"""


def make_tools(ctx: AgentContext) -> list:
    store, session, services = ctx.store, ctx.session, ctx.services

    def _resolve(order_id: str | None) -> Order:
        oid = order_id or session.active_order_id
        if not oid:
            raise StoreError("The customer has no active order.")
        order = store.get_order(oid)
        if order.customer_id != session.customer_id:
            raise StoreError(f"No order with id {oid}.")
        return order

    @beta_async_tool
    async def get_order_status(order_id: str | None = None) -> str:
        """Get status, ETA (minutes) and status history of an order.

        Args:
            order_id: Order id; omit for the customer's current order.
        """
        def run():
            o = _resolve(order_id)
            return {"order_id": o.id, "status": o.status.value, "eta_minutes": eta_minutes(o),
                    "items": [store.describe_item(i) for i in o.items], "total": o.total,
                    "history": [(s.value, t.strftime("%H:%M:%S")) for s, t in o.status_history]}
        return guarded(run)

    @beta_async_tool
    async def list_my_orders() -> str:
        """List the customer's orders (id, date, status, total), newest first."""
        return guarded(lambda: [{"id": o.id, "date": o.created_at, "status": o.status.value,
                                 "total": o.total}
                                for o in reversed(store.orders_for(session.customer_id))])

    @beta_async_tool
    async def cancel_order(order_id: str | None = None) -> str:
        """Cancel an order. Only possible while its status is PLACED.

        Args:
            order_id: Order id; omit for the customer's current order.
        """
        def run():
            o = store.cancel_order(_resolve(order_id).id)
            services.stop_tracking(o.id)
            if session.active_order_id == o.id:
                session.active_order_id = None
            return {"order_id": o.id, "status": o.status.value}
        return guarded(run)

    return [get_order_status, list_my_orders, cancel_order]


AGENT = AgentSpec(name="tracker", label="Order Tracker", system=SYSTEM, effort="low",
                  make_tools=make_tools)
