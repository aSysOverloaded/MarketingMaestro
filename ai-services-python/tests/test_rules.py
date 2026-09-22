"""Deterministic pieces: banned words, branding, rule-based profile and ranking."""
import pytest

import app.ai.ranker as ranker
from app.ai.evaluator import banned_words_in as banned
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


FRIDGE = DEFAULT_CATALOG[0].model_dump(exclude={"hero_image", "page_number"})


def test_grounding_flags_the_invented_app_from_the_live_run():
    from app.ai.grounding import find_ungrounded_terms

    # Taken from a real draft the LLM critic approved: every spec is correct except the app.
    draft = {
        "headline": "Adventure-Ready Refrigeration for Your Basecamp",
        "subheadline": "Spacious, smart, and built to keep up with your explorations.",
        "paragraphs": [
            "Welcome to Your Adventure-Ready Kitchen – This Samsung Family Hub Refrigerator offers a generous 26.5 cu. ft. interior.",
            "Smart Technology for the Modern Explorer – The Wi-Fi Connected Screen lets you check camera feeds via the SmartThings app.",
        ],
        "cta": "Starting at $2,499. Choose from 3 finishes.",
    }
    assert find_ungrounded_terms(draft, FRIDGE) == ["SmartThings"]


def test_grounding_flags_acronyms_and_unlisted_numbers():
    from app.ai.grounding import find_ungrounded_terms

    draft = {"paragraphs": ["Holds 30 cu. ft. with NFC pairing, Samsung's best."]}
    assert find_ungrounded_terms(draft, FRIDGE) == ["NFC", "30"]


def test_critic_model_override(monkeypatch):
    from app.ai.llm import get_chat_model
    from app.config import settings

    monkeypatch.setattr(settings, "llm_critic_model", "strong/model")
    assert get_chat_model("critic").model_name == "strong/model"
    assert get_chat_model("writer").model_name == settings.llm_model


def test_grounding_tolerates_spelling_and_unicode_hyphen_variants():
    from app.ai.grounding import find_ungrounded_terms

    # Both seen in real drafts: "WiFi" for the spec's "Wi-Fi", and U+2011 non-breaking hyphens.
    draft = {"paragraphs": ["The WiFi Connected Screen and Adventure\u2011Focused design, via the SmartThings app."]}
    assert find_ungrounded_terms(draft, FRIDGE) == ["SmartThings"]


def test_grounding_short_terms_need_a_whole_word_match():
    from app.ai.grounding import find_ungrounded_terms

    # "ai" appears inside the fridge's "Stainless", but the fridge has no AI feature.
    assert find_ungrounded_terms({"paragraphs": ["Smart AI cooling."]}, FRIDGE) == ["AI"]
    washer = DEFAULT_CATALOG[1].model_dump()  # features include "AI DD Smart Fabric Care"
    assert find_ungrounded_terms({"paragraphs": ["AI DD fabric care."]}, washer) == []
