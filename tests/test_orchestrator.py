"""Orchestrator flow with Claude stubbed out (no network, no API key needed)."""

import asyncio

import pytest

from app import llm
from app.agents.base import AgentContext
from app.agents.checkout import confirm_and_place
from app.models import OrderStatus, Route
from app.orchestrator import Orchestrator
from app.store import Store, StoreError

FAST = {"PLACED": 0.01, "PREPARING": 0.01, "READY": 0.01, "OUT_FOR_DELIVERY": 0.01}


@pytest.fixture
def calls(monkeypatch):
    """Stub the router and agent runs; records which agents ran and with what messages."""
    log = {"route_to": "intake", "runs": []}

    async def fake_route(transcript, state):
        return Route(agent=log["route_to"], reason="test")

    async def fake_run_agent(system, tools, messages, effort, **kwargs):
        log["runs"].append({"system": system, "messages": messages, "tools": tools})
        return f"reply #{len(log['runs'])}"

    monkeypatch.setattr(llm, "route", fake_route)
    monkeypatch.setattr(llm, "run_agent", fake_run_agent)
    return log


def drain(session):
    frames = []
    while not session.outbox.empty():
        frames.append(session.outbox.get_nowait())
    return frames


async def test_message_is_routed_and_reply_recorded(calls):
    orch = Orchestrator(Store(), FAST)
    s = orch.session("parthiv")
    calls["route_to"] = "recommender"
    await orch.handle(s, "what should I get?")
    assert "FLAVOR RECOMMENDER" in calls["runs"][0]["system"]
    assert s.history == [{"role": "user", "content": "what should I get?"},
                         {"role": "assistant", "content": "reply #1"}]
    msgs = [f for f in drain(s) if f["type"] == "message"]
    assert msgs == [{"type": "message", "agent": "Flavor Recommender", "text": "reply #1"}]


async def test_messages_sent_while_busy_are_merged_into_one_turn(calls, monkeypatch):
    orch = Orchestrator(Store(), FAST)
    s = orch.session("zarna")
    release = asyncio.Event()

    async def slow_run_agent(system, tools, messages, effort, **kwargs):
        calls["runs"].append({"messages": messages})
        if len(calls["runs"]) == 1:
            await release.wait()  # first turn is "thinking" while more messages arrive
        return f"reply #{len(calls['runs'])}"

    monkeypatch.setattr(llm, "run_agent", slow_run_agent)
    first = asyncio.create_task(orch.handle(s, "one scoop"))
    await asyncio.sleep(0.01)
    await orch.handle(s, "what is happening?")  # returns at once: queued
    await orch.handle(s, "make it a cup")
    release.set()
    await first
    assert len(calls["runs"]) == 2  # one run for the first message, one for both queued ones
    assert calls["runs"][1]["messages"][-1] == {
        "role": "user", "content": "what is happening?\nmake it a cup"}
    assert s.turn == 2 and not s.pending and not s.draining


async def test_greet_runs_once(calls):
    orch = Orchestrator(Store(), FAST)
    s = orch.session("dirgh")
    await orch.greet(s)
    await orch.greet(s)
    assert len(calls["runs"]) == 1 and s.history[0]["role"] == "user"


async def test_place_order_requires_confirmation_on_later_turn(calls):
    store = Store()
    orch = Orchestrator(store, FAST)
    s = orch.session("dirgh")
    ctx = AgentContext(store, s, orch)
    s.turn = 1
    s.add_to_cart(store, flavors=["vanilla"], scoops=1, container="cup")
    with pytest.raises(StoreError, match="price"):
        confirm_and_place(ctx)
    s.quoted_turn = 1  # price shown this turn
    with pytest.raises(StoreError, match="confirm"):
        confirm_and_place(ctx)
    s.turn = 2  # customer replied "yes"
    result = confirm_and_place(ctx)
    assert s.cart == [] and s.active_order_id == result["order_id"]
    orch.stop_tracking(result["order_id"])


async def test_delivery_pushes_updates_and_starts_followup(calls):
    store = Store()
    orch = Orchestrator(store, FAST)
    s = orch.session("dirgh")
    s.turn, s.quoted_turn = 2, 1
    s.add_to_cart(store, flavors=["mango"], scoops=1, container="cone")
    s.quoted_turn = 1
    order_id = confirm_and_place(AgentContext(store, s, orch))["order_id"]

    await asyncio.wait_for(orch.tracker.tasks[order_id], timeout=2)
    await asyncio.wait_for(asyncio.gather(*orch._background), timeout=2)

    statuses = [f["status"] for f in drain(s) if f["type"] == "status"]
    assert statuses == [st.value for st in (OrderStatus.PLACED, OrderStatus.PREPARING,
                                            OrderStatus.READY, OrderStatus.OUT_FOR_DELIVERY,
                                            OrderStatus.DELIVERED)]
    assert s.feedback_order_id == order_id and s.active_order_id is None
    followup = calls["runs"][-1]
    assert "FOLLOW-UP" in followup["system"]
    assert "was just delivered" in followup["messages"][-1]["content"]
    # the event prompt is not persisted in the shared history
    assert all("was just delivered" not in m["content"] for m in s.history)
