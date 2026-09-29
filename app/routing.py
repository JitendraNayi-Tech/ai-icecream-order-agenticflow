"""Fast rule-based routing, tried before the (slow) LLM router.

Most chat turns are unambiguous: a reply to the question an agent just asked, "checkout",
"yes" to a price summary, "where is my order". Deciding those with rules saves a whole
Claude call per message. Anything the rules don't recognise returns None and falls back
to the LLM router.
"""

import re

from app.session import Session

AFFIRM = re.compile(r"(yes|yep|yeah|y|ok|okay|sure|confirm(ed)?|go ahead|do it|place (it|the order)"
                    r"|sounds good|perfect)\b[\s.!]*(please)?[\s.!]*")
CHECKOUT = re.compile(r"\b(check ?out|pay|bill|total|promo|coupon|discount code|code \w+"
                      r"|place (my |the )?order)\b")
TRACKING = re.compile(r"\b(where('s| is)|status|eta|how long|track|cancel|arriv|deliver)")
RECOMMEND = re.compile(r"\b(recommend|suggest|not sure|what should|surprise|popular|what'?s good"
                       r"|ideas?|options)\b")

# Agents whose open question should receive the customer's next message.
STICKY = ("intake", "checkout", "followup")


def quick_route(session: Session, text: str) -> str | None:
    t = text.lower().strip()
    if session.feedback_order_id:
        return "followup"
    if session.quoted_turn is not None and session.cart and AFFIRM.fullmatch(t):
        return "checkout"
    if CHECKOUT.search(t):
        return "checkout"
    if session.active_order_id and TRACKING.search(t):
        return "tracker"
    if RECOMMEND.search(t):
        return "recommender"
    if session.last_agent in STICKY and _asked_question(session):
        return session.last_agent
    return None


def _asked_question(session: Session) -> bool:
    """Did the latest shop message end with a question? (history[-1] is the customer's turn.)"""
    if len(session.history) < 2 or session.history[-2]["role"] != "assistant":
        return False
    return "?" in session.history[-2]["content"][-80:]
