from app.routing import quick_route
from app.session import Session
from app.store import Store


def make_session(**kw) -> Session:
    s = Session(customer_id="zarna")
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def with_reply(session: Session, agent: str, reply: str, text: str) -> Session:
    session.add_message("assistant", reply)
    session.add_message("user", text)
    session.last_agent = agent
    return session


def test_answer_to_intake_question_sticks_to_intake():
    s = with_reply(make_session(), "intake", "How many scoops: 1, 2, or 3? 🍋", "2 scoops")
    assert quick_route(s, "2 scoops") == "intake"


def test_no_question_means_no_sticky_route():
    s = with_reply(make_session(), "intake", "Added to your cart.", "hmm")
    assert quick_route(s, "hmm") is None  # falls back to the LLM router


def test_yes_after_price_goes_to_checkout():
    store = Store()
    s = make_session(quoted_turn=1)
    s.add_to_cart(store, flavors=["vanilla"], scoops=1, container="cup")
    s.quoted_turn = 1
    assert quick_route(s, "Yes please!") == "checkout"


def test_keywords():
    assert quick_route(make_session(), "let's checkout") == "checkout"
    assert quick_route(make_session(), "what do you recommend?") == "recommender"
    assert quick_route(make_session(active_order_id="ORD-1"), "where is my order") == "tracker"
    # tracking words only route to the tracker when there is an active order
    assert quick_route(make_session(), "where is my order") is None


def test_pending_feedback_wins():
    assert quick_route(make_session(feedback_order_id="ORD-1"), "it was great, 5 stars") == "followup"
