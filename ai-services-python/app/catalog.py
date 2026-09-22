"""Domain models shared across the pipeline: the customer profile, catalog products and
recommendations, plus the static catalog used when no uploaded catalog is usable.

Field names on Recommendation are part of the /api/recommend response contract that
static/index.html reads (product_id, score, matched_rules, explanation).
"""
from typing import List, Optional

from pydantic import BaseModel, Field


class CustomerInput(BaseModel):
    """What the customer submitted on the form."""
    age: int
    income: float
    family_size: int
    hobbies: List[str]
    location: str


class UserProfile(BaseModel):
    user_profile_id: str
    segment: str
    budget_tier: str
    attributes: CustomerInput


class Product(BaseModel):
    id: str
    model: str
    # 0 means the catalog states no price. Rendered as "on request", never as $0.
    base_price: float = 0
    # Taken from the catalog: printing a euro price with a dollar sign is a false claim.
    currency: Optional[str] = None
    category: Optional[str] = None
    capacity: Optional[str] = None
    power: Optional[str] = None
    features: List[str] = Field(default_factory=list)
    colors: List[str] = Field(default_factory=list)
    page_number: Optional[int] = None
    hero_image: Optional[str] = None


class Recommendation(BaseModel):
    recommendation_id: str
    product_id: str
    score: int
    matched_rules: List[str]
    explanation: str


# Used only when there is no uploaded catalog, or it yielded no products. Every run that
# lands here records a warning, because these are not the customer's products.
DEFAULT_CATALOG: List[Product] = [
    Product(
        id="appliance_fridge_samsung",
        model="Samsung Family Hub Refrigerator",
        base_price=2499,
        category="Refrigerator",
        capacity="26.5 cu. ft.",
        features=["Wi-Fi Connected Screen", "Triple Cooling System", "Internal Cameras", "Water & Ice Dispenser"],
        colors=["Stainless Steel", "Black Stainless Steel"],
    ),
    Product(
        id="appliance_washer_lg",
        model="LG TurboWash Washing Machine",
        base_price=899,
        category="Washing Machine",
        capacity="5.0 cu. ft.",
        features=["AI DD Smart Fabric Care", "TurboWash 360", "Steam Technology", "ThinQ Wi-Fi Control"],
        colors=["Graphite", "White"],
    ),
    Product(
        id="appliance_dishwasher_bosch",
        model="Bosch 800 Series Dishwasher",
        base_price=1299,
        category="Dishwasher",
        capacity="16 Place Settings",
        features=["CrystalDry Technology", "Whisper Quiet 42 dBA", "Flexible 3rd Rack", "Home Connect Smart Control"],
        colors=["Stainless Steel", "Black Stainless Steel"],
    ),
]
