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


async def test_greet_runs_once_without_llm(calls):
    orch = Orchestrator(Store(), FAST)
    s = orch.session("dirgh")
    await orch.greet(s)
    await orch.greet(s)
    assert calls["runs"] == []  # templated greeting, no Claude call
    assert len(s.history) == 1 and "Cookie Monster" in s.history[0]["content"]


def test_system_prompt_is_identical_across_customers_and_turns():
    """The cached prefix must not contain anything customer- or turn-specific."""
    store = Store()
    orch = Orchestrator(store, FAST)
    a, b = orch.session("parthiv"), orch.session("pavani")
    sys_a1, vol_a1 = orch.build_prompt(a, "intake")
    a.add_to_cart(store, flavors=["vanilla"], scoops=1, container="cup")
    a.turn, a.quoted_turn = 3, 2
    sys_a2, vol_a2 = orch.build_prompt(a, "intake")
    sys_b, vol_b = orch.build_prompt(b, "intake")
    assert sys_a1 == sys_a2 == sys_b
    assert "Madagascar Vanilla" in sys_a1  # the menu is part of the cached prefix
    assert "Parthiv" not in sys_a1 and "Parthiv" in vol_a1
    assert vol_a1 != vol_a2  # cart/state changes only touch the volatile part
    assert "dairy-free" in vol_b.lower()


async def test_checkout_and_confirmation_use_no_llm(calls):
    store = Store()
    orch = Orchestrator(store, FAST)
    s = orch.session("parthiv")
    s.add_to_cart(store, flavors=["cookies_cream"], scoops=2, container="cup", quantity=2)
    await orch.handle(s, "checkout with SUMMER10")
    assert "Total: $9.60" in s.history[-1]["content"]  # $12.00 - (10% gold + 10% promo)
    await orch.handle(s, "yes")
    assert "Your order is placed" in s.history[-1]["content"]
    assert calls["runs"] == [] and s.active_order_id
    orch.stop_tracking(s.active_order_id)


async def test_checkout_with_a_question_still_goes_to_the_agent(calls):
    store = Store()
    orch = Orchestrator(store, FAST)
    s = orch.session("parthiv")
    s.add_to_cart(store, flavors=["vanilla"], scoops=1, container="cup")
    await orch.handle(s, "can I pay by card at checkout?")
    assert len(calls["runs"]) == 1 and "CHECKOUT" in calls["runs"][0]["system"]


async def test_good_rating_recorded_without_llm_bad_rating_goes_to_agent(calls):
    store = Store()
    orch = Orchestrator(store, FAST)
    s = orch.session("zarna")
    s.feedback_order_id = "ORD-2002"
    await orch.handle(s, "5 stars, loved it!")
    assert store.orders["ORD-2002"].feedback.rating == 5 and calls["runs"] == []
    s.feedback_order_id = "ORD-2001"
    await orch.handle(s, "2 stars, it had melted")
    assert len(calls["runs"]) == 1 and "FOLLOW-UP" in calls["runs"][0]["system"]


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
    # the follow-up opens with a templated rating request, no Claude call
    assert "rate it 1-5" in s.history[-1]["content"] and calls["runs"] == []
