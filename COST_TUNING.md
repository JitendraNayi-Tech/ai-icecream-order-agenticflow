# Cost per Order: Before vs After Tuning

Every table below measures the same scripted order: **2 cups × 2 scoops of Cookies & Cream →
checkout → "yes" → delivery → 5★ feedback**.
- **Backend:** Claude Code CLI with Sonnet 5 for agents and Haiku 4.5 for the router.
- **Cost:** the API-equivalent figure the CLI reports per call. On a subscription it isn't billed
  per call; it uses up the subscription's usage allowance.
- **Where CLI runs:** each CLI run also makes one small internal Haiku call, included in the costs.

## 1. Before tuning (customer: Parthiv)

| # | Step | Agent | Handled by | Cost (USD) |
|---|---|---|---|---|
| 1 | Greeting on chat open | Flavor Recommender | Claude | 0.0169 |
| 2 | Route "I want 2 cups, 2 scoops each" | Router | Claude (Haiku) | 0.0025 |
| 3 | Ask which flavor | Order Intake | Claude | 0.0207 |
| 4 | Add 2 cups to cart | Order Intake | Claude + tool | 0.0106 |
| 5 | Price summary | Checkout & Pricing | Claude + tools | 0.0169 |
| 6 | Place order on "yes" | Checkout & Pricing | Claude + tool | 0.0156 |
| 7 | 5 live status updates | Order Tracker | Code | 0.0000 |
| 8 | Ask for rating | Follow-up & Feedback | Claude | 0.0167 |
| 9 | Record 5★ and thank | Follow-up & Feedback | Claude + tool | 0.0113 |
| | **Total: 8 Claude runs** | | | **0.1113** |

Cache tokens: 14,249 read / 14,192 written. **5,000 orders ≈ $556.**

## 2. After tuning (Parthiv, cold cache → Jagrut, warm cache)

| # | Step | Agent | Handled by | Parthiv (USD) | Jagrut (USD) |
|---|---|---|---|---|---|
| 1 | Greeting on chat open | Flavor Recommender | Code (template) | 0.0000 | 0.0000 |
| 2 | Route "I want 2 cups, 2 scoops each" | Router | Claude (Haiku) | 0.0029 | 0.0030 |
| 3 | Ask which flavor | Order Intake | Claude | 0.0123 | 0.0052 |
| 4 | Add 2 cups to cart | Order Intake | Claude + tool | 0.0103 | 0.0106 |
| 5 | Price summary | Checkout & Pricing | Code | 0.0000 | 0.0000 |
| 6 | Place order on "yes" | Checkout & Pricing | Code | 0.0000 | 0.0000 |
| 7 | 5 live status updates | Order Tracker | Code | 0.0000 | 0.0000 |
| 8 | Ask for rating | Follow-up & Feedback | Code (template) | 0.0000 | 0.0000 |
| 9 | Record 5★ and thank | Follow-up & Feedback | Code | 0.0000 | 0.0000 |
| | **Total: 3 Claude runs** | | | **0.0255** | **0.0187** |

Cache tokens: Parthiv 7,350 read / 2,518 written. Jagrut 8,740 read / 1,230 written; his first
call reused 2,740 tokens cached by Parthiv's order. **5,000 orders ≈ $95–130.**

## 3. Tuning parameters and factors

| Factor | Before | After | Effect |
|---|---|---|---|
| Prompt layout | Changing state *before* the menu, so the cache broke every call | Fixed rules + menu first, changing data last | Cached prefix read at ~10% of the input price |
| Cache scope | Rebuilt per call | Shared across turns **and customers** | Warm cache cut the first Claude call by ~58% ($0.0123 → $0.0052) |
| Predictable steps | All via Claude | 5 steps in code (`app/fastpath.py`) | −5 Claude runs per order |
| Claude runs per order | 8 | 3 | −63% |
| Routing | Rules first, LLM fallback | Unchanged | 1 router call per order |
| Models | Sonnet 5 agents, Haiku router | Unchanged | Same quality, no downgrade |
| Safety net | n/a | Questions, invalid codes, stock issues, low ratings → Claude | No loss of handling for edge cases |
| **Cost per order** | **$0.111** | **$0.019–0.026** | **4–6× cheaper** |
| **Share of a $5 ice cream** | 2.2% | 0.4–0.5% | Well below card fees (~3%) |

**Next levers (not applied yet):**
- a routing rule for obvious orders (−$0.003),
- Haiku for simple agents,
- a trimmed menu,
- the API backend (no per-run CLI overhead call).
