"""Agent 2 - Flavor Recommender: suggestions from order history, ratings and stock."""

import json

from anthropic import beta_async_tool

from app.agents.base import SHARED_RULES, AgentContext, AgentSpec, guarded

SYSTEM = SHARED_RULES + """
You are the FLAVOR RECOMMENDER agent. Suggest 2-3 flavors (and optionally a container or
topping pairing) tailored to this customer.
- The menu and the customer's taste profile are below (no need to fetch them again).
  Only suggest in-stock flavors. Use get_order_history if you want details of past orders.
- Returning customer: lean on flavors they rated highly, suggest one new flavor with similar
  tags, and never push flavors they disliked. Mention why ("you loved the mango last time").
- New customer (no history): suggest best sellers and ask one question about their taste.
- If this is the start of the conversation, greet the customer by name first.
- Don't add anything to the cart yourself; invite them to say what they'd like.
"""


def make_tools(ctx: AgentContext) -> list:
    store, cid = ctx.store, ctx.session.customer_id

    @beta_async_tool
    async def get_customer_preferences() -> str:
        """Get the customer's name, loyalty tier, per-flavor order counts, average ratings, liked and disliked flavors."""
        def run():
            c = store.customers[cid]
            return {"name": c.name, "loyalty_tier": c.loyalty_tier, **store.preferences(cid)}
        return guarded(run)

    @beta_async_tool
    async def get_order_history() -> str:
        """Get the customer's past orders with items and feedback, oldest first."""
        def run():
            return [{"id": o.id, "date": o.created_at.date(), "status": o.status.value,
                     "items": [store.describe_item(i) for i in o.items],
                     "feedback": o.feedback.model_dump() if o.feedback else None}
                    for o in store.orders_for(cid)]
        return guarded(run)

    @beta_async_tool
    async def get_menu() -> str:
        """Get the full menu with flavor tags and whether each flavor is in stock."""
        return guarded(store.menu_summary)

    @beta_async_tool
    async def get_best_sellers() -> str:
        """Get the most popular in-stock flavor ids."""
        return guarded(lambda: store.best_sellers())

    return [get_customer_preferences, get_order_history, get_menu, get_best_sellers]


def context(ctx: AgentContext) -> str:
    cid = ctx.session.customer_id
    profile = {"loyalty_tier": ctx.store.customers[cid].loyalty_tier,
               **ctx.store.preferences(cid), "best_sellers": ctx.store.best_sellers()}
    return ctx.store.menu_text() + "\n\nCUSTOMER TASTE PROFILE\n" + json.dumps(profile)


AGENT = AgentSpec(name="recommender", label="Flavor Recommender", system=SYSTEM,
                  effort="medium", make_tools=make_tools, context=context)
