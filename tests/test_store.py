import pytest

from app.models import Customer, Feedback, OrderStatus
from app.session import Session
from app.store import Store, StoreError


@pytest.fixture
def store():
    return Store()


def test_seeded_order_prices_match_pricing_rules(store):
    # ORD-1001: 2 scoops cookies & cream (2 x 3.00) in a waffle cone (1.25); gold = 10% off
    order = store.orders["ORD-1001"]
    assert (order.subtotal, order.discount, order.total) == (7.25, 0.73, 6.52)  # half-up cents


def test_promo_stacks_with_loyalty(store):
    item = store.build_item(1, ["vanilla"], 1, "cup")
    q = store.quote([item], "zarna", "summer10")  # silver 5% + 10%
    assert q.promo_code == "SUMMER10"
    assert q.total == round(2.5 * 0.85, 2)


def test_build_item_accepts_names_and_rejects_bad_input(store):
    item = store.build_item(1, ["Alphonso Mango"], 2, "Waffle Cone", ["hot fudge"])
    assert item.flavors == ["mango"] and item.container == "waffle_cone" and item.toppings == ["hot_fudge"]
    with pytest.raises(StoreError):
        store.build_item(1, ["bubblegum"], 1, "cup")
    with pytest.raises(StoreError):
        store.build_item(1, ["mango", "vanilla"], 1, "cup")
    with pytest.raises(StoreError, match="out of stock"):
        store.build_item(1, ["mint_chip"], 1, "cup")


def test_place_and_cancel_order_adjusts_stock(store):
    before = store.flavor("mango").stock_scoops
    order = store.place_order("dirgh", [store.build_item(1, ["mango"], 2, "cup", quantity=2)])
    assert store.flavor("mango").stock_scoops == before - 4
    store.cancel_order(order.id)
    assert store.flavor("mango").stock_scoops == before
    assert order.status == OrderStatus.CANCELLED


def test_new_order_ids_never_collide_with_seeded_orders(store):
    seeded = set(store.orders)
    order = store.place_order("dirgh", [store.build_item(1, ["vanilla"], 1, "cup")])
    assert order.id not in seeded and seeded <= set(store.orders)


def test_cannot_cancel_after_preparing(store):
    order = store.place_order("dirgh", [store.build_item(1, ["vanilla"], 1, "cup")])
    store.set_status(order.id, OrderStatus.PREPARING)
    with pytest.raises(StoreError):
        store.cancel_order(order.id)


def test_preferences_use_ratings(store):
    assert store.preferences("parthiv")["liked"] == ["cookies_cream"]
    pavani = store.preferences("pavani")
    assert "mango_sorbet" in pavani["liked"] and pavani["disliked"] == ["strawberry"]
    store.customers["newbie"] = Customer(id="newbie", name="Newbie")
    assert store.preferences("newbie")["is_new_customer"]


def test_feedback_updates_preferences(store):
    order = store.place_order("dirgh", [store.build_item(1, ["pistachio"], 1, "cup")])
    store.set_status(order.id, OrderStatus.DELIVERED)
    store.record_feedback(order.id, Feedback(rating=5, sentiment="positive"))
    assert "pistachio" in store.preferences("dirgh")["liked"]


def test_coupon_is_single_use_and_customer_bound(store):
    promo = store.issue_coupon("dirgh", 90)
    assert promo.percent == 25  # capped
    with pytest.raises(StoreError):
        store.validate_promo(promo.code, "zarna")
    store.place_order("dirgh", [store.build_item(1, ["vanilla"], 1, "cup")], promo.code)
    with pytest.raises(StoreError, match="already been used"):
        store.validate_promo(promo.code, "dirgh")


def test_session_cart_ops_invalidate_quote(store):
    s = Session(customer_id="dirgh")
    s.add_to_cart(store, flavors=["vanilla"], scoops=1, container="cup")
    s.quoted_turn = 1
    s.update_cart_item(store, 1, quantity=3)
    assert s.cart[0].quantity == 3 and s.quoted_turn is None
    s.remove_from_cart(1)
    assert s.cart == []
