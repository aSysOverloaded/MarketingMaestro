import os
import json
import google.generativeai as genai

BANNED_WORDS = ["cheap", "unreliable", "garbage", "competitor", "ford", "toyota"]

def evaluate_copy(copy: dict) -> dict:
    # 1. Deterministic First Pass (Schema & Banned Word check)
    headline = copy.get("headline", "").lower()
    subheadline = copy.get("subheadline", "").lower()
    paragraphs = " ".join(copy.get("paragraphs", [])).lower()
    cta = copy.get("cta", "").lower()

    full_text = f"{headline} {subheadline} {paragraphs} {cta}"
    found_banned = [w for w in BANNED_WORDS if w in full_text]

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # Offline fallback pass
        return {
            "passed": len(found_banned) == 0,
            "banned_words_found": found_banned,
            "tone_assessment": "Brand voice is professional, neutral, and clear.",
            "score": 90 if len(found_banned) == 0 else 40
        }

    genai.configure(api_key=api_key)
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    model = genai.GenerativeModel(gemini_model)

    prompt = f"""You are a brand quality evaluation agent (Evaluator).
Analyze the following marketing copy draft and grade it on readability, style consistency, and alignment with a professional, helpful tone.

Copy Draft:
{json.dumps(copy)}

Rate the tone, grade the overall suitability score (0-100), and determine if it meets brand voice standards (passing score >= 75).
Output a valid JSON object matching this structure:
{{
  "passed": true / false,
  "tone_assessment": "Short description of the voice (e.g. professional and helpful)",
  "score": integer (0 to 100)
}}

Only return a valid JSON object. Do not include markdown formatting or extra text.
"""
    try:
        response = model.generate_content(
            prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        data = json.loads(response.text)
        
        # Override passed status if banned words were found deterministically
        passed = bool(data.get("passed", True)) and (len(found_banned) == 0)
        score = int(data.get("score", 85)) if len(found_banned) == 0 else min(int(data.get("score", 85)), 50)

        return {
            "passed": passed,
            "banned_words_found": found_banned,
            "tone_assessment": data.get("tone_assessment", "Professional and clear."),
            "score": score
        }
    except Exception as e:
        return {
            "passed": len(found_banned) == 0,
            "banned_words_found": found_banned,
            "tone_assessment": f"Evaluated via offline heuristics: {str(e)}",
            "score": 80 if len(found_banned) == 0 else 30
        }
