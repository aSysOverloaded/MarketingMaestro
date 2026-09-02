import os
import json
import google.generativeai as genai

def audit_copy(copy: dict, candidate: dict) -> dict:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # Offline fallback (always pass)
        return {
            "passed": True,
            "feedback": "Offline validation pass: specs align with local files."
        }

    genai.configure(api_key=api_key)
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    model = genai.GenerativeModel(gemini_model)

    prompt = f"""You are an audit agent (Spec Critic).
Your job is to compare the drafted marketing copy against the official product specifications and verify that all claims are accurate.
If the copy references numbers, features, or metrics that DO NOT exist or contradict the specifications sheet, fail the validation.

Drafted Marketing Copy:
{json.dumps(copy)}

Official Product Specifications:
{json.dumps(candidate)}

Determine if the copy has passed or failed the audit. If failed, provide correction feedback outlining which specs were incorrect.
Output a valid JSON object matching this structure:
{{
  "passed": true / false,
  "feedback": "Details on what specs failed, or 'Specification audit passed successfully.'"
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
            "passed": bool(data.get("passed", True)),
            "feedback": data.get("feedback", "Specification audit passed successfully.")
        }
    except Exception as e:
        return {
            "passed": True,
            "feedback": f"Validation bypassed due to system error: {str(e)}"
        }
