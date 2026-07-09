import os
import json
import google.generativeai as genai

def generate_copy(segment: str, sections: list, candidate: dict) -> dict:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # Offline fallback mock data
        model_name = candidate.get("Model", "Premium Product Selection")
        return {
            "headline": f"Dynamic Living Meets Modern Performance",
            "subheadline": f"Tailored perfectly for your {segment} lifestyle parameters.",
            "paragraphs": [
                f"We are excited to spotlight the {model_name}. Engineered to meet the high standards of a {segment} profile, this choice blends premium capacity with smart convenience features.",
                "With state-of-the-art efficiency, whisper-quiet operations, and fingerprint resistant finishes, this selection elevates your everyday routine seamlessly."
            ],
            "cta": "Arrange a live interactive demonstration or consult with a product specialist today."
        }

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-3.5-flash')

    prompt = f"""You are a professional copywriter agent. Write persuasive copy for a personalized marketing brochure.
Customer Segment: {segment}
Brochure Outline: {json.dumps(sections)}
Product Specifications: {json.dumps(candidate)}

Output a valid JSON object matching this structure exactly:
{{
  "headline": "A short, catchy, benefit-driven headline",
  "subheadline": "A supporting subheadline highlighting suitability",
  "paragraphs": [
     "Paragraph 1 expanding on the planner outline points and product specifications.",
     "Paragraph 2 highlighting dynamic everyday convenience features."
  ],
  "cta": "Action-oriented CTA text (e.g. Schedule a live demonstration or contact our sales specialists)"
}}

Only return a valid JSON object. Do not include markdown formatting or extra text.
"""
    try:
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        data = json.loads(response.text)
        return {
            "headline": data.get("headline", "Premium Curation Selection"),
            "subheadline": data.get("subheadline", "Sophisticated style meets efficiency."),
            "paragraphs": data.get("paragraphs", ["Tailored brochure copy content for your selection."]),
            "cta": data.get("cta", "Request a live demonstration today.")
        }
    except Exception as e:
        return {
            "headline": "Premium Curation Selection",
            "subheadline": "Sophisticated style meets high-efficiency features.",
            "paragraphs": ["Tailored brochure copy content written for your selection."],
            "cta": "Schedule a product demonstration."
        }
