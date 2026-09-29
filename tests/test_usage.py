from app import usage
from app.orchestrator import Orchestrator
from app.store import Store

FAST = {"PLACED": 0.01, "PREPARING": 0.01, "READY": 0.01, "OUT_FOR_DELIVERY": 0.01}

CLI_RESULT = {"modelUsage": {  # one `claude -p` run: the CLI's small Haiku call + the agent
    "claude-haiku-4-5": {"inputTokens": 700, "outputTokens": 15, "cacheReadInputTokens": 0,
                         "cacheCreationInputTokens": 0, "costUSD": 0.0008},
    "claude-sonnet-5": {"inputTokens": 4, "outputTokens": 200, "cacheReadInputTokens": 6000,
                        "cacheCreationInputTokens": 700, "costUSD": 0.0095},
}}


def record_cli_run(customer: str, agent: str, order_id: str | None = None) -> None:
    token = usage.current_call.set((customer, agent, order_id))
    try:
        usage.record_claude_code(CLI_RESULT)
    finally:
        usage.current_call.reset(token)


def test_one_cli_run_counts_once_but_sums_all_models():
    record_cli_run("parthiv", "intake")
    usage.record_code("parthiv", "checkout")
    b = usage.cost_breakdown(usage.records_for("parthiv"))
    assert b["claude_runs"] == 1 and round(b["ai_cost_usd"], 4) == 0.0103
    assert b["cache_read_tokens"] == 6000
    rows = {r["agent"]: r for r in b["by_agent"]}
    assert rows["intake"]["runs"] == 1 and rows["checkout"] == {
        "agent": "checkout", "runs": 0, "cost_usd": 0.0, "code_steps": 1}


async def test_feedback_that_closes_the_order_still_counts_toward_it():
    store = Store()
    orch = Orchestrator(store, FAST)
    s = orch.session("zarna")
    s.feedback_order_id = "ORD-2002"  # delivered, awaiting feedback
    await orch.handle(s, "5 stars, loved it!")  # fast path records it and clears the flag
    current = orch.usage_panel(s)["current"]
    assert current["order_id"] == "ORD-2002" and current["status"] == "DELIVERED"
    assert [a["label"] for a in current["by_agent"]] == ["Follow-up & Feedback"]


def test_calls_without_a_caller_are_ignored():
    usage.record_claude_code(CLI_RESULT)  # no current_call set (e.g. a test script)
    assert usage.LEDGER == []


async def test_panel_moves_order_to_history_when_next_order_starts():
    store = Store()
    orch = Orchestrator(store, FAST)
    s = orch.session("parthiv")
    record_cli_run("parthiv", "intake")
    s.add_to_cart(store, flavors=["cookies_cream"], scoops=2, container="cup", quantity=2)
    panel = orch.usage_panel(s)["current"]
    assert panel["status"] == "IN CART" and panel["total"] == 10.80 and panel["claude_runs"] == 1

    await orch.handle(s, "checkout")  # fast path: price summary (code)
    await orch.handle(s, "yes")       # fast path: order placed (code)
    orch.stop_tracking(s.active_order_id)
    panel = orch.usage_panel(s)
    order_id = panel["current"]["order_id"]
    assert order_id == s.active_order_id and panel["current"]["total"] == 10.80
    assert round(panel["current"]["ai_share_pct"], 3) == round(0.0103 / 10.80 * 100, 3)
    labels = [a["label"] for a in panel["current"]["by_agent"]]
    assert labels == ["Order Intake", "Checkout & Pricing"]

    # The next order starts: the finished one moves to "Earlier orders".
    s.active_order_id = None
    s.add_to_cart(store, flavors=["vanilla"], scoops=1, container="cup")
    panel = orch.usage_panel(s)
    assert panel["current"]["status"] == "IN CART"
    assert [h["order_id"] for h in panel["history"]] == [order_id]
