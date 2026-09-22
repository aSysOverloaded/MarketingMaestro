from langchain_core.prompts import ChatPromptTemplate

from app.ai.llm import invoke_structured
from app.ai.schemas import ProfileOutput
from app.catalog import CustomerInput, UserProfile

PROMPT = ChatPromptTemplate.from_template(
    """You are an expert marketing profiling agent.
Classify the following customer into a segment and budget tier.
- Age: {age}
- Annual Income: ${income}
- Family Size: {family_size}
- Hobbies: {hobbies}
- Location: {location}

Segments: Adventure (outdoor/active hobbies), Executive (high income, professional focus),
Family (household needs dominate), Standard (none of the above).
Budget tiers: Economy, Mid-Range, Premium, Ultra Luxury."""
)

# Used by the rule-based fallback only.
OUTDOOR_HOBBIES = {
    "trekking", "hiking", "camping", "climbing", "cycling", "biking", "fishing",
    "kayaking", "skiing", "surfing", "running", "travel", "mountaineering",
}


def classify_profile(customer: CustomerInput, job_id: str) -> UserProfile:
    parsed = invoke_structured("profile", PROMPT, ProfileOutput, {
        "age": customer.age,
        "income": f"{customer.income:,.0f}",
        "family_size": customer.family_size,
        "hobbies": ", ".join(customer.hobbies),
        "location": customer.location,
    }, job_id)
    return UserProfile(
        user_profile_id=f"{job_id}_profile",
        segment=parsed.segment,
        budget_tier=parsed.budget_tier,
        attributes=customer,  # pass-through: submitted values, never LLM-generated
    )


def rule_based_profile(customer: CustomerInput, job_id: str) -> UserProfile:
    """Deterministic fallback. Replaces the old canned "Adventure / Premium" profile that
    ignored the customer's input entirely."""
    hobbies = {h.lower() for h in customer.hobbies}
    if hobbies & OUTDOOR_HOBBIES:
        segment = "Adventure"
    elif customer.family_size >= 3:
        segment = "Family"
    elif customer.income >= 250_000:
        segment = "Executive"
    else:
        segment = "Standard"

    if customer.income < 50_000:
        tier = "Economy"
    elif customer.income < 120_000:
        tier = "Mid-Range"
    elif customer.income < 300_000:
        tier = "Premium"
    else:
        tier = "Ultra Luxury"

    return UserProfile(user_profile_id=f"{job_id}_profile", segment=segment, budget_tier=tier, attributes=customer)
