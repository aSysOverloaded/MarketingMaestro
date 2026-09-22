"""Deterministic pieces: banned words, branding, rule-based profile and ranking."""
import pytest

import app.ai.ranker as ranker
from app.ai.evaluator import _deterministic_banned_word_scan as banned
from app.ai.profile import rule_based_profile
from app.ai.schemas import RankedItem, RankingOutput
from app.catalog import DEFAULT_CATALOG, CustomerInput, Product
from app.render.brochure import brand_for_model

CUSTOMER = CustomerInput(age=32, income=120000, family_size=4, hobbies=["trekking", "camping"], location="Seattle, WA")


def test_banned_words_match_whole_words_only():
    assert banned({"paragraphs": ["An affordable choice for your family."]}) == []
    assert banned({"headline": "Not cheap, just good"}) == ["cheap"]


@pytest.mark.parametrize("model, brand", [
    ("Bosch 800 Series Dishwasher", "Premium Home"),  # was branded LG via the "wash" substring
    ("Whirlpool Front Load Washer", "Premium Home"),
    ("LG TurboWash Washing Machine", "LG Electronics"),
    ("Samsung Family Hub Refrigerator", "Samsung"),
    ("Bulgari Espresso Machine", "Premium Home"),  # "lg" inside a word is not LG
])
def test_brand_for_model(model, brand):
    assert brand_for_model(model).name == brand


def test_rule_based_profile_uses_the_input():
    assert rule_based_profile(CUSTOMER, "j").segment == "Adventure"
    homebody = CUSTOMER.model_copy(update={"hobbies": ["reading"], "income": 400000, "family_size": 1})
    p = rule_based_profile(homebody, "j")
    assert (p.segment, p.budget_tier) == ("Executive", "Ultra Luxury")


def test_score_products_budget_and_unknown_price():
    unpriced = Product(id="tent", model="Trail Tent", category="Tent", features=["Camping ready"])
    recs = ranker.score_products(CUSTOMER, [*DEFAULT_CATALOG, unpriced], "j")
    assert len(recs) == 4
    assert [r.score for r in recs] == sorted([r.score for r in recs], reverse=True)
    tent = next(r for r in recs if r.product_id == "tent")
    assert "Price not listed - budget fit not assessed" in tent.matched_rules
    assert any("camping" in rule for rule in tent.matched_rules)


def test_rank_products_drops_invented_and_duplicate_ids(monkeypatch):
    output = RankingOutput(recommendations=[
        RankedItem(product_id="made_up", score=99, matched_rules=[], explanation="x"),
        RankedItem(product_id="appliance_washer_lg", score=140, matched_rules=["a"], explanation="y"),
        RankedItem(product_id="appliance_washer_lg", score=80, matched_rules=["b"], explanation="z"),
    ])
    monkeypatch.setattr(ranker, "invoke_structured", lambda *a, **k: output)
    recs = ranker.rank_products(CUSTOMER, "Adventure", "Premium", DEFAULT_CATALOG, "j")
    assert [(r.product_id, r.score) for r in recs] == [("appliance_washer_lg", 100)]


def test_rank_products_raises_when_nothing_valid(monkeypatch):
    output = RankingOutput(recommendations=[RankedItem(product_id="made_up", score=90, matched_rules=[], explanation="x")])
    monkeypatch.setattr(ranker, "invoke_structured", lambda *a, **k: output)
    with pytest.raises(RuntimeError):
        ranker.rank_products(CUSTOMER, "Adventure", "Premium", DEFAULT_CATALOG, "j")
