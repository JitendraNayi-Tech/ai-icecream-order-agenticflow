from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class OrderStatus(str, Enum):
    PLACED = "PLACED"
    PREPARING = "PREPARING"
    READY = "READY"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


STATUS_FLOW = [
    OrderStatus.PLACED,
    OrderStatus.PREPARING,
    OrderStatus.READY,
    OrderStatus.OUT_FOR_DELIVERY,
    OrderStatus.DELIVERED,
]


class Flavor(BaseModel):
    id: str
    name: str
    price_per_scoop: float
    tags: list[str] = []
    stock_scoops: int
    popularity: int = 0  # higher = more popular; drives best-seller suggestions


class Container(BaseModel):
    id: str
    name: str
    price: float


class Topping(BaseModel):
    id: str
    name: str
    price: float


class Promo(BaseModel):
    code: str
    percent: int
    customer_id: str | None = None  # None = anyone can use it
    single_use: bool = False
    used: bool = False


class Menu(BaseModel):
    flavors: list[Flavor]
    containers: list[Container]
    toppings: list[Topping]
    promos: list[Promo] = []


class CartItem(BaseModel):
    line_id: int
    flavors: list[str]  # flavor ids, one per scoop at most
    scoops: int = Field(ge=1, le=3)
    container: str
    toppings: list[str] = []
    quantity: int = Field(default=1, ge=1, le=10)


class Feedback(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = ""
    sentiment: Literal["positive", "neutral", "negative"] = "neutral"


class Order(BaseModel):
    id: str
    customer_id: str
    items: list[CartItem]
    subtotal: float = 0.0
    discount: float = 0.0
    total: float = 0.0
    promo_code: str | None = None
    status: OrderStatus = OrderStatus.PLACED
    created_at: datetime = Field(default_factory=datetime.now)
    status_history: list[tuple[OrderStatus, datetime]] = []
    feedback: Feedback | None = None
    remake_of: str | None = None


class Customer(BaseModel):
    id: str
    name: str
    loyalty_tier: Literal["none", "silver", "gold"] = "none"
    notes: str = ""  # dietary needs etc.; shown to every agent


class PriceLine(BaseModel):
    line_id: int
    description: str
    unit_price: float
    quantity: int
    line_total: float


class PriceQuote(BaseModel):
    lines: list[PriceLine]
    subtotal: float
    loyalty_percent: int
    promo_code: str | None
    promo_percent: int
    discount: float
    total: float


AgentName = Literal["intake", "recommender", "checkout", "tracker", "followup"]


class Route(BaseModel):
    agent: AgentName
    reason: str
