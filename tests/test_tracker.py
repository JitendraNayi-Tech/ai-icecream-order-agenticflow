import asyncio

from app.agents.tracker import OrderTracker, eta_minutes
from app.models import OrderStatus
from app.store import Store

FAST = {"PLACED": 0.01, "PREPARING": 0.01, "READY": 0.01, "OUT_FOR_DELIVERY": 0.01}


async def test_tracker_walks_full_status_flow():
    store = Store()
    seen = []

    async def on_status(order):
        seen.append(order.status)

    tracker = OrderTracker(store, on_status, FAST)
    order = store.place_order("dirgh", [store.build_item(1, ["vanilla"], 1, "cup")])
    tracker.start(order)
    await asyncio.wait_for(tracker.tasks[order.id], timeout=2)
    assert seen == [OrderStatus.PLACED, OrderStatus.PREPARING, OrderStatus.READY,
                    OrderStatus.OUT_FOR_DELIVERY, OrderStatus.DELIVERED]
    assert eta_minutes(order, FAST) == 0


async def test_cancelled_order_stops_advancing():
    store = Store()
    seen = []

    async def on_status(order):
        seen.append(order.status)

    tracker = OrderTracker(store, on_status, {**FAST, "PLACED": 0.2})
    order = store.place_order("dirgh", [store.build_item(1, ["vanilla"], 1, "cup")])
    tracker.start(order)
    await asyncio.sleep(0.05)
    store.cancel_order(order.id)
    tracker.stop(order.id)
    await asyncio.sleep(0.3)
    assert seen == [OrderStatus.PLACED]
    assert order.status == OrderStatus.CANCELLED
