"""In-memory data store seeded from data/*.json. Resets on every restart.

All business rules (pricing, stock, preferences) live here as plain functions so they
can be unit-tested without Claude. Agent tools are thin wrappers around these methods.
"""

import json
import secrets
from collections import Counter, defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from app import config
from app.models import (
    CartItem,
    Customer,
    Feedback,
    Flavor,
    Menu,
    Order,
    OrderStatus,
    PriceLine,
    PriceQuote,
    Promo,
)


def money(amount: float) -> float:
    """Round to cents, half-up (float round() would turn 0.825 into 0.82)."""
    return float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class StoreError(ValueError):
    """Raised for invalid requests; tools surface the message back to Claude."""


class Store:
    def __init__(self, data_dir: Path = config.DATA_DIR):
        self.menu = Menu.model_validate_json((data_dir / "menu.json").read_text())
        seed = json.loads((data_dir / "customers.json").read_text())
        self.customers = {c["id"]: Customer.model_validate(c) for c in seed["customers"]}
        self.orders: dict[str, Order] = {}
        for o in seed["orders"]:
            order = Order.model_validate(o)
            if "total" not in o:  # seed files omit prices; derive them from the pricing rules
                q = self.quote(order.items, order.customer_id)
                order.subtotal, order.discount, order.total = q.subtotal, q.discount, q.total
            self.orders[order.id] = order
        # New order ids continue after the highest seeded id so they never overwrite history.
        self._order_seq = max((int(oid.split("-")[1]) for oid in self.orders), default=0) + 1000

    # ---------- menu lookups ----------

    def flavor(self, key: str) -> Flavor:
        return self._lookup(self.menu.flavors, key, "flavor")

    def _lookup(self, items, key: str, kind: str):
        k = key.strip().lower()
        for item in items:
            if k in (item.id.lower(), item.name.lower()):
                return item
        # loose match on partial name ("mango" -> "Alphonso Mango")
        matches = [i for i in items if k in i.name.lower() or k.replace(" ", "_") == i.id]
        if len(matches) == 1:
            return matches[0]
        raise StoreError(f"Unknown {kind} '{key}'. Valid: {', '.join(i.id for i in items)}")

    def menu_summary(self) -> dict:
        return {
            "flavors": [
                {"id": f.id, "name": f.name, "price_per_scoop": f.price_per_scoop,
                 "tags": f.tags, "in_stock": f.stock_scoops > 0}
                for f in self.menu.flavors
            ],
            "containers": [c.model_dump() for c in self.menu.containers],
            "toppings": [t.model_dump() for t in self.menu.toppings],
            "rules": "1-3 scoops per item; each scoop may be a different flavor.",
        }

    def menu_text(self) -> str:
        """Compact menu for system prompts. Ids are what tools expect."""
        flavors = "\n".join(
            f"- {f.id}: {f.name}, ${f.price_per_scoop:.2f}/scoop, tags {', '.join(f.tags) or '-'}"
            + ("" if f.stock_scoops > 0 else " (OUT OF STOCK)")
            for f in self.menu.flavors)
        containers = ", ".join(f"{c.id} (+${c.price:.2f})" for c in self.menu.containers)
        toppings = ", ".join(f"{t.id} (+${t.price:.2f})" for t in self.menu.toppings)
        return (f"MENU (use these ids in tools)\nFlavors:\n{flavors}\nContainers: {containers}\n"
                f"Toppings: {toppings}\n1-3 scoops per item; each scoop may be a different flavor.")

    def best_sellers(self, n: int = 3) -> list[str]:
        ranked = sorted(self.menu.flavors, key=lambda f: -f.popularity)
        return [f.id for f in ranked if f.stock_scoops > 0][:n]

    # ---------- cart items ----------

    def build_item(self, line_id: int, flavors: list[str], scoops: int, container: str,
                   toppings: list[str] | None = None, quantity: int = 1) -> CartItem:
        if not flavors:
            raise StoreError("At least one flavor is required.")
        if not 1 <= scoops <= 3:
            raise StoreError("Scoops must be between 1 and 3.")
        if len(flavors) > scoops:
            raise StoreError(f"{len(flavors)} flavors don't fit in {scoops} scoop(s).")
        if not 1 <= quantity <= 10:
            raise StoreError("Quantity must be between 1 and 10.")
        flavor_ids = [self.flavor(f).id for f in flavors]
        for fid in flavor_ids:
            if self.flavor(fid).stock_scoops <= 0:
                raise StoreError(f"Sorry, {self.flavor(fid).name} is out of stock.")
        return CartItem(
            line_id=line_id,
            flavors=flavor_ids,
            scoops=scoops,
            container=self._lookup(self.menu.containers, container, "container").id,
            toppings=[self._lookup(self.menu.toppings, t, "topping").id for t in (toppings or [])],
            quantity=quantity,
        )

    def scoops_needed(self, items: list[CartItem]) -> Counter:
        """Flavor id -> scoops. Scoops cycle through the item's flavors."""
        need: Counter = Counter()
        for item in items:
            for i in range(item.scoops):
                need[item.flavors[i % len(item.flavors)]] += item.quantity
        return need

    def stock_problems(self, items: list[CartItem]) -> list[str]:
        problems = []
        for fid, n in self.scoops_needed(items).items():
            f = self.flavor(fid)
            if f.stock_scoops < n:
                problems.append(f"{f.name}: need {n} scoop(s), only {f.stock_scoops} left")
        return problems

    def describe_item(self, item: CartItem) -> str:
        names = " + ".join(self.flavor(f).name for f in item.flavors)
        container = self._lookup(self.menu.containers, item.container, "container").name
        desc = f"{item.scoops} scoop(s) {names} in a {container}"
        if item.toppings:
            desc += " with " + ", ".join(
                self._lookup(self.menu.toppings, t, "topping").name for t in item.toppings)
        return desc

    # ---------- pricing ----------

    def unit_price(self, item: CartItem) -> float:
        scoops = sum(self.flavor(item.flavors[i % len(item.flavors)]).price_per_scoop
                     for i in range(item.scoops))
        container = self._lookup(self.menu.containers, item.container, "container").price
        toppings = sum(self._lookup(self.menu.toppings, t, "topping").price for t in item.toppings)
        return money(scoops + container + toppings)

    def validate_promo(self, code: str, customer_id: str) -> Promo:
        for p in self.menu.promos:
            if p.code.upper() == code.strip().upper():
                if p.customer_id and p.customer_id != customer_id:
                    raise StoreError(f"Promo {p.code} belongs to another customer.")
                if p.single_use and p.used:
                    raise StoreError(f"Promo {p.code} has already been used.")
                return p
        raise StoreError(f"Promo code '{code}' is not valid.")

    def quote(self, items: list[CartItem], customer_id: str, promo_code: str | None = None) -> PriceQuote:
        if not items:
            raise StoreError("The cart is empty.")
        lines = []
        for item in items:
            unit = self.unit_price(item)
            lines.append(PriceLine(line_id=item.line_id, description=self.describe_item(item),
                                   unit_price=unit, quantity=item.quantity,
                                   line_total=money(unit * item.quantity)))
        subtotal = money(sum(line.line_total for line in lines))
        loyalty = config.LOYALTY_DISCOUNT[self.customers[customer_id].loyalty_tier]
        promo = self.validate_promo(promo_code, customer_id) if promo_code else None
        promo_pct = promo.percent if promo else 0
        discount = money(subtotal * (loyalty + promo_pct) / 100)
        return PriceQuote(lines=lines, subtotal=subtotal, loyalty_percent=loyalty,
                          promo_code=promo.code if promo else None, promo_percent=promo_pct,
                          discount=discount, total=money(subtotal - discount))

    # ---------- orders ----------

    def place_order(self, customer_id: str, items: list[CartItem], promo_code: str | None = None,
                    remake_of: str | None = None) -> Order:
        problems = self.stock_problems(items)
        if problems:
            raise StoreError("Not enough stock: " + "; ".join(problems))
        if remake_of:
            subtotal = discount = total = 0.0
            promo = None
        else:
            q = self.quote(items, customer_id, promo_code)
            subtotal, discount, total, promo = q.subtotal, q.discount, q.total, q.promo_code
        for fid, n in self.scoops_needed(items).items():
            self.flavor(fid).stock_scoops -= n
        if promo:
            p = self.validate_promo(promo, customer_id)
            if p.single_use:
                p.used = True
        self._order_seq += 1
        order = Order(id=f"ORD-{self._order_seq}", customer_id=customer_id,
                      items=[i.model_copy() for i in items], subtotal=subtotal, discount=discount,
                      total=total, promo_code=promo, remake_of=remake_of)
        order.status_history.append((OrderStatus.PLACED, datetime.now()))
        self.orders[order.id] = order
        return order

    def set_status(self, order_id: str, status: OrderStatus) -> Order:
        order = self.orders[order_id]
        order.status = status
        order.status_history.append((status, datetime.now()))
        return order

    def cancel_order(self, order_id: str) -> Order:
        order = self.get_order(order_id)
        if order.status != OrderStatus.PLACED:
            raise StoreError(f"Order {order_id} is already {order.status.value} and can't be cancelled.")
        for fid, n in self.scoops_needed(order.items).items():
            self.flavor(fid).stock_scoops += n
        return self.set_status(order_id, OrderStatus.CANCELLED)

    def get_order(self, order_id: str) -> Order:
        if order_id not in self.orders:
            raise StoreError(f"No order with id {order_id}.")
        return self.orders[order_id]

    def orders_for(self, customer_id: str) -> list[Order]:
        return sorted((o for o in self.orders.values() if o.customer_id == customer_id),
                      key=lambda o: o.created_at)

    # ---------- feedback & preferences ----------

    def record_feedback(self, order_id: str, feedback: Feedback) -> Order:
        order = self.get_order(order_id)
        order.feedback = feedback
        return order

    def issue_coupon(self, customer_id: str, percent: int) -> Promo:
        percent = max(5, min(percent, config.MAX_COUPON_PERCENT))
        promo = Promo(code=f"SORRY-{secrets.token_hex(3).upper()}", percent=percent,
                      customer_id=customer_id, single_use=True)
        self.menu.promos.append(promo)
        return promo

    def preferences(self, customer_id: str) -> dict:
        """Per-flavor order counts and average ratings from delivered orders."""
        counts: Counter = Counter()
        ratings: dict[str, list[int]] = defaultdict(list)
        for order in self.orders_for(customer_id):
            if order.status != OrderStatus.DELIVERED:
                continue
            flavors = {f for item in order.items for f in item.flavors}
            for f in flavors:
                counts[f] += 1
                if order.feedback:
                    ratings[f].append(order.feedback.rating)
        avg = {f: round(sum(r) / len(r), 1) for f, r in ratings.items()}
        return {
            "flavor_order_counts": dict(counts),
            "flavor_avg_rating": avg,
            "liked": sorted(f for f, a in avg.items() if a >= 4),
            "disliked": sorted(f for f, a in avg.items() if a <= 2),
            "is_new_customer": not counts,
        }
