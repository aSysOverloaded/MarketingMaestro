import os
import json
import google.generativeai as genai

def generate_plan(segment: str, recommendation: dict) -> list:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # Offline fallback mock data
        return [
            {
                "title": "Welcome and Overview",
                "points": [
                    "Acknowledge the customer's lifestyle goals and segment parameters.",
                    "Present a warm, welcoming introduction mapping to their category profile."
                ]
            },
            {
                "title": "Why This Selection Fits Your Family",
                "points": [
                    "Highlight large cooking capacities and Flex Duo versatility.",
                    "Elaborate on certified reliability and smart home convenience functions."
                ]
            },
            {
                "title": "Premium Technical Highlights",
                "points": [
                    "Focus on dual fuel precision heat and slide-in ergonomic configuration.",
                    "Outline finish options (e.g. fingerprint resistant stainless steel) and dimensions."
                ]
            }
        ]

    genai.configure(api_key=api_key)
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    model = genai.GenerativeModel(gemini_model)
    
    prompt = f"""You are a content planning agent. Your task is to plan the sections of a personalized marketing brochure.
Customer Segment: {segment}
Recommended Product: {json.dumps(recommendation)}

Output a JSON array of sections. Each section must be an object with:
- "title": Title of the brochure page/section (string)
- "points": Key items and writing points to cover in this section (list of strings)

Only return a valid JSON array. Do not include markdown formatting or extra text.
"""
    try:
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        data = json.loads(response.text)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict) and "sections" in data:
            return data["sections"]
        return []
    except Exception as e:
        # Safe fallback
        return [
            {"title": "Product Overview", "points": ["Introduction", "Why it fits your profile"]},
            {"title": "Specifications & Features", "points": ["Key technical highlights", "Certified benefits"]}
        ]
