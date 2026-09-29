# Learn: Architecture of the Ice Cream Agentic Flow

An architect-level walkthrough of this POC: the patterns it uses, why, what they cost, and what
would change for production. See `README.md` to run it and `CLAUDE.md` for the code map.

---

## 1. The system in one paragraph

A customer chats with an ice cream shop in natural language. Five specialist Claude agents
share the work:
- **Order Intake** builds the cart from free text.
- **Flavor Recommender** personalises suggestions from history and ratings.
- **Checkout & Pricing** quotes the price and places the order after explicit confirmation.
- **Order Tracker** pushes live delivery updates and answers "where's my order?".
- **Follow-up & Feedback** collects ratings after delivery and recovers bad experiences.

A plain-code **orchestrator** routes each message to one agent. Every agent works against a
**shared state**, and deterministic Python enforces all business rules.

```
Browser ──WebSocket──► FastAPI ──► Orchestrator ──► Router (rules → LLM fallback)
                                        │
                                        ├──► Agent run (Claude) ──tools──► Session + Store
                                        │        via SDK tool runner   OR   Claude Code CLI + MCP
                                        │
OrderTracker (timers) ──events──────────┘   (status updates, auto-start Follow-up)
```

---

## 2. Patterns used

| Pattern | Where | Why |
|---|---|---|
| **Router / specialist agents** | `orchestrator.py`, `agents/*.py` | Small prompts and small tool sets per agent mean fewer mistakes and are easier to test and change |
| **Blackboard (shared state)** | `session.py`, `store.py` | Agents never message each other; they read and write shared history, cart, flags and orders. Handoffs are implicit and debuggable |
| **Hybrid routing: rules first, LLM fallback** | `routing.py`, `llm.route` | Deterministic rules handle the common cases in ~0 ms. The LLM handles only ambiguous messages |
| **Sticky routing** | `routing.quick_route` | An agent that asked a question receives the answer, which keeps multi-turn slot filling coherent |
| **Tool use (function calling)** | `make_tools()` in each agent | The LLM decides; code executes. The model never mutates state directly |
| **Context binding (closures)** | `make_tools(ctx)` | Tools are bound to one customer's session, so the model can't pass or forge customer ids |
| **Deterministic core, probabilistic edge** | `store.py` | Pricing, stock, promos and cancellation rules are plain Python, unit-tested and never left to the model |
| **Human-in-the-loop confirmation** | `checkout.confirm_and_place` | Orders are placed only after the customer saw the price on an earlier turn |
| **Event-driven agents** | `OrderTracker`, `_on_status` | A state change ("delivered") triggers an agent without any user message |
| **Feedback loop** | Follow-up → `store.preferences` → Recommender | Ratings change future recommendations |
| **Ports & adapters (swappable LLM backend)** | `llm.py`, `claude_code.py`, `mcp_bridge.py` | Agent code is backend-agnostic: paid API or local Claude Code CLI |
| **Message coalescing** | `Orchestrator.handle`, `Session.pending` | Messages sent while an agent is busy become one turn, so there's no queue of stale replies |

**Deliberately not used:** agent frameworks (LangChain/LangGraph/CrewAI), vector RAG,
agent-to-agent protocols. At this size, about 150 lines of explicit orchestration are clearer
than a framework, and the data is small and structured (see §6).

---

## 3. Agent design

Each agent is an `AgentSpec` (`agents/base.py`):

| Field | Purpose |
|---|---|
| `system` | Role, scope and handoff rules. Shared rules come from `SHARED_RULES` |
| `make_tools(ctx)` | The agent's tools, as closures over `AgentContext(store, session, services)` |
| `context(ctx)` | Optional data put into the prompt each turn (menu, cart, taste profile) |
| `effort` | Reasoning depth: `low` for Tracker and the router, `medium` for the rest |

Design rules the agents follow:
- **One job per agent, with the fewest tools that do it** (least privilege). Checkout can place
  orders; Intake cannot.
- **Tools return errors as data.** `guarded()` turns a `StoreError` into `{"error": ...}` so the
  model can explain or fix the problem instead of the app crashing.
- **Agents know about each other only by name.** `SHARED_RULES` lists all five, so an agent can
  redirect the customer ("say 'checkout' when ready") instead of doing another agent's job.

---

## 4. Models and APIs

Two interchangeable backends, selected by `LLM_BACKEND`:

| | `api` backend | `claude_code` backend (default without a key) |
|---|---|---|
| Transport | Anthropic Python SDK (`anthropic`) | Local `claude -p` (headless Claude Code) as a subprocess |
| Auth and billing | `ANTHROPIC_API_KEY`, pay per token | The user's Claude Code login (subscription), personal use |
| Agent loop | SDK beta **tool runner** (`client.beta.messages.tool_runner`) | Claude Code's own loop |
| Tools | Passed in-process (`@beta_async_tool`) | Exposed over **MCP** at `/mcp/{customer}/{agent}` |
| Agent model | `claude-opus-5-5`, adaptive thinking | `sonnet` (`CLAUDE_CODE_MODEL`) |
| Router model | `claude-opus-5-5` at effort `low`, **structured output** (`messages.parse` → Pydantic `Route`) | `haiku`, one-word answer |
| Safety fallback | Server-side `fallbacks="default"` on refusals | n/a |

**Why MCP here:** the CLI runs in a separate process, so it can't receive Python functions.
MCP (Model Context Protocol) is the standard way to lend tools to an AI client.
`mcp_bridge.py` is a hand-rolled, ~60-line JSON-RPC server implementing `initialize`,
`ping`, `tools/list` and `tools/call`. It calls the *same* tool objects as the API backend,
which is why switching backends didn't touch any agent file.

---

## 5. Context engineering

What each model call sees, in order:
1. **System prompt**:
   - the agent's role and rules,
   - customer identity and loyalty tier,
   - `Customer.notes` (e.g. *dairy-free*; every agent sees it),
   - a live session-state summary,
   - the agent's `context()` block (menu, cart, taste profile).
2. **Conversation**: the shared, text-only history. Tool calls stay inside a single run and are
   never stored in it.
3. **Events**: synthetic prompts such as "[order delivered, start the follow-up]", shown to the
   agent for that run only and never stored.

Principles:
- **Pre-load what's always needed, fetch the rest with tools.** Putting the menu in the prompt
  removed one tool round-trip per turn and stopped the model guessing invalid ids.
- **Keep history text-only.** It's portable across agents and backends, and it stays small.
- **Keep the stable part of the system prompt first** (role, rules) and the volatile part last
  (state), which helps prompt caching on the API backend.

---

## 6. Memory and retrieval

| Kind | Implementation |
|---|---|
| Short-term (conversation) | `Session.history`, per customer, shared by all agents |
| Working state | `Session` fields: `cart`, `quoted_turn`, `active_order_id`, `feedback_order_id`, `last_agent`, `pending` |
| Long-term | `Store.orders` + feedback; `Store.preferences()` derives liked/disliked flavors |
| Retrieval | **Agentic retrieval** (the agent calls tools like `get_order_history`) + **context injection** |

**Not classic RAG.** There are no embeddings and no vector database: the data is small,
structured, and needs exact answers (prices, stock). Vector RAG becomes worth adding for
*unstructured* knowledge such as allergen documents, FAQs or thousands of reviews.

---

## 7. Speed (measured on the `claude_code` backend)

| Stage | Cost |
|---|---|
| Claude Code process start | ~3 s per call |
| LLM router call (Haiku) | ~4 s |
| Agent turn, no tool | ~4–5 s |
| Agent turn, 1–2 tool rounds | ~5–7 s |

Optimisations applied and their effect (typical reply time went from **10–16 s to 4–7 s**):
1. **Rules before the LLM router.** 5 of 6 messages in the test script needed no router call.
2. **Context pre-loading.** One fewer tool round-trip per turn.
3. **Message coalescing.** No backlog of stale replies when the user types while waiting.
4. **Cheaper models where accuracy allows:** Haiku for routing, Sonnet for agents.
5. **Timeout at 90 s** with a clear retry message, instead of a silent 3-minute hang.

The next steps would be:
- a long-lived CLI process (`--input-format stream-json`) to remove the ~3 s start-up cost,
- streaming replies to the UI,
- or the API backend, which has no process start-up at all.

---

## 8. Quality and reliability

- **Deterministic guardrails in code:** stock checks, 1–3 scoops, promo validity, coupon cap (25%,
  single-use, bound to one customer), cancel only while `PLACED`, confirm before placing an order.
- **Money handling:** `Decimal` with half-up rounding. Python's `round(0.825, 2)` returns 0.82,
  which a test caught.
- **Derived data, not duplicated data:** seed orders omit prices, and the store computes them
  from the pricing rules, so they can't drift apart.
- **Ids never reused:** new order ids start after the highest seeded id. A replay found a new
  order overwriting a seeded one before this fix.
- **Graceful failure:** every backend error becomes a chat message, and the UI never hangs on
  "typing…".
- **Tests (22, no LLM needed):** store rules, tracker state machine, routing rules, message
  coalescing and orchestration run with the LLM stubbed (`monkeypatch llm.route/run_agent`).
- **End-to-end replay scripts** (run during development) drive a real conversation over the
  WebSocket and log route decisions, tool calls and per-turn latency.

---

## 9. Security posture (POC level)

- The CLI runs with **built-in tools disabled** (`--tools ""`): no shell, no file edits. Only the
  shop's MCP tools are allowed (`--allowedTools mcp__shop`, `--strict-mcp-config`).
- The CLI runs in an **empty temp directory**, so it doesn't load this repo's `CLAUDE.md` or
  settings.
- Tool calls are **scoped by URL** to one customer and one agent. Note: the `/mcp` endpoint has
  no auth, which is acceptable only on localhost.
- Secrets stay in `.env`, which git ignores.

---

## 10. Trade-offs and limitations

| Decision | Upside | Downside |
|---|---|---|
| In-memory storage | Zero setup | Resets on restart; single process only |
| One CLI process per turn | No API key or cost | ~3 s overhead; needs a logged-in machine; not for multi-user hosting |
| Rules-first routing | Fast, predictable | Keyword rules can misroute unusual phrasing; the LLM fallback covers the rest |
| Text-only shared history | Simple, portable | Agents don't see earlier tool results unless they're mentioned in text |
| Hand-rolled MCP | No dependency churn | Implements only the subset Claude Code needs |

---

## 11. Path to production

1. Replace `Store` with a database (SQLite → Postgres); the interface is already isolated.
2. Use the API backend (or a pooled, long-lived agent process); add prompt caching and streaming.
3. Add auth: real customer identity on the WebSocket, and an authenticated or removed `/mcp`.
4. Observability: trace per turn (route, tool calls, tokens, latency), plus an eval set of real
   conversations that checks routing accuracy and task completion.
5. Replace the simulated tracker with real kitchen and delivery webhooks.
6. Add vector RAG only if unstructured knowledge (allergens, FAQs) enters scope.

---

## 12. Lessons learned while building it

- **The LLM is the slow part, and so is anything you wrap around it.** Measure each stage
  before optimising.
- **"Lost messages" were really out-of-order replies.** Concurrency bugs look like AI bugs.
- **Give models ids, not names.** Pre-loading the menu with ids eliminated invalid tool calls.
- **Keep business rules out of prompts.** Prompts guide; code enforces.
- **Environment matters:** a stale server on the port and a machine out of memory both
  looked like application bugs. Check the process and the port first.

---

## Glossary

- **Agent**: an LLM, instructions and tools, running a think → act → observe loop.
- **Tool use / function calling**: the model asks for a function call with arguments; your code runs it.
- **MCP**: Model Context Protocol, a standard way for AI clients to discover and call tools on a server.
- **Orchestrator / router**: code or a model that decides which agent handles a message.
- **Blackboard**: agents coordinate through shared state instead of direct messages.
- **Structured output**: forcing the model's reply to match a JSON schema.
- **Context engineering**: choosing exactly what the model sees on each call.
- **RAG**: retrieval-augmented generation, fetching relevant text (often by vector similarity) into the prompt.
- **Headless Claude Code**: `claude -p`, non-interactive mode, used here as the LLM backend.
- **Effort**: how much reasoning the model spends (`low` → `max`); a speed/quality dial.
