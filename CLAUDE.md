# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
.venv\Scripts\activate                    # venv lives in .venv
pip install -r requirements.txt
uvicorn app.main:app --reload             # http://localhost:8000
pytest                                    # all tests; Claude is stubbed, no API key needed
pytest tests/test_orchestrator.py::test_greet_runs_once   # a single test
```

Env: `LLM_BACKEND` (`claude_code` or `api`; defaults to `api` only when `ANTHROPIC_API_KEY` is set), `CLAUDE_CODE_MODEL` / `CLAUDE_CODE_ROUTER_MODEL` (default `sonnet` / `haiku`), `APP_URL` (default `http://127.0.0.1:8000`, must match the uvicorn port), `ICECREAM_MODEL` (API backend, default `claude-opus-5-5`), `TRACKER_SPEED` (multiplies status delays).

## Architecture

FastAPI + WebSocket chat POC where five Claude agents cooperate on one ice cream order.
The five agents are Order Intake, Flavor Recommender, Checkout & Pricing, Order Tracker, and Follow-up & Feedback.

- **Request path:** `main.py` handles WS `/ws/{customer_id}` and calls `Orchestrator.handle`. `llm.route` makes a structured-output call that returns a `Route` naming one agent. Then `llm.run_agent` runs that agent through the SDK beta tool runner. The reply goes into `Session.outbox`, and the WS writer task sends it.
- **Only `app/llm.py` talks to Claude.** Tests monkeypatch `llm.route` and `llm.run_agent`. It dispatches on `config.LLM_BACKEND`:
  - `api`: Anthropic SDK tool runner; needs an API key, pay-as-you-go.
  - `claude_code` (`app/claude_code.py`): spawns one `claude -p` process per call, using the local Claude Code subscription login. Built-in CLI tools are disabled (`--tools ""`), and it runs in an empty temp cwd so this CLAUDE.md isn't loaded. The agent's tools reach it over MCP: `--mcp-config` points at this app's `/mcp/{customer_id}/{agent}`, served by `app/mcp_bridge.py`, a hand-rolled JSON-RPC endpoint. That endpoint calls the same `make_tools(ctx)` tool objects via `tool.call(args)`. So the agent code is backend-agnostic.
- **Agents** live in `app/agents/*.py`. Each one exports `AGENT = AgentSpec(...)` (system prompt, effort, `make_tools`) and is registered in `app/agents/__init__.py`. `make_tools(ctx)` returns `@beta_async_tool` closures bound to an `AgentContext` (store, session, services), so Claude never passes customer ids. Tool bodies are wrapped in `guarded()`, which turns a `StoreError` into an `{"error": ...}` result for Claude.
- **Business rules** (pricing, stock, promos, preferences) are plain methods on `app/store.py::Store` and are unit-tested directly. Keep that logic out of tool closures. If a rule inside a tool needs a test, pull it out into a function, as `checkout.confirm_and_place` does.
- **Shared history:** `Session.history` is one text-only history per customer, shared by every agent. Tool-call turns never go into it. `add_message` merges consecutive same-role turns so roles alternate. Synthetic events (greeting, "order delivered") are passed to the agent as extra user turns and are not stored.
- **Live tracking:** `OrderTracker` (`agents/tracker.py`) runs one asyncio task per order and walks through `STATUS_FLOW` using `config.STATUS_DELAYS`. On each change it calls `Orchestrator._on_status`, which pushes a `status` frame and a chat line. At DELIVERED it sets `session.feedback_order_id` and starts the Follow-up agent in the background.
- **Checkout safety:** `place_order` refuses unless the price was shown (`quoted_turn`) on an earlier customer turn. Any cart or promo change resets `quoted_turn`.
- **WS frame types:** `message`, `typing`, `status`, `cart`, `usage`. They are handled in `static/index.html`, whose 3-column layout is: cost panel | chat | cart and order.
- **Cost tracking (`app/usage.py`):** a process-global ledger of every Claude run, with tokens and the API-equivalent `costUSD` the CLI reports (priced from `API_PRICES` on the API backend), plus every fast-path step (model `"code"`, $0).
  - The orchestrator sets `usage.current_call = (customer, agent, order_in_scope)` when a step starts. Capturing the order in scope *before* the step matters: a step can end the order, as recording feedback clears `feedback_order_id`.
  - Records made before an order exists are attributed to it once it's placed (`tag_untagged`).
  - `Orchestrator.usage_panel()` builds the panel data, also served at `/api/usage/{customer_id}`. `tests/conftest.py` clears the ledger between tests.
- **Routing:** `Orchestrator.pick_agent` tries `routing.quick_route` first (rules: an open question from intake/checkout/followup keeps the next message, "yes" after a price goes to checkout, keywords). It calls the LLM router only when no rule matches, which saves a whole Claude call on most turns.
- **Busy turns:** messages sent while an agent is working queue in `Session.pending`. The `handle` loop (guarded by `session.draining`) merges them into one customer turn once the current reply is done.
- **Prompt layout (prompt caching):** `Orchestrator.build_prompt` returns `(system, volatile)`.
  - `system` = agent rules + `AgentSpec.static_context(store)` (the menu). It must stay byte-identical across customers and turns, since it's the cached prefix shared by everyone; `test_system_prompt_is_identical_across_customers_and_turns` enforces this.
  - `volatile` = customer, `Customer.notes` (e.g. dairy-free), session state, and `AgentSpec.context(ctx)` (cart, taste profile). It's sent after the conversation: appended to the CLI prompt, or as a mid-conversation system message on the API backend, where `system` also carries an explicit `cache_control` breakpoint.
  - Never put per-customer or per-turn data into `system` or `static_context`.
- **Fast paths (`app/fastpath.py`):** deterministic replies with no Claude call, for the greeting, the price summary on a plain "checkout [with CODE]", placing the order on "yes", the post-delivery rating request, and clear 4–5★ feedback. Each returns `None` for anything unusual (questions, invalid promo, stock problems, low ratings, complaints), which falls through to the Claude agent.
- **Storage** is in memory only, seeded from `data/*.json`, and resets on restart. Seed orders omit prices; `Store.__init__` computes them from the pricing rules. New order ids continue after the highest seeded id.
