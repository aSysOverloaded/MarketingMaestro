"""Deterministic grounding check: flag specific-looking terms in marketing copy that do not
appear anywhere in the product's specs.

The LLM critic is good at contradictions but weak at noticing plausible additions - in a
live run it passed copy promising a "SmartThings app" for a fridge whose specs never mention
one. Invented features tend to be exactly the terms this catches:

- internal-capital names: SmartThings, ThinQ, TurboWash, iPhone
- acronyms of 2+ letters: NFC, OLED, AI
- numbers: 30 cu. ft., 5-year, 42 dBA

Ordinary words, including Title Case headings like "Adventure-Ready Kitchen", are left to the
LLM critic; checking them deterministically would drown real findings in false positives.
"""
import json
import re
from typing import List

# Word-ish tokens, keeping internal hyphens/apostrophes together ("Wi-Fi", "Samsung's").
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'’]*(?:-[A-Za-z0-9]+)*")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_INTERNAL_CAP = re.compile(r"[a-z][A-Z]")  # SmartThings, ThinQ, TurboWash, iPhone
_ACRONYM = re.compile(r"^[A-Z]{2,}$")  # NFC, OLED, AI

# Single-digit integers are usually counts ("3 options"), so they are not checked. The cost:
# an invented "5-year warranty" slips through to the LLM critic.
_MAX_UNCHECKED_INTEGER = 9


# LLMs often emit non-breaking/unicode hyphens (U+2010-2015, U+2212) where specs use "-".
_DASHES = re.compile("[‐-―−]")


def _spec_text(product: dict) -> str:
    return json.dumps(product, ensure_ascii=False)


def _squash(text: str) -> str:
    # Compare ignoring case, spacing and punctuation, so "WiFi" matches a spec's "Wi-Fi".
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _spec_numbers(spec_text: str) -> set:
    return {float(n.replace(",", "")) for n in _NUMBER.findall(spec_text)}


def _is_specific_term(part: str) -> bool:
    # Checked per hyphen-part, so Title Case compounds like "Adventure-Ready" don't count.
    return bool(_ACRONYM.match(part) or _INTERNAL_CAP.search(part))


def find_ungrounded_terms(copy: dict, product: dict) -> List[str]:
    """Check writer copy ({headline, subheadline, paragraphs, cta}) against a product's specs."""
    text = " ".join([copy.get("headline", ""), copy.get("subheadline", ""), *copy.get("paragraphs", []), copy.get("cta", "")])
    return find_ungrounded_in_text(text, product)


def find_ungrounded_in_text(text: str, source: dict) -> List[str]:
    """Check free text against any JSON-able source of truth (specs, customer profile, ...)."""
    spec = _spec_text(source)
    spec_squashed = _squash(spec)
    spec_words = {_squash(t) for t in _TOKEN.findall(_DASHES.sub("-", spec))}
    spec_numbers = _spec_numbers(spec)

    text = _DASHES.sub("-", text)

    found: List[str] = []

    def add(term: str) -> None:
        if term not in found:
            found.append(term)

    for token in _TOKEN.findall(text):
        base = re.sub(r"['’]s$", "", token)  # Samsung's -> Samsung
        key = _squash(base)
        # Short terms must match a whole spec word: "ai" is a substring of "stainless".
        if key in spec_words or (len(key) > 4 and key in spec_squashed):
            continue
        if any(_is_specific_term(part) for part in base.split("-")):
            add(base)

    for raw in _NUMBER.findall(text):
        value = float(raw.replace(",", "").rstrip("."))
        if value in spec_numbers:
            continue
        if value.is_integer() and value <= _MAX_UNCHECKED_INTEGER:
            continue
        add(raw.rstrip(".,"))

    return found
