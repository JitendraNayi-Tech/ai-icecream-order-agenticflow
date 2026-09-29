"""Agent 1 - Order Intake: turns natural language into structured cart items."""

import json

from anthropic import beta_async_tool

from app.agents.base import SHARED_RULES, AgentContext, AgentSpec, guarded

SYSTEM = SHARED_RULES + """
You are the ORDER INTAKE agent. Turn what the customer says into cart items.
- The menu and current cart are below; use their ids with add_to_cart / update_cart_item /
  remove_from_cart. No need to call get_menu or view_cart unless something changed.
- When the customer's message answers your previous question, apply it right away and ask
  only for what is still missing. Never re-ask something they already told you.
- Each item needs: flavors (1-3), scoops (1-3, at least the number of flavors), container
  (cup, cone, waffle_cone), optional toppings, quantity.
- If something required is missing or ambiguous (e.g. no container, "a big one"), ask ONE
  short clarifying question instead of guessing. Sensible defaults are fine only when the
  customer says "whatever"/"surprise me".
- "that one" / "the first suggestion" refers to flavors recommended earlier in the chat.
- After changing the cart, confirm what's in it in one line and ask if they'd like
  anything else or are ready to check out.
"""


def make_tools(ctx: AgentContext) -> list:
    store, session = ctx.store, ctx.session

    @beta_async_tool
    async def get_menu() -> str:
        """Get the full menu: flavors (with stock), containers, toppings and prices."""
        return guarded(store.menu_summary)

    @beta_async_tool
    async def add_to_cart(flavors: list[str], scoops: int, container: str,
                          toppings: list[str] | None = None, quantity: int = 1) -> str:
        """Add one ice cream item to the cart.

        Args:
            flavors: Flavor ids, one per distinct flavor (max = scoops).
            scoops: Number of scoops per item, 1-3.
            container: Container id: cup, cone or waffle_cone.
            toppings: Optional topping ids.
            quantity: How many identical items, 1-10.
        """
        def run():
            item = session.add_to_cart(store, flavors=flavors, scoops=scoops, container=container,
                                       toppings=toppings, quantity=quantity)
            return {"added": store.describe_item(item), "line_id": item.line_id,
                    "cart": session.cart_view(store)}
        return guarded(run)

    @beta_async_tool
    async def update_cart_item(line_id: int, flavors: list[str] | None = None,
                               scoops: int | None = None, container: str | None = None,
                               toppings: list[str] | None = None, quantity: int | None = None) -> str:
        """Change fields of an existing cart line. Only pass the fields that change.

        Args:
            line_id: The cart line to change.
            flavors: New flavor ids.
            scoops: New scoop count, 1-3.
            container: New container id.
            toppings: New full list of topping ids (use [] to clear).
            quantity: New quantity.
        """
        def run():
            session.update_cart_item(store, line_id, flavors=flavors, scoops=scoops,
                                     container=container, toppings=toppings, quantity=quantity)
            return {"cart": session.cart_view(store)}
        return guarded(run)

    @beta_async_tool
    async def remove_from_cart(line_id: int) -> str:
        """Remove a line from the cart.

        Args:
            line_id: The cart line to remove.
        """
        def run():
            session.remove_from_cart(line_id)
            return {"cart": session.cart_view(store)}
        return guarded(run)

    @beta_async_tool
    async def view_cart() -> str:
        """Show the current cart."""
        return guarded(lambda: {"cart": session.cart_view(store)})

    return [get_menu, add_to_cart, update_cart_item, remove_from_cart, view_cart]


def context(ctx: AgentContext) -> str:
    return (ctx.store.menu_text() + "\n\nCURRENT CART\n"
            + json.dumps(ctx.session.cart_view(ctx.store) or "empty"))


AGENT = AgentSpec(name="intake", label="Order Intake", system=SYSTEM, effort="medium",
                  make_tools=make_tools, context=context)
