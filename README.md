# Ice Cream Order Agentic Flow (POC)

A chat-based ice cream ordering assistant. Five cooperating Claude agents take the order,
suggest flavors, check out, send live delivery updates, and follow up after delivery.

## Agents

| # | Agent | What it does |
|---|---|---|
| 1 | **Order Intake** | Turns natural language into cart items; asks clarifying questions |
| 2 | **Flavor Recommender** | Suggests flavors from past orders + ratings + stock; best sellers for new customers |
| 3 | **Checkout & Pricing** | Stock check, loyalty/promo pricing, explicit confirmation, places the order |
| 4 | **Order Tracker** | Background status updates (PLACED → … → DELIVERED) pushed live; answers "where's my order?", cancels |
| 5 | **Follow-up & Feedback** | Auto-starts on delivery; collects rating, detects sentiment, offers coupon/remake; feeds ratings back to Agent 2 |

An orchestrator routes every customer message to one agent using a small Claude
structured-output call.

## Run

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Two ways to power the agents (see `.env.example`):
- **Claude Code (default, no API key):** needs the [Claude Code](https://claude.com/claude-code) CLI
  installed and logged in. Each agent turn runs `claude -p` on your own subscription. For
  personal, local use only.
- **Claude API:** put `ANTHROPIC_API_KEY=...` in `.env` (pay-as-you-go). Setting a key switches
  the backend automatically.

📘 **New to agentic AI?** Read [learn.md](learn.md) for the architecture: patterns, models,
context engineering, MCP, speed and quality trade-offs.

Open http://localhost:8000 and pick a customer:
- **Parthiv** (gold): cookies & cream
- **Zarna** (silver): chocolate and vanilla
- **Dirgh**: Cookie Monster, cookies & cream milkshake
- **Pavani** (gold): strawberry and mango, dairy-free only
- **Jagrut** (silver): chocolate, cookies & cream
- **Aryan**: chocolate chip and banana sundae

Try: "what should I get?" → "2 scoops of the first one in a waffle cone" → "checkout with SUMMER10"
→ "yes" → watch the status updates → answer the feedback question.

Set `TRACKER_SPEED=0.3` in `.env` for a faster delivery demo.

## Tests

```bash
pytest                      # all tests; Claude is stubbed, no API key needed
pytest tests/test_store.py::test_promo_stacks_with_loyalty   # a single test
```

Data is in memory, seeded from `data/menu.json` and `data/customers.json`, and resets on restart.
