"""Deterministic replies for predictable steps, so they cost no Claude call.

Same idea as rules-first routing: code handles what is predictable (greeting, showing the
price, placing a confirmed order, asking for / recording a good rating) and anything
unusual returns None, which hands the message to the Claude agent as before.
"""

import re

from app.agents.base import AgentContext
from app.agents.checkout import confirm_and_place
from app.models import Feedback, PriceQuote
from app.routing import AFFIRM
from app.store import StoreError

SIMPLE_CHECKOUT = re.compile(
    r"(ok(ay)?[, ]+|yes[, ]+|let'?s |i want to |i'?m ready to |ready to )?"
    r"(check ?out|pay|place (my |the )?order)( now| please)?"
    r"( (with|using)( (the )?(promo|coupon|discount))?( code)? (?P<code>[\w-]+))?[\s.!]*")
EXPLICIT_CODE = re.compile(r"\b(?:code|promo|coupon)\s+([\w-]+)")
RATING = re.compile(r"\b([1-5])\s*(?:/\s*5|stars?|out of 5)?\b|\b(one|two|three|four|five) stars?\b")
WORD_RATING = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
COMPLAINT = re.compile(r"\b(melt|wrong|missing|late|cold|warm|bad|awful|terrible|worst|"
                       r"broken|spill|not good|disappoint|refund|sick|stale)")


def greeting(ctx: AgentContext) -> str:
    """Personalised opener from the taste profile; no LLM call."""
    store, cid = ctx.store, ctx.session.customer_id
    customer = store.customers[cid]
    prefs = store.preferences(cid)
    avg = prefs["flavor_avg_rating"]
    favourites = sorted(prefs["liked"], key=lambda f: (-avg.get(f, 0),
                                                      -prefs["flavor_order_counts"].get(f, 0)))
    favourites = [f for f in favourites if store.flavor(f).stock_scoops > 0][:2]
    if favourites:
        names = " and ".join(store.flavor(f).name for f in favourites)
        return (f"Hi {customer.name}, welcome back! 🍦 Your favourites are {names}. "
                "Want the usual, or shall I suggest something new? Just tell me what you'd like.")
    names = ", ".join(store.flavor(f).name for f in store.best_sellers())
    return (f"Hi {customer.name}, welcome to Scoops & Co! 🍦 Our best sellers today are {names}. "
            "Tell me what you'd like, or ask me for a suggestion.")


def checkout(ctx: AgentContext, text: str) -> str | None:
    """Show the price for a plain "checkout", or place the order on a plain "yes"."""
    session, store = ctx.session, ctx.store
    t = text.lower().strip()

    if AFFIRM.fullmatch(t) and session.cart and session.quoted_turn is not None:
        try:
            placed = confirm_and_place(ctx)
        except StoreError:
            return None  # e.g. stock ran out meanwhile: let the agent explain
        return (f"Your order is placed! 🎉 Order **{placed['order_id']}**, total "
                f"${placed['total']:.2f}. I'll post live updates right here.")

    m = SIMPLE_CHECKOUT.fullmatch(t)
    if not m or not session.cart:
        return None
    code = m.group("code") or next(iter(EXPLICIT_CODE.findall(t)), None)
    if code:
        try:
            session.promo_code = store.validate_promo(code, session.customer_id).code
        except StoreError:
            return None  # invalid code: let the agent handle the conversation
    if store.stock_problems(session.cart):
        return None
    quote = store.quote(session.cart, session.customer_id, session.promo_code)
    session.quoted_turn = session.turn
    return format_quote(quote)


def format_quote(q: PriceQuote) -> str:
    lines = [f"- {l.quantity}× {l.description}: ${l.line_total:.2f}" for l in q.lines]
    lines.append(f"Subtotal: ${q.subtotal:.2f}")
    if q.loyalty_percent:
        lines.append(f"Loyalty discount: {q.loyalty_percent}%")
    if q.promo_code:
        lines.append(f"Promo {q.promo_code}: {q.promo_percent}%")
    if q.discount:
        lines.append(f"You save: -${q.discount:.2f}")
    lines.append(f"**Total: ${q.total:.2f}**")
    return "Here's your order summary:\n" + "\n".join(lines) + '\n\nReply "yes" to place the order!'


def feedback_request(ctx: AgentContext) -> str:
    """First follow-up message after delivery."""
    name = ctx.store.customers[ctx.session.customer_id].name
    return (f"Your order has arrived, {name}! 🍨 How was it? "
            "Please rate it 1-5, and tell us if anything could have been better.")


def feedback_reply(ctx: AgentContext, text: str) -> str | None:
    """Record a clearly positive rating (4-5, no complaint). Anything else goes to the agent."""
    session = ctx.session
    if not session.feedback_order_id:
        return None
    t = text.lower()
    m = RATING.search(t)
    if not m or COMPLAINT.search(t):
        return None
    rating = int(m.group(1)) if m.group(1) else WORD_RATING[m.group(2)]
    if rating < 4:
        return None  # low or middling rating: the agent handles it with care
    ctx.store.record_feedback(session.feedback_order_id,
                              Feedback(rating=rating, comment=text.strip()[:200], sentiment="positive"))
    session.feedback_order_id = None
    name = ctx.store.customers[session.customer_id].name
    return (f"Thank you, {name}! 🎉 So glad you enjoyed it. We'll remember your favourites "
            "for next time.")


def try_handle(agent: str, ctx: AgentContext, text: str) -> str | None:
    """Deterministic reply for this agent and message, or None to use the Claude agent."""
    if agent == "checkout":
        return checkout(ctx, text)
    if agent == "followup":
        return feedback_reply(ctx, text)
    return None
