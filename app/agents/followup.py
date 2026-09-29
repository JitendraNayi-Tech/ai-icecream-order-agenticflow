"""Agent 5 - Follow-up & Feedback: post-delivery rating, sentiment, service recovery."""

from typing import Literal

from anthropic import beta_async_tool

from app import config
from app.agents.base import SHARED_RULES, AgentContext, AgentSpec, guarded
from app.models import Feedback
from app.store import StoreError

SYSTEM = SHARED_RULES + f"""
You are the FOLLOW-UP & FEEDBACK agent. You take over after an order is delivered.
- When the order has just been delivered, ask how it was and for a 1-5 rating.
- When the customer answers, call record_feedback with their rating, the gist of their
  comment, and your read of the sentiment. If they gave a comment but no number, ask for one.
- Rating <= 2 or a clear complaint (melted, wrong flavor, missing item): apologise sincerely
  and offer ONE remedy: a free remake (request_remake) for a wrong/damaged order, or a coupon
  (issue_coupon, 10-{config.MAX_COUPON_PERCENT}%) otherwise. Use the tool once the customer
  accepts, or straight away if they asked for it.
- Rating >= 4: thank them, and mention that their favourites will shape future suggestions.
- If they don't want to give feedback, call skip_feedback and wish them well.
"""


def make_tools(ctx: AgentContext) -> list:
    store, session, services = ctx.store, ctx.session, ctx.services
    cid = session.customer_id

    def _order_id() -> str:
        if not session.feedback_order_id:
            raise StoreError("There is no delivered order awaiting feedback.")
        return session.feedback_order_id

    @beta_async_tool
    async def get_delivered_order() -> str:
        """Get the delivered order that feedback is being collected for."""
        def run():
            o = store.get_order(_order_id())
            return {"order_id": o.id, "items": [store.describe_item(i) for i in o.items],
                    "total": o.total}
        return guarded(run)

    @beta_async_tool
    async def record_feedback(rating: int, comment: str,
                              sentiment: Literal["positive", "neutral", "negative"]) -> str:
        """Save the customer's feedback for the delivered order.

        Args:
            rating: 1-5 stars.
            comment: Short summary of what the customer said.
            sentiment: Overall sentiment of the feedback.
        """
        def run():
            oid = _order_id()
            store.record_feedback(oid, Feedback(rating=rating, comment=comment, sentiment=sentiment))
            session.feedback_order_id = None
            return {"saved": True, "order_id": oid}
        return guarded(run)

    @beta_async_tool
    async def issue_coupon(percent: int, reason: str) -> str:
        """Issue a single-use discount code for the customer's next order.

        Args:
            percent: Discount percent (capped server-side).
            reason: Why the coupon is issued.
        """
        return guarded(lambda: {"code": (p := store.issue_coupon(cid, percent)).code,
                                "percent": p.percent})

    @beta_async_tool
    async def request_remake(reason: str) -> str:
        """Send a free replacement of the last delivered order. Starts live tracking for it.

        Args:
            reason: What went wrong with the original order.
        """
        def run():
            last = next((o for o in reversed(store.orders_for(cid))
                         if o.status.value == "DELIVERED"), None)
            if not last:
                raise StoreError("No delivered order to remake.")
            order = store.place_order(cid, last.items, remake_of=last.id)
            session.active_order_id = order.id
            services.start_tracking(session, order)
            return {"remake_order_id": order.id, "total": order.total}
        return guarded(run)

    @beta_async_tool
    async def skip_feedback() -> str:
        """The customer doesn't want to leave feedback; stop asking."""
        def run():
            session.feedback_order_id = None
            return {"skipped": True}
        return guarded(run)

    return [get_delivered_order, record_feedback, issue_coupon, request_remake, skip_feedback]


AGENT = AgentSpec(name="followup", label="Follow-up & Feedback", system=SYSTEM, effort="medium",
                  make_tools=make_tools)
