"""Agent 3 - Checkout & Pricing: stock check, price quote, promo, explicit confirmation, place order."""

import json

from anthropic import beta_async_tool

from app.agents.base import SHARED_RULES, AgentContext, AgentSpec, guarded
from app.store import StoreError

SYSTEM = SHARED_RULES + """
You are the CHECKOUT & PRICING agent.
Flow:
1. If the customer mentions a promo code, call apply_promo.
2. Call check_stock, then price_cart, and show a compact summary: each line, subtotal,
   loyalty/promo discount, total. Ask them to confirm ("reply 'yes' to place the order").
3. Only when the customer has explicitly confirmed in a LATER message, call place_order.
   place_order will refuse if the customer hasn't seen the current price yet.
4. After placing, give the order id and say live updates will follow in this chat.
If the cart is empty, say so and invite them to order. If stock is short, explain and
suggest they adjust the order.
"""


def confirm_and_place(ctx: AgentContext) -> dict:
    """Place the order only if the customer saw the current price on an earlier turn."""
    session = ctx.session
    if session.quoted_turn is None:
        raise StoreError("Show the customer the price (price_cart) before placing the order.")
    if session.quoted_turn >= session.turn:
        raise StoreError("Wait for the customer to confirm the price in their next message.")
    order = ctx.store.place_order(session.customer_id, session.cart, session.promo_code)
    session.cart.clear()
    session.promo_code = None
    session.quoted_turn = None
    session.active_order_id = order.id
    ctx.services.start_tracking(session, order)
    return {"order_id": order.id, "total": order.total, "status": order.status.value}


def make_tools(ctx: AgentContext) -> list:
    store, session = ctx.store, ctx.session
    cid = session.customer_id

    @beta_async_tool
    async def view_cart() -> str:
        """Show the current cart."""
        return guarded(lambda: {"cart": session.cart_view(store), "promo_code": session.promo_code})

    @beta_async_tool
    async def check_stock() -> str:
        """Check the cart against current stock. Returns a list of problems (empty = OK)."""
        return guarded(lambda: {"problems": store.stock_problems(session.cart)})

    @beta_async_tool
    async def apply_promo(code: str) -> str:
        """Validate a promo code and attach it to this checkout.

        Args:
            code: The promo code the customer gave.
        """
        def run():
            promo = store.validate_promo(code, cid)
            session.promo_code = promo.code
            session.quoted_turn = None
            return {"applied": promo.code, "percent": promo.percent}
        return guarded(run)

    @beta_async_tool
    async def price_cart() -> str:
        """Price the cart with loyalty discount and any applied promo. Show this to the customer before placing."""
        def run():
            quote = store.quote(session.cart, cid, session.promo_code)
            session.quoted_turn = session.turn
            return quote.model_dump()
        return guarded(run)

    @beta_async_tool
    async def place_order() -> str:
        """Place the order for the current cart. Only after the customer confirmed the priced summary."""
        return guarded(lambda: confirm_and_place(ctx))

    return [view_cart, check_stock, apply_promo, price_cart, place_order]


def context(ctx: AgentContext) -> str:
    return ("CURRENT CART\n" + json.dumps(ctx.session.cart_view(ctx.store) or "empty")
            + f"\nPromo applied: {ctx.session.promo_code or 'none'}")


AGENT = AgentSpec(name="checkout", label="Checkout & Pricing", system=SYSTEM, effort="medium",
                  make_tools=make_tools, context=context)
